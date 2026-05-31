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

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Type

import numpy as np
import torch
from logflow import get_logger

from fluxstudio.bridge import SAMPLE_TYPE, SOURCE_TYPE

if TYPE_CHECKING:
    from waivefront.visualizers import Colormap

logger = get_logger(__name__)


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

    CATEGORY = "Taidal/FluxStudio"
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
                    {"default": 0, "min": 0, "max": _MAX_INDEX, "control_after_generate": _INDEX_CONTROL_DEFAULT},
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

    CATEGORY = "Taidal/FluxStudio"
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
        self, sample: Any, field: str = "input", colormap: "Colormap" = "viridis", max_size: int = 512
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


# Statically-defined adapter nodes, merged into the auto-discovered set by fluxstudio/__init__.py.
NODE_CLASS_MAPPINGS: Dict[str, Type] = {
    "Taidal_WalkDataset": WalkDatasetNode,
    "Taidal_ExtractSample": SampleExtractorNode,
}

NODE_DISPLAY_NAME_MAPPINGS: Dict[str, str] = {
    "Taidal_WalkDataset": "Walk Dataset (by index)",
    "Taidal_ExtractSample": "Extract from Sample",
}
