"""Tests for ``fluxstudio.nodes`` — the WalkDataset stepper and SampleExtractor adapter nodes.

These are ComfyUI adapter glue, driven headlessly. ``SampleExtractorNode`` unpacks a Sample's
``input`` / ``target`` onto typed ComfyUI sockets (IMAGE / MASK / STRING / INT / FLOAT) so it feeds
NATIVE ComfyUI viewers; it is NOT an output node. Image rendering is delegated to
``waivefront.visualizers.value_to_image`` (installed in the dev venv); one test forces that import to
fail to exercise the graceful-degradation placeholder.
"""

import sys
import threading
import time
from types import ModuleType
from unittest.mock import patch

import numpy as np
import pytest
import torch
from dataflux.sample import Sample

from fluxstudio.nodes import (
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
    SampleExtractorNode,
    WalkDatasetNode,
    _colormap_choices,
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
    assert SampleExtractorNode.RETURN_TYPES == ("IMAGE", "MASK", "STRING", "INT", "FLOAT")
    assert SampleExtractorNode.RETURN_NAMES == ("image", "mask", "text", "int", "float")
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
    image, mask, text, ival, fval = SampleExtractorNode().extract(sample, field="input")
    # ComfyUI IMAGE: [B, H, W, 3] float32 in [0, 1].
    assert isinstance(image, torch.Tensor)
    assert image.shape[0] == 1 and image.shape[-1] == 3 and image.dtype == torch.float32
    assert 0.0 <= float(image.min()) and float(image.max()) <= 1.0
    # A non-2-D, non-scalar field -> empty mask + zero numbers.
    assert tuple(mask.shape) == (1, 1, 1)
    assert ival == 0 and fval == 0.0
    assert isinstance(text, str)


def test_extract_scalar_target_with_label_name() -> None:
    sample = Sample(input=np.zeros((4, 4, 3), dtype="uint8"), target=7, metadata={"label_name": "drone_x"})
    _image, _mask, text, ival, fval = SampleExtractorNode().extract(sample, field="target")
    assert text == "drone_x (id=7)"
    assert ival == 7 and fval == 7.0


def test_extract_scalar_target_without_label_name() -> None:
    sample = Sample(input=np.zeros((4, 4, 3), dtype="uint8"), target=2, metadata={})
    _i, _m, text, ival, _f = SampleExtractorNode().extract(sample, field="target")
    assert text == "2"
    assert ival == 2


def test_extract_2d_target_yields_comfy_mask() -> None:
    sample = Sample(input=np.zeros((4, 4, 3), dtype="uint8"), target=np.eye(6, dtype=bool), metadata={})
    _image, mask, _text, _i, _f = SampleExtractorNode().extract(sample, field="target")
    # ComfyUI MASK: [B, H, W] float32 in [0, 1].
    assert tuple(mask.shape) == (1, 6, 6) and mask.dtype == torch.float32
    assert 0.0 <= float(mask.min()) and float(mask.max()) <= 1.0


def test_extract_torch_scalar_target_is_coerced() -> None:
    sample = Sample(input=np.zeros((4, 4, 3), dtype="uint8"), target=torch.tensor(5, dtype=torch.int64))
    _i, _m, text, ival, fval = SampleExtractorNode().extract(sample, field="target")
    assert ival == 5 and fval == 5.0 and text == "5"


def test_extract_text_summary_is_compact_for_arrays() -> None:
    sample = Sample(input=torch.rand(3, 8, 8), target=0)
    _i, _m, text, _iv, _fv = SampleExtractorNode().extract(sample, field="input")
    # Compact summary, not the raw ArrayType dataclass repr.
    assert "ArrayType(" not in text
    assert "shape=(3, 8, 8)" in text and "float32" in text


def test_extract_image_degrades_without_waivefront() -> None:
    # Force `from waivefront.visualizers import value_to_image` to fail -> 1x1 placeholder image,
    # while the text/number outputs still work.
    empty = ModuleType("waivefront.visualizers")  # lacks value_to_image
    sample = Sample(input=np.zeros((4, 4, 3), dtype="uint8"), target=9, metadata={"label_name": "x"})
    with patch.dict(sys.modules, {"waivefront.visualizers": empty}):
        image, _mask, text, ival, _f = SampleExtractorNode().extract(sample, field="target")
    assert tuple(image.shape) == (1, 1, 1, 3)
    assert text == "x (id=9)" and ival == 9


def test_extract_bool_target_coerces_to_int() -> None:
    sample = Sample(input=np.zeros((4, 4, 3), dtype="uint8"), target=True)
    _i, _m, text, ival, fval = SampleExtractorNode().extract(sample, field="target")
    assert ival == 1 and fval == 1.0 and text == "1"


def test_extract_numpy_scalar_target() -> None:
    sample = Sample(input=np.zeros((4, 4, 3), dtype="uint8"), target=np.int64(5))
    _i, _m, text, ival, _f = SampleExtractorNode().extract(sample, field="target")
    assert ival == 5 and text == "5"


def test_extract_pil_target_yields_mask() -> None:
    from PIL import Image

    sample = Sample(input=np.zeros((4, 4, 3), dtype="uint8"), target=Image.new("L", (6, 6)))
    _image, mask, _text, _i, _f = SampleExtractorNode().extract(sample, field="target")
    assert tuple(mask.shape) == (1, 6, 6)


def test_extract_string_field_passthrough() -> None:
    sample = Sample(input="hello world", target=0)
    _i, _m, text, _iv, _fv = SampleExtractorNode().extract(sample, field="input")
    assert text == "hello world"


def test_extract_text_summary_for_pythontype_and_anytype() -> None:
    # list target -> PythonType("list") summary; None target -> AnyType fallback.
    list_text = SampleExtractorNode().extract(Sample(input=0, target=[1, 2, 3]), field="target")[2]
    assert list_text == "list"
    none_text = SampleExtractorNode().extract(Sample(input=0, target=None), field="target")[2]
    assert none_text == "AnyType"


# --------------------------------------------------------------------------- #
# Static registration mappings
# --------------------------------------------------------------------------- #


def test_static_node_mappings_present() -> None:
    assert NODE_CLASS_MAPPINGS["Taidal_WalkDataset"] is WalkDatasetNode
    assert NODE_CLASS_MAPPINGS["Taidal_ExtractSample"] is SampleExtractorNode
    assert NODE_DISPLAY_NAME_MAPPINGS["Taidal_WalkDataset"] == "Walk Dataset (by index)"
    assert NODE_DISPLAY_NAME_MAPPINGS["Taidal_ExtractSample"] == "Extract from Sample"


# --------------------------------------------------------------------------- #
# RunNode — terminal OUTPUT node that calls .run() on a built runnable
# --------------------------------------------------------------------------- #


def test_run_node_calls_run() -> None:
    from fluxstudio.bridge import RUNNABLE_TYPE
    from fluxstudio.nodes import RunNode

    assert RunNode.OUTPUT_NODE is True
    assert RunNode.INPUT_TYPES()["required"]["runnable"] == (RUNNABLE_TYPE,)

    calls = []

    class _Runnable:
        def run(self) -> None:
            calls.append(True)

    status = RunNode().run(_Runnable())
    assert calls == [True]
    assert "_Runnable" in status[0] and "complete" in status[0]


def test_run_node_rejects_non_runnable() -> None:
    from fluxstudio.nodes import RunNode

    with pytest.raises(RuntimeError, match="no callable run"):
        RunNode().run(object())


def test_run_node_restores_autograd_for_torch_runner_under_inference_mode() -> None:
    # ComfyUI wraps graph execution in torch.inference_mode(); a TorchRunner (training) must get
    # normal autograd restored inside run() or loss.backward() fails. Simulate the wrapping and
    # assert a tensor built inside run() carries a grad graph.
    from fluxstudio.nodes import RunNode

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
        RunNode().run(_TorchRunnable())

    assert observed == {"grad_enabled": True, "inference": False, "has_grad": True}


def test_run_node_leaves_inference_mode_for_plain_runnable() -> None:
    # An inference-only runnable (no __torch_runner__) runs as-is under ComfyUI's inference mode.
    from fluxstudio.nodes import RunNode

    observed = {}

    class _PlainRunnable:
        def run(self) -> None:
            observed["inference"] = torch.is_inference_mode_enabled()

    with torch.inference_mode():
        RunNode().run(_PlainRunnable())

    assert observed["inference"] is True


def test_run_node_interrupt_stops_running_runnable() -> None:
    # The user pressed ComfyUI's interrupt button mid-run: RunNode polls the flag, injects a
    # KeyboardInterrupt into the worker thread, and re-raises the interrupt signal for a clean stop.
    import fluxstudio.nodes as nodes
    from fluxstudio.nodes import RunNode, _interrupt_exception_type

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
            RunNode().run(_SlowRunnable())

    assert ran_to_completion["value"] is False


def test_run_node_propagates_genuine_worker_exception() -> None:
    # A real error inside run() (not an interrupt) is surfaced to the caller, unchanged.
    from fluxstudio.nodes import RunNode

    class _Boom:
        def run(self) -> None:
            raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        RunNode().run(_Boom())


def test_run_node_grad_context_is_active_inside_worker_thread() -> None:
    # The grad restore for a TorchRunner must happen ON the worker thread (torch grad mode is
    # thread-local), and the work must run on a DIFFERENT thread than the caller.
    from fluxstudio.nodes import RunNode

    main_thread = threading.get_ident()
    observed = {}

    class _TorchRunnable:
        __torch_runner__ = True

        def run(self) -> None:
            observed["thread"] = threading.get_ident()
            observed["grad_enabled"] = torch.is_grad_enabled()
            observed["inference"] = torch.is_inference_mode_enabled()

    with torch.inference_mode():
        status = RunNode().run(_TorchRunnable())

    assert observed["thread"] != main_thread
    assert observed["grad_enabled"] is True
    assert observed["inference"] is False
    assert "_TorchRunnable" in status[0] and "complete" in status[0]
