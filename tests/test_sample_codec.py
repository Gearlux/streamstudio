"""Round-trip tests for `fluxstudio.sample_codec`."""

from __future__ import annotations

import base64
import json
from io import BytesIO

import numpy as np
import pytest
from PIL import Image

from fluxstudio.sample_codec import encode_sample, encode_value


def _ensure_json_safe(payload: dict) -> None:
    """The whole point of the codec is that this never raises."""
    json.dumps(payload)


def test_scalar_encoding() -> None:
    for v in (1, 1.5, True, "hello"):
        out = encode_value(v)
        assert out["kind"] == "scalar"
        assert out["value"] == v
        _ensure_json_safe(out)


def test_none_encoding() -> None:
    out = encode_value(None)
    assert out == {"kind": "null"}


def test_ndarray_encoding_2d_includes_preview() -> None:
    arr = np.linspace(0.0, 1.0, num=64, dtype=np.float32).reshape(8, 8)
    out = encode_value(arr)
    assert out["kind"] == "ndarray"
    assert out["shape"] == [8, 8]
    assert out["dtype"] == "float32"
    assert "preview" in out
    assert out["preview"]["mime"] == "image/png"
    assert out["stats"]["min"] == pytest.approx(0.0)
    assert out["stats"]["max"] == pytest.approx(1.0)
    _ensure_json_safe(out)


def test_ndarray_encoding_1d_no_preview() -> None:
    arr = np.arange(10, dtype=np.float32)
    out = encode_value(arr)
    assert out["kind"] == "ndarray"
    assert out["shape"] == [10]
    assert "preview" not in out
    _ensure_json_safe(out)


def test_ndarray_empty_array_safe() -> None:
    arr = np.array([], dtype=np.float32)
    out = encode_value(arr)
    assert out["kind"] == "ndarray"
    assert out["shape"] == [0]
    assert out["stats"]["min"] == 0.0


def test_ndarray_non_numeric_dtype_no_stats() -> None:
    arr = np.array(["a", "bb", "ccc"], dtype=object)
    out = encode_value(arr)
    assert out["kind"] == "ndarray"
    assert out["stats"] == {}


def test_ndarray_with_nan_finite_only_stats() -> None:
    arr = np.array([1.0, np.nan, 3.0, np.inf], dtype=np.float32)
    out = encode_value(arr)
    assert out["stats"]["min"] == pytest.approx(1.0)
    assert out["stats"]["max"] == pytest.approx(3.0)


def test_pil_image_round_trip() -> None:
    img = Image.new("RGB", (32, 16), color=(128, 64, 32))
    out = encode_value(img)
    assert out["kind"] == "image"
    assert out["mime"] == "image/png"
    assert out["width"] == 32
    assert out["height"] == 16
    decoded = Image.open(BytesIO(base64.b64decode(out["data"])))
    assert decoded.size == (32, 16)
    _ensure_json_safe(out)


def test_torch_tensor_encoding() -> None:
    torch = pytest.importorskip("torch")
    t = torch.linspace(0.0, 1.0, steps=64).reshape(8, 8)
    out = encode_value(t)
    assert out["kind"] == "tensor"
    assert out["shape"] == [8, 8]
    assert "preview" in out
    _ensure_json_safe(out)


def test_torch_complex_dtype_stats_empty_but_safe() -> None:
    torch = pytest.importorskip("torch")
    t = torch.zeros(3, dtype=torch.complex64)
    out = encode_value(t)
    assert out["kind"] == "tensor"
    assert out["stats"] == {}
    _ensure_json_safe(out)


def test_sequence_payload() -> None:
    out = encode_value([1, 2, 3])
    assert out == {"kind": "sequence", "length": 3, "type": "list"}


def test_mapping_payload() -> None:
    out = encode_value({"a": 1, "b": 2})
    assert out["kind"] == "mapping"
    assert sorted(out["keys"]) == ["a", "b"]


def test_opaque_fallback_truncates_long_repr() -> None:
    class Foo:
        def __repr__(self) -> str:
            return "x" * 1000

    out = encode_value(Foo())
    assert out["kind"] == "opaque"
    assert out["repr"].endswith("...")


def test_encode_sample_packages_all_three_fields() -> None:
    from dataflux.sample import Sample

    arr = np.zeros((4, 4), dtype=np.float32)
    s = Sample(input=arr, target=42, metadata={"key": "value", "count": 7})
    out = encode_sample(s)
    assert out["input"]["kind"] == "ndarray"
    assert out["target"]["kind"] == "scalar"
    assert out["metadata"] == {"key": "value", "count": 7}
    _ensure_json_safe(out)


def test_encode_sample_handles_non_dict_metadata() -> None:
    class SyntheticSample:
        input = 1
        target = 2
        metadata = "not a dict"

    out = encode_sample(SyntheticSample())
    assert "_repr" in out["metadata"]


def test_encode_sample_sanitises_metadata_complex_values() -> None:
    from dataflux.sample import Sample

    s = Sample(
        input=None,
        target=None,
        metadata={
            "list": [1, 2, 3],
            "dict": {"a": 1},
            "ndarray": np.zeros(3),
            "obj": object(),
        },
    )
    meta = encode_sample(s)["metadata"]
    assert meta["list"]["kind"] == "sequence"
    assert meta["dict"]["kind"] == "mapping"
    assert meta["ndarray"]["kind"] == "ndarray"
    assert isinstance(meta["obj"], str)
