"""Type → visualizer dispatch registry for FluxStudio nodes.

Waivefront deliberately has no type-registry — composition is explicit in YAML.
FluxStudio needs the opposite: given an arbitrary value flowing through a node,
pick a renderer automatically. The default registry covers numpy / PIL / torch /
scalars; users can override per-node via an explicit `type_hint` string.

Reuses `sample_codec.encode_value` rather than re-implementing rendering — the
codec already knows how to produce JSON-friendly payloads with previews.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from fluxstudio.sample_codec import encode_value
from logflow import get_logger

logger = get_logger(__name__)

Encoder = Callable[[Any], Dict[str, Any]]

_HINT_REGISTRY: Dict[str, Encoder] = {}
_TYPE_REGISTRY: Dict[type, Encoder] = {}


def register(key: Any, encoder: Encoder) -> None:
    """Register an encoder under a type or a string hint."""
    if isinstance(key, str):
        _HINT_REGISTRY[key] = encoder
    elif isinstance(key, type):
        _TYPE_REGISTRY[key] = encoder
    else:
        raise TypeError(f"register key must be str or type, got {type(key).__name__}")


def unregister(key: Any) -> None:
    """Remove a previously registered encoder. Silent no-op if not registered."""
    if isinstance(key, str):
        _HINT_REGISTRY.pop(key, None)
    elif isinstance(key, type):
        _TYPE_REGISTRY.pop(key, None)


def list_encoders() -> Dict[str, list[str]]:
    """Public catalog: visible to the frontend via /api/visualizers."""
    return {
        "hints": sorted(_HINT_REGISTRY.keys()),
        "types": sorted(f"{t.__module__}.{t.__qualname__}" for t in _TYPE_REGISTRY),
    }


def dispatch(value: Any, *, type_hint: Optional[str] = None) -> Dict[str, Any]:
    """Resolve a renderer in priority order: hint → MRO → default codec."""
    if type_hint and type_hint in _HINT_REGISTRY:
        return _HINT_REGISTRY[type_hint](value)
    for cls in type(value).__mro__:
        encoder = _TYPE_REGISTRY.get(cls)
        if encoder is not None:
            return encoder(value)
    return encode_value(value)


def _install_defaults() -> None:
    """Built-in encoders. All delegate to `sample_codec` to avoid duplication."""
    register("scalar", encode_value)
    register("ndarray", encode_value)
    register("image", encode_value)
    register("tensor", encode_value)
    register("opaque", encode_value)


def _install_waivefront_optional() -> None:
    """If waivefront is installed, register a `spectrogram` hint that renders an ndarray
    spectrogram via waivefront.visualizers.RenderSpectrogramImageOp before encoding.

    Pure delegation — no waivefront logic is duplicated here.
    """
    try:
        from dataflux.sample import Sample
        from waivefront.visualizers import RenderSpectrogramImageOp
    except ImportError:
        return  # waivefront extra not installed; silently skip

    def encode_spectrogram(value: Any) -> Dict[str, Any]:
        op = RenderSpectrogramImageOp(
            image_width_px=512, image_height_px=128, draw_overlays=False
        )
        rendered = op(Sample(input=value, target=None, metadata={}))
        return encode_value(rendered.input)

    register("spectrogram", encode_spectrogram)


_install_defaults()
_install_waivefront_optional()
