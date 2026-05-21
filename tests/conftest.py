"""Shared fixtures for FluxStudio tests."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import numpy as np
import pytest

from dataflux.core import Flux
from dataflux.ops.numpy import RescaleOp, StandardizeOp
from dataflux.sample import Sample


@pytest.fixture
def tiny_pipeline_yaml() -> Path:
    return Path(__file__).resolve().parent.parent / "examples" / "tiny_pipeline.yaml"


@pytest.fixture
def sample_array() -> np.ndarray:
    return np.linspace(0.0, 255.0, num=8, dtype=np.float32)


@pytest.fixture
def mini_flux() -> Flux:
    """A 4-sample, 2-op Flux built in-process so executor tests stay independent of YAML."""

    def _source() -> Iterator[np.ndarray]:
        for i in range(4):
            yield np.full((4,), fill_value=float(i * 64), dtype=np.float32)

    class _Source:
        def __iter__(self) -> Iterator[np.ndarray]:
            return _source()

        def __len__(self) -> int:
            return 4

    flux = Flux(source=_Source())
    flux.ops.extend(
        [RescaleOp(in_min=0.0, in_max=255.0), StandardizeOp(mean=0.5, std=0.5)]
    )
    return flux


@pytest.fixture
def numpy_sample(sample_array: np.ndarray) -> Sample:
    return Sample(input=sample_array, target=None, metadata={"source": "fixture"})
