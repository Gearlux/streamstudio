"""Tests for ``fluxstudio.nodes`` — the WalkDataset stepper and SampleViewer adapter nodes.

These are ComfyUI adapter glue, so the tests drive them headlessly: ``folder_paths`` (a
ComfyUI-only module) is absent during the test run, so ``SampleViewerNode`` skips the on-disk
preview while still returning the IMAGE tensor + info; one test injects a fake ``folder_paths``
into ``sys.modules`` to exercise the PNG-save path.
"""

import sys
import types
from pathlib import Path
from typing import Any, Tuple
from unittest.mock import patch

import numpy as np
import pytest
import torch
from dataflux.sample import Sample

from fluxstudio.nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS, SampleViewerNode, WalkDatasetNode


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
    assert idx[1]["control_after_generate"] is True


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
# SampleViewerNode
# --------------------------------------------------------------------------- #


def test_viewer_input_types_take_a_sample_socket() -> None:
    required = SampleViewerNode.INPUT_TYPES()["required"]
    assert required["sample"] == ("DATAFLUX_SAMPLE",)
    assert required["colormap"][0] == "STRING"
    assert required["max_size"][0] == "INT"
    assert SampleViewerNode.OUTPUT_NODE is True


def test_viewer_returns_image_tensor_and_info_headless() -> None:
    # folder_paths is not importable here -> on-disk preview skipped, result still produced.
    sample = Sample(input=np.zeros((8, 8, 3), dtype="uint8"), target=7, metadata={"k": "v"})
    out = SampleViewerNode().view(sample=sample)
    image, info = out["result"]
    assert isinstance(image, torch.Tensor)
    assert image.shape[0] == 1 and image.shape[-1] == 3 and image.dtype == torch.float32
    assert 0.0 <= float(image.min()) and float(image.max()) <= 1.0
    assert "target=7" in info and "k=v" in info
    assert out["ui"]["images"] == []  # headless: no preview file written
    assert out["ui"]["text"] == [info]


def test_viewer_saves_preview_when_folder_paths_present(tmp_path: Path) -> None:
    fake_fp = types.ModuleType("folder_paths")

    def _temp_dir() -> str:
        return str(tmp_path)

    def _save_path(prefix: str, out_dir: str, width: int, height: int) -> Tuple[Any, ...]:
        return str(tmp_path), "img", 1, "", prefix

    fake_fp.get_temp_directory = _temp_dir  # type: ignore[attr-defined]
    fake_fp.get_save_image_path = _save_path  # type: ignore[attr-defined]

    sample = Sample(input=np.zeros((8, 8, 3), dtype="uint8"), target=0)
    with patch.dict(sys.modules, {"folder_paths": fake_fp}):
        out = SampleViewerNode().view(sample=sample)

    imgs = out["ui"]["images"]
    assert len(imgs) == 1 and imgs[0]["type"] == "temp"
    assert (tmp_path / imgs[0]["filename"]).exists()


# --------------------------------------------------------------------------- #
# Static registration mappings
# --------------------------------------------------------------------------- #


def test_static_node_mappings_present() -> None:
    assert NODE_CLASS_MAPPINGS["Taidal_WalkDataset"] is WalkDatasetNode
    assert NODE_CLASS_MAPPINGS["Taidal_ShowSample"] is SampleViewerNode
    assert NODE_DISPLAY_NAME_MAPPINGS["Taidal_WalkDataset"] == "Walk Dataset (by index)"
    assert NODE_DISPLAY_NAME_MAPPINGS["Taidal_ShowSample"] == "Show Sample"
