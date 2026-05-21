"""Routes that expose the op palette and visualizer registry."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

from fluxstudio.discovery import DEFAULT_MODULES, scan_packages
from fluxstudio.visualizers import list_encoders
from logflow import get_logger

logger = get_logger(__name__)

router = APIRouter()


@router.get("/ops")
def list_ops(modules: Optional[str] = None) -> Dict[str, Any]:
    """Return op schemas grouped by category for the node palette.

    `modules` is an optional comma-separated override. When omitted, the
    DEFAULT_MODULES from `fluxstudio.discovery` are scanned.
    """
    targets: Optional[List[str]] = None
    if modules:
        targets = [m.strip() for m in modules.split(",") if m.strip()]
        if not targets:
            raise HTTPException(
                status_code=400, detail="Empty 'modules' query parameter."
            )
    schemas = scan_packages(targets)
    return {
        "modules_scanned": targets if targets is not None else list(DEFAULT_MODULES),
        "schemas": schemas,
    }


@router.get("/visualizers")
def list_visualizers() -> Dict[str, Any]:
    """Return the catalog of registered type-hint and type encoders."""
    return list_encoders()
