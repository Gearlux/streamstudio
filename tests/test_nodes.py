"""Tests for ``fluxstudio.nodes`` — the WalkDataset stepper and SampleExtractor adapter nodes.

These are ComfyUI adapter glue, driven headlessly. ``SampleExtractorNode`` unpacks a Sample's
``input`` / ``target`` onto typed ComfyUI sockets (IMAGE / MASK / STRING / INT / FLOAT) so it feeds
NATIVE ComfyUI viewers; it is NOT an output node. Image rendering is delegated to
``waivefront.visualizers.value_to_image`` (installed in the dev venv); one test forces that import to
fail to exercise the graceful-degradation placeholder.
"""

import json
import sys
import threading
import time
from types import ModuleType
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest
import torch
from dataflux.ops.torch import RescaleOp
from dataflux.ops.transform_chain import TransformChain
from dataflux.sample import Sample

from fluxstudio.bridge import ARRAY_TYPE, OP_TYPE, SAMPLE_TYPE, create_comfy_node
from fluxstudio.nodes import (
    _RETAG_KEEP,
    _RETAG_NONE,
    ANY,
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
    ApplyTextTemplateNode,
    ArrayHistogramViewerNode,
    ComposeSampleNode,
    ConfusionMatrixViewerNode,
    DisplayAnyNode,
    DrawTextNode,
    ExtractMetadataNode,
    ImageGridNode,
    MixSamplesNode,
    RetagObjectNode,
    SampleExtractorNode,
    SubgraphOpExtractNode,
    ValueNode,
    WalkDatasetNode,
    YamlDumpNode,
    _build_confusion_payloads,
    _build_histogram_payload,
    _collect_images,
    _colormap_choices,
    _describe_any,
    _letterbox,
    _parse_class_names,
    _text_positions,
    _to_yaml_str,
    compile_subgraph_ops,
)


class _ImgSource:
    """Indexable source of small RGB image Samples."""

    def __init__(self, n: int = 4) -> None:
        self.n = n

    def __len__(self) -> int:
        return self.n

    def __getitem__(self, index: int) -> Sample:
        arr = (np.ones((8, 8, 3)) * (index * 40)).astype("uint8")
        return Sample(input=arr, target=index, metadata={"idx": index})


# --------------------------------------------------------------------------- #
# WalkDatasetNode
# --------------------------------------------------------------------------- #


def test_walk_index_widget_has_control_after_generate() -> None:
    idx = WalkDatasetNode.INPUT_TYPES()["required"]["index"]
    assert idx[0] == "INT"
    # A *string* default makes ComfyUI's control combo open on that mode; "increment" steps through
    # the dataset one sample per Queue Prompt instead of randomizing (the bare-``True`` fallback).
    assert idx[1]["control_after_generate"] == "increment"


def test_walk_returns_sample_at_index() -> None:
    (sample,) = WalkDatasetNode().walk(source=_ImgSource(4), index=2, wrap=True)
    assert isinstance(sample, Sample)
    assert sample.target == 2


def test_walk_wraps_modulo_length() -> None:
    (sample,) = WalkDatasetNode().walk(source=_ImgSource(4), index=5, wrap=True)
    assert sample.target == 1  # 5 % 4


def test_walk_no_wrap_out_of_range_raises() -> None:
    with pytest.raises(IndexError, match="out of range"):
        WalkDatasetNode().walk(source=_ImgSource(3), index=9, wrap=False)


def test_walk_no_wrap_in_range_ok() -> None:
    (sample,) = WalkDatasetNode().walk(source=_ImgSource(3), index=2, wrap=False)
    assert sample.target == 2


def test_walk_empty_source_raises() -> None:
    with pytest.raises(RuntimeError, match="empty"):
        WalkDatasetNode().walk(source=_ImgSource(0), index=0, wrap=True)


def test_walk_accepts_existing_flux() -> None:
    from dataflux.core import Flux

    flux = Flux.from_source(_ImgSource(2))
    (sample,) = WalkDatasetNode().walk(source=flux, index=1, wrap=True)
    assert sample.target == 1


def test_walk_step_widget_config() -> None:
    # The step widget is an INT, default 1, min 1.
    # The actual increment is applied by web/walk_dataset_step.js (sets index.options.step2);
    # Python's walk() receives the already-advanced index and uses it directly.
    types = WalkDatasetNode.INPUT_TYPES()
    step_widget = types["required"]["step"]
    assert step_widget[0] == "INT"
    assert step_widget[1]["default"] == 1
    assert step_widget[1]["min"] == 1


def test_walk_step_python_uses_index_directly() -> None:
    # Python ignores step and resolves the index as-is (the JS side advances the index).
    # So walk(index=15, step=10) returns sample 15, not sample 150.
    (sample,) = WalkDatasetNode().walk(source=_ImgSource(100), index=15, step=10, wrap=True)
    assert sample.target == 15


def test_walk_step_param_does_not_affect_wrap() -> None:
    # wrap is still applied to index directly, regardless of step.
    (sample,) = WalkDatasetNode().walk(source=_ImgSource(10), index=13, step=5, wrap=True)
    assert sample.target == 3  # 13 % 10


def test_walk_step_param_accepted_but_index_governs_out_of_range() -> None:
    with pytest.raises(IndexError, match="out of range"):
        WalkDatasetNode().walk(source=_ImgSource(5), index=10, step=3, wrap=False)


# --------------------------------------------------------------------------- #
# SampleExtractorNode
# --------------------------------------------------------------------------- #


def test_extractor_input_types_and_outputs() -> None:
    required = SampleExtractorNode.INPUT_TYPES()["required"]
    assert required["sample"] == ("DATAFLUX_SAMPLE",)
    assert required["field"][0] == ["input", "target"]
    # colormap is a combo (enumeration), not free text — its options are the closed waivefront
    # ``Colormap`` set, and the default ("viridis") is one of them.
    colormap_options = required["colormap"][0]
    assert isinstance(colormap_options, list)
    assert "viridis" in colormap_options and "gray" in colormap_options
    assert required["colormap"][1]["default"] == "viridis"
    assert required["max_size"][0] == "INT"
    assert SampleExtractorNode.RETURN_TYPES == (
        "IMAGE",
        "MASK",
        "STRING",
        "INT",
        "FLOAT",
        "DATAFLUX_ARRAY",
        "DATAFLUX_VALUE",
    )
    assert SampleExtractorNode.RETURN_NAMES == ("image", "mask", "text", "int", "float", "array", "value")
    # NOT an output node — it feeds native ComfyUI viewers.
    assert getattr(SampleExtractorNode, "OUTPUT_NODE", False) is False


def test_colormap_choices_are_the_waivefront_literal_set() -> None:
    # Single source of truth: the dropdown options ARE waivefront's closed Colormap set (get_args).
    from waivefront.visualizers import COLORMAPS

    assert _colormap_choices() == list(COLORMAPS)
    assert "viridis" in _colormap_choices()


def test_colormap_choices_fall_back_when_waivefront_absent() -> None:
    # With the [waivefront] extra missing, the node still registers with a single safe option
    # (the IMAGE output is a placeholder then anyway).
    with patch.dict(sys.modules, {"waivefront.visualizers": None}):
        assert _colormap_choices() == ["viridis"]


def test_extract_input_image_yields_comfy_image() -> None:
    sample = Sample(input=np.zeros((8, 8, 3), dtype="uint8"), target=3, metadata={})
    image, mask, text, ival, fval, arr, value = SampleExtractorNode().extract(sample, field="input")
    # ComfyUI IMAGE: [B, H, W, 3] float32 in [0, 1].
    assert isinstance(image, torch.Tensor)
    assert image.shape[0] == 1 and image.shape[-1] == 3 and image.dtype == torch.float32
    assert 0.0 <= float(image.min()) and float(image.max()) <= 1.0
    # A non-2-D, non-scalar field -> empty mask + zero numbers.
    assert tuple(mask.shape) == (1, 1, 1)
    assert ival == 0 and fval == 0.0
    assert isinstance(text, str)
    # DATAFLUX_ARRAY output passes the raw ndarray field through UNCHANGED (not normalized/colormapped).
    assert isinstance(arr, np.ndarray) and arr.shape == (8, 8, 3)
    assert arr is sample.input
    # DATAFLUX_VALUE output is the raw field VERBATIM (same object).
    assert value is sample.input


def test_extract_scalar_target_with_label_name() -> None:
    sample = Sample(input=np.zeros((4, 4, 3), dtype="uint8"), target=7, metadata={"label_name": "drone_x"})
    _image, _mask, text, ival, fval, _arr, _value = SampleExtractorNode().extract(sample, field="target")
    assert text == "drone_x (id=7)"
    assert ival == 7 and fval == 7.0


def test_extract_scalar_target_without_label_name() -> None:
    sample = Sample(input=np.zeros((4, 4, 3), dtype="uint8"), target=2, metadata={})
    _i, _m, text, ival, _f, _arr, _value = SampleExtractorNode().extract(sample, field="target")
    assert text == "2"
    assert ival == 2


def test_extract_2d_target_yields_comfy_mask() -> None:
    sample = Sample(input=np.zeros((4, 4, 3), dtype="uint8"), target=np.eye(6, dtype=bool), metadata={})
    _image, mask, _text, _i, _f, _arr, _value = SampleExtractorNode().extract(sample, field="target")
    # ComfyUI MASK: [B, H, W] float32 in [0, 1].
    assert tuple(mask.shape) == (1, 6, 6) and mask.dtype == torch.float32
    assert 0.0 <= float(mask.min()) and float(mask.max()) <= 1.0


def test_extract_torch_scalar_target_is_coerced() -> None:
    sample = Sample(input=np.zeros((4, 4, 3), dtype="uint8"), target=torch.tensor(5, dtype=torch.int64))
    _i, _m, text, ival, fval, _arr, _value = SampleExtractorNode().extract(sample, field="target")
    assert ival == 5 and fval == 5.0 and text == "5"


def test_extract_text_summary_is_compact_for_arrays() -> None:
    sample = Sample(input=torch.rand(3, 8, 8), target=0)
    _i, _m, text, _iv, _fv, arr, _value = SampleExtractorNode().extract(sample, field="input")
    # Compact summary, not the raw ArrayType dataclass repr.
    assert "ArrayType(" not in text
    assert "shape=(3, 8, 8)" in text and "float32" in text
    # A tensor field passes through the array socket UNCHANGED (still a tensor).
    assert torch.is_tensor(arr) and tuple(arr.shape) == (3, 8, 8)


def test_extract_image_degrades_without_waivefront() -> None:
    # Force `from waivefront.visualizers import value_to_image` to fail -> 1x1 placeholder image,
    # while the text/number outputs still work.
    empty = ModuleType("waivefront.visualizers")  # lacks value_to_image
    sample = Sample(input=np.zeros((4, 4, 3), dtype="uint8"), target=9, metadata={"label_name": "x"})
    with patch.dict(sys.modules, {"waivefront.visualizers": empty}):
        image, _mask, text, ival, _f, _arr, _value = SampleExtractorNode().extract(sample, field="target")
    assert tuple(image.shape) == (1, 1, 1, 3)
    assert text == "x (id=9)" and ival == 9


def test_extract_bool_target_coerces_to_int() -> None:
    sample = Sample(input=np.zeros((4, 4, 3), dtype="uint8"), target=True)
    _i, _m, text, ival, fval, _arr, _value = SampleExtractorNode().extract(sample, field="target")
    assert ival == 1 and fval == 1.0 and text == "1"


def test_extract_numpy_scalar_target() -> None:
    sample = Sample(input=np.zeros((4, 4, 3), dtype="uint8"), target=np.int64(5))
    _i, _m, text, ival, _f, _arr, _value = SampleExtractorNode().extract(sample, field="target")
    assert ival == 5 and text == "5"


def test_extract_pil_target_yields_mask() -> None:
    from PIL import Image

    sample = Sample(input=np.zeros((4, 4, 3), dtype="uint8"), target=Image.new("L", (6, 6)))
    _image, mask, _text, _i, _f, arr, _value = SampleExtractorNode().extract(sample, field="target")
    assert tuple(mask.shape) == (1, 6, 6)
    # A PIL field is converted to an ndarray on the array socket (native mode: "L" -> 2-D).
    assert isinstance(arr, np.ndarray) and arr.shape == (6, 6)


def test_extract_string_field_passthrough() -> None:
    sample = Sample(input="hello world", target=0)
    _i, _m, text, _iv, _fv, _arr, _value = SampleExtractorNode().extract(sample, field="input")
    assert text == "hello world"


def test_extract_text_summary_for_pythontype_and_anytype() -> None:
    # list target -> full YAML content (not just "list"); None target -> AnyType fallback.
    list_text = SampleExtractorNode().extract(Sample(input=0, target=[1, 2, 3]), field="target")[2]
    # Full content must be present — each element appears in the YAML.
    assert "1" in list_text and "3" in list_text
    assert list_text != "list"  # old truncated summary is gone
    none_text = SampleExtractorNode().extract(Sample(input=0, target=None), field="target")[2]
    assert none_text == "AnyType"


def test_extract_none_sample_raises_legible_error() -> None:
    # A None sample (upstream op produced nothing) must give an actionable message, not a bare
    # ``AttributeError: 'NoneType' object has no attribute 'input'``.
    with pytest.raises(ValueError, match="empty sample"):
        SampleExtractorNode().extract(None, field="input")


def test_extract_sample_value_output_is_the_raw_list_field() -> None:
    # A list-valued field (a list of boxes) comes out VERBATIM on the DATAFLUX_VALUE ``value`` output,
    # so it wires straight into a nested-container param socket (OverlayBoxes.boxes).
    boxes = [(20, 10, 80, 50), (200, 100, 300, 150)]
    sample = Sample(input=boxes, target=None, metadata={})
    value = SampleExtractorNode().extract(sample, field="input")[-1]  # the ``value`` output
    assert value is boxes


# --------------------------------------------------------------------------- #
# ExtractMetadataNode
# --------------------------------------------------------------------------- #


def test_extract_metadata_input_types_and_outputs() -> None:
    req = ExtractMetadataNode.INPUT_TYPES()["required"]
    assert req["sample"] == ("DATAFLUX_SAMPLE",)
    assert req["key"][0] == "STRING" and req["key"][1]["default"] == "detection_boxes"
    assert ExtractMetadataNode.RETURN_TYPES == ("DATAFLUX_VALUE", "DATAFLUX_ARRAY", "STRING")
    assert ExtractMetadataNode.RETURN_NAMES == ("value", "array", "text")
    assert NODE_CLASS_MAPPINGS["Taidal_ExtractMetadata"] is ExtractMetadataNode


def test_extract_metadata_pulls_value_by_key() -> None:
    # The canonical flow: a model stamps metadata['detection_boxes']; this extracts it as a
    # DATAFLUX_VALUE to wire into OverlayBoxes.boxes.
    boxes = [[20, 10, 80, 50], [200, 100, 300, 150]]
    sample = Sample(input=0, target=None, metadata={"detection_boxes": boxes, "detection_labels": ["WLAN"]})
    value, array, text = ExtractMetadataNode().extract(sample, key="detection_boxes")
    assert value == boxes  # verbatim list of boxes on the DATAFLUX_VALUE output
    assert array == boxes  # a non-array value passes through verbatim too
    assert "20" in text and "300" in text  # repr view
    # a different key
    assert ExtractMetadataNode().extract(sample, key="detection_labels")[0] == ["WLAN"]


def test_extract_metadata_missing_key_is_graceful() -> None:
    sample = Sample(input=0, target=None, metadata={"detection_boxes": [[1, 2, 3, 4]]})
    value, _array, text = ExtractMetadataNode().extract(sample, key="not_a_key")
    assert value is None and text == ""


def test_extract_metadata_array_value_passes_through() -> None:
    emb = np.arange(6, dtype="float32").reshape(2, 3)
    sample = Sample(input=0, target=None, metadata={"detection_embeddings": emb})
    value, array, _text = ExtractMetadataNode().extract(sample, key="detection_embeddings")
    assert value is emb and array is emb  # ndarray passes through unchanged on both outputs


def test_extract_metadata_none_sample_raises_legible_error() -> None:
    with pytest.raises(ValueError, match="empty sample"):
        ExtractMetadataNode().extract(None, key="detection_boxes")


# --------------------------------------------------------------------------- #
# ArrayHistogramViewerNode
# --------------------------------------------------------------------------- #


def test_array_histogram_input_types_and_outputs() -> None:
    types = ArrayHistogramViewerNode.INPUT_TYPES()
    required, optional = types["required"], types["optional"]
    assert required["bins"][0] == "INT" and required["channel"][0] == "INT"
    assert required["max_size"][0] == "INT"
    # channel default -1 (all/auto); colormap is the closed Colormap combo.
    assert required["channel"][1]["default"] == -1
    assert isinstance(required["colormap"][0], list) and "viridis" in required["colormap"][0]
    # Inputs are OPTIONAL so the node renders an empty histogram when nothing is wired.
    assert optional["array"] == (ARRAY_TYPE,)
    assert optional["image"] == ("IMAGE",)
    # A viewer that also passes the array through; it IS an output node (runs without downstream wiring).
    assert ArrayHistogramViewerNode.RETURN_TYPES == (ARRAY_TYPE,)
    assert ArrayHistogramViewerNode.RETURN_NAMES == ("array",)
    assert ArrayHistogramViewerNode.OUTPUT_NODE is True
    # No hidden unique_id — that would make run_output.js / node_timing.js graft their widgets on.
    assert "unique_id" not in types.get("hidden", {})


def test_array_histogram_view_returns_ui_and_passthrough() -> None:
    arr = np.linspace(0.0, 1.0, 64 * 32, dtype=np.float32).reshape(64, 32)
    out = ArrayHistogramViewerNode().view(bins=32, channel=-1, colormap="viridis", max_size=128, array=arr)
    assert set(out) == {"ui", "result"}
    # Passthrough: the array socket carries the input unchanged.
    assert out["result"][0] is arr
    payload = out["ui"]["taidal_array_histogram"][0]
    assert len(payload["histogram"]["counts"]) == 32
    assert payload["image"].startswith("data:image/png;base64,")
    assert payload["stats"]["count"] == 64 * 32
    assert payload["grid_width"] > 0 and payload["grid_height"] > 0


def test_array_histogram_payload_is_json_safe_with_nan() -> None:
    # A NaN/Inf-laden array must still produce a payload that survives ComfyUI's JSON websocket
    # encoding (allow_nan=False) — non-finite floats become null, never bare NaN/Infinity tokens.
    arr = np.array([[0.0, np.nan], [np.inf, 1.0]], dtype=np.float32)
    payload = _build_histogram_payload(arr, bins=8, channel=-1, colormap="gray", max_size=64)
    encoded = json.dumps(payload, allow_nan=False)  # raises ValueError if any NaN/Inf leaked through
    assert "NaN" not in encoded and "Infinity" not in encoded
    # The non-finite grid cells are None, not 0.0 (we signal "missing", we don't fabricate a value).
    assert None in payload["values"]
    # Stats over the finite values only.
    assert payload["stats"]["min"] == 0.0 and payload["stats"]["max"] == 1.0


def test_array_histogram_image_input_uses_first_frame() -> None:
    # A native ComfyUI IMAGE ([B,H,W,C] float in [0,1]) is histogrammed in its native range.
    image = torch.rand(2, 16, 12, 3)
    out = ArrayHistogramViewerNode().view(channel=-1, colormap="viridis", max_size=64, image=image)
    payload = out["ui"]["taidal_array_histogram"][0]
    assert payload["channels"] == 3
    assert 0.0 <= payload["stats"]["min"] and payload["stats"]["max"] <= 1.0


def test_array_histogram_empty_when_nothing_wired() -> None:
    out = ArrayHistogramViewerNode().view()
    payload = out["ui"]["taidal_array_histogram"][0]
    assert payload["stats"]["count"] == 0
    assert "message" in payload
    assert out["result"][0] is None


def test_array_histogram_large_gray_image_does_not_raise() -> None:
    # Regression for the real-image failure: a 256x256 gray image (65536 px) hit numpy 2.2.x's
    # uniform-bins fast path, which block-accumulates via np.bincount and broke with
    # "operands could not be broadcast together with shapes (256,) (257,) (256,)". The earlier
    # node tests used <65536-element arrays so they took the non-block path and missed it.
    gray = np.tile(np.arange(256, dtype=np.uint8), (256, 1))  # [256, 256] uint8 gradient
    out = ArrayHistogramViewerNode().view(bins=256, channel=-1, colormap="gray", max_size=256, array=gray)
    payload = out["ui"]["taidal_array_histogram"][0]
    assert len(payload["histogram"]["counts"]) == 256
    assert payload["stats"]["count"] == 256 * 256
    assert payload["image"].startswith("data:image/png;base64,")


# --------------------------------------------------------------------------- #
# ConfusionMatrixViewerNode
# --------------------------------------------------------------------------- #


def test_confusion_matrix_input_types_and_outputs() -> None:
    types = ConfusionMatrixViewerNode.INPUT_TYPES()
    optional = types["optional"]
    # The evaluator's generic `all_metrics` @output (JSON STRING) wires in via forceInput.
    assert optional["metrics"][0] == "STRING" and optional["metrics"][1]["forceInput"] is True
    assert optional["matrix"] == (ARRAY_TYPE,)
    assert optional["class_names"][1]["forceInput"] is True
    assert ConfusionMatrixViewerNode.RETURN_TYPES == (ARRAY_TYPE,)
    assert ConfusionMatrixViewerNode.RETURN_NAMES == ("matrix",)
    assert ConfusionMatrixViewerNode.OUTPUT_NODE is True
    assert "unique_id" not in types.get("hidden", {})


def test_confusion_matrix_registered() -> None:
    assert NODE_CLASS_MAPPINGS["Taidal_ConfusionMatrix"] is ConfusionMatrixViewerNode
    assert NODE_DISPLAY_NAME_MAPPINGS["Taidal_ConfusionMatrix"] == "Confusion Matrix"


def test_confusion_matrix_view_extracts_all_cms_from_all_metrics() -> None:
    # The evaluator's `all_metrics` JSON: every metric. The viewer extracts EVERY confusion matrix
    # (square-2D), ignoring scalars/vectors, and ships ONE payload per matrix.
    am = json.dumps(
        {
            "test/acc": 0.93,
            "test/cm_a": [[50, 2, 1], [3, 47, 0], [0, 1, 49]],
            "test/per_class": [0.9, 0.8, 0.95],  # vector, not a CM
            "test/cm_b": [[5, 1], [0, 4]],
        }
    )
    out = ConfusionMatrixViewerNode().view(metrics=am, class_names='["cat", "dog", "fox"]')
    assert set(out) == {"ui", "result"}
    payloads = out["ui"]["taidal_confusion_matrix"]
    assert [(p["name"], p["n_classes"]) for p in payloads] == [("test/cm_a", 3), ("test/cm_b", 2)]
    assert out["result"][0].shape == (3, 3)  # first matrix's counts pass through
    json.dumps(out["ui"], allow_nan=False)  # JSON-safe for the websocket


def test_confusion_matrix_view_from_raw_array_and_comma_labels() -> None:
    out = ConfusionMatrixViewerNode().view(matrix=np.array([[5, 1], [0, 4]]), class_names="a, b")
    payloads = out["ui"]["taidal_confusion_matrix"]
    assert len(payloads) == 1 and payloads[0]["class_names"] == ["a", "b"]
    # The payload rounds to 6 decimals for compactness, so compare with a matching tolerance.
    assert payloads[0]["normalized"]["true"][0] == [pytest.approx(5 / 6, abs=1e-5), pytest.approx(1 / 6, abs=1e-5)]


def test_confusion_matrix_view_empty_when_nothing_wired() -> None:
    out = ConfusionMatrixViewerNode().view()
    assert out["ui"]["taidal_confusion_matrix"] == []
    assert out["result"][0] is None


def test_confusion_matrix_view_empty_when_metrics_has_no_cm() -> None:
    out = ConfusionMatrixViewerNode().view(metrics=json.dumps({"acc": 0.9, "vec": [1, 2, 3]}))
    assert out["ui"]["taidal_confusion_matrix"] == []


def test_parse_class_names_handles_json_comma_and_blank() -> None:
    assert _parse_class_names('["x", "y"]') == ["x", "y"]
    assert _parse_class_names("p, q, r") == ["p", "q", "r"]
    assert _parse_class_names("") is None and _parse_class_names(None) is None


def test_build_confusion_payloads_is_json_safe() -> None:
    # An empty-row matrix normalizes to None (not NaN) so the payload survives JSON encoding.
    payloads = _build_confusion_payloads(json.dumps({"cm": [[0, 0], [1, 3]]}), None, None)
    json.dumps(payloads, allow_nan=False)
    assert payloads[0]["normalized"]["true"][0] == [None, None]


def test_array_histogram_select_channel_payload() -> None:
    # CHW array: channel 1 plane is all ones -> histogram of a single channel.
    arr = np.stack([np.full((20, 16), c, dtype=np.float32) for c in range(3)], axis=0)
    payload = _build_histogram_payload(arr, bins=8, channel=1, colormap="viridis", max_size=64)
    assert payload["channels"] == 3 and payload["channel"] == 1
    assert payload["stats"]["min"] == 1.0 and payload["stats"]["max"] == 1.0


# --------------------------------------------------------------------------- #
# DisplayAnyNode + ApplyTextTemplateNode
# --------------------------------------------------------------------------- #


def test_display_any_input_types_and_outputs() -> None:
    types = DisplayAnyNode.INPUT_TYPES()
    assert set(types["required"]) == {"value", "mode"}
    assert types["required"]["mode"][0] == ["raw value", "tensor shape"]
    assert DisplayAnyNode.RETURN_TYPES == ("STRING",) and DisplayAnyNode.RETURN_NAMES == ("text",)
    assert DisplayAnyNode.OUTPUT_NODE is True
    assert DisplayAnyNode.VALIDATE_INPUTS() is True  # wildcard input accepts anything


def test_display_any_raw_and_shape_modes() -> None:
    out = DisplayAnyNode().display("hello world", mode="raw value")
    assert out["result"][0] == "hello world" and out["ui"]["text"] == ["hello world"]
    val = {"img": torch.zeros(3, 8, 8), "boxes": np.zeros((2, 4), dtype=np.float32), "n": [1, 2, 3]}
    shape_text = DisplayAnyNode().display(val, mode="tensor shape")["ui"]["text"][0]
    assert "Tensor(shape=(3, 8, 8)" in shape_text
    assert "ndarray(shape=(2, 4)" in shape_text
    assert "len=3" in shape_text


def test_describe_any_unpacks_sample_in_shape_mode() -> None:
    sample = Sample(input=torch.zeros(2, 4), target=5, metadata={})
    assert _describe_any(sample, mode="tensor shape").startswith("Sample(input=Tensor(shape=(2, 4)")


def test_display_any_raw_truncates_huge_values() -> None:
    text = DisplayAnyNode().display("x" * 50_000, mode="raw value")["result"][0]
    assert "truncated" in text and len(text) < 50_000


def test_apply_text_template_formats_and_handles_missing() -> None:
    # {a}/{b} substituted; the unknown {x} renders empty (never raises).
    assert ApplyTextTemplateNode().apply(template="{a} dB @ {b} Hz | {x}", a=-30, b=2400)[0] == "-30 dB @ 2400 Hz | "


def test_apply_text_template_format_spec_and_none() -> None:
    # Format specs work on raw numeric inputs; an unwired (None) input is the empty string.
    assert ApplyTextTemplateNode().apply(template="{a:.2f}", a=3.14159)[0] == "3.14"
    assert ApplyTextTemplateNode().apply(template="[{a}{b}]", a="x")[0] == "[x]"


def test_apply_text_template_malformed_is_caught() -> None:
    # A positional {0} (no positional args) must surface as an error string, not crash the node.
    assert ApplyTextTemplateNode().apply(template="{0}", a="x")[0].startswith("<template error")


def test_value_node_parses_literals_and_is_registered() -> None:
    from fluxstudio.bridge import VALUE_TYPE
    from fluxstudio.nodes import parse_value_literal

    assert NODE_CLASS_MAPPINGS["Taidal_Value"] is ValueNode
    assert ValueNode.RETURN_TYPES == (VALUE_TYPE,)
    # Python literal (tuples / parens JSON can't), JSON, and the raw-string / empty fallbacks.
    assert parse_value_literal("[(20, 10, 80, 50), (200, 100, 300, 150)]") == [(20, 10, 80, 50), (200, 100, 300, 150)]
    assert parse_value_literal('{"a": [1, 2]}') == {"a": [1, 2]}
    assert parse_value_literal("[255, 0, 0]") == [255, 0, 0]
    assert parse_value_literal("") is None
    assert parse_value_literal("not a literal") == "not a literal"
    # The node produces the parsed object on its single output.
    assert ValueNode().produce("[(1, 2, 3, 4)]") == ([(1, 2, 3, 4)],)


# --------------------------------------------------------------------------- #
# DrawTextNode
# --------------------------------------------------------------------------- #


def test_draw_text_input_types_and_outputs() -> None:
    types = DrawTextNode.INPUT_TYPES()
    req = types["required"]
    assert req["text"][0] == "STRING" and req["text"][1].get("multiline") is True
    assert req["position"][0] == _text_positions() and "top-left" in req["position"][0]
    assert types["optional"]["image"] == ("IMAGE",)
    assert DrawTextNode.RETURN_TYPES == ("IMAGE",) and DrawTextNode.RETURN_NAMES == ("image",)


def test_draw_text_blank_canvas_yields_comfy_image() -> None:
    # No image wired -> a fresh width x height canvas; output is ComfyUI IMAGE [1, H, W, 3] float [0,1].
    out = DrawTextNode().draw(text="Hi", width=120, height=48, background="black", color="white")[0]
    assert isinstance(out, torch.Tensor)
    assert tuple(out.shape) == (1, 48, 120, 3) and out.dtype == torch.float32
    assert 0.0 <= float(out.min()) and float(out.max()) <= 1.0
    assert float(out.sum()) > 0.0  # white text drawn


def test_draw_text_onto_image_preserves_dims() -> None:
    # A native ComfyUI IMAGE input is drawn on (frame 0), keeping its H x W.
    image = torch.zeros(1, 40, 96, 3)
    out = DrawTextNode().draw(text="X", image=image, color="white", position="center")[0]
    assert tuple(out.shape) == (1, 40, 96, 3)
    assert float(out.sum()) > 0.0  # text pixels present on the (previously black) image


# --------------------------------------------------------------------------- #
# ComposeSampleNode
# --------------------------------------------------------------------------- #


def test_compose_input_types_has_three_sample_sockets() -> None:
    required = ComposeSampleNode.INPUT_TYPES()["required"]
    assert set(required) == {"input_sample", "target_sample", "metadata_sample"}
    for key in ("input_sample", "target_sample", "metadata_sample"):
        assert required[key][0] == "DATAFLUX_SAMPLE"


def test_compose_takes_input_from_first_sample() -> None:
    s1 = Sample(input=np.array([1, 2, 3]), target=None, metadata={})
    s2 = Sample(input=np.array([9, 9, 9]), target="box", metadata={})
    s3 = Sample(input=None, target=None, metadata={"src": "camera"})
    (out,) = ComposeSampleNode().compose(s1, s2, s3)
    assert np.array_equal(out.input, s1.input)


def test_compose_takes_target_from_second_sample() -> None:
    s1 = Sample(input=0, target="original", metadata={})
    s2 = Sample(input=0, target="override", metadata={})
    s3 = Sample(input=0, target=None, metadata={})
    (out,) = ComposeSampleNode().compose(s1, s2, s3)
    assert out.target == "override"


def test_compose_takes_metadata_from_third_sample() -> None:
    s1 = Sample(input=0, target=None, metadata={"a": 1})
    s2 = Sample(input=0, target=None, metadata={"b": 2})
    s3 = Sample(input=0, target=None, metadata={"c": 3, "src": "lidar"})
    (out,) = ComposeSampleNode().compose(s1, s2, s3)
    assert out.metadata == {"c": 3, "src": "lidar"}


def test_compose_does_not_merge_metadata() -> None:
    # ComposeSampleNode is STRICT — only metadata_sample's dict is forwarded.
    s1 = Sample(input=0, target=None, metadata={"from_s1": True})
    s2 = Sample(input=0, target=None, metadata={"from_s2": True})
    s3 = Sample(input=0, target=None, metadata={"from_s3": True})
    (out,) = ComposeSampleNode().compose(s1, s2, s3)
    assert "from_s1" not in out.metadata
    assert "from_s2" not in out.metadata
    assert out.metadata == {"from_s3": True}


def test_compose_return_type_is_sample() -> None:
    s = Sample(input=0, target=0, metadata={})
    (out,) = ComposeSampleNode().compose(s, s, s)
    assert isinstance(out, Sample)


# --------------------------------------------------------------------------- #
# MixSamplesNode
# --------------------------------------------------------------------------- #


def test_mix_input_types_has_three_sample_sockets() -> None:
    required = MixSamplesNode.INPUT_TYPES()["required"]
    assert set(required) == {"input_sample", "target_sample", "metadata_sample"}
    for key in ("input_sample", "target_sample", "metadata_sample"):
        assert required[key][0] == "DATAFLUX_SAMPLE"


def test_mix_takes_input_from_first_sample() -> None:
    s1 = Sample(input=42, target=None, metadata={})
    s2 = Sample(input=99, target="t", metadata={})
    s3 = Sample(input=0, target=None, metadata={})
    (out,) = MixSamplesNode().mix(s1, s2, s3)
    assert out.input == 42


def test_mix_takes_target_from_second_sample() -> None:
    s1 = Sample(input=0, target="old", metadata={})
    s2 = Sample(input=0, target="new", metadata={})
    s3 = Sample(input=0, target=None, metadata={})
    (out,) = MixSamplesNode().mix(s1, s2, s3)
    assert out.target == "new"


def test_mix_merges_metadata_from_all_three() -> None:
    s1 = Sample(input=0, target=None, metadata={"a": 1})
    s2 = Sample(input=0, target=None, metadata={"b": 2})
    s3 = Sample(input=0, target=None, metadata={"c": 3})
    (out,) = MixSamplesNode().mix(s1, s2, s3)
    assert out.metadata == {"a": 1, "b": 2, "c": 3}


def test_mix_metadata_later_keys_win() -> None:
    # metadata_sample (s3) should override target_sample (s2) which overrides input_sample (s1).
    s1 = Sample(input=0, target=None, metadata={"key": "from_s1"})
    s2 = Sample(input=0, target=None, metadata={"key": "from_s2"})
    s3 = Sample(input=0, target=None, metadata={"key": "from_s3"})
    (out,) = MixSamplesNode().mix(s1, s2, s3)
    assert out.metadata["key"] == "from_s3"


def test_mix_handles_empty_metadata_dicts() -> None:
    s1 = Sample(input=0, target=None, metadata={})
    s2 = Sample(input=0, target="t", metadata={})
    s3 = Sample(input=0, target=None, metadata={"x": 7})
    (out,) = MixSamplesNode().mix(s1, s2, s3)
    assert out.metadata == {"x": 7}


def test_mix_return_type_is_sample() -> None:
    s = Sample(input=0, target=0, metadata={})
    (out,) = MixSamplesNode().mix(s, s, s)
    assert isinstance(out, Sample)


# --------------------------------------------------------------------------- #
# Static registration mappings
# --------------------------------------------------------------------------- #


def test_static_node_mappings_present() -> None:
    assert NODE_CLASS_MAPPINGS["Taidal_WalkDataset"] is WalkDatasetNode
    assert NODE_CLASS_MAPPINGS["Taidal_ExtractSample"] is SampleExtractorNode
    assert NODE_CLASS_MAPPINGS["Taidal_ArrayHistogram"] is ArrayHistogramViewerNode
    assert NODE_CLASS_MAPPINGS["Taidal_DisplayAny"] is DisplayAnyNode
    assert NODE_CLASS_MAPPINGS["Taidal_ApplyTextTemplate"] is ApplyTextTemplateNode
    assert NODE_CLASS_MAPPINGS["Taidal_DrawText"] is DrawTextNode
    assert NODE_CLASS_MAPPINGS["Taidal_ComposeSample"] is ComposeSampleNode
    assert NODE_CLASS_MAPPINGS["Taidal_MixSamples"] is MixSamplesNode
    assert NODE_CLASS_MAPPINGS["Taidal_YamlDump"] is YamlDumpNode
    assert NODE_DISPLAY_NAME_MAPPINGS["Taidal_WalkDataset"] == "Walk Dataset (by index)"
    assert NODE_DISPLAY_NAME_MAPPINGS["Taidal_ExtractSample"] == "Extract from Sample"
    assert NODE_DISPLAY_NAME_MAPPINGS["Taidal_ArrayHistogram"] == "Array / Tensor Histogram"
    assert NODE_DISPLAY_NAME_MAPPINGS["Taidal_DisplayAny"] == "Display Any"
    assert NODE_DISPLAY_NAME_MAPPINGS["Taidal_ApplyTextTemplate"] == "Apply Text Template"
    assert NODE_DISPLAY_NAME_MAPPINGS["Taidal_DrawText"] == "Draw Text to Image"
    assert NODE_DISPLAY_NAME_MAPPINGS["Taidal_ComposeSample"] == "Compose Sample (3-way)"
    assert NODE_DISPLAY_NAME_MAPPINGS["Taidal_MixSamples"] == "Mix Samples (merge metadata)"
    assert NODE_DISPLAY_NAME_MAPPINGS["Taidal_YamlDump"] == "YAML Dump"


# --------------------------------------------------------------------------- #
# SubgraphOpExtractNode — compile an upstream op DAG into one DATAFLUX_OP
# --------------------------------------------------------------------------- #


def _subgraph_op_mappings() -> dict:
    """node_mappings with RescaleOp + the terminator + a non-op boundary (the source stand-in)."""
    return {
        "Taidal_RescaleOp": create_comfy_node(RescaleOp, "Taidal_RescaleOp"),
        "Taidal_SubgraphOp": SubgraphOpExtractNode,
        "WalkDatasetNode": type("WalkDatasetNode", (object,), {}),
    }


def _two_op_subgraph_prompt() -> dict:
    """boundary(1) → RescaleOp(2) → RescaleOp(3) → terminator(4).sample."""
    return {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": {
            "class_type": "Taidal_RescaleOp",
            "inputs": {"sample": ["1", 0], "in_min": 0.0, "in_max": 10.0, "out_max": 1.0},
        },
        "3": {
            "class_type": "Taidal_RescaleOp",
            "inputs": {"sample": ["2", 0], "in_min": 0.0, "in_max": 1.0, "out_max": 100.0},
        },
        "4": {"class_type": "Taidal_SubgraphOp", "inputs": {"sample": ["3", 0]}},
    }


def test_subgraph_op_registered_and_socket_shape() -> None:
    assert NODE_CLASS_MAPPINGS["Taidal_SubgraphOp"] is SubgraphOpExtractNode
    assert NODE_DISPLAY_NAME_MAPPINGS["Taidal_SubgraphOp"] == "Extract as Op"
    assert SubgraphOpExtractNode.RETURN_TYPES == (OP_TYPE,)
    types = SubgraphOpExtractNode.INPUT_TYPES()
    assert types["optional"]["sample"][0] == SAMPLE_TYPE
    # Hidden PROMPT + UNIQUE_ID are what let execute() compile its own ancestor cone.
    assert types["hidden"] == {"prompt": "PROMPT", "unique_id": "UNIQUE_ID"}


def test_subgraph_op_sample_input_is_lazy_and_never_requested() -> None:
    # The terminator compiles statically from the PROMPT and never reads the runtime sample, so its
    # ``sample`` input MUST be lazy + ``check_lazy_status`` MUST return [] — otherwise ComfyUI would
    # eagerly execute the (sourceless) upstream op cone and crash a downstream viewer on a None sample.
    types = SubgraphOpExtractNode.INPUT_TYPES()
    assert types["optional"]["sample"][1].get("lazy") is True
    assert SubgraphOpExtractNode().check_lazy_status(sample=None, prompt={"x": 1}, unique_id="4") == []


def test_compile_subgraph_ops_linear_chain_matches_direct_application() -> None:
    ops = compile_subgraph_ops(_two_op_subgraph_prompt(), "4", _subgraph_op_mappings())
    assert len(ops) == 2
    sample = Sample(input=torch.tensor([0.0, 5.0, 10.0]), target=None, metadata={})
    got = ops[1](ops[0](sample))
    expected = RescaleOp(in_min=0.0, in_max=1.0, out_max=100.0)(RescaleOp(in_min=0.0, in_max=10.0, out_max=1.0)(sample))
    assert torch.allclose(got.input, expected.input)


def test_compile_subgraph_ops_is_empty_when_unwired_or_missing_inputs() -> None:
    mappings = _subgraph_op_mappings()
    # Terminator present but with no sample wired → identity (empty op-list).
    assert compile_subgraph_ops({"4": {"class_type": "Taidal_SubgraphOp", "inputs": {}}}, "4", mappings) == []
    # Defensive: missing prompt / unique_id (e.g. invoked outside ComfyUI) → empty, never raises.
    assert compile_subgraph_ops(None, "4", mappings) == []
    assert compile_subgraph_ops({"4": {}}, None, mappings) == []


def test_extract_wraps_compiled_ops_in_a_transformchain(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = [RescaleOp(in_min=0.0, in_max=2.0)]
    monkeypatch.setattr("fluxstudio.nodes.compile_subgraph_ops", lambda prompt, unique_id: sentinel)
    (op,) = SubgraphOpExtractNode().extract(sample=None, prompt={"x": 1}, unique_id="4")
    assert isinstance(op, TransformChain)
    assert op.ops == sentinel


def test_extract_compiled_op_applies_the_inner_pipeline_to_a_sample() -> None:
    # End-to-end through the node: the returned TransformChain IS the two-op inner pipeline.
    ops = compile_subgraph_ops(_two_op_subgraph_prompt(), "4", _subgraph_op_mappings())
    chain = TransformChain(ops=ops)
    sample = Sample(input=torch.tensor([2.0, 4.0, 8.0]), target=None, metadata={})
    got = chain(sample)
    assert got is not None  # the chain is non-filtering, so a sample always survives
    expected = RescaleOp(in_min=0.0, in_max=1.0, out_max=100.0)(RescaleOp(in_min=0.0, in_max=10.0, out_max=1.0)(sample))
    assert torch.allclose(got.input, expected.input)


# --------------------------------------------------------------------------- #
# run_runnable — runs a built runnable (each per-class runnable node's execute calls this)
# --------------------------------------------------------------------------- #


def test_run_runnable_calls_run_and_returns_text() -> None:
    from fluxstudio.nodes import run_runnable

    calls = []

    class _Runnable:
        def run(self) -> None:
            calls.append(True)

    text = run_runnable(_Runnable())
    assert calls == [True]
    # run_runnable returns the captured text; the node's execute wraps it in {"ui": {"text": [...]}}.
    assert "_Runnable" in text and "complete" in text


def test_run_runnable_captures_runner_output() -> None:
    # The runner's stdout/stderr is captured and returned as the text.
    from fluxstudio.nodes import run_runnable

    class _Printy:
        def run(self) -> None:
            print("epoch 1: loss=0.5")
            print("done", file=sys.stderr)

    text = run_runnable(_Printy())
    assert "epoch 1: loss=0.5" in text
    assert "done" in text


def test_collapse_carriage_returns_keeps_final_frame() -> None:
    from fluxstudio.nodes import _collapse_carriage_returns

    # tqdm/Lightning repaint a line with \r; only the final frame per line survives.
    assert _collapse_carriage_returns("a\rb\rc\nx\ry") == "c\ny"
    assert _collapse_carriage_returns("plain\nlines") == "plain\nlines"


def test_strip_ansi_removes_csi_sequences() -> None:
    from fluxstudio.nodes import _strip_ansi

    esc = "\x1b"
    # A Rich/Lightning progress frame: erase-line + dim + reset + dim/underline SGR codes.
    raw = f"{esc}[2KTesting 10000/10000 {esc}[2m0:00:40{esc}[0m {esc}[2;4m249.83it/s{esc}[0m"
    cleaned = _strip_ansi(raw)
    assert cleaned == "Testing 10000/10000 0:00:40 249.83it/s"
    assert esc not in cleaned
    # Plain text is untouched (incl. the box-drawing bar glyphs).
    assert _strip_ansi("plain ━━━ 50%") == "plain ━━━ 50%"


def test_run_runnable_strips_ansi_progress_codes() -> None:
    # A Rich-style bar emits cursor/erase + dim/underline ANSI codes that a plain text widget can't
    # render. The captured text must be stripped clean; the live console tee keeps them (so the bar
    # still animates in the terminal) — not asserted here.
    from fluxstudio.nodes import run_runnable

    esc = "\x1b"

    class _RichBar:
        def run(self) -> None:
            sys.stdout.write(f"\r{esc}[2KTesting 1/10000")
            sys.stdout.write(f"\r{esc}[2KTesting 10000/10000 {esc}[2m0:00:40{esc}[0m {esc}[2;4m249.83it/s{esc}[0m\n")
            sys.stdout.flush()

    text = run_runnable(_RichBar(), unique_id=None)
    assert esc not in text
    assert "[2K" not in text and "[0m" not in text
    assert "Testing 10000/10000 0:00:40 249.83it/s" in text


def test_run_runnable_streams_output_live(monkeypatch: pytest.MonkeyPatch) -> None:
    # The runner's output is pushed to the node's widget DURING the run (so progress bars update live),
    # not only at the end. We record each _send_run_output call and assert >1 push (live + final).
    import time

    from fluxstudio import nodes as nodes_mod
    from fluxstudio.nodes import run_runnable

    pushes: list = []
    monkeypatch.setattr(nodes_mod, "_send_run_output", lambda uid, text: pushes.append((uid, text)))

    class _Progress:
        def run(self) -> None:
            for i in range(3):
                # A \r-repainted progress bar (tqdm/Lightning style) on a single line.
                sys.stdout.write(f"\rstep {i} {(i + 1) * 33}%")
                sys.stdout.flush()
                time.sleep(0.12)  # span several 0.1s poll ticks so the loop streams mid-run
            print("\rdone 100%")

    text = run_runnable(_Progress(), unique_id="7")

    # Streaming fired more than once (at least one live push before the final one), all addressed to
    # this node, and the \r-collapsed final frame is what lands on the returned text / last push.
    assert len(pushes) >= 2
    assert all(uid == "7" for uid, _ in pushes)
    assert text == "done 100%"
    assert pushes[-1][1] == "done 100%"


def test_send_run_output_is_a_safe_noop_without_comfyui() -> None:
    # No unique_id (and no ComfyUI `server` module in the dev env) -> a silent no-op, never raising.
    from fluxstudio.nodes import _send_run_output

    _send_run_output(None, "anything")  # unique_id None: returns immediately
    _send_run_output("1", "anything")  # server absent: guarded try/except swallows the import error


def test_make_progress_bar_is_a_safe_noop_without_comfyui() -> None:
    # No unique_id, and no importable `comfy.utils` in the dev env -> None (no native bar), never raising.
    from fluxstudio.nodes import _make_progress_bar

    assert _make_progress_bar(None) is None  # no node id
    assert _make_progress_bar("1") is None  # comfy.utils absent in the fluxstudio test env


def test_run_runnable_drives_native_progress_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    # A runnable that supports the comfy-free marainer.progress protocol (set_progress_callback)
    # drives ComfyUI's native per-node ProgressBar: the poll loop forwards the reported (value, total)
    # to update_absolute, and the bar is filled to 100% on a clean finish.
    import time

    from fluxstudio import nodes as nodes_mod
    from fluxstudio.nodes import run_runnable

    updates: list = []

    class _FakeBar:
        def update_absolute(self, value: float, total: float | None = None, preview: object = None) -> None:
            updates.append((value, total))

    monkeypatch.setattr(nodes_mod, "_make_progress_bar", lambda uid: _FakeBar())

    class _Reporting:
        def __init__(self) -> None:
            self._cb: Any = None

        def set_progress_callback(self, cb: Any) -> None:
            self._cb = cb

        def run(self) -> None:
            for i in range(1, 4):
                self._cb(float(i), 3.0, "Training")
                time.sleep(0.12)  # span several 0.1s poll ticks so the loop forwards mid-run

    run_runnable(_Reporting(), unique_id="7")

    assert updates, "the native progress bar was never driven"
    # Every drive carried the reported total (the loop skips while total is still 0), and the bar
    # was completed to 100% on finish.
    assert all(total == 3.0 for _, total in updates)
    assert updates[-1] == (3.0, 3.0)


def test_run_runnable_without_progress_support_leaves_bar_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    # A runnable with no set_progress_callback runs normally and never drives the bar (total stays 0).
    from fluxstudio import nodes as nodes_mod
    from fluxstudio.nodes import run_runnable

    updates: list = []

    class _FakeBar:
        def update_absolute(self, *args: object, **kwargs: object) -> None:
            updates.append(args)

    monkeypatch.setattr(nodes_mod, "_make_progress_bar", lambda uid: _FakeBar())

    class _Plain:
        def run(self) -> None:
            print("done")

    text = run_runnable(_Plain(), unique_id="7")
    assert "done" in text
    assert updates == []


def test_run_runnable_rejects_non_runnable() -> None:
    from fluxstudio.nodes import run_runnable

    with pytest.raises(RuntimeError, match="no callable run"):
        run_runnable(object())


def test_run_runnable_restores_autograd_for_torch_runner_under_inference_mode() -> None:
    # ComfyUI wraps graph execution in torch.inference_mode(); a TorchRunner (training) must get
    # normal autograd restored inside run() or loss.backward() fails. Simulate the wrapping and
    # assert a tensor built inside run() carries a grad graph.
    from fluxstudio.nodes import run_runnable

    observed = {}

    class _TorchRunnable:
        __torch_runner__ = True

        def run(self) -> None:
            observed["grad_enabled"] = torch.is_grad_enabled()
            observed["inference"] = torch.is_inference_mode_enabled()
            lin = torch.nn.Linear(4, 2)  # built HERE, like a lazy backbone
            loss = lin(torch.randn(3, 4)).sum()
            loss.backward()  # would raise without the grad restore
            observed["has_grad"] = lin.weight.grad is not None

    with torch.inference_mode():
        run_runnable(_TorchRunnable())

    assert observed == {"grad_enabled": True, "inference": False, "has_grad": True}


def test_run_runnable_leaves_inference_mode_for_plain_runnable() -> None:
    # An inference-only runnable (no __torch_runner__) runs as-is under ComfyUI's inference mode.
    from fluxstudio.nodes import run_runnable

    observed = {}

    class _PlainRunnable:
        def run(self) -> None:
            observed["inference"] = torch.is_inference_mode_enabled()

    with torch.inference_mode():
        run_runnable(_PlainRunnable())

    assert observed["inference"] is True


def test_run_runnable_interrupt_stops_running_runnable() -> None:
    # The user pressed ComfyUI's interrupt button mid-run: run_runnable polls the flag, injects a
    # KeyboardInterrupt into the worker thread, and re-raises the interrupt signal for a clean stop.
    import fluxstudio.nodes as nodes
    from fluxstudio.nodes import _interrupt_exception_type, run_runnable

    started = threading.Event()
    ran_to_completion = {"value": False}

    class _SlowRunnable:
        def run(self) -> None:
            started.set()
            # A cooperative loop standing in for a training / sample-iteration loop: it unwinds at a
            # statement boundary when the injected KeyboardInterrupt arrives, before completing.
            for _ in range(10_000):
                time.sleep(0.005)
            ran_to_completion["value"] = True

    # Flip the interrupt flag True only once the worker is actually running.
    def _interrupted() -> bool:
        return started.is_set()

    with patch.object(nodes, "_processing_interrupted", _interrupted):
        with pytest.raises(_interrupt_exception_type()):
            run_runnable(_SlowRunnable())

    assert ran_to_completion["value"] is False


def test_run_runnable_propagates_genuine_worker_exception() -> None:
    # A real error inside run() (not an interrupt) is surfaced to the caller, unchanged.
    from fluxstudio.nodes import run_runnable

    class _Boom:
        def run(self) -> None:
            raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        run_runnable(_Boom())


def test_run_runnable_grad_context_is_active_inside_worker_thread() -> None:
    # The grad restore for a TorchRunner must happen ON the worker thread (torch grad mode is
    # thread-local), and the work must run on a DIFFERENT thread than the caller.
    from fluxstudio.nodes import run_runnable

    main_thread = threading.get_ident()
    observed = {}

    class _TorchRunnable:
        __torch_runner__ = True

        def run(self) -> None:
            observed["thread"] = threading.get_ident()
            observed["grad_enabled"] = torch.is_grad_enabled()
            observed["inference"] = torch.is_inference_mode_enabled()

    with torch.inference_mode():
        text = run_runnable(_TorchRunnable())

    assert observed["thread"] != main_thread
    assert observed["grad_enabled"] is True
    assert observed["inference"] is False
    assert "_TorchRunnable" in text and "complete" in text


# ---------------------------------------------------------------------------
# RetagObjectNode — the strict-socket escape hatch
# ---------------------------------------------------------------------------


def test_retag_node_registered() -> None:
    assert NODE_CLASS_MAPPINGS["Taidal_Retag"] is RetagObjectNode
    assert NODE_DISPLAY_NAME_MAPPINGS["Taidal_Retag"] == "Retag (role / task)"


def test_retag_node_io_is_wildcard() -> None:
    """Input + initial output are the litegraph wildcard ``*`` so the node accepts any object and
    (until web/retag.js narrows the output to the chosen role) docks anywhere."""
    assert RetagObjectNode.RETURN_TYPES == (ANY,)
    assert RetagObjectNode.RETURN_NAMES == ("object",)
    spec = RetagObjectNode.INPUT_TYPES()
    assert spec["required"]["object"] == (ANY,)
    # The wildcard compares equal to every concrete socket type (the AnyType contract).
    assert ANY == "DATAFLUX_OBJECT:model"
    assert not (ANY != "DATAFLUX_SOURCE")
    # Combos carry the keep / none sentinels.
    for field in ("target_role", "target_task"):
        options = spec["required"][field][0]
        assert options[:2] == [_RETAG_KEEP, _RETAG_NONE]


def test_retag_restamps_and_passes_through() -> None:
    """Picking a target re-stamps the object's per-instance tag and returns the SAME object."""

    class _Obj:
        pass

    obj = _Obj()
    # ``role`` drives the socket (task-agnostic); ``task`` is guidance only.
    out = RetagObjectNode().retag(obj, target_role="model", target_task="classification")
    assert out == (obj,)  # same object, passed through
    assert obj.__confluid_role__ == "model"  # type: ignore[attr-defined]
    assert obj.__confluid_task__ == "classification"  # type: ignore[attr-defined]


def test_retag_keep_is_noop_and_none_clears() -> None:
    class _Obj:
        __confluid_role__ = "model"
        __confluid_task__ = "classification"

    obj = _Obj()
    # (keep) leaves the existing class-level tags untouched (no instance shadow).
    RetagObjectNode().retag(obj, target_role=_RETAG_KEEP, target_task=_RETAG_KEEP)
    assert "__confluid_role__" not in vars(obj)
    # (none) clears via a per-instance None shadow.
    RetagObjectNode().retag(obj, target_role=_RETAG_NONE, target_task=_RETAG_NONE)
    assert obj.__confluid_role__ is None
    assert obj.__confluid_task__ is None


# --------------------------------------------------------------------------- #
# _to_yaml_str + YamlDumpNode
# --------------------------------------------------------------------------- #


def test_to_yaml_str_plain_list() -> None:
    result = _to_yaml_str([[10, 20, 30, 40], [50, 60, 70, 80]])
    assert "10" in result and "80" in result


def test_to_yaml_str_plain_dict() -> None:
    result = _to_yaml_str({"snr": 12.5, "label": "wifi"})
    assert "snr" in result and "12.5" in result and "wifi" in result


def test_to_yaml_str_configurable_object() -> None:
    from confluid import configurable

    @configurable
    class _DummyOp:
        def __init__(self, gain: float = 1.0) -> None:
            self.gain = gain

    result = _to_yaml_str(_DummyOp(gain=2.5))
    # confluid.dump uses !class: tag notation
    assert "_DummyOp" in result
    assert "2.5" in result


def test_to_yaml_str_non_serializable_falls_back_to_repr() -> None:
    class _UnknownThing:
        def __repr__(self) -> str:
            return "UnknownThing()"

    result = _to_yaml_str(_UnknownThing())
    assert "UnknownThing" in result


def test_extract_list_target_shows_full_yaml() -> None:
    """_to_text for a list target must show the complete content, not just 'list'."""
    bboxes = [[10, 20, 100, 200], [30, 40, 150, 250]]
    sample = Sample(input=np.zeros((8, 8, 3), dtype="uint8"), target=bboxes, metadata={})
    _img, _mask, text, _i, _f, _arr, _value = SampleExtractorNode().extract(sample, field="target")
    # Full content present — not just a type summary.
    assert "10" in text and "250" in text


def test_extract_dict_metadata_shows_full_yaml() -> None:
    meta = {"snr_db": 12.5, "modulation": "QPSK", "label": 7}
    sample = Sample(input=np.zeros((4, 4, 3), dtype="uint8"), target=meta, metadata={})
    _img, _mask, text, _i, _f, _arr, _value = SampleExtractorNode().extract(sample, field="target")
    assert "snr_db" in text and "12.5" in text and "QPSK" in text


def test_yaml_dump_node_list() -> None:
    (yaml_str,) = YamlDumpNode().dump([[1, 2], [3, 4]])
    assert "1" in yaml_str and "4" in yaml_str


def test_yaml_dump_node_dict() -> None:
    (yaml_str,) = YamlDumpNode().dump({"key": "value", "n": 42})
    assert "key" in yaml_str and "value" in yaml_str and "42" in yaml_str


def test_yaml_dump_node_configurable() -> None:
    from confluid import configurable

    @configurable
    class _Op:
        def __init__(self, rate: float = 0.5) -> None:
            self.rate = rate

    (yaml_str,) = YamlDumpNode().dump(_Op(rate=0.9))
    assert "_Op" in yaml_str and "0.9" in yaml_str


def test_yaml_dump_node_sample_does_not_produce_class_tag() -> None:
    """A Sample NamedTuple must NOT be serialized via confluid.dump (which produces
    the useless 'class:dataflux.sample.Sample ''' output) — it should fall through
    to yaml.safe_dump or repr() and show the actual field values."""
    sample = Sample(input=np.zeros((4, 4), dtype="float32"), target=1, metadata={"label": "wifi"})
    (yaml_str,) = YamlDumpNode().dump(sample)
    assert "class:dataflux.sample.Sample" not in yaml_str


def test_yaml_dump_node_input_types() -> None:
    spec = YamlDumpNode.INPUT_TYPES()
    assert spec["required"]["object"] == (ANY,)


def test_yaml_dump_node_return_types() -> None:
    assert YamlDumpNode.RETURN_TYPES == ("STRING",)
    assert YamlDumpNode.RETURN_NAMES == ("yaml",)


# --------------------------------------------------------------------------- #
# ImageGridNode
# --------------------------------------------------------------------------- #


def _make_comfy_image(h: int, w: int, value: float = 0.5) -> torch.Tensor:
    """Create a [1, H, W, 3] float32 IMAGE tensor (ComfyUI batch format)."""
    return torch.full((1, h, w, 3), value, dtype=torch.float32)


def test_image_grid_registered() -> None:
    assert NODE_CLASS_MAPPINGS["Taidal_ImageGrid"] is ImageGridNode
    assert NODE_DISPLAY_NAME_MAPPINGS["Taidal_ImageGrid"] == "Image Grid"


def test_image_grid_input_types() -> None:
    spec = ImageGridNode.INPUT_TYPES()
    for key in ("columns", "cell_width", "cell_height", "padding", "background"):
        assert key in spec["required"], f"missing required widget: {key}"
    assert "image_0" in spec["optional"]
    assert spec["optional"]["image_0"] == ("IMAGE",)
    assert ImageGridNode.RETURN_TYPES == ("IMAGE",)
    assert ImageGridNode.RETURN_NAMES == ("grid",)
    # Not a terminal OUTPUT_NODE — feeds native ComfyUI Preview Image.
    assert getattr(ImageGridNode, "OUTPUT_NODE", False) is False


def test_image_grid_no_images_returns_placeholder() -> None:
    (grid,) = ImageGridNode().build_grid()
    assert isinstance(grid, torch.Tensor)
    assert grid.shape[0] == 1  # batch dim present


def test_image_grid_single_image_correct_output_shape() -> None:
    img = _make_comfy_image(100, 200)
    (grid,) = ImageGridNode().build_grid(columns=1, cell_width=128, cell_height=128, padding=0, image_0=img)
    assert grid.shape == (1, 128, 128, 3)
    assert grid.dtype == torch.float32
    assert 0.0 <= float(grid.min()) and float(grid.max()) <= 1.0


def test_image_grid_multiple_images_correct_grid_size() -> None:
    # 6 images, 3 columns → 2 rows; grid_w = 3*64+4*4=208, grid_h = 2*64+3*4=140
    kwargs = {f"image_{i}": _make_comfy_image(64, 64) for i in range(6)}
    (grid,) = ImageGridNode().build_grid(
        columns=3, cell_width=64, cell_height=64, padding=4, **kwargs  # type: ignore[arg-type]
    )
    assert grid.shape == (1, 140, 208, 3)


def test_image_grid_partial_last_row() -> None:
    # 5 images, 3 columns → 2 rows (last row has 2 cells + 1 empty)
    kwargs = {f"image_{i}": _make_comfy_image(32, 32) for i in range(5)}
    (grid,) = ImageGridNode().build_grid(
        columns=3, cell_width=32, cell_height=32, padding=0, **kwargs  # type: ignore[arg-type]
    )
    assert grid.shape == (1, 64, 96, 3)


def test_image_grid_background_color_applied() -> None:
    # Landscape image (4×2) in a square cell: top/bottom rows are padding (background).
    img = _make_comfy_image(2, 4, 0.0)  # 2H × 4W black image
    (grid,) = ImageGridNode().build_grid(
        columns=1, cell_width=64, cell_height=64, padding=0, background=1.0, image_0=img
    )
    # Scale = min(64/4, 64/2) = 16; new_h = 32, pad_top = 16 — top row is all background.
    assert float(grid[0, 0, 0, 0]) > 0.9


def test_image_grid_none_slots_skipped() -> None:
    # Unconnected optional slots arrive as None — must not crash and must be excluded.
    img = _make_comfy_image(32, 32)
    (grid,) = ImageGridNode().build_grid(
        columns=2, cell_width=32, cell_height=32, padding=0, image_0=img, image_1=None, image_2=img
    )
    # 2 valid images, 2 columns → 1 row
    assert grid.shape == (1, 32, 64, 3)


def test_collect_images_sorted_by_index() -> None:
    imgs = [_make_comfy_image(4, 4, v) for v in [0.1, 0.5, 0.9]]
    kwargs = {"image_2": imgs[2], "image_0": imgs[0], "image_1": imgs[1], "columns": 2}
    result = _collect_images(kwargs)
    assert len(result) == 3
    assert float(result[0][0, 0, 0, 0]) == pytest.approx(0.1, abs=0.01)
    assert float(result[2][0, 0, 0, 0]) == pytest.approx(0.9, abs=0.01)


def test_collect_images_drops_none() -> None:
    img = _make_comfy_image(4, 4)
    result = _collect_images({"image_0": img, "image_1": None, "image_2": img})
    assert len(result) == 2


def test_letterbox_output_shape_and_dtype() -> None:
    img = torch.zeros(50, 100, 3, dtype=torch.float32)
    cell = _letterbox(img, cell_w=128, cell_h=128, bg=0.0)
    assert cell.shape == (128, 128, 3)
    assert cell.dtype == np.dtype("uint8")


def test_letterbox_preserves_aspect_ratio() -> None:
    # 100×50 (2:1 landscape) into a 100×100 cell: content fills 100×50, padded top/bottom.
    img = torch.ones(50, 100, 3, dtype=torch.float32)  # white image
    cell = _letterbox(img, cell_w=100, cell_h=100, bg=0)
    # Top row should be background (0), not image content.
    assert int(cell[0, 50, 0]) == 0
    # Middle row should contain image content (white → 255).
    assert int(cell[50, 50, 0]) == 255


def test_letterbox_background_colour() -> None:
    # Landscape image (4×2) in a square cell: top/bottom rows are padding.
    img = torch.zeros(2, 4, 3, dtype=torch.float32)  # 2H × 4W black image
    cell = _letterbox(img, cell_w=64, cell_h=64, bg=0.5)
    # scale=16, new_h=32, pad_top=16 — top row is background (~128).
    assert abs(int(cell[0, 0, 0]) - 128) <= 1
