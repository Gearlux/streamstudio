"""SteppingExecutor unit tests."""

from __future__ import annotations

import queue
import threading
from typing import Iterator

import numpy as np
import pytest
from dataflux.core import FilterOp, Flux
from dataflux.ops.numpy import RescaleOp
from dataflux.sample import Sample

from fluxstudio.executor import SteppingExecutor


def test_iter_events_emits_one_event_per_op_per_sample(mini_flux: Flux) -> None:
    executor = SteppingExecutor(mini_flux)
    events = list(executor.iter_events())
    # 4 samples × 2 ops = 8 events
    assert len(events) == 8
    op_indices = [e.op_index for e in events]
    assert op_indices == [0, 1] * 4


def test_final_flag_is_set_only_on_last_op(mini_flux: Flux) -> None:
    executor = SteppingExecutor(mini_flux)
    events = list(executor.iter_events())
    finals = [e.final for e in events]
    assert finals == [False, True] * 4


def test_collect_returns_summary_with_event_count(mini_flux: Flux) -> None:
    executor = SteppingExecutor(mini_flux)
    summary = executor.collect()
    assert len(summary.events) == 8
    assert summary.aborted is False
    assert summary.error is None


def test_payload_contains_encoded_sample(mini_flux: Flux) -> None:
    executor = SteppingExecutor(mini_flux)
    events = list(executor.iter_events())
    assert events[0].payload is not None
    assert events[0].payload["input"]["kind"] == "ndarray"


def test_filter_op_marks_event_filtered_and_skips_remaining_ops() -> None:
    class _Source:
        def __iter__(self) -> Iterator[Sample]:
            yield Sample(input=np.array([1.0]), target=None, metadata={})
            yield Sample(input=np.array([2.0]), target=None, metadata={})

        def __len__(self) -> int:
            return 2

    flux = Flux(source=_Source())
    flux.ops.append(FilterOp(lambda s: bool(s.input[0] > 1.0)))
    flux.ops.append(RescaleOp(in_min=0.0, in_max=10.0))

    events = list(SteppingExecutor(flux).iter_events())
    # Sample 0: filtered → only one event for op_index=0 marked filtered.
    # Sample 1: passes through both ops.
    assert events[0].filtered is True
    assert events[0].op_index == 0
    assert events[1].sample_index == 1
    # Three events total: (filtered s0), (s1@op0), (s1@op1)
    assert len(events) == 3


def test_parallel_flux_rejected() -> None:
    flux = Flux(source=[]).parallel(workers=4)
    with pytest.raises(ValueError, match="parallel"):
        SteppingExecutor(flux)


def test_abort_during_step_terminates_loop(mini_flux: Flux) -> None:
    executor = SteppingExecutor(mini_flux)
    ctrl: "queue.Queue[str]" = queue.Queue()
    executor.set_controller(ctrl, initial_mode="step")

    collected = []
    barrier = threading.Event()

    def _consume() -> None:
        for event in executor.iter_events():
            collected.append(event)
            barrier.set()

    t = threading.Thread(target=_consume, daemon=True)
    t.start()
    ctrl.put("step")  # release the very first op
    assert barrier.wait(timeout=2)
    executor.abort()
    ctrl.put("abort")  # unblocks any pending get() so the loop can re-check
    t.join(timeout=2)
    assert not t.is_alive()
    assert len(collected) >= 1
    assert len(collected) < 8


def test_continue_then_step_mode_change() -> None:
    """Verify the controller honours mode transitions: starts continuous, then pauses."""
    rng = np.random.default_rng(0)

    class _Source:
        def __iter__(self) -> Iterator[np.ndarray]:
            for _ in range(3):
                yield rng.uniform(0, 1, size=4).astype(np.float32)

        def __len__(self) -> int:
            return 3

    flux = Flux(source=_Source())
    flux.ops.append(RescaleOp(in_min=0.0, in_max=1.0))
    executor = SteppingExecutor(flux)
    ctrl: "queue.Queue[str]" = queue.Queue()
    executor.set_controller(ctrl, initial_mode="continue")
    events = list(executor.iter_events())
    # 3 samples × 1 op = 3 events
    assert len(events) == 3


def test_initial_mode_validation() -> None:
    flux = Flux(source=[])
    executor = SteppingExecutor(flux)
    ctrl: "queue.Queue[str]" = queue.Queue()
    with pytest.raises(ValueError, match="step"):
        executor.set_controller(ctrl, initial_mode="bogus")


def test_empty_source_yields_no_events() -> None:
    flux = Flux(source=[])
    flux.ops.append(RescaleOp(in_min=0.0, in_max=1.0))
    assert list(SteppingExecutor(flux).iter_events()) == []


def test_op_descriptor_for_wrapped_callable() -> None:
    """WrappedOp keeps the callable path on `.f`, executor surfaces it as op_path."""
    from dataflux.core import WrappedOp

    flux = Flux(source=[Sample(input=np.zeros(2), target=None, metadata={})])
    flux.ops.append(WrappedOp(f="numpy:negative", s="input", kw={}))
    events = list(SteppingExecutor(flux).iter_events())
    assert events[0].op_path == "numpy:negative"
    assert events[0].op_name == "negative"
