"""Static ComfyUI adapter nodes for FluxStudio: walk a SampleFlux source by index, and extract a record key.

These are ComfyUI orchestration/adapter glue — NOT SampleFlux data ops — so they live here rather
than in sampleflux / waivefront. A sample is a PLAIN dict (``sampleflux.items.Record``) of typed
items; ``SampleExtractorNode`` unpacks one record entry (a ``field`` key, with ``input``/``target``
falling back to a key-preference scan) onto statically-typed ComfyUI sockets (IMAGE / MASK / STRING /
INT / FLOAT) so it wires into NATIVE ComfyUI viewers (Preview Image, a text node, …) instead of a
bespoke in-canvas viewer — the relevant type is derived from the entry's item class. The actual
image rendering is delegated to ``waivefront.visualizers.value_to_image`` (per the "no
FluxStudio-only visualizers" mandate); this module only bridges to ComfyUI's tensor protocols and
provides the index-walk stepper.
"""

import ast
import base64
import contextvars
import ctypes
import io
import json
import math as _math_module
import re
import string
import sys
import threading
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Type

import numpy as np
import torch
from loggair import get_logger

from fluxstudio.bridge import ARRAY_TYPE, OP_TYPE, SAMPLE_TYPE, SOURCE_TYPE, VALUE_TYPE, node_root

if TYPE_CHECKING:
    from sampleflux.ops.image import TextPosition
    from waivefront.visualizers import Colormap

logger = get_logger(__name__)

# Palette folder for the hand-written adapter nodes. Mirrors the auto-discovered nodes' root: nested
# under ``Taidal/`` by default, or at the palette root (``FluxStudio``) when the CLI's
# ``--no-default-nodes`` flag is active (see bridge.node_root). Computed at import — the CLI exports
# the env var before launching ComfyUI, so it is set by the time this module is imported.
_FLUXSTUDIO_CATEGORY = "/".join(p for p in (node_root(), "FluxStudio") if p)

# Key-preference scan backing the legacy ``input`` / ``target`` field selections: a record is a
# plain dict with NO role tags, so the conventional key names carry the meaning (exactly like every
# torch batch dict). ``input`` falls back to the record's FIRST key, ``target`` to its SECOND.
_KEY_PREFERENCES: Dict[str, Tuple[str, ...]] = {
    "input": ("input", "image", "signal", "iq"),
    "target": ("target", "label", "mask", "class"),
}

# Every conventional primary-entry name — the record keys the Mix/Compose nodes treat as
# input/target payloads rather than metadata. A record has no role tags, so METADATA is defined
# as "every key not conventionally named as a primary" (a positional fallback pick must NOT
# exclude ordinary metadata keys from the union — only the name convention does).
_ALL_PRIMARY_NAMES = frozenset(_KEY_PREFERENCES["input"]) | frozenset(_KEY_PREFERENCES["target"])


def _resolve_field_key(record: Any, field: str) -> Optional[str]:
    """Resolve a ``field`` selection to an actual record KEY, or ``None``.

    ``field`` names a record key directly; the legacy ``input`` / ``target`` selections (or a name
    not present in the record) fall back to the :data:`_KEY_PREFERENCES` scan — ``input`` → the
    first of ``input``/``image``/… else the record's FIRST key; ``target`` → ``target``/``label``/…
    else the record's SECOND key (``None`` for a single-entry record). ``None`` for an empty /
    non-dict carrier or an unknown non-role name, so a typo degrades gracefully.
    """
    if not isinstance(record, dict) or not record:
        return None
    if field in record:
        return field
    for pref in _KEY_PREFERENCES.get(field, ()):
        if pref in record:
            return pref
    keys = list(record.keys())
    if field == "input":
        return keys[0]
    if field == "target":
        return keys[1] if len(keys) > 1 else None
    return None


def _field_payload(record: Any, key: Optional[str]) -> Any:
    """The payload behind ``record[key]``: a :class:`~sampleflux.Label` yields its ``.value``, any
    other typed item its ``item_data`` payload, a plain value passes through. ``None`` when absent."""
    from sampleflux import Label, item_data

    if key is None or not isinstance(record, dict):
        return None
    item = record.get(key)
    if isinstance(item, Label):
        return item.value
    return item_data(item)


def _mix_records(
    input_record: Any, target_record: Any, metadata_record: Any, *, merge_all: bool = True
) -> Dict[str, Any]:
    """Build a new record dict: the primary-input entry from ``input_record``, the target entry from
    ``target_record``, and the remaining (metadata-like) entries from ``metadata_record`` —
    ``merge_all=True`` unions the non-primary entries of all three (input lowest priority).

    A sample is a plain dict with no role tags, so "input"/"target" resolve via the same
    key-preference scan the extractor uses (:func:`_resolve_field_key`) and "metadata" is every
    key NOT conventionally named as a primary (:data:`_ALL_PRIMARY_NAMES` — an unconventionally
    named primary rides the metadata union, the price of a tag-less carrier). The chosen
    input/target entries are written LAST, so the node contract ("input comes from
    ``input_record``") always holds on a key collision.
    """
    out: Dict[str, Any] = {}
    ik = _resolve_field_key(input_record, "input")
    tk = _resolve_field_key(target_record, "target")
    meta_sources = (input_record, target_record, metadata_record) if merge_all else (metadata_record,)
    for src in meta_sources:
        if not isinstance(src, dict):
            continue
        for key, value in src.items():
            if key not in _ALL_PRIMARY_NAMES:
                out[key] = value
    if ik is not None and isinstance(input_record, dict):
        out[ik] = input_record[ik]
    if tk is not None and isinstance(target_record, dict):
        out[tk] = target_record[tk]
    return out


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
    """Emit the sample record at ``index`` from a ``SAMPLEFLUX_SOURCE``; ``index`` auto-increments each run.

    The ``index`` widget uses ComfyUI's ``control_after_generate``, defaulting to **increment** so every
    Queue Prompt advances to the next sample in order (set the control to "fixed" / index 0 to reset, or
    "randomize" to sample at random). With ``wrap`` enabled the index wraps modulo the source length;
    otherwise an out-of-range index raises.
    """

    __confluid_display_name__ = "Walk Dataset (by index)"
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
                "step": ("INT", {"default": 1, "min": 1, "max": _MAX_INDEX}),
                "wrap": ("BOOLEAN", {"default": True}),
            }
        }

    def walk(self, source: Any, index: int, step: int = 1, wrap: bool = True) -> Tuple[Any, ...]:
        # `step` is managed by web/walk_dataset_step.js — it sets index.options.step2 so
        # ComfyUI's native control_after_generate increments by `step` instead of 1.
        # Python receives the already-advanced `index` and uses it directly.
        from sampleflux.core import Flux

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
        logger.debug(f"WalkDatasetNode: yielding sample {resolved}/{length} (step={step})")
        return (flux[resolved],)


class SampleExtractorNode:
    """Unpack one entry of a sample record (a plain dict) onto typed ComfyUI sockets.

    Takes a ``SAMPLEFLUX_SAMPLE`` (a record dict) and a ``field`` KEY — type any record key;
    the legacy ``input`` / ``target`` selections fall back to a key-preference scan
    (``input``/``image``/… else the first key; ``target``/``label``/… else the second) — and
    exposes the selected value on several statically-typed outputs — ``IMAGE``,
    ``MASK``, ``STRING``, ``INT``, ``FLOAT``, ``SAMPLEFLUX_ARRAY`` — so you wire the one
    your viewer / consumer expects (Preview Image, a text node, a math node, the
    **Array / Tensor Histogram** viewer, …). ComfyUI types sockets statically, so
    rather than retype one socket per run we surface every plausible type; which one
    is meaningful is *derived from the entry's item class* (``Image`` / ``Mask`` /
    ``Regions`` / ``Label`` / …, else the live value's Python type):

    * ``IMAGE`` — the field rendered via ``waivefront.visualizers.value_to_image``
      (PIL / tensor / 2-D map → ``[1, H, W, 3]`` float in ``[0, 1]``). A non-visual
      value renders as a small text image so the socket is always wireable.
    * ``MASK`` — a 2-D field as ``[1, H, W]`` float in ``[0, 1]`` (segmentation
      targets); a non-2-D field yields an empty ``1×1`` mask.
    * ``STRING`` — a text view: a scalar/label (with ``label_name`` from metadata
      when the target carries it), else a compact type summary.
    * ``INT`` / ``FLOAT`` — the value as a number when it is a 0-D scalar (e.g. a
      class id), else ``0`` / ``0.0``.
    * ``array`` (``SAMPLEFLUX_ARRAY``) — the RAW field value on demand (a tensor stays
      a tensor, an ndarray stays an ndarray, a PIL image becomes an ndarray), for the
      **Array / Tensor Histogram** viewer or any array-consuming node. Unlike
      ``IMAGE``/``MASK`` this is NOT normalized/colormapped — it carries the original
      numbers so a downstream node can inspect or process them.
    * ``value`` (``SAMPLEFLUX_VALUE``) — the RAW field value VERBATIM (a list, dict, or
      any structured object — NOT coerced to an ndarray like ``array``), so a
      list-valued ``input``/``target`` (a list of boxes) wires straight into a
      nested-container param socket (``OverlayBoxes.boxes``; see
      ``bridge._needs_value_socket``).

    Unlike the removed ``SampleViewerNode`` this is NOT an ``OUTPUT_NODE``: it
    produces typed values for native ComfyUI viewers, so previewing/rendering lives
    in those nodes and only field extraction lives here.
    """

    __confluid_display_name__ = "Extract from Sample"
    CATEGORY = _FLUXSTUDIO_CATEGORY
    RETURN_TYPES = ("IMAGE", "MASK", "STRING", "INT", "FLOAT", ARRAY_TYPE, VALUE_TYPE)
    RETURN_NAMES = ("image", "mask", "text", "int", "float", "array", "value")
    FUNCTION = "extract"

    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        return {
            "required": {
                "sample": (SAMPLE_TYPE,),
                # A free-text record KEY (the record model has no closed role set). The default
                # ``input`` — like a saved canvas's ``input``/``target`` combo value — resolves
                # via the key-preference scan (see _resolve_field_key), so old canvases keep working.
                "field": ("STRING", {"default": "input"}),
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
        if sample is None:
            # A required input arriving as None means the upstream op produced no record (e.g. an
            # op chain executed with no source feeding it). Fail with a legible message instead of
            # an opaque ``AttributeError`` / ``KeyError`` downstream.
            raise ValueError(
                "Extract from Sample received an empty sample (None) — its upstream op produced no "
                "sample, which usually means the op chain ran with no source. If this viewer sits "
                "on an op-only path that is compiled by 'Extract as Op' (subgraph-as-an-Op), it does "
                "not need to run during a pipeline execution; wire it to a live source to preview, "
                "or move it off the compiled op path."
            )
        key = _resolve_field_key(sample, field)
        value = _field_payload(sample, key)
        spec = self._field_spec(sample, key, value)
        scalar = self._scalar(value)
        image = self._to_image(value, colormap, max_size)
        mask = self._to_mask(value)
        text = self._to_text(value, spec, scalar, sample, field)
        int_val = int(scalar) if scalar is not None else 0
        float_val = float(scalar) if scalar is not None else 0.0
        array_val = self._to_array(value)
        logger.debug(f"SampleExtractorNode: field={field} type={spec} scalar={scalar}")
        # ``value`` is the RAW field value, verbatim (a list of boxes stays a list) → SAMPLEFLUX_VALUE.
        return (image, mask, text, int_val, float_val, array_val, value)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_array(value: Any) -> Any:
        """The raw field value for the ``SAMPLEFLUX_ARRAY`` output (the histogram viewer's input).

        Tensors and ndarrays pass through UNCHANGED (preserving dtype / device); a PIL image becomes
        an ndarray; anything else (a scalar, a bbox list) is passed through verbatim — the consuming
        node coerces it (``sampleflux.ops.image.select_channel`` / ``array_histogram`` handle the rest).
        """
        if hasattr(value, "convert"):  # PIL.Image.Image -> ndarray (keep native mode: L=2-D, RGB=3-D)
            return np.array(value)
        return value

    @staticmethod
    def _field_spec(sample: Any, key: Optional[str], value: Any) -> Any:
        """A short descriptor of the selected entry — its item class name (``Image`` / ``Mask`` /
        ``Regions`` / ``Label`` / ``NDArrayItem``), else the raw value's Python type name.

        A record's values are typed items (each carries its own type), so the descriptor comes
        straight off the entry's class."""
        if isinstance(sample, dict) and key in sample:
            return type(sample[key]).__name__
        return type(value).__name__

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
        """Coerce ``value`` to a 2-D ndarray, or ``None`` if the shape is not mask-like.

        Handles:
          ``[H, W]``       → as-is
          ``[1, H, W]``    → squeeze batch/channel dim
          ``[H, W, 1]``    → squeeze trailing dim
          ``[C, H, W]``    → take channel 0 (channels-first, e.g. PyTorch segmentation output)
          ``[H, W, C]``    → take channel 0 on the last axis (channels-last)
        Anything else (e.g. a 1-D signal, bounding-box list) returns ``None``.
        """
        if torch.is_tensor(value):
            value = value.detach().cpu().numpy()
        elif hasattr(value, "convert"):  # PIL.Image.Image
            value = np.array(value.convert("L"))
        if not isinstance(value, np.ndarray):
            return None
        arr = np.squeeze(value)
        if arr.ndim == 2:
            return arr
        if arr.ndim == 3 and np.issubdtype(arr.dtype, np.floating):
            # Float 3-D arrays are segmentation probability maps ([C,H,W] or [H,W,C]).
            # Integer/uint8 3-D arrays are colour images — not masks, fall through to None.
            # C=1 was already squeezed above, so C >= 2 here.
            # Prefer channels-first ([C,H,W]): the smallest axis is almost always C.
            if arr.shape[0] <= arr.shape[1] and arr.shape[0] <= arr.shape[2]:
                return np.asarray(arr[0])  # channels-first: [C, H, W] -> [H, W]
            return np.asarray(arr[..., 0])  # channels-last:  [H, W, C] -> [H, W]
        return None

    @staticmethod
    def _to_text(value: Any, spec: Any, scalar: Optional[float], sample: Any, field: str) -> str:
        """A concise text view of the field — a scalar/label value, else a compact type summary."""
        if isinstance(value, str):
            return value
        if scalar is not None:
            # ``label_name`` is simply another record key on the record model.
            meta = sample if isinstance(sample, dict) else {}
            label = meta.get("label_name") if field == "target" else None
            n: Any = int(scalar) if float(scalar).is_integer() else scalar
            return f"{label} (id={n})" if label is not None else str(n)
        # Lists and dicts: show full content as YAML so bounding-box annotations, metadata
        # dicts, etc. are human-readable rather than just "list" or "dict".
        if isinstance(value, (list, dict)):
            return _to_yaml_str(value)
        shape = getattr(value, "shape", None)
        shape_str = f" shape={tuple(shape)}" if shape is not None else ""
        return f"{SampleExtractorNode._spec_summary(spec)}{shape_str}".strip()

    @staticmethod
    def _spec_summary(spec: Any) -> str:
        """Compact, human-readable summary of the field descriptor (an item/type class name)."""
        if isinstance(spec, str):
            return spec  # _field_spec emits the item class name directly on the record model
        return type(spec).__name__


# --------------------------------------------------------------------------- #
# Array / Tensor histogram viewer
# --------------------------------------------------------------------------- #

# The hover-readout value grid is nearest-downsampled so its longest side is at most this many cells.
# The rendered image may be large (``max_size`` up to 4096), but the per-pixel value grid shipped to
# the browser must stay small enough to JSON-encode cheaply — ~256 keeps the payload well under ~0.5 MB
# while still giving a faithful (real-sample, not interpolated) readout.
_HOVER_GRID_MAX = 256


def _sanitize_float(x: Any) -> Optional[float]:
    """A JSON-safe float: finite values pass through, ``NaN`` / ``±inf`` become ``None``.

    ComfyUI ships a node's ``ui`` payload over a websocket via ``json.dumps`` (default
    ``allow_nan=True``), which emits bare ``NaN`` / ``Infinity`` tokens — and the browser's
    ``JSON.parse`` REJECTS those, corrupting the whole message. So every float that may be non-finite
    (raw sample values, bin edges, stats) MUST be sanitized before it enters the payload. We use an
    explicit finite check rather than ``np.nan_to_num`` (which would substitute misleading finite
    numbers instead of signalling "missing").
    """
    try:
        v = float(x)
    except (TypeError, ValueError):  # pragma: no cover - defensive: non-numeric slipping through
        return None
    return v if _math_module.isfinite(v) else None


def _downsample_nearest(arr2d: np.ndarray, max_side: int) -> np.ndarray:
    """Nearest-neighbour (integer-stride) downsample a 2-D array so its longest side ≤ ``max_side``.

    Striding (not interpolation) keeps every emitted value a REAL sample of the original array, so the
    hover readout reports true values rather than blended ones.
    """
    h, w = arr2d.shape[:2]
    longest = max(h, w)
    if max_side <= 0 or longest <= max_side:
        return arr2d
    step = int(_math_module.ceil(longest / max_side))
    return arr2d[::step, ::step]


def _rgb_to_png_data_url(rgb: np.ndarray) -> str:
    """Encode an ``(H, W, 3)`` uint8 RGB array as a base64 ``data:image/png`` URL for the browser."""
    from PIL import Image as PILImage

    buf = io.BytesIO()
    PILImage.fromarray(np.asarray(rgb, dtype=np.uint8)).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _build_histogram_payload(
    value: Any,
    bins: int = 256,
    channel: int = -1,
    colormap: "Colormap" = "viridis",
    max_size: int = 512,
) -> Dict[str, Any]:
    """Build the JSON-safe payload the Array/Tensor Histogram node ships to ``web/array_histogram.js``.

    All computation lives in sampleflux (``array_histogram`` / ``select_channel`` / ``value_to_image``);
    this only assembles their output into a browser payload — a base64 PNG of the colormapped image,
    the histogram counts/edges + stats, and a small nearest-downsampled value grid for the hover
    readout (with original dims so the frontend can map a hovered display pixel back to the source
    coordinate). Every float that could be non-finite is sanitized to ``None`` (see
    :func:`_sanitize_float`) so the payload survives ComfyUI's JSON websocket encoding.
    """
    from sampleflux.ops.image import array_histogram, channel_count, select_channel, value_to_image

    hist = array_histogram(value, bins=bins, channel=channel)
    channels = int(hist.get("channels", channel_count(value)))

    # Rendered preview: an "all" (channel < 0) view of a MULTI-channel array renders as the natural
    # RGB image (value_to_image's own channel handling); everything else — a selected channel, a 2-D
    # map, or a 1-D strip — goes through select_channel → colormap so it always shows something useful.
    image_url, image_w, image_h = "", 0, 0
    try:
        if channels > 1 and (channel is None or channel < 0):
            rgb = value_to_image(value, colormap=colormap, max_size=max_size)
        else:
            rgb = value_to_image(select_channel(value, channel), colormap=colormap, max_size=max_size)
        image_url = _rgb_to_png_data_url(rgb)
        image_h, image_w = int(rgb.shape[0]), int(rgb.shape[1])
    except Exception as exc:  # noqa: BLE001 - a render failure must still leave a usable histogram
        logger.debug(f"ArrayHistogramViewerNode: image render failed ({type(exc).__name__}: {exc})")

    # Hover value grid: the selected 2-D view, nearest-downsampled, with original dims kept so the
    # frontend can map a hovered display pixel back to the source coordinate + value.
    disp = select_channel(value, channel)
    orig_h, orig_w = int(disp.shape[0]), int(disp.shape[1])
    grid = _downsample_nearest(disp, _HOVER_GRID_MAX)
    grid_h, grid_w = int(grid.shape[0]), int(grid.shape[1])
    values = [_sanitize_float(v) for v in np.round(grid.ravel(), 6).tolist()]

    payload: Dict[str, Any] = {
        "image": image_url,
        "image_width": image_w,
        "image_height": image_h,
        "histogram": {
            "counts": [int(c) for c in hist["counts"]],
            "bin_edges": [_sanitize_float(e) for e in hist["bin_edges"]],
        },
        "values": values,
        "grid_width": grid_w,
        "grid_height": grid_h,
        "orig_width": orig_w,
        "orig_height": orig_h,
        "stats": {
            "min": _sanitize_float(hist["min"]),
            "max": _sanitize_float(hist["max"]),
            "mean": _sanitize_float(hist["mean"]),
            "std": _sanitize_float(hist["std"]),
            "count": int(hist["count"]),
        },
        "channels": channels,
        "channel": int(channel),
        "dtype": str(getattr(value, "dtype", type(value).__name__)),
        "shape": str(tuple(value.shape)) if hasattr(value, "shape") else "",
    }
    if hist["count"] == 0:
        payload["message"] = "no finite numeric values to histogram"
    return payload


class ExtractMetadataNode:
    """Extract one metadata entry from a sample record by KEY → a wireable value.

    Models / ops stamp structured results into the record — a list of detection boxes under
    ``"detection_boxes"``, per-detection ``"detection_labels"`` / ``"detection_scores"``, an embedding
    array, a mask. On the record model metadata is simply another dict key (or an attribute riding on
    a typed item value). This node pulls ONE entry by ``key`` and emits it on three typed outputs so
    you wire the one your consumer expects:

    * ``value`` (``SAMPLEFLUX_VALUE``) — the entry VERBATIM (a list of boxes, a dict), to wire straight
      into a nested-container param socket (``OverlayBoxes.boxes`` — see ``bridge._needs_value_socket``).
    * ``array`` (``SAMPLEFLUX_ARRAY``) — the entry as an array when it is array-like (a tensor / ndarray
      passes through, a PIL image becomes an ndarray), for the Array / Tensor Histogram viewer.
    * ``text`` (``STRING``) — a compact ``repr`` view for inspection (wire into *Display Any* / a sink).

    A missing key yields ``None`` / an empty array / ``""`` (never raises), so a key typo degrades
    gracefully. Pairs with **Extract from Sample** (which unpacks an entry's PAYLOAD); this emits
    ``record[key]`` verbatim — the canonical flow ``model → (record['detection_boxes']) → OverlayBoxes.boxes``.
    """

    __confluid_display_name__ = "Extract from Metadata"
    CATEGORY = _FLUXSTUDIO_CATEGORY
    RETURN_TYPES = (VALUE_TYPE, ARRAY_TYPE, "STRING")
    RETURN_NAMES = ("value", "array", "text")
    FUNCTION = "extract"

    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        return {
            "required": {
                "sample": (SAMPLE_TYPE,),
                "key": ("STRING", {"default": "detection_boxes"}),
            }
        }

    def extract(self, sample: Any, key: str = "detection_boxes") -> Tuple[Any, Any, str]:
        if sample is None:
            raise ValueError(
                "Extract from Metadata received an empty sample (None) — its upstream op produced no "
                "sample. Wire it to a live source / model output to read metadata."
            )
        meta = sample if isinstance(sample, dict) else {}
        value = meta.get(key)
        if value is None:
            # Metadata may also ride as an ATTRIBUTE on a typed item value (an Image's layout,
            # a Regions' canvas) — scan the record's items for one so named.
            for item in meta.values():
                attr = getattr(item, key, None)
                if attr is not None:
                    value = attr
                    break
        array_val = SampleExtractorNode._to_array(value)  # PIL→ndarray; tensor/ndarray/list pass verbatim
        text = "" if value is None else repr(value)
        if len(text) > _MAX_DISPLAY_CHARS:
            text = text[:_MAX_DISPLAY_CHARS] + " …"
        logger.debug(f"ExtractMetadataNode: key={key!r} found={value is not None} type={type(value).__name__}")
        return (value, array_val, text)


class ArrayHistogramViewerNode:
    """Display the values of an array / tensor: a histogram, a colormapped image, and a hover readout.

    A FluxStudio analogue of *ComfyUI-Olm-Histogram* for SampleFlux data. Wire the ``array`` output of
    **Extract from Sample** (``SAMPLEFLUX_ARRAY``) — or a native ComfyUI ``IMAGE`` — into this node and
    **Queue Prompt**. It renders, on the node itself:

    * a **histogram** of the value distribution (linear or log y-scale);
    * the array **rendered as an image** (one channel colormapped, or the natural RGB for "all"); and
    * a **hover readout** — move the cursor over the image to read the value (and source coordinate)
      under the pointer.

    Widgets: ``channel`` (``-1`` = all channels → mean view / natural RGB; ``0..N`` selects one),
    ``colormap`` (the closed ``Colormap`` set), ``bins`` (histogram resolution) and ``max_size`` (the
    rendered image's longest side). Change ``channel`` / ``colormap`` and re-run to recompute; the log
    toggle in the widget redraws instantly without a re-run.

    A sanctioned custom in-canvas VIEWER node (see the *Adapter Nodes Are Glue* mandate): the rendering
    / analysis MATH lives in sampleflux (``array_histogram`` / ``select_channel`` / ``value_to_image``)
    and the node is thin glue — it base64-encodes that output and ships it to ``web/array_histogram.js``
    to draw. Custom viewer nodes are how you inspect data under ``--no-default-nodes`` (which prunes
    ComfyUI's native viewers); the histogram + hover-readout has no native equivalent regardless. The
    input array also passes straight through the ``array`` output so the viewer can sit inline.
    """

    __confluid_display_name__ = "Array / Tensor Histogram"
    CATEGORY = _FLUXSTUDIO_CATEGORY
    RETURN_TYPES = (ARRAY_TYPE,)
    RETURN_NAMES = ("array",)
    FUNCTION = "view"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        return {
            "required": {
                "bins": ("INT", {"default": 256, "min": 1, "max": 4096}),
                # -1 = all channels (mean view + natural RGB render); 0..N selects one channel.
                "channel": ("INT", {"default": -1, "min": -1, "max": 4096}),
                # A combo of the closed ``Colormap`` set (single source of truth — see _colormap_choices).
                "colormap": (_colormap_choices(), {"default": "viridis"}),
                "max_size": ("INT", {"default": 512, "min": 32, "max": 4096}),
            },
            "optional": {
                "array": (ARRAY_TYPE,),
                "image": ("IMAGE",),
            },
        }

    def view(
        self,
        bins: int = 256,
        channel: int = -1,
        colormap: "Colormap" = "viridis",
        max_size: int = 512,
        array: Any = None,
        image: Any = None,
    ) -> Dict[str, Any]:
        value = self._select_value(array, image)
        payload = _build_histogram_payload(value, bins=bins, channel=channel, colormap=colormap, max_size=max_size)
        logger.debug(
            f"ArrayHistogramViewerNode: channels={payload['channels']} count={payload['stats']['count']} "
            f"grid={payload['grid_width']}x{payload['grid_height']}"
        )
        # ``ui`` drives web/array_histogram.js (and is re-delivered on workflow re-open); ``result``
        # passes the array through so the viewer can sit inline in a pipeline.
        return {"ui": {"taidal_array_histogram": [payload]}, "result": (value,)}

    @staticmethod
    def _select_value(array: Any, image: Any) -> Any:
        """The value to inspect: the wired ``array`` if present, else frame 0 of a native ComfyUI IMAGE.

        A ComfyUI ``IMAGE`` is a ``[B, H, W, C]`` float tensor in ``[0, 1]``; take the first frame and
        histogram it in that native range. ``None`` (nothing wired) yields an empty histogram.
        """
        if array is not None:
            return array
        if image is not None:
            if torch.is_tensor(image) and image.dim() == 4:
                return image[0]
            return image
        return None


def _parse_class_names(raw: Any) -> Optional[List[str]]:
    """Parse a class-names input — a JSON list ``["a","b"]`` or a comma string ``a, b`` — to a list.

    Returns ``None`` for an empty/blank input (the payload builder then defaults to indices).
    """
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw]
    text = str(raw).strip()
    if not text:
        return None
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except (ValueError, TypeError):
            pass
    return [part.strip() for part in text.split(",") if part.strip()]


def _build_confusion_payloads(metrics: Any, matrix: Any, class_names: Any) -> List[Dict[str, Any]]:
    """Build the LIST of confusion-matrix payloads the viewer ships to ``web/confusion_matrix.js``.

    The extraction + normalization MATH lives in sampleflux (``confusion_matrices_payload`` — finds every
    square-2D entry in a metrics result, builds raw counts + the three ``true``/``pred``/``all``
    normalizations per matrix); this is thin glue. Inputs (first non-empty wins): ``metrics`` = an
    evaluator's ``all_metrics`` JSON dict (every metric — the CMs are extracted by shape, not name),
    else a standalone raw ``matrix`` array. ``[]`` when nothing usable is wired (the JS shows a hint).
    """
    from sampleflux.ops.image import confusion_matrices_payload

    names = _parse_class_names(class_names)
    if metrics is not None and str(metrics).strip() not in ("", "{}", "[]"):
        try:
            doc = json.loads(metrics) if isinstance(metrics, str) else metrics
        except (ValueError, TypeError):
            doc = None
        if doc is not None:
            payloads = confusion_matrices_payload(doc, class_names=names)
            if payloads:
                return payloads
    if matrix is not None:
        return confusion_matrices_payload(matrix, class_names=names)
    return []


class ConfusionMatrixViewerNode:
    """Display confusion matrices ON THE NODE: a class-labeled grid, normalize toggle, and hover readout.

    Wire the **``all_metrics``** output of a classification evaluator (a JSON ``STRING`` of every metric)
    — or a standalone raw ``N×N`` matrix on the ``matrix`` (``SAMPLEFLUX_ARRAY``) input plus optional
    ``class_names`` — into this node and **Queue Prompt**. It extracts EVERY confusion matrix (the
    square-2D metrics — there can be several) and renders one at a time, on the node's own canvas:

    * a **class-labeled grid** — rows = true class, columns = predicted class, cells coloured by value
      (the diagonal = correct predictions), with the class names down the left and across the top;
    * a **matrix selector** (when several matrices are present, an on-canvas button cycles between them);
    * a **normalize toggle** (an on-canvas button cycling ``none`` → ``true`` (row) → ``pred`` (col) →
      ``all``), display-only — the three normalizations are precomputed, so toggling never re-runs; and
    * a **hover readout** — move the cursor over a cell to read ``true=<class>, pred=<class>`` and the
      count / fraction under the pointer.

    A sanctioned custom in-canvas VIEWER node (see the *Adapter Nodes Are Glue* mandate): the extraction
    + normalization MATH lives in sampleflux (``confusion_matrices_payload``) — the evaluator output stays
    GENERIC (all metrics, no "which is the confusion matrix" knowledge); this node only ships the
    JSON-safe payloads to ``web/confusion_matrix.js`` to draw. It works regardless of
    ``--no-default-nodes`` (no native viewer needed). The first matrix's counts pass through the
    ``matrix`` output so the node can sit inline.
    """

    __confluid_display_name__ = "Confusion Matrix"
    CATEGORY = _FLUXSTUDIO_CATEGORY
    RETURN_TYPES = (ARRAY_TYPE,)
    RETURN_NAMES = ("matrix",)
    FUNCTION = "view"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        return {
            "required": {},
            "optional": {
                # The evaluator's `all_metrics` @output (a JSON STRING of every metric). forceInput =>
                # an input socket, not a widget, so the evaluator's STRING output wires straight in.
                "metrics": ("STRING", {"forceInput": True}),
                # Standalone use: a raw N×N matrix + optional labels (JSON list or comma string).
                "matrix": (ARRAY_TYPE,),
                "class_names": ("STRING", {"forceInput": True}),
            },
        }

    def view(
        self,
        metrics: Any = None,
        matrix: Any = None,
        class_names: Any = None,
    ) -> Dict[str, Any]:
        payloads = _build_confusion_payloads(metrics, matrix, class_names)
        logger.debug(f"ConfusionMatrixViewerNode: {len(payloads)} confusion matrix(es) extracted")
        # `ui` drives web/confusion_matrix.js (re-delivered on workflow re-open); `result` passes the
        # FIRST matrix's raw counts through so the viewer can sit inline.
        out = np.asarray(payloads[0]["counts"], dtype=np.int64) if payloads and payloads[0].get("counts") else None
        return {"ui": {"taidal_confusion_matrix": payloads}, "result": (out,)}


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


def _make_progress_bar(unique_id: Optional[str]) -> Any:
    """ComfyUI's native per-node ``ProgressBar`` for ``unique_id`` (the green node overlay), or ``None``.

    Lazy + guarded like :func:`_send_run_output`: ``comfy.utils`` only exists INSIDE a running ComfyUI,
    and a missing ``unique_id`` (fluxstudio's own test/dev env) ⇒ ``None`` so the runner proceeds with
    no native bar (the streamed text widget still works). The ``node_id`` is passed EXPLICITLY because
    the runnable's ``run()`` executes on a WORKER thread (:func:`run_runnable`) that does NOT inherit
    ComfyUI's ``executing_context`` contextvar — without an explicit id the progress hook can't infer
    which node to fill. Falls back to the older no-``node_id`` constructor (then relying on the server's
    last-executing-node, which IS our node during ``execute``) so an older ComfyUI still shows a bar.
    """
    if unique_id is None:
        return None
    try:
        from comfy.utils import ProgressBar  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - not inside ComfyUI ⇒ no native bar
        return None
    try:
        return ProgressBar(1, node_id=unique_id)
    except TypeError:  # pragma: no cover - older ComfyUI: ProgressBar(total) without node_id
        try:
            return ProgressBar(1)
        except Exception:  # noqa: BLE001
            return None


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

    # Native ComfyUI per-node progress bar (the green overlay). A runnable that supports the
    # comfy-free sampleflux.runnable.ProgressReporting protocol records its latest (value, total) into
    # `progress` from the WORKER thread (just a dict write); the poll loop below drives the ProgressBar
    # from THIS (calling) thread — so all websocket I/O stays on the same thread as _send_run_output,
    # with no new cross-thread send concerns. Absent comfy / no unique_id ⇒ progress_bar is None.
    progress_bar = _make_progress_bar(unique_id)
    progress: Dict[str, float] = {"value": 0.0, "total": 0.0}

    def _on_progress(value: float, total: float, desc: str = "") -> None:
        progress["value"] = value
        progress["total"] = total

    set_progress_callback = getattr(runnable, "set_progress_callback", None)
    if callable(set_progress_callback):
        set_progress_callback(_on_progress)

    def _drive_progress_bar() -> None:
        if progress_bar is not None and progress["total"] > 0:
            progress_bar.update_absolute(progress["value"], progress["total"])

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

    # copy_context().run so the worker inherits contextvars — confluid's engine
    # state (an active materialization context for late !ref:/Lazy flows) rides
    # a ContextVar that a raw Thread target would silently drop.
    worker_ctx = contextvars.copy_context()
    worker = threading.Thread(
        target=lambda: worker_ctx.run(_target), name=f"run_runnable-{type(runnable).__name__}", daemon=True
    )
    # Tee stdout/stderr for the run window: the worker's prints/progress/loggair lines mirror to the
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
            # Drive ComfyUI's native green progress bar from the runnable's reported (value, total).
            _drive_progress_bar()
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
    # Fill the native progress bar to 100% on a clean finish (the last poll may have caught the run
    # mid-step). ComfyUI's executor also calls finish_progress for this node, so this is belt-and-braces.
    if progress_bar is not None and progress["total"] > 0:
        progress_bar.update_absolute(progress["total"], progress["total"])
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
    """Re-tag a SampleFlux object's ROLE (and task guidance) so it docks into a different role slot.

    Object-member sockets are ROLE-qualified and TASK-AGNOSTIC (``SAMPLEFLUX_OBJECT:model`` — see
    ``bridge._object_socket_for_class``), so a node connects where its ROLE matches regardless of
    task. This is the deliberate, visible ESCAPE HATCH: wire any object in, pick a ``target_role``,
    and the object passes through re-stamped — ``web/retag.js`` retypes the OUTPUT socket to the
    chosen ``SAMPLEFLUX_OBJECT:<role>`` so it docks into that role's slots (a generic object made to
    fit a ``model`` slot, or a model re-cast as a ``loss``). ``target_task`` re-stamps only the
    guidance tag (palette / navigaitor ranking) — it no longer affects the socket. ``(keep)`` leaves
    the existing tag; ``(none)`` clears it (generic ``SAMPLEFLUX_OBJECT``). Per-INSTANCE (never mutates
    the class).

    Round-trip caveat: graphio's whole-graph YAML export does not yet handle Retag nodes (no
    ``TAIDAL_CALLABLE_PATH``) — it raises ``UnsupportedTopologyError`` like the other canvas-only
    adapters. The Retag node is a canvas convenience; hand-written YAML controls types directly.
    """

    __confluid_display_name__ = "Retag (role / task)"
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
    """Build a new sample record by plucking each part from a different source record.

    Takes three ``SAMPLEFLUX_SAMPLE`` inputs (record dicts) and assembles a fresh record where:

    * the primary-input entry comes from ``input_sample`` (its ``input``/``image``/… key),
    * the target entry comes from ``target_sample`` (its ``target``/``label``/… key),
    * every remaining (metadata-like) entry comes from ``metadata_sample``.

    Use this when three parallel pipeline branches each carry the "right" entry
    and you need to reunite them — e.g. the left branch produced a rendered
    image, the right branch produced detection boxes, and a third carries the
    original provenance metadata.
    """

    __confluid_display_name__ = "Compose Sample (3-way)"
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
        result = _mix_records(input_sample, target_sample, metadata_sample, merge_all=False)
        return (result,)


class MixSamplesNode:
    """Build a new sample record combining entries from three source records, merging all metadata.

    * the primary-input entry comes from ``input_sample`` (its ``input``/``image``/… key),
    * the target entry comes from ``target_sample`` (its ``target``/``label``/… key),
    * the metadata is the shallow union of all three records' NON-primary entries
      (``input_sample`` lowest priority, ``metadata_sample`` highest — later keys win).

    Use this when you want to accumulate metadata produced at different pipeline
    stages rather than discard two records' metadata entirely.
    """

    __confluid_display_name__ = "Mix Samples (merge metadata)"
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
        result = _mix_records(input_sample, target_sample, metadata_sample)
        return (result,)


# ---------------------------------------------------------------------------
# Subgraph → Op extraction
# ---------------------------------------------------------------------------


def compile_subgraph_ops(prompt: Any, unique_id: Any, node_mappings: Optional[Dict[str, Any]] = None) -> List[Any]:
    """Compile the op DAG feeding ``unique_id`` into a flat list of LIVE ``record -> record`` ops.

    The engine behind :class:`SubgraphOpExtractNode`. Given ComfyUI's hidden ``PROMPT`` (the
    flat API graph — native subgraph instances are already expanded into their inner nodes by
    the frontend, so the inner op DAG is present) and the terminator's own ``UNIQUE_ID``, it
    runs the ops-export DAG→sequential compiler (:func:`fluxstudio.export.export_ops_yaml`) in
    **target mode** over the terminator's sample-ancestor cone — exactly the ops that compute
    the record wired into the terminator, fan-out/``Mix`` fan-in flattened with context
    Save/Use — and materializes the resulting op-list into live callables (the same
    ``confluid.load`` → ``materialize`` path :meth:`sampleflux.core.Flux.from_ops_yaml` uses).

    Self-contained by design: no widget snapshot and no compile route are needed because the
    flat prompt already carries the whole inner DAG. Returns ``[]`` for an unwired terminator
    (``Pipeline([])`` is the identity).

    Args:
        prompt: ComfyUI's hidden ``PROMPT`` — the flat ``{node_id: {class_type, inputs}}`` graph.
        unique_id: The terminator node's hidden ``UNIQUE_ID`` (the export target anchor).
        node_mappings: FluxStudio's ``NODE_CLASS_MAPPINGS``; defaults to the live package mapping
            (lazy-imported) so the node works in ComfyUI, while tests can inject their own.
    """
    if not prompt or unique_id is None:
        return []
    import confluid

    from fluxstudio.export import export_ops_yaml

    mappings: Dict[str, Any]
    if node_mappings is not None:
        mappings = node_mappings
    else:  # pragma: no cover - exercised inside ComfyUI, not the unit tests
        from fluxstudio import NODE_CLASS_MAPPINGS

        mappings = NODE_CLASS_MAPPINGS
    # Target mode: the cone of the terminator is precisely the inner pipeline (the terminator
    # itself is a non-op boundary, so it is never scheduled — only its ancestors compile).
    yaml_doc = export_ops_yaml(prompt, mappings, target_ids=[str(unique_id)])
    loaded = confluid.load(yaml_doc)
    raw_ops = loaded.get("ops", []) if isinstance(loaded, dict) else []
    return list(confluid.materialize(raw_ops))


class SubgraphOpExtractNode:
    """Turn an upstream op DAG into a single ``SAMPLEFLUX_OP`` — the "subgraph as an Op" terminator.

    Draw a normal ``record -> record`` op pipeline (an arbitrary DAG — fan-out, ``Mix``
    fan-in allowed) and wire its terminal sample into this node's ``sample`` input. The node
    compiles that whole sub-pipeline into ONE ``sampleflux.transform.Pipeline``
    (DAG→sequential, via the ops-export compiler) and emits it on the ``op`` output
    (``SAMPLEFLUX_OP``), so the transformation is reusable as a single Op: feed it into a
    higher-order op (``Parallel`` / ``Pipeline`` / ``Enable``), a single-op slot
    (``ConfigureOp.target`` / ``RandomApply.op``), or chain it.

    The headline use is INSIDE a ComfyUI native subgraph: build the inner pipeline starting
    from the subgraph's ``SAMPLEFLUX_SAMPLE`` input boundary, drop this node at the end, and
    promote its ``op`` output to the subgraph boundary — the subgraph instance then exposes a
    ``SAMPLEFLUX_OP`` output and acts as an Op when wired into another node (or as a graph when a
    sample is run through the boundary inline). It also works on the flat canvas.

    Adapter glue, NOT a data op (it assembles the real ``Pipeline``), so it lives here
    with Walk/Extract/Mix. Self-contained at runtime: it reads ComfyUI's hidden ``PROMPT`` +
    ``UNIQUE_ID`` and compiles its own ancestor cone (see :func:`compile_subgraph_ops`) — no
    snapshot widget, no compile route.

    The ``sample`` input is **LAZY** (``check_lazy_status`` always returns ``[]``): the node
    compiles STATICALLY from the ``PROMPT`` and never reads the runtime sample VALUE, so it must
    never force ComfyUI to execute the upstream op cone. That matters because the cone is an
    op-only ``record -> record`` transform with NO source on the canvas (the source feeds the
    Flux at runtime, not the op chain) — eager evaluation would run the head op on ``None`` and
    crash a downstream viewer / value-chain ``Extract from Sample``. Lazy gating
    (``comfy_execution/graph.py``: a lazy link is not added as a dependency unless requested)
    leaves the whole upstream unexecuted while the ``PROMPT`` still carries the full DAG to
    compile. Using the subgraph as a *graph* (running a sample through its boundary) is
    unaffected — that pulls the subgraph's SAMPLE output, not this node's ``op`` output.
    """

    __confluid_display_name__ = "Extract as Op"
    CATEGORY = _FLUXSTUDIO_CATEGORY
    RETURN_TYPES = (OP_TYPE,)
    RETURN_NAMES = ("op",)
    FUNCTION = "extract"

    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        return {
            # ``sample`` is LAZY: we compile from PROMPT, never the runtime value, so we must NOT
            # make ComfyUI execute the (sourceless) upstream op cone just to feed this socket.
            # See the class docstring + ``check_lazy_status`` below.
            "optional": {"sample": (SAMPLE_TYPE, {"lazy": True})},
            # PROMPT = the flat API graph (subgraphs already expanded); UNIQUE_ID = this node's
            # id — together they let execute() compile its own upstream cone with no JS/route.
            "hidden": {"prompt": "PROMPT", "unique_id": "UNIQUE_ID"},
        }

    def check_lazy_status(self, sample: Any = None, prompt: Any = None, unique_id: Any = None) -> List[str]:
        # Never request the lazy ``sample``: compilation reads the static PROMPT, so the runtime
        # sample value is never needed and the upstream op cone stays unexecuted.
        return []

    def extract(self, sample: Any = None, prompt: Any = None, unique_id: Any = None) -> Tuple[Any, ...]:
        from sampleflux import Pipeline

        ops = compile_subgraph_ops(prompt, unique_id)
        logger.debug(f"SubgraphOpExtractNode: compiled {len(ops)} op(s) from the upstream DAG into a Pipeline.")
        return (Pipeline(transforms=ops),)


# ---------------------------------------------------------------------------
# YAML serialization helper + node
# ---------------------------------------------------------------------------


def _to_yaml_str(obj: Any) -> str:
    """Serialize ``obj`` to a YAML string.

    Taidal ``@configurable`` instances use ``confluid.dump`` (preserves
    ``!class:`` / ``!ref:`` tags).  Everything else falls through to
    ``yaml.safe_dump``; non-serializable objects fall back to ``repr()``.
    """
    if getattr(type(obj), "__confluid_configurable__", False):
        try:
            from confluid import dump

            return dump(obj).strip()
        except Exception:
            pass
    try:
        import yaml

        return yaml.safe_dump(obj, default_flow_style=False, allow_unicode=True, sort_keys=False).strip()
    except Exception:
        return repr(obj)


class YamlDumpNode:
    """Serialize any Taidal op, source, configurable object, or plain Python value to YAML.

    Wire any object — a SampleFlux op, a source, a trained model, a bounding-box list, a
    metadata dict — into the ``object`` socket and read the YAML text from ``yaml``.

    * Taidal ``@configurable`` objects are dumped with ``confluid.dump`` (round-trippable
      ``!class:Foo(param=value)`` format).
    * Plain Python containers (lists, dicts, scalars) use standard ``yaml.safe_dump``.
    * Anything else falls back to ``repr()``.

    Chain the ``yaml`` output to a **Show Text** node to inspect the object.
    """

    __confluid_display_name__ = "YAML Dump"
    CATEGORY = _FLUXSTUDIO_CATEGORY
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("yaml",)
    FUNCTION = "dump"

    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        return {"required": {"object": (ANY,)}}

    def dump(self, object: Any) -> Tuple[str]:
        return (_to_yaml_str(object),)


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

    __confluid_display_name__ = "Math"
    CATEGORY = _MATH_CATEGORY
    RETURN_TYPES = ("FLOAT", "INT", "BOOLEAN", "STRING")
    RETURN_NAMES = ("float", "int", "bool", "str")
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
    ) -> Tuple[float, int, bool, str]:
        # Unwired optional inputs arrive as None — treat as 0.
        a = 0 if a is None else a
        b = 0 if b is None else b
        c = 0 if c is None else c
        d = 0 if d is None else d
        e = 0 if e is None else e
        result = _eval_formula(formula, a, b, c, d, e)
        return (float(result), int(result), bool(result), str(result))


# ---------------------------------------------------------------------------
# Image grid node
# ---------------------------------------------------------------------------


def _letterbox(img: torch.Tensor, cell_w: int, cell_h: int, bg: float) -> np.ndarray:
    """Scale img to fit inside cell_w×cell_h, centred, padding with bg. Never crops.

    Args:
        img: [H, W, 3] float32 tensor in [0, 1] — first frame of a ComfyUI IMAGE batch.
        cell_w: target cell width in pixels.
        cell_h: target cell height in pixels.
        bg: background grayscale level in [0, 1].

    Returns: [cell_h, cell_w, 3] uint8 ndarray.
    """
    arr = (img.detach().cpu().numpy() * 255).clip(0, 255).astype(np.uint8)
    if arr.ndim == 2:
        arr = np.stack([arr, arr, arr], axis=-1)
    elif arr.shape[2] == 1:
        arr = np.concatenate([arr, arr, arr], axis=-1)
    elif arr.shape[2] == 4:
        arr = arr[:, :, :3]

    H, W = arr.shape[:2]
    bg_val = int(bg * 255)
    cell = np.full((cell_h, cell_w, 3), bg_val, dtype=np.uint8)

    if W == 0 or H == 0:
        return cell

    scale = min(cell_w / W, cell_h / H)
    new_w = max(1, int(W * scale))
    new_h = max(1, int(H * scale))
    pad_left = (cell_w - new_w) // 2
    pad_top = (cell_h - new_h) // 2

    try:
        from PIL import Image as PILImage

        _lanczos = PILImage.LANCZOS  # type: ignore[attr-defined]
        resized = np.array(PILImage.fromarray(arr).resize((new_w, new_h), _lanczos))
    except ImportError:
        # Fallback: torch bilinear when PIL is absent.
        t = torch.from_numpy(arr.transpose(2, 0, 1)[None].astype(np.float32))
        t_resized = torch.nn.functional.interpolate(t, size=(new_h, new_w), mode="bilinear", align_corners=False)
        resized = t_resized[0].permute(1, 2, 0).numpy().clip(0, 255).astype(np.uint8)

    cell[pad_top : pad_top + new_h, pad_left : pad_left + new_w] = resized
    return cell


def _collect_images(kwargs: Dict[str, Any]) -> List[torch.Tensor]:
    """Pull dynamic ``image_0`` / ``image_1`` / … inputs from kwargs, sorted by index, dropping None."""
    indexed = [
        (int(k[6:]), v) for k, v in kwargs.items() if k.startswith("image_") and k[6:].isdigit() and v is not None
    ]
    return [v for _, v in sorted(indexed)]


class ImageGridNode:
    """Arrange a dynamic list of images into a rescaled grid.

    Each image is letterboxed (scaled to fit, padded — never cropped) into a uniform
    cell, then cells are tiled row-by-row. Wire as many ``image_N`` sockets as you
    like; a fresh empty slot appears automatically after each connected one
    (``web/image_list.js``). Feed the ``IMAGE`` output into a **Preview Image** node.
    """

    __confluid_display_name__ = "Image Grid"
    CATEGORY = _FLUXSTUDIO_CATEGORY
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("grid",)
    FUNCTION = "build_grid"

    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        return {
            "required": {
                "columns": ("INT", {"default": 4, "min": 1, "max": 32}),
                "cell_width": ("INT", {"default": 256, "min": 16, "max": 4096}),
                "cell_height": ("INT", {"default": 256, "min": 16, "max": 4096}),
                "padding": ("INT", {"default": 8, "min": 0, "max": 256}),
                "background": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.01}),
            },
            "optional": {
                # Seed slot: image_list.js grows image_1, image_2, … as connections are made.
                "image_0": ("IMAGE",),
            },
        }

    def build_grid(
        self,
        columns: int = 4,
        cell_width: int = 256,
        cell_height: int = 256,
        padding: int = 8,
        background: float = 0.0,
        **kwargs: Any,
    ) -> Tuple[Any, ...]:
        images = _collect_images(kwargs)

        if not images:
            return (torch.zeros(1, 1, 1, 3, dtype=torch.float32),)

        cells = [_letterbox(img[0], cell_width, cell_height, background) for img in images]

        rows = _math_module.ceil(len(cells) / columns)
        grid_w = columns * cell_width + (columns + 1) * padding
        grid_h = rows * cell_height + (rows + 1) * padding

        bg_val = int(background * 255)
        grid = np.full((grid_h, grid_w, 3), bg_val, dtype=np.uint8)

        for i, cell in enumerate(cells):
            col = i % columns
            row = i // columns
            x = padding + col * (cell_width + padding)
            y = padding + row * (cell_height + padding)
            grid[y : y + cell_height, x : x + cell_width] = cell

        return (torch.from_numpy(grid.astype(np.float32) / 255.0)[None],)


# ---------------------------------------------------------------------------
# Display Any + Apply Text Template (utility / inspection nodes)
# ---------------------------------------------------------------------------

# Cap the text a Display Any node ships to the widget — a huge repr (a whole tensor printed) would
# bloat the workflow's run history and the UI.
_MAX_DISPLAY_CHARS = 10_000


def _shape_of(value: Any, _depth: int = 0) -> str:
    """A compact structural summary of any value — shapes of tensors/arrays, recursive for containers.

    Used by :class:`DisplayAnyNode`'s "tensor shape" mode: ``Tensor(shape=…, dtype=…)`` / ``ndarray(…)``
    for arrays, walks dicts / lists / tuples (capped); a sample record is a plain dict, so the dict
    branch already unpacks it key by key. Anything else falls back to a short ``repr``.
    """
    if torch.is_tensor(value):
        return f"Tensor(shape={tuple(value.shape)}, dtype={value.dtype})"
    if isinstance(value, np.ndarray):
        return f"ndarray(shape={value.shape}, dtype={value.dtype})"
    if _depth > 6:
        return f"{type(value).__name__}(…)"
    if isinstance(value, dict):
        inner = ", ".join(f"{k}: {_shape_of(v, _depth + 1)}" for k, v in list(value.items())[:16])
        more = f", … (+{len(value) - 16})" if len(value) > 16 else ""
        return "{" + inner + more + "}"
    if isinstance(value, (list, tuple)):
        items = list(value)
        shown = ", ".join(_shape_of(v, _depth + 1) for v in items[:8])
        more = f", … (+{len(items) - 8})" if len(items) > 8 else ""
        open_, close = ("[", "]") if isinstance(value, list) else ("(", ")")
        return f"{open_}{shown}{more}{close} len={len(items)}"
    text = repr(value)
    return text if len(text) <= 200 else text[:200] + "…"


def _describe_any(value: Any, mode: str = "raw value") -> str:
    """Render any wired value to display text for :class:`DisplayAnyNode` (raw repr, or shape summary)."""
    if mode == "tensor shape":
        return _shape_of(value)
    text = value if isinstance(value, str) else repr(value)
    if len(text) > _MAX_DISPLAY_CHARS:
        text = text[:_MAX_DISPLAY_CHARS] + f"\n… (truncated — {len(text)} chars total)"
    return text


class DisplayAnyNode:
    """Show ANY wired value as text ON the node — a debug / inspection viewer.

    A Taidal-native analogue of WAA's *Display Any*: wire any output (a scalar, a list, a sample
    record dict, a tensor, a SampleFlux op/source) into ``value`` and its text representation
    appears on the node after **Queue Prompt** (``web/display_any.js`` fills a read-only multiline
    widget). ``mode`` picks the view:

    * ``raw value`` — ``repr(value)`` (strings shown verbatim), truncated for huge values.
    * ``tensor shape`` — a structural summary: shapes/dtypes of tensors & arrays, recursively through
      lists / dicts (a sample record included — answers "what shape is this actually?").

    The text is also emitted on the ``text`` STRING output so it can be chained. Works regardless of
    ``--no-default-nodes`` / ``--only-taidal-nodes`` (no native viewer needed).
    """

    __confluid_display_name__ = "Display Any"
    CATEGORY = _FLUXSTUDIO_CATEGORY
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    FUNCTION = "display"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        return {
            "required": {
                "value": (ANY, {}),
                "mode": (["raw value", "tensor shape"], {"default": "raw value"}),
            }
        }

    @classmethod
    def VALIDATE_INPUTS(cls, input_types: Any = None) -> bool:
        # Accept ANY upstream type on the wildcard ``value`` input — ComfyUI otherwise rejects a wire
        # whose type doesn't string-match ``"*"``.
        return True

    def display(self, value: Any = None, mode: str = "raw value") -> Dict[str, Any]:
        text = _describe_any(value, mode)
        logger.debug(f"DisplayAnyNode: mode={mode} len={len(text)}")
        return {"ui": {"text": [text]}, "result": (text,)}


class _SafeFormatDict(dict):
    """A format mapping whose missing keys render as ``""`` (so a stray ``{x}`` never raises)."""

    def __missing__(self, key: str) -> str:
        return ""


class ApplyTextTemplateNode:
    """Fill a text template from up to five inputs — a string formatter (à la mtb's *Apply Text Template*).

    Write a ``template`` with ``{a}`` … ``{e}`` placeholders and wire values into the matching
    ``a`` … ``e`` inputs (any type — each is ``str()``-ified; an unwired input is the empty string).
    Standard Python ``str.format`` spec works (``{a:.2f}`` etc.); an unknown placeholder renders empty
    rather than erroring. Emits the filled string on ``text`` (wire it into *Display Any* / a sink).

    Examples::

        template="{a} dB @ {b} Hz", a=-30, b=2.4e9  ->  "-30 dB @ 2400000000.0 Hz"
        template="class: {a}",      a="drone"        ->  "class: drone"
    """

    __confluid_display_name__ = "Apply Text Template"
    CATEGORY = _FLUXSTUDIO_CATEGORY
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    FUNCTION = "apply"

    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        return {
            "required": {
                "template": ("STRING", {"default": "{a} {b}", "multiline": True}),
            },
            "optional": {name: (ANY, {}) for name in ("a", "b", "c", "d", "e")},
        }

    def apply(
        self,
        template: str = "",
        a: Any = None,
        b: Any = None,
        c: Any = None,
        d: Any = None,
        e: Any = None,
    ) -> Tuple[str]:
        # Pass raw values (None → "") so format specs work: ``{a}`` → str(value), ``{a:.2f}`` → a
        # formatted number. A missing ``{x}`` renders "" via _SafeFormatDict.__missing__.
        mapping = _SafeFormatDict(
            {k: ("" if v is None else v) for k, v in (("a", a), ("b", b), ("c", c), ("d", d), ("e", e))}
        )
        try:
            text = string.Formatter().vformat(template, (), mapping)
        except (ValueError, IndexError, KeyError) as exc:  # malformed spec / positional {0}
            text = f"<template error: {exc}>"
        return (text,)


def parse_value_literal(text: str) -> Any:
    """Parse a Python / JSON literal STRING into a real Python value (the ``Value`` node + YAML import).

    ``ast.literal_eval`` first — it handles tuples / parens / single quotes that JSON can't (a list of
    boxes ``[(20, 10, 80, 50), …]``) — then ``json.loads`` as a fallback; a value that is neither parses
    is returned as the raw (stripped) string. An empty field yields ``None`` (an unset value). Literals
    only (no code execution).
    """
    stripped = (text or "").strip()
    if not stripped:
        return None
    for parse in (ast.literal_eval, json.loads):
        try:
            return parse(stripped)
        except (ValueError, SyntaxError, TypeError):
            continue
    return stripped


class ValueNode:
    """Produce a structured Python VALUE (a list of boxes, a list of lists, a dict) from a literal.

    Many constructor params can't be sensibly typed into a single scalar widget — a list of bounding
    boxes (``[(20, 10, 80, 50), (200, 100, 300, 150)]``), a list of colours, a dict of lists. The bridge
    renders such a param as a wired ``SAMPLEFLUX_VALUE`` input socket (``bridge._needs_value_socket``);
    THIS node is its producer: type a **Python or JSON literal** in ``value`` and it parses it (via
    :func:`parse_value_literal`) into the real object on the ``value`` output, which you wire into the
    param's socket. Reusable — one Value node can feed several consumers.

    Examples (type into ``value``)::

        [(20, 10, 80, 50), (200, 100, 300, 150)]   # a list of boxes   -> OverlayBoxes.boxes
        [(255, 0, 0), (0, 255, 0)]                  # a list of colours -> OverlayBoxes.colors
        {"a": [1, 2], "b": [3, 4]}                  # a dict of lists
    """

    __confluid_display_name__ = "Value"
    CATEGORY = _FLUXSTUDIO_CATEGORY
    RETURN_TYPES = (VALUE_TYPE,)
    RETURN_NAMES = ("value",)
    FUNCTION = "produce"

    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        return {"required": {"value": ("STRING", {"default": "[]", "multiline": True})}}

    def produce(self, value: str = "") -> Tuple[Any]:
        return (parse_value_literal(value),)


# ---------------------------------------------------------------------------
# Draw Text to Image
# ---------------------------------------------------------------------------


def _text_positions() -> List[str]:
    """The 9-grid text-anchor options — the closed ``sampleflux.ops.image.TextPosition`` set.

    Single source of truth (== ``get_args(TextPosition)``); falls back to one option if sampleflux's
    image module can't be imported (degrades like ``_colormap_choices``).
    """
    try:
        from sampleflux.ops.image import TEXT_POSITIONS

        return list(TEXT_POSITIONS)
    except ImportError:  # pragma: no cover - sampleflux is a hard dep; defensive
        return ["top-left"]


class DrawTextNode:
    """Draw text onto an image — or onto a fresh blank canvas — and output a ComfyUI ``IMAGE``.

    Wire a ``text`` string (e.g. from **Apply Text Template** / **Display Any**) and, optionally, an
    ``image`` to draw on; with no image a blank ``width`` × ``height`` canvas of color ``background``
    is created. The text word-wraps to the image width (``wrap``) and is anchored by the 9-grid
    ``position`` with a ``margin`` inset; ``color`` / ``background`` accept any PIL color name or hex.
    Feed the ``IMAGE`` output into *Preview Image*, the *Array / Tensor Histogram*, or a save node.

    Adapter glue (bridges ComfyUI's ``STRING`` + ``IMAGE`` to an ``IMAGE``, like *Image Grid*): the
    text-drawing MATH lives in ``sampleflux.ops.image.draw_text`` — this node only converts ComfyUI's
    ``[B,H,W,C]`` float image to/from the ``(H,W,3)`` uint8 array that function works in.
    """

    __confluid_display_name__ = "Draw Text to Image"
    CATEGORY = _FLUXSTUDIO_CATEGORY
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "draw"

    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        return {
            "required": {
                "text": ("STRING", {"default": "Hello", "multiline": True}),
                "font_size": ("INT", {"default": 24, "min": 1, "max": 512}),
                "color": ("STRING", {"default": "white"}),
                "position": (_text_positions(), {"default": "top-left"}),
                "wrap": ("BOOLEAN", {"default": True}),
                "margin": ("INT", {"default": 8, "min": 0, "max": 1024}),
                # Used only when no ``image`` is wired (a fresh blank canvas to draw on):
                "background": ("STRING", {"default": "black"}),
                "width": ("INT", {"default": 512, "min": 1, "max": 8192}),
                "height": ("INT", {"default": 256, "min": 1, "max": 8192}),
            },
            "optional": {"image": ("IMAGE",)},
        }

    def draw(
        self,
        text: str = "",
        font_size: int = 24,
        color: str = "white",
        position: "TextPosition" = "top-left",
        wrap: bool = True,
        margin: int = 8,
        background: str = "black",
        width: int = 512,
        height: int = 256,
        image: Any = None,
    ) -> Tuple[Any, ...]:
        from sampleflux.ops.image import draw_text

        bg: Optional[np.ndarray] = None
        if image is not None:
            # ComfyUI IMAGE: [B, H, W, C] float in [0, 1] -> frame 0 as a uint8 RGB array.
            frame = image[0] if torch.is_tensor(image) and image.dim() == 4 else image
            arr = frame.detach().cpu().numpy() if torch.is_tensor(frame) else np.asarray(frame)
            bg = (np.clip(arr, 0.0, 1.0) * 255.0).astype(np.uint8)
        rgb = draw_text(
            text,
            bg,
            width=width,
            height=height,
            font_size=font_size,
            color=color,
            background=background,
            position=position,
            margin=margin,
            wrap=wrap,
        )
        return (torch.from_numpy(rgb.astype(np.float32) / 255.0)[None,],)


# Statically-defined adapter nodes, merged into the auto-discovered set by fluxstudio/__init__.py.
# Keys are stable workflow identifiers — do not rename them.  Display names come from each class's
# ``__confluid_display_name__`` attribute (set by ``@configurable(display_name=...)`` or directly).
NODE_CLASS_MAPPINGS: Dict[str, Type] = {
    "Taidal_WalkDataset": WalkDatasetNode,
    "Taidal_ExtractSample": SampleExtractorNode,
    "Taidal_ExtractMetadata": ExtractMetadataNode,
    "Taidal_ArrayHistogram": ArrayHistogramViewerNode,
    "Taidal_ConfusionMatrix": ConfusionMatrixViewerNode,
    "Taidal_DisplayAny": DisplayAnyNode,
    "Taidal_ApplyTextTemplate": ApplyTextTemplateNode,
    "Taidal_DrawText": DrawTextNode,
    "Taidal_Retag": RetagObjectNode,
    "Taidal_ComposeSample": ComposeSampleNode,
    "Taidal_MixSamples": MixSamplesNode,
    "Taidal_SubgraphOp": SubgraphOpExtractNode,
    "Taidal_YamlDump": YamlDumpNode,
    "Taidal_Math": MathNode,
    "Taidal_ImageGrid": ImageGridNode,
    "Taidal_Value": ValueNode,
}

NODE_DISPLAY_NAME_MAPPINGS: Dict[str, str] = {
    k: getattr(cls, "__confluid_display_name__", cls.__name__) for k, cls in NODE_CLASS_MAPPINGS.items()
}
