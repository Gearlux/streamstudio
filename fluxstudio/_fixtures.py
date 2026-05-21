"""Deterministic data sources used by FluxStudio examples and tests.

Kept inside the package (not in `tests/`) so they are importable from a
Confluid YAML document — `!class:fluxstudio._fixtures:NumericArraySource`
must resolve at runtime, not just under pytest.
"""

from __future__ import annotations

from typing import Iterator

import numpy as np

from confluid import configurable


@configurable
class NumericArraySource:
    """Yield `count` deterministic float32 arrays of length `size` in [0, 255].

    Used as the source of `examples/tiny_pipeline.yaml`. Produces values that
    `RescaleOp(in_min=0, in_max=255)` can normalize cleanly.
    """

    def __init__(self, count: int = 4, size: int = 8, seed: int = 0) -> None:
        self.count = int(count)
        self.size = int(size)
        self.seed = int(seed)

    def __iter__(self) -> Iterator[np.ndarray]:
        rng = np.random.default_rng(self.seed)
        for _ in range(self.count):
            yield rng.uniform(0.0, 255.0, size=self.size).astype(np.float32)

    def __len__(self) -> int:
        return self.count
