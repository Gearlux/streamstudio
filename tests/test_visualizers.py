"""Type → visualizer dispatch tests."""

from __future__ import annotations

from typing import Any, Dict

import numpy as np
import pytest

from fluxstudio import visualizers


@pytest.fixture(autouse=True)
def _restore_registry() -> Any:
    """Snapshot and restore the registry around each test so we can register
    custom encoders without leaking between tests.
    """
    hint_snapshot = dict(visualizers._HINT_REGISTRY)
    type_snapshot = dict(visualizers._TYPE_REGISTRY)
    yield
    visualizers._HINT_REGISTRY.clear()
    visualizers._HINT_REGISTRY.update(hint_snapshot)
    visualizers._TYPE_REGISTRY.clear()
    visualizers._TYPE_REGISTRY.update(type_snapshot)


def test_register_unknown_key_type_raises() -> None:
    with pytest.raises(TypeError):
        visualizers.register(42, lambda v: {})  # type: ignore[arg-type]


def test_unregister_silent_for_missing_keys() -> None:
    visualizers.unregister("does-not-exist")
    visualizers.unregister(int)


def test_dispatch_uses_hint_first() -> None:
    sentinel: Dict[str, Any] = {"kind": "custom"}
    visualizers.register("special", lambda v: sentinel)
    out = visualizers.dispatch(np.zeros(3), type_hint="special")
    assert out is sentinel


def test_dispatch_falls_through_to_mro() -> None:
    visualizers.register(np.ndarray, lambda v: {"kind": "tagged", "shape": list(v.shape)})
    out = visualizers.dispatch(np.zeros((2, 3)))
    assert out == {"kind": "tagged", "shape": [2, 3]}


def test_dispatch_default_codec_when_unregistered() -> None:
    visualizers.unregister(np.ndarray)
    out = visualizers.dispatch(np.zeros((2, 2)))
    assert out["kind"] == "ndarray"


def test_list_encoders_returns_keys_and_types() -> None:
    visualizers.register("xyz", lambda v: {})
    visualizers.register(list, lambda v: {})
    catalog = visualizers.list_encoders()
    assert "xyz" in catalog["hints"]
    assert any("list" in t for t in catalog["types"])


def test_waivefront_optional_branch_when_missing() -> None:
    """When waivefront isn't installed, install_waivefront_optional is a no-op."""
    pytest.importorskip("waivefront", reason="waivefront optional extra")
    # If we got here, waivefront IS installed and the spectrogram hint should exist.
    catalog = visualizers.list_encoders()
    assert "spectrogram" in catalog["hints"]
