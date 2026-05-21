"""Run lifecycle and per-run WebSocket streaming.

Cross-loop coordination note: the worker thread shares state with the WS
handler **only** through the `history` list and a `threading.Event`. We
deliberately avoid `asyncio.Queue` + `run_coroutine_threadsafe` because
FastAPI's TestClient creates a fresh event loop per request, leaving an
asyncio.Queue's bound loop dead between calls. A polling WS handler that
reads `history` is slightly less elegant but works identically under
TestClient and a long-lived uvicorn process.
"""

from __future__ import annotations

import asyncio
import dataclasses
import queue
import threading
import uuid
from typing import Any, Dict, List, Optional

from confluid.loader import load as confluid_load
from dataflux.core import Flux
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from logflow import get_logger
from pydantic import BaseModel

from fluxstudio.executor import StepEvent, SteppingExecutor

logger = get_logger(__name__)

router = APIRouter()

_WS_POLL_INTERVAL_S = 0.02


class _Run:
    """In-process run handle. The worker thread appends every event to
    `history`; consumers replay from `history` and watch `done` for completion.
    """

    def __init__(self, run_id: str, executor: SteppingExecutor) -> None:
        self.run_id = run_id
        self.executor = executor
        self.control: "queue.Queue[str]" = queue.Queue()
        self.history: List[Dict[str, Any]] = []
        self.thread: Optional[threading.Thread] = None
        self.done = threading.Event()
        self.error: Optional[str] = None

    def start(self, mode: str) -> None:
        self.executor.set_controller(self.control, initial_mode=mode)

        def _run() -> None:
            try:
                for event in self.executor.iter_events():
                    self.history.append(dataclasses.asdict(event))
            except Exception as exc:  # pragma: no cover - defensive
                self.error = repr(exc)
            finally:
                self.done.set()

        self.thread = threading.Thread(target=_run, name=f"run-{self.run_id}", daemon=True)
        self.thread.start()


_RUN_REGISTRY: Dict[str, _Run] = {}


def get_run(run_id: str) -> _Run:
    run = _RUN_REGISTRY.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Unknown run {run_id!r}")
    return run


class RunRequest(BaseModel):
    yaml: str
    mode: str = "step"  # 'step' (pause before each op) or 'continue' (run to completion)


class RunResponse(BaseModel):
    run_id: str


@router.post("/run", response_model=RunResponse)
async def start_run(req: RunRequest) -> RunResponse:
    if req.mode not in ("step", "continue"):
        raise HTTPException(
            status_code=400,
            detail=f"mode must be 'step' or 'continue', got {req.mode!r}",
        )
    try:
        flux = confluid_load(req.yaml)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Confluid failed to load YAML: {exc}")

    if not isinstance(flux, Flux):
        raise HTTPException(
            status_code=400,
            detail=f"YAML did not produce a Flux instance (got {type(flux).__name__}).",
        )

    try:
        executor = SteppingExecutor(flux)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    run_id = uuid.uuid4().hex
    run = _Run(run_id=run_id, executor=executor)
    _RUN_REGISTRY[run_id] = run
    run.start(mode=req.mode)
    return RunResponse(run_id=run_id)


class ControlBody(BaseModel):
    command: str  # 'step' | 'continue' | 'abort'


@router.post("/run/{run_id}/control")
def control(run_id: str, body: ControlBody) -> Dict[str, Any]:
    if body.command not in ("step", "continue", "abort"):
        raise HTTPException(status_code=400, detail=f"Unknown command {body.command!r}")
    run = get_run(run_id)
    if body.command == "abort":
        run.executor.abort()
    run.control.put(body.command)
    return {"ok": True, "run_id": run_id, "command": body.command}


@router.get("/run/{run_id}/history")
def history(run_id: str) -> Dict[str, Any]:
    run = get_run(run_id)
    return {
        "run_id": run_id,
        "events": list(run.history),
        "done": run.done.is_set(),
        "error": run.error,
    }


@router.websocket("/run/{run_id}/ws")
async def stream(ws: WebSocket, run_id: str) -> None:
    await ws.accept()
    run = _RUN_REGISTRY.get(run_id)
    if run is None:
        await ws.close(code=4404)
        return
    sent = 0
    try:
        while True:
            # Drain any new events appended since our last send.
            current_len = len(run.history)
            while sent < current_len:
                await ws.send_json(run.history[sent])
                sent += 1
                # Yield to the event loop after every send so starlette's
                # TestClient WS portal can drain its queue.
                await asyncio.sleep(0)
            if run.done.is_set() and sent >= len(run.history):
                # Sentinel uses 'event_kind' (not 'final') because StepEvent
                # already carries a boolean 'final' field meaning "last op of
                # the pipeline" — collision would make clients break early on
                # mid-stream events.
                await ws.send_json({"event_kind": "stream_end"})
                break
            await asyncio.sleep(_WS_POLL_INTERVAL_S)
    except WebSocketDisconnect:  # pragma: no cover - tested via TestClient
        return
    finally:
        try:
            await ws.close()
        except Exception:  # pragma: no cover
            pass


# Convenience helpers (used by tests; not part of the public API).


def _reset_registry_for_tests() -> None:
    _RUN_REGISTRY.clear()


def _registry_size() -> int:
    return len(_RUN_REGISTRY)


__all__ = [
    "router",
    "StepEvent",
    "RunRequest",
    "RunResponse",
    "ControlBody",
    "_reset_registry_for_tests",
    "_registry_size",
]
