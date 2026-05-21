"""Single-process iterator-as-debugger for DataFlux pipelines.

The executor walks a `Flux` one op at a time, emitting a `StepEvent` after
each op produces a Sample. A control queue lets a caller (the FastAPI run
endpoint, or a unit test) drive `step` / `continue` / `abort`.

Reuses Flux's existing `_iter_sequential` semantics conceptually but does NOT
call it — we re-implement the inner loop here because we need to pause between
ops, which `_iter_sequential` does not expose. The reimplementation is tiny
(<30 lines) and deliberately mirrors `Flux._iter_sequential` /
`_worker_task` so behaviour stays consistent.

`.parallel(workers>1)` runs are explicitly rejected — multiprocess debugging
is a future feature (use `Tee` + sink + re-read for now).
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from dataflux.core import Flux, _check_ops_materialized
from dataflux.sample import Sample
from fluxstudio.sample_codec import encode_sample
from logflow import get_logger

logger = get_logger(__name__)


@dataclass
class StepEvent:
    """One event per op application. `payload` is the encoded post-op sample,
    or `None` if the op filtered the sample out (e.g. FilterOp).
    """

    sample_index: int
    op_index: int
    op_name: str
    op_path: str
    payload: Optional[Dict[str, Any]] = None
    filtered: bool = False
    final: bool = False


@dataclass
class RunSummary:
    """Returned when stepping completes (either normally or via abort)."""

    samples_consumed: int = 0
    events: List[StepEvent] = field(default_factory=list)
    aborted: bool = False
    error: Optional[str] = None


def _op_descriptor(op: Any) -> tuple[str, str]:
    """Pull a (display_name, dotted_path) tuple out of an arbitrary op.

    Live op classes have `__class__.__module__` + `__class__.__qualname__`;
    `WrappedOp` keeps the wrapped function's path on `.f`.
    """
    wrapped = getattr(op, "f", None)
    if isinstance(wrapped, str):
        return (wrapped.rsplit(":", 1)[-1], wrapped)
    cls = type(op)
    return (cls.__qualname__, f"{cls.__module__}:{cls.__qualname__}")


class SteppingExecutor:
    """Drive a `Flux` one op at a time.

    Threading model: the executor runs synchronously in `iter_events()`. A
    separate control queue (set via `set_controller`) is consulted before each
    op so a caller in another thread can pause/continue/abort. Tests use
    `iter_events()` without a controller — every op fires immediately.
    """

    _CMD_STEP = "step"
    _CMD_CONTINUE = "continue"
    _CMD_ABORT = "abort"

    def __init__(self, flux: Flux) -> None:
        if getattr(flux, "_workers", 1) > 1:
            raise ValueError(
                f"SteppingExecutor does not support parallel Flux runs "
                f"(_workers={flux._workers}). Drop .parallel() before debugging."
            )
        self.flux = flux
        self._controller: Optional["queue.Queue[str]"] = None
        self._mode: str = self._CMD_CONTINUE
        self._abort = threading.Event()

    def set_controller(
        self, ctrl: "queue.Queue[str]", initial_mode: str = "step"
    ) -> None:
        """Enable pause/resume by polling `ctrl` before each op.

        `initial_mode='step'` makes the executor block on the first op until
        the caller posts a command; `'continue'` runs to completion until told
        otherwise.
        """
        if initial_mode not in (self._CMD_STEP, self._CMD_CONTINUE):
            raise ValueError(
                f"initial_mode must be 'step' or 'continue', got {initial_mode!r}"
            )
        self._controller = ctrl
        self._mode = initial_mode

    def abort(self) -> None:
        """Signal abort. Safe to call from any thread."""
        self._abort.set()

    def _wait_for_command(self) -> Optional[str]:
        """Block until the controller releases us. Returns the command we got
        ('step' or 'continue'), or None if no controller is wired (free-running).
        """
        if self._controller is None:
            return None
        if self._mode == self._CMD_CONTINUE:
            try:
                cmd = self._controller.get_nowait()
            except queue.Empty:
                return self._CMD_CONTINUE
        else:
            cmd = self._controller.get()
        if cmd == self._CMD_ABORT:
            self._abort.set()
        elif cmd in (self._CMD_STEP, self._CMD_CONTINUE):
            self._mode = cmd
        return cmd

    def iter_events(self) -> Iterator[StepEvent]:
        """Walk the Flux, yielding one StepEvent per op-per-sample."""
        source = self.flux._guard_live_source()
        if source is None:
            return
        ops = list(self.flux.ops)
        _check_ops_materialized(ops)

        for sample_index, raw in enumerate(source):
            if self._abort.is_set():
                return
            current: Optional[Sample] = Sample.from_any(raw)
            for op_index, op in enumerate(ops):
                self._wait_for_command()
                if self._abort.is_set():
                    return
                op_name, op_path = _op_descriptor(op)
                if current is None:
                    yield StepEvent(
                        sample_index=sample_index,
                        op_index=op_index,
                        op_name=op_name,
                        op_path=op_path,
                        payload=None,
                        filtered=True,
                    )
                    break
                result = op(current)
                event = StepEvent(
                    sample_index=sample_index,
                    op_index=op_index,
                    op_name=op_name,
                    op_path=op_path,
                    filtered=result is None,
                    final=op_index == len(ops) - 1 and result is not None,
                    payload=encode_sample(result) if result is not None else None,
                )
                yield event
                current = result
                if current is None:
                    break

    def collect(self) -> RunSummary:
        """Run the pipeline to completion (no controller) and return everything emitted."""
        summary = RunSummary()
        try:
            for event in self.iter_events():
                summary.events.append(event)
                if event.op_index == 0:
                    summary.samples_consumed += 1 if not event.filtered else 0
        except Exception as exc:  # pragma: no cover - defensive
            summary.error = repr(exc)
        summary.aborted = self._abort.is_set()
        return summary
