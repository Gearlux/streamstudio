"""Static ComfyUI adapter nodes for FluxStudio: walk a DataFlux source by index, and extract a Sample.

These are ComfyUI orchestration/adapter glue — NOT DataFlux data ops — so they live here rather
than in dataflux / waivefront. ``SampleExtractorNode`` unpacks a ``Sample``'s ``input`` / ``target``
onto statically-typed ComfyUI sockets (IMAGE / MASK / STRING / INT / FLOAT) so it wires into NATIVE
ComfyUI viewers (Preview Image, a text node, …) instead of a bespoke in-canvas viewer — the relevant
type is derived from the sample's declared/inferred features (``Sample.describe()``). The actual
image rendering is delegated to ``waivefront.visualizers.value_to_image`` (per the "no
FluxStudio-only visualizers" mandate); this module only bridges to ComfyUI's tensor protocols and
provides the index-walk stepper.
"""

import ctypes
import io
import math as _math_module
import re
import sys
import threading
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Type

import numpy as np
import torch
from logflow import get_logger

from fluxstudio.bridge import SAMPLE_TYPE, SOURCE_TYPE, node_root

if TYPE_CHECKING:
    from waivefront.visualizers import Colormap

logger = get_logger(__name__)

# Palette folder for the hand-written adapter nodes. Mirrors the auto-discovered nodes' root: nested
# under ``Taidal/`` by default, or at the palette root (``FluxStudio``) when the CLI's
# ``--no-default-nodes`` flag is active (see bridge.node_root). Computed at import — the CLI exports
# the env var before launching ComfyUI, so it is set by the time this module is imported.
_FLUXSTUDIO_CATEGORY = "/".join(p for p in (node_root(), "FluxStudio") if p)


def _colormap_choices() -> List[str]:
    """The colormap dropdown options — the closed ``waivefront.visualizers.Colormap`` set.

    Sourced from waivefront's ``COLORMAPS`` tuple (itself ``get_args(Colormap)``) so the dropdown,
    ``value_to_image``'s annotation, and navigaitor's form-spec all enumerate ONE source of truth.
    Falls back to a single ``"viridis"`` option when the ``[waivefront]`` extra is absent (the IMAGE
    output is a placeholder then anyway), so the node still registers without waivefront installed.
    """
    try:
        from waivefront.visualizers import COLORMAPS

        return list(COLORMAPS)
    except ImportError:  # waivefront extra not installed — see _to_image's placeholder path.
        return ["viridis"]


# Matches ComfyUI's seed-widget convention so the frontend renders a control_after_generate spinner.
_MAX_INDEX = 0xFFFFFFFFFFFFFFFF

# Default control-after-generate mode for the index widget. ComfyUI honours a *string* value here as
# the combo's default (values: fixed/increment/decrement/randomize); passing the bare ``True`` instead
# falls back to the frontend default (which for a seed-like INT can land on "randomize"). We want
# stepping THROUGH the dataset, so default to "increment" — each Queue Prompt advances one sample.
_INDEX_CONTROL_DEFAULT = "increment"


class WalkDatasetNode:
    """Emit the ``Sample`` at ``index`` from a ``DATAFLUX_SOURCE``; ``index`` auto-increments each run.

    The ``index`` widget uses ComfyUI's ``control_after_generate``, defaulting to **increment** so every
    Queue Prompt advances to the next sample in order (set the control to "fixed" / index 0 to reset, or
    "randomize" to sample at random). With ``wrap`` enabled the index wraps modulo the source length;
    otherwise an out-of-range index raises.
    """

    CATEGORY = _FLUXSTUDIO_CATEGORY
    RETURN_TYPES = (SAMPLE_TYPE,)
    RETURN_NAMES = ("sample",)
    FUNCTION = "walk"

    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        return {
            "required": {
                "source": (SOURCE_TYPE,),
                "index": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": _MAX_INDEX,
                        "control_after_generate": _INDEX_CONTROL_DEFAULT,
                    },
                ),
                "wrap": ("BOOLEAN", {"default": True}),
            }
        }

    def walk(self, source: Any, index: int, wrap: bool = True) -> Tuple[Any, ...]:
        from dataflux.core import Flux

        flux = source if isinstance(source, Flux) else Flux.from_source(source)
        length = len(flux)
        if length <= 0:
            raise RuntimeError("WalkDatasetNode: source is empty (len == 0); nothing to walk.")
        if wrap:
            resolved = index % length
        elif not 0 <= index < length:
            raise IndexError(
                f"WalkDatasetNode: index {index} out of range for source of length {length} "
                "(enable 'wrap' to cycle)."
            )
        else:
            resolved = index
        logger.debug(f"WalkDatasetNode: yielding sample {resolved}/{length}")
        return (flux[resolved],)


class SampleExtractorNode:
    """Unpack a ``Sample``'s ``input`` or ``target`` onto typed ComfyUI sockets.

    Takes a ``DATAFLUX_SAMPLE`` and a ``field`` selector (``input`` / ``target``) and
    exposes the selected value on several statically-typed outputs — ``IMAGE``,
    ``MASK``, ``STRING``, ``INT``, ``FLOAT`` — so you wire the one your viewer /
    consumer expects (Preview Image, a text node, a math node, …). ComfyUI types
    sockets statically, so rather than retype one socket per run we surface every
    plausible type; which one is meaningful is *derived from the sample's features*
    (``Sample.describe()`` — stored ``__features__`` / ``__spec__`` when present,
    else inferred from the live value):

    * ``IMAGE`` — the field rendered via ``waivefront.visualizers.value_to_image``
      (PIL / tensor / 2-D map → ``[1, H, W, 3]`` float in ``[0, 1]``). A non-visual
      value renders as a small text image so the socket is always wireable.
    * ``MASK`` — a 2-D field as ``[1, H, W]`` float in ``[0, 1]`` (segmentation
      targets); a non-2-D field yields an empty ``1×1`` mask.
    * ``STRING`` — a text view: a scalar/label (with ``label_name`` from metadata
      when the target carries it), else a compact type summary.
    * ``INT`` / ``FLOAT`` — the value as a number when it is a 0-D scalar (e.g. a
      class id), else ``0`` / ``0.0``.

    Unlike the removed ``SampleViewerNode`` this is NOT an ``OUTPUT_NODE``: it
    produces typed values for native ComfyUI viewers, so previewing/rendering lives
    in those nodes and only field extraction lives here.
    """

    CATEGORY = _FLUXSTUDIO_CATEGORY
    RETURN_TYPES = ("IMAGE", "MASK", "STRING", "INT", "FLOAT")
    RETURN_NAMES = ("image", "mask", "text", "int", "float")
    FUNCTION = "extract"

    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        return {
            "required": {
                "sample": (SAMPLE_TYPE,),
                "field": (["input", "target"], {"default": "input"}),
                # A combo (enumeration), not free text — the closed waivefront ``Colormap`` set, so the
                # dropdown can only offer colormaps ``value_to_image`` actually accepts. Single source of
                # truth: the options come from ``COLORMAPS`` (== ``get_args(Colormap)``).
                "colormap": (_colormap_choices(), {"default": "viridis"}),
                "max_size": ("INT", {"default": 512, "min": 32, "max": 4096}),
            }
        }

    def extract(
        self,
        sample: Any,
        field: str = "input",
        colormap: "Colormap" = "viridis",
        max_size: int = 512,
    ) -> Tuple[Any, ...]:
        value = sample.input if field == "input" else sample.target
        spec = self._field_spec(sample, field, value)
        scalar = self._scalar(value)
        image = self._to_image(value, colormap, max_size)
        mask = self._to_mask(value)
        text = self._to_text(value, spec, scalar, sample, field)
        int_val = int(scalar) if scalar is not None else 0
        float_val = float(scalar) if scalar is not None else 0.0
        logger.debug(f"SampleExtractorNode: field={field} type={spec} scalar={scalar}")
        return (image, mask, text, int_val, float_val)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _field_spec(sample: Any, field: str, value: Any) -> Any:
        """The selected field's TypeSpec — from declared features, else inferred from the value."""
        from dataflux.typespec import infer_type

        try:
            described = sample.describe()
            return described.input if field == "input" else described.target
        except Exception:  # pragma: no cover - defensive: malformed stored type metadata
            return infer_type(value)

    @staticmethod
    def _scalar(value: Any) -> Optional[float]:
        """``value`` as a Python number when it is a 0-D scalar, else ``None``."""
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, np.generic) or (isinstance(value, np.ndarray) and value.ndim == 0):
            v = value.item()
            return v if isinstance(v, (int, float)) else None
        if torch.is_tensor(value) and value.dim() == 0:
            v = value.item()
            return v if isinstance(v, (int, float)) else None
        return None

    @staticmethod
    def _to_image(value: Any, colormap: "Colormap", max_size: int) -> torch.Tensor:
        """Render ``value`` to a ComfyUI IMAGE tensor ``[1, H, W, 3]`` float in ``[0, 1]``."""
        try:
            from waivefront.visualizers import value_to_image
        except ImportError:
            # waivefront extra absent — degrade to a 1×1 placeholder so the text/number outputs
            # still work; install fluxstudio[waivefront] to render images.
            logger.debug("SampleExtractorNode: waivefront not installed; IMAGE output is a placeholder.")
            return torch.zeros((1, 1, 1, 3), dtype=torch.float32)
        rgb = value_to_image(value, colormap=colormap, max_size=max_size)
        return torch.from_numpy(rgb.astype(np.float32) / 255.0)[None,]

    @classmethod
    def _to_mask(cls, value: Any) -> torch.Tensor:
        """A 2-D field as a ComfyUI MASK tensor ``[1, H, W]`` float in ``[0, 1]``; else empty ``1×1``."""
        arr = cls._as_2d(value)
        if arr is None:
            return torch.zeros((1, 1, 1), dtype=torch.float32)
        arr = arr.astype(np.float32)
        lo, hi = float(np.min(arr)), float(np.max(arr))
        arr = (arr - lo) / (hi - lo) if hi > lo else np.clip(arr, 0.0, 1.0)
        return torch.from_numpy(arr)[None,]

    @staticmethod
    def _as_2d(value: Any) -> Optional[np.ndarray]:
        """Coerce ``value`` to a 2-D ndarray (squeezing singleton axes), or ``None`` if not 2-D-like."""
        if torch.is_tensor(value):
            value = value.detach().cpu().numpy()
        elif hasattr(value, "convert"):  # PIL.Image.Image
            value = np.array(value.convert("L"))
        if not isinstance(value, np.ndarray):
            return None
        arr = np.squeeze(value)
        return arr if arr.ndim == 2 else None

    @staticmethod
    def _to_text(value: Any, spec: Any, scalar: Optional[float], sample: Any, field: str) -> str:
        """A concise text view of the field — a scalar/label value, else a compact type summary."""
        if isinstance(value, str):
            return value
        if scalar is not None:
            label = (sample.metadata or {}).get("label_name") if field == "target" else None
            n: Any = int(scalar) if float(scalar).is_integer() else scalar
            return f"{label} (id={n})" if label is not None else str(n)
        shape = getattr(value, "shape", None)
        shape_str = f" shape={tuple(shape)}" if shape is not None else ""
        return f"{SampleExtractorNode._spec_summary(spec)}{shape_str}".strip()

    @staticmethod
    def _spec_summary(spec: Any) -> str:
        """Compact, human-readable summary of a derived TypeSpec (avoids the verbose dataclass repr)."""
        semantic = getattr(spec, "semantic", None)
        dtype = getattr(spec, "dtype", None)
        if semantic is not None or dtype is not None:
            return " ".join(p for p in (semantic, dtype) if p) or "array"
        name = getattr(spec, "qualname", None)  # PythonType.qualname (e.g. "list")
        return name if isinstance(name, str) else type(spec).__name__


def _processing_interrupted() -> bool:
    """Whether the user pressed ComfyUI's interrupt button.

    Lazy + guarded: ``comfy`` only exists when running INSIDE ComfyUI, not in
    fluxstudio's own test/dev environment — so absent comfy ⇒ no interrupt
    source ⇒ ``False`` (the run proceeds to completion exactly as before). Kept
    as a tiny standalone function so the interrupt path is trivially
    monkeypatchable in tests. Mirrors the "degrade gracefully when an optional
    dependency is absent" pattern used by ``_colormap_choices`` above.
    """
    try:
        from comfy import model_management
    except Exception:  # comfy not importable outside a running ComfyUI
        return False
    return bool(model_management.processing_interrupted())


def _interrupt_exception_type() -> Type[BaseException]:
    """ComfyUI's ``InterruptProcessingException`` if available, else ``KeyboardInterrupt``.

    ComfyUI's executor recognises ``InterruptProcessingException`` as a clean
    user-interrupt (it resets the UI instead of surfacing a traceback). Outside
    ComfyUI the type doesn't exist, so we fall back to ``KeyboardInterrupt`` —
    enough for tests to observe that the run was cancelled.
    """
    try:
        from comfy.model_management import InterruptProcessingException

        exc_type: Type[BaseException] = InterruptProcessingException
        return exc_type
    except Exception:  # comfy not importable outside a running ComfyUI
        return KeyboardInterrupt


def _raise_interrupt_in_thread(thread_id: int, exc_type: Type[BaseException] = KeyboardInterrupt) -> None:
    """Asynchronously raise ``exc_type`` in another thread (cooperative cancel).

    ComfyUI checks its interrupt flag only BETWEEN nodes, but :func:`run_runnable`'s
    ``runnable.run()`` is one long blocking call — so we run it on a worker
    thread and, when the user hits interrupt, inject an exception into that
    worker via CPython's ``PyThreadState_SetAsyncExc``. This is the sanctioned
    mechanism for cancelling an otherwise-uncooperative thread; the exception is
    delivered at the next Python bytecode boundary (between training batches /
    pipeline samples), so it cannot break into a single long-running C kernel —
    an acceptable limitation for our per-batch / per-sample loops.
    """
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(thread_id), ctypes.py_object(exc_type))
    if res > 1:
        # Should only ever affect the one target thread; undo if it hit more so
        # we don't leave a pending async exception dangling on another thread.
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(thread_id), None)
        raise SystemError("PyThreadState_SetAsyncExc affected more than one thread")


# How much captured run output to keep in the node's display widget (the TAIL — final metrics /
# any error are the most relevant). The full stream still reaches the live ComfyUI console (the tee).
_MAX_OUTPUT_CHARS = 50_000


class _TeeStream:
    """A text stream that mirrors writes to an ``original`` stream AND accumulates them in ``buffer``.

    Installed as ``sys.stdout`` / ``sys.stderr`` only while the :func:`run_runnable` worker runs, so the live
    ComfyUI console still shows training output AS IT HAPPENS while we also capture it for the node's
    output widget. Attribute access (``isatty`` / ``fileno`` / ``encoding`` — tqdm probes these)
    delegates to the original stream so a real progress bar still renders in the console.
    """

    def __init__(self, original: Any, buffer: "io.StringIO") -> None:
        self._original = original
        self._buffer = buffer

    def write(self, text: str) -> int:
        try:
            self._original.write(text)
        except Exception:  # noqa: BLE001 - a broken console must never abort the run
            pass
        self._buffer.write(text)
        return len(text)

    def flush(self) -> None:
        try:
            self._original.flush()
        except Exception:  # noqa: BLE001
            pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)


def _collapse_carriage_returns(text: str) -> str:
    """Render ``\\r`` overwrites like a terminal would — keep only the final segment per line.

    Progress bars (tqdm / Lightning) repaint a line with ``\\r`` rather than a newline; raw-captured
    that floods the widget with hundreds of partial frames. Keeping the text after the last ``\\r`` on
    each line collapses each bar to its final state.
    """
    return "\n".join(line.rsplit("\r", 1)[-1] if "\r" in line else line for line in text.split("\n"))


# ANSI CSI escape sequences (ECMA-48): ESC '[' params intermediates final-byte. Rich / tqdm progress
# bars emit these for color, dim/underline (\x1b[2m / \x1b[2;4m), erase-line (\x1b[2K) and cursor
# moves — none of which a plain ComfyUI text widget can render, so they show up as literal "[2K"/"[0m"
# garbage. NO_COLOR strips the *color* SGR codes but not dim/underline/erase, so we strip the lot for
# the WIDGET copy only; the live console tee keeps them so the bar still animates in the terminal.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _strip_ansi(text: str) -> str:
    """Remove ANSI CSI escape sequences so captured progress-bar output reads as plain text."""
    return _ANSI_RE.sub("", text)


def _send_run_output(unique_id: Optional[str], text: str) -> None:
    """Push a live snapshot of the run's captured output to the node's widget (``web/run_output.js``).

    Lazy + guarded like :func:`_processing_interrupted`: ComfyUI's ``server`` module only exists
    INSIDE a running ComfyUI, so an absent ``server`` (fluxstudio's own test/dev env) or a missing
    ``unique_id`` ⇒ a silent no-op — the run still completes and the final ``{"ui": {"text": [...]}}``
    return populates the widget. Uses ComfyUI's websocket push (``PromptServer.instance.send_sync``)
    so the widget updates DURING the run: the streamed snapshot is already ``\\r``-collapsed to each
    progress bar's latest frame, so a tqdm / Lightning bar updates IN PLACE in the node, live.
    """
    if unique_id is None:
        return
    try:
        import server  # type: ignore[import-not-found]

        server.PromptServer.instance.send_sync("taidal.run_output", {"node": unique_id, "text": text})
    except Exception:  # noqa: BLE001 - a UI push must never abort or slow the run
        pass


def run_runnable(runnable: Any, unique_id: Optional[str] = None) -> str:
    """Run a built trainer / evaluator / processor and return its captured stdout/stderr.

    The SHARED runner: every generated runnable node calls this on execute (it IS the terminal
    ``OUTPUT_NODE`` — see :func:`fluxstudio.bridge._build_runnable_node`). The work:

    * **Autograd restore.** ComfyUI evaluates the whole graph under ``torch.inference_mode()``
      (``execution.py``) for cheap, grad-free node runs — fatal for gradient-based TRAINING (model
      params, forward activations, and the loss become inference tensors with no autograd graph, so
      ``loss.backward()`` dies with *"element 0 … does not require grad"*). A runnable that declares
      itself a ``marainer.TorchRunner`` (duck-typed ``__torch_runner__``) gets normal autograd restored
      for the duration of ``run()``; inference-only runnables (evaluators / ``DatasetProcessor``) run
      under inference mode (which the worker re-enters explicitly — grad mode is THREAD-LOCAL so the
      worker does not inherit the calling thread's context).
    * **Interruptible.** ComfyUI polls its interrupt flag only BETWEEN nodes, but ``run()`` is one
      blocking call (minutes/hours). So it runs on a daemon worker thread while this thread polls
      :func:`_processing_interrupted` every 0.1 s; on interrupt it injects ``KeyboardInterrupt`` into the
      worker (:func:`_raise_interrupt_in_thread`) and re-raises ComfyUI's ``InterruptProcessingException``
      for a clean stop. The injection lands at a Python statement boundary (between batches / samples).
    * **Live output.** The worker's stdout/stderr is tee'd to the live console AND a buffer; the poll
      loop streams the latest ``\\r``-collapsed, ANSI-stripped snapshot via :func:`_send_run_output`
      (the ``taidal.run_output`` event) so ``web/run_output.js`` updates the node's log widget in place.

    Returns the final captured text (the caller wraps it in ``{"ui": {"text": [...]}}`` for the widget).
    """
    run = getattr(runnable, "run", None)
    if not callable(run):
        raise RuntimeError(
            f"run_runnable: wired object {type(runnable).__name__} has no callable run() "
            "(expected a trainer / evaluator / processor)."
        )
    logger.info(f"Running {type(runnable).__name__}…")

    torch_runner = bool(getattr(runnable, "__torch_runner__", False))
    # torch's grad/inference mode is THREAD-LOCAL, so the worker thread does NOT inherit the
    # torch.inference_mode() ComfyUI wraps graph execution in — capture it here and re-establish
    # the correct context inside the worker.
    was_inference = torch.is_inference_mode_enabled()
    holder: Dict[str, BaseException] = {}

    def _target() -> None:
        try:
            if torch_runner:
                with torch.inference_mode(False), torch.enable_grad():
                    run()
            elif was_inference:
                with torch.inference_mode():
                    run()
            else:
                run()
        except BaseException as exc:  # noqa: BLE001 — surfaced to the caller thread below
            holder["exc"] = exc

    worker = threading.Thread(target=_target, name=f"run_runnable-{type(runnable).__name__}", daemon=True)
    # Tee stdout/stderr for the run window: the worker's prints/progress/logflow lines mirror to the
    # live console AND accumulate in `buffer` for the node's log widget. Restored in finally BEFORE any
    # interrupt/error re-raise so a failure never leaves the streams swapped.
    buffer = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _TeeStream(old_out, buffer), _TeeStream(old_err, buffer)

    def _snapshot() -> str:
        snap = _strip_ansi(_collapse_carriage_returns(buffer.getvalue()))
        if len(snap) > _MAX_OUTPUT_CHARS:
            snap = "… (output truncated — see the console for the full log)\n" + snap[-_MAX_OUTPUT_CHARS:]
        return snap

    interrupted = False
    last_sent = ""
    try:
        worker.start()
        while worker.is_alive():
            worker.join(0.1)
            if not interrupted and _processing_interrupted():
                logger.info(f"Interrupt requested — stopping {type(runnable).__name__}…")
                interrupted = True
                if worker.ident is not None:
                    _raise_interrupt_in_thread(worker.ident, KeyboardInterrupt)
            # Stream the latest output to the node's widget so progress bars update LIVE. Push only
            # when the collapsed snapshot changed, so an idle/quiet worker costs nothing.
            snapshot = _snapshot()
            if snapshot != last_sent:
                last_sent = snapshot
                _send_run_output(unique_id, snapshot)
    finally:
        sys.stdout, sys.stderr = old_out, old_err

    captured = _snapshot()

    exc = holder.get("exc")
    if interrupted or isinstance(exc, (KeyboardInterrupt, _interrupt_exception_type())):
        # Surface ComfyUI's clean-interrupt signal (resets the UI, no traceback).
        raise _interrupt_exception_type()()
    if exc is not None:
        raise exc
    text = captured.strip() or f"{type(runnable).__name__}.run() complete"
    # Final live push (covers any output produced after the last poll).
    _send_run_output(unique_id, text)
    return text


class AnyType(str):
    """A ComfyUI socket type that compares equal to EVERY other type (litegraph wildcard).

    ComfyUI gates a connection on socket-type equality; the established custom-node idiom for a
    "connect to anything" socket is a ``str`` subclass whose ``__ne__`` is always ``False`` (so the
    server-side ``any_type != other`` check passes) carrying the literal ``"*"`` (which the litegraph
    frontend treats as a wildcard). Used by :class:`RetagObjectNode` for its INPUT (accept any object
    output) and its initial OUTPUT (``web/retag.js`` narrows the output to the chosen category at
    edit time).
    """

    def __ne__(self, other: object) -> bool:  # noqa: D105 - wildcard equality
        return False

    def __eq__(self, other: object) -> bool:  # noqa: D105
        return True

    def __hash__(self) -> int:  # noqa: D105 - str subclass stays hashable
        return hash(str(self))


ANY = AnyType("*")

# Sentinels for the Retag combos: keep the object's existing tag, or clear it.
_RETAG_KEEP = "(keep)"
_RETAG_NONE = "(none)"


def _retag_options(kind: str) -> List[str]:
    """Combo options for the Retag node: ``(keep)`` / ``(none)`` + every registered task/role.

    Read from the confluid registry at ``INPUT_TYPES`` call time (ComfyUI queries ``/object_info``
    AFTER entry-point discovery has populated the registry), so the picker reflects whatever is
    installed. ``kind`` is ``"task"`` or ``"role"``.
    """
    try:
        from confluid import get_registry

        reg = get_registry()
        values = sorted(reg.list_tasks() if kind == "task" else reg.list_roles())
    except Exception:  # pragma: no cover - registry must always be importable; defensive
        values = []
    return [_RETAG_KEEP, _RETAG_NONE, *values]


class RetagObjectNode:
    """Re-tag a DataFlux object's ROLE (and task guidance) so it docks into a different role slot.

    Object-member sockets are ROLE-qualified and TASK-AGNOSTIC (``DATAFLUX_OBJECT:model`` — see
    ``bridge._object_socket_for_class``), so a node connects where its ROLE matches regardless of
    task. This is the deliberate, visible ESCAPE HATCH: wire any object in, pick a ``target_role``,
    and the object passes through re-stamped — ``web/retag.js`` retypes the OUTPUT socket to the
    chosen ``DATAFLUX_OBJECT:<role>`` so it docks into that role's slots (a generic object made to
    fit a ``model`` slot, or a model re-cast as a ``loss``). ``target_task`` re-stamps only the
    guidance tag (palette / navigaitor ranking) — it no longer affects the socket. ``(keep)`` leaves
    the existing tag; ``(none)`` clears it (generic ``DATAFLUX_OBJECT``). Per-INSTANCE (never mutates
    the class).

    Round-trip caveat: graphio's whole-graph YAML export does not yet handle Retag nodes (no
    ``TAIDAL_CALLABLE_PATH``) — it raises ``UnsupportedTopologyError`` like the other canvas-only
    adapters. The Retag node is a canvas convenience; hand-written YAML controls types directly.
    """

    CATEGORY = _FLUXSTUDIO_CATEGORY
    RETURN_TYPES = (ANY,)
    RETURN_NAMES = ("object",)
    FUNCTION = "retag"

    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        return {
            "required": {
                "object": (ANY,),
                "target_role": (_retag_options("role"), {"default": _RETAG_KEEP}),
                "target_task": (_retag_options("task"), {"default": _RETAG_KEEP}),
            }
        }

    @staticmethod
    def _apply(obj: Any, attr: str, value: str) -> None:
        """Per-instance set/clear of a ``__confluid_*`` tag (``(keep)`` is a no-op)."""
        if value == _RETAG_KEEP:
            return
        try:
            setattr(obj, attr, None if value == _RETAG_NONE else value)
        except Exception as exc:  # noqa: BLE001 - some objects forbid attr-set; surface, don't crash
            logger.debug(f"Retag: could not set {attr} on {type(obj).__name__}: {exc}")

    def retag(self, object: Any, target_role: str = _RETAG_KEEP, target_task: str = _RETAG_KEEP) -> Tuple[Any, ...]:
        # ``object`` is a live member instance OR a confluid ``LazyClass`` marker — both accept a
        # per-instance attribute that shadows the class-level ``@configurable`` tag for downstream
        # consumers. ``role`` drives the wire type (the actual docking); ``task`` is guidance only.
        # The wire-level retype (what lets it dock elsewhere) is done by retag.js off ``target_role``.
        self._apply(object, "__confluid_role__", target_role)
        self._apply(object, "__confluid_task__", target_task)
        return (object,)


# ---------------------------------------------------------------------------
# Sample composition nodes
# ---------------------------------------------------------------------------


class ComposeSampleNode:
    """Build a new Sample by plucking each field from a different source Sample.

    Takes three ``DATAFLUX_SAMPLE`` inputs and assembles a fresh Sample where:

    * ``input``    comes from ``input_sample``
    * ``target``   comes from ``target_sample``
    * ``metadata`` comes from ``metadata_sample``

    Use this when three parallel pipeline branches each carry the "right" field
    and you need to reunite them — e.g. the left branch produced a rendered
    image, the right branch produced detection boxes, and a third carries the
    original provenance metadata.
    """

    CATEGORY = _FLUXSTUDIO_CATEGORY
    RETURN_TYPES = (SAMPLE_TYPE,)
    RETURN_NAMES = ("sample",)
    FUNCTION = "compose"

    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        return {
            "required": {
                "input_sample": (SAMPLE_TYPE,),
                "target_sample": (SAMPLE_TYPE,),
                "metadata_sample": (SAMPLE_TYPE,),
            }
        }

    def compose(self, input_sample: Any, target_sample: Any, metadata_sample: Any) -> Tuple[Any, ...]:
        result = input_sample._replace(
            target=target_sample.target,
            metadata=metadata_sample.metadata,
        )
        return (result,)


class MixSamplesNode:
    """Build a new Sample by combining fields from three source Samples and merging all metadata.

    * ``input``    comes from ``input_sample``
    * ``target``   comes from ``target_sample``
    * ``metadata`` is the shallow dict union of all three samples' metadata dicts
      (``input_sample`` lowest priority, ``metadata_sample`` highest — later keys win)

    Use this when you want to accumulate metadata produced at different pipeline
    stages rather than discard two samples' metadata entirely.
    """

    CATEGORY = _FLUXSTUDIO_CATEGORY
    RETURN_TYPES = (SAMPLE_TYPE,)
    RETURN_NAMES = ("sample",)
    FUNCTION = "mix"

    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        return {
            "required": {
                "input_sample": (SAMPLE_TYPE,),
                "target_sample": (SAMPLE_TYPE,),
                "metadata_sample": (SAMPLE_TYPE,),
            }
        }

    def mix(self, input_sample: Any, target_sample: Any, metadata_sample: Any) -> Tuple[Any, ...]:
        m1 = input_sample.metadata if isinstance(input_sample.metadata, dict) else {}
        m2 = target_sample.metadata if isinstance(target_sample.metadata, dict) else {}
        m3 = metadata_sample.metadata if isinstance(metadata_sample.metadata, dict) else {}
        result = input_sample._replace(
            target=target_sample.target,
            metadata={**m1, **m2, **m3},
        )
        return (result,)


# ---------------------------------------------------------------------------
# Scalar math expression nodes
# ---------------------------------------------------------------------------

# Palette folder for math utility nodes — sibling to _FLUXSTUDIO_CATEGORY.
_MATH_CATEGORY = "/".join(p for p in (node_root(), "Math") if p)

# A restricted math namespace for formula evaluation: every public symbol from
# the stdlib ``math`` module plus the built-in scalar helpers. No builtins are
# passed to ``eval`` so ``exec``, ``import``, ``open``, etc. are unavailable.
_MATH_NAMESPACE: Dict[str, Any] = {k: getattr(_math_module, k) for k in dir(_math_module) if not k.startswith("_")}
_MATH_NAMESPACE.update({"abs": abs, "min": min, "max": max, "round": round, "pow": pow})


def _eval_formula(formula: str, a: Any, b: Any, c: Any, d: Any, e: Any) -> Any:
    """Evaluate a math formula string with variables a–e in a safe restricted namespace.

    Variables a–e take precedence over same-named math constants (e.g. ``math.e``).
    """
    # Math namespace first, then variables — so user variables (including 'e') shadow
    # any math-module constants of the same name (e.g. math.e ≈ 2.718 must not win).
    local_ns: Dict[str, Any] = {**_MATH_NAMESPACE, "a": a, "b": b, "c": c, "d": d, "e": e}
    try:
        return eval(formula, {"__builtins__": {}}, local_ns)  # noqa: S307
    except Exception as exc:
        raise ValueError(f"Math formula {formula!r} failed: {exc}") from exc


class MathNode:
    """Evaluate a formula over up to 5 named inputs (a–e) and emit float, int, and bool outputs.

    Inputs a–e accept ANY wired ComfyUI type (INT, FLOAT, BOOLEAN, STRING, …) — wire only
    those your formula uses; unwired inputs default to ``0``. The ``formula`` is a Python
    expression with access to all ``math`` module symbols (``sqrt``, ``sin``, ``log``, …)
    plus ``abs``, ``min``, ``max``, ``round``, ``pow``. Variable names shadow same-named
    math constants (e.g. wiring ``e=5`` is ``5``, not Euler's ``2.718``).

    The single result is cast to all three output types simultaneously — wire whichever
    output socket your downstream node expects.

    Examples::

        a=3, b=4, formula="sqrt(a**2 + b**2)"  →  float=5.0, int=5, bool=True
        a=10, formula="a > 5"                  →  float=1.0, int=1, bool=True
        a=6, b=7, formula="a * b"              →  float=42.0, int=42, bool=True
    """

    CATEGORY = _MATH_CATEGORY
    RETURN_TYPES = ("FLOAT", "INT", "BOOLEAN")
    RETURN_NAMES = ("float", "int", "bool")
    FUNCTION = "compute"

    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        return {
            "required": {
                "formula": ("STRING", {"default": "a + b", "multiline": False}),
            },
            "optional": {
                "a": (ANY,),
                "b": (ANY,),
                "c": (ANY,),
                "d": (ANY,),
                "e": (ANY,),
            },
        }

    def compute(
        self,
        formula: str,
        a: Any = 0,
        b: Any = 0,
        c: Any = 0,
        d: Any = 0,
        e: Any = 0,
    ) -> Tuple[float, int, bool]:
        # Unwired optional inputs arrive as None — treat as 0.
        a = 0 if a is None else a
        b = 0 if b is None else b
        c = 0 if c is None else c
        d = 0 if d is None else d
        e = 0 if e is None else e
        result = _eval_formula(formula, a, b, c, d, e)
        return (float(result), int(result), bool(result))


# Statically-defined adapter nodes, merged into the auto-discovered set by fluxstudio/__init__.py.
NODE_CLASS_MAPPINGS: Dict[str, Type] = {
    "Taidal_WalkDataset": WalkDatasetNode,
    "Taidal_ExtractSample": SampleExtractorNode,
    "Taidal_Retag": RetagObjectNode,
    "Taidal_ComposeSample": ComposeSampleNode,
    "Taidal_MixSamples": MixSamplesNode,
    "Taidal_Math": MathNode,
}

NODE_DISPLAY_NAME_MAPPINGS: Dict[str, str] = {
    "Taidal_WalkDataset": "Walk Dataset (by index)",
    "Taidal_ExtractSample": "Extract from Sample",
    "Taidal_Retag": "Retag (role / task)",
    "Taidal_ComposeSample": "Compose Sample (3-way)",
    "Taidal_MixSamples": "Mix Samples (merge metadata)",
    "Taidal_Math": "Math",
}
