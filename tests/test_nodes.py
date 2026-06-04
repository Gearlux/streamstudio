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
    _RETAG_KEEP,
    _RETAG_NONE,
    ANY,
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
    RetagObjectNode,
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
