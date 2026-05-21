"""JSON-safe encoding of DataFlux Samples for the FluxStudio wire protocol.

The frontend cannot consume raw `numpy.ndarray`, `torch.Tensor`, or
`PIL.Image.Image` over HTTP/WebSocket. `encode_sample` produces a small,
self-describing dict per Sample field. Each payload carries a `kind` tag
the frontend uses to pick the right viewer.
"""

from __future__ import annotations

import base64
import io
from typing import Any, Dict, Optional

import numpy as np

from logflow import get_logger

logger = get_logger(__name__)

_PREVIEW_MAX_DIM = 256
_REPR_MAX_LEN = 240


def _encode_pil(image: Any) -> Dict[str, Any]:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return {
        "kind": "image",
        "mime": "image/png",
        "width": image.width,
        "height": image.height,
        "data": base64.b64encode(buf.getvalue()).decode("ascii"),
    }


def _ndarray_preview_png(arr: np.ndarray) -> Optional[str]:
    """Render a 2D ndarray as a small grayscale PNG. Returns None if not 2D."""
    if arr.ndim != 2:
        return None
    try:
        from PIL import Image
    except ImportError:
        return None  # pragma: no cover

    finite = np.where(np.isfinite(arr), arr, 0.0)
    lo = float(finite.min()) if finite.size else 0.0
    hi = float(finite.max()) if finite.size else 1.0
    if hi <= lo:
        norm = np.zeros_like(finite, dtype=np.uint8)
    else:
        norm = ((finite - lo) / (hi - lo) * 255.0).clip(0, 255).astype(np.uint8)
    img = Image.fromarray(norm, mode="L")
    img.thumbnail((_PREVIEW_MAX_DIM, _PREVIEW_MAX_DIM))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _ndarray_stats(arr: np.ndarray) -> Dict[str, float]:
    if arr.size == 0:
        return {"min": 0.0, "max": 0.0, "mean": 0.0}
    if not np.issubdtype(arr.dtype, np.number):
        return {}
    if np.issubdtype(arr.dtype, np.complexfloating):
        # min/max are not well-defined for complex; silently dropping the
        # imaginary part would mislead the UI. Skip stats instead.
        return {}
    finite = arr[np.isfinite(arr)] if np.issubdtype(arr.dtype, np.floating) else arr
    if finite.size == 0:
        return {"min": 0.0, "max": 0.0, "mean": 0.0}
    return {
        "min": float(finite.min()),
        "max": float(finite.max()),
        "mean": float(finite.mean()),
    }


def _encode_ndarray(arr: np.ndarray) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "kind": "ndarray",
        "shape": list(arr.shape),
        "dtype": str(arr.dtype),
        "stats": _ndarray_stats(arr),
    }
    preview = _ndarray_preview_png(arr)
    if preview is not None:
        payload["preview"] = {"mime": "image/png", "data": preview}
    return payload


def _encode_torch(tensor: Any) -> Dict[str, Any]:
    detached = tensor.detach().cpu()
    payload: Dict[str, Any] = {
        "kind": "tensor",
        "shape": list(detached.shape),
        "dtype": str(detached.dtype),
    }
    try:
        arr = detached.numpy()
        payload["stats"] = _ndarray_stats(arr)
        preview = _ndarray_preview_png(arr) if arr.ndim == 2 else None
        if preview is not None:
            payload["preview"] = {"mime": "image/png", "data": preview}
    except (TypeError, RuntimeError):
        # Complex / bfloat16 / other dtypes that don't roundtrip through numpy.
        payload["stats"] = {}
    return payload


def _is_pil_image(value: Any) -> bool:
    try:
        from PIL.Image import Image as PILImage
    except ImportError:  # pragma: no cover
        return False
    return isinstance(value, PILImage)


def _is_torch_tensor(value: Any) -> bool:
    try:
        import torch
    except ImportError:  # pragma: no cover
        return False
    return isinstance(value, torch.Tensor)


def encode_value(value: Any) -> Dict[str, Any]:
    """Encode a single value (sample.input / sample.target) into a wire payload."""
    if value is None:
        return {"kind": "null"}
    if isinstance(value, (int, float, bool, str)):
        return {"kind": "scalar", "value": value}
    if isinstance(value, np.ndarray):
        return _encode_ndarray(value)
    if _is_pil_image(value):
        return _encode_pil(value)
    if _is_torch_tensor(value):
        return _encode_torch(value)
    if isinstance(value, (list, tuple)):
        return {"kind": "sequence", "length": len(value), "type": type(value).__name__}
    if isinstance(value, dict):
        return {"kind": "mapping", "keys": [str(k) for k in list(value.keys())[:32]]}
    text = repr(value)
    if len(text) > _REPR_MAX_LEN:
        text = text[:_REPR_MAX_LEN] + "..."
    return {"kind": "opaque", "type": type(value).__name__, "repr": text}


def _safe_metadata(metadata: Any) -> Dict[str, Any]:
    """Coerce metadata into a JSON-friendly dict; never let a bad value blow up the wire."""
    if not isinstance(metadata, dict):
        return {"_repr": repr(metadata)[:_REPR_MAX_LEN]}
    out: Dict[str, Any] = {}
    for key, value in metadata.items():
        skey = str(key)
        if isinstance(value, (str, int, float, bool, type(None))):
            out[skey] = value
        elif isinstance(value, (list, tuple)):
            out[skey] = {"kind": "sequence", "length": len(value)}
        elif isinstance(value, dict):
            out[skey] = {
                "kind": "mapping",
                "keys": [str(k) for k in list(value.keys())[:16]],
            }
        elif isinstance(value, np.ndarray):
            out[skey] = {
                "kind": "ndarray",
                "shape": list(value.shape),
                "dtype": str(value.dtype),
            }
        else:
            out[skey] = repr(value)[:_REPR_MAX_LEN]
    return out


def encode_sample(sample: Any) -> Dict[str, Any]:
    """Encode a DataFlux Sample (or anything with input/target/metadata) for the wire."""
    return {
        "input": encode_value(getattr(sample, "input", None)),
        "target": encode_value(getattr(sample, "target", None)),
        "metadata": _safe_metadata(getattr(sample, "metadata", {})),
    }
