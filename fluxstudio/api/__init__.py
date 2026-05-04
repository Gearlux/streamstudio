"""FastAPI routers for FluxStudio."""

from fluxstudio.api.graph import router as graph_router
from fluxstudio.api.ops import router as ops_router
from fluxstudio.api.preferences import router as preferences_router
from fluxstudio.api.runs import router as runs_router

__all__ = ["graph_router", "ops_router", "preferences_router", "runs_router"]
