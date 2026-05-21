"""FastAPI application factory for FluxStudio.

Use `create_app()` everywhere — including tests, examples, and the
`fluxstudio serve` CLI entry point. The factory wires every router under
`/api/*` and mounts the frontend at `/` (or a friendly placeholder if it
hasn't been built).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from fluxstudio.api import graph_router, ops_router, preferences_router, runs_router
from fluxstudio.static import mount_frontend
from logflow import get_logger

logger = get_logger(__name__)


def create_app() -> FastAPI:
    """Build the FastAPI app. Idempotent (each call returns a fresh instance)."""
    app = FastAPI(title="FluxStudio", version="0.1.0")
    # Vite dev server runs on a different port; permissive CORS for local dev.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(ops_router, prefix="/api", tags=["ops"])
    app.include_router(graph_router, prefix="/api", tags=["graph"])
    app.include_router(runs_router, prefix="/api", tags=["runs"])
    app.include_router(preferences_router, prefix="/api", tags=["preferences"])
    mount_frontend(app)
    return app
