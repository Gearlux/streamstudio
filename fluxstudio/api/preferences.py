"""Routes for reading and patching user preferences."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from logflow import get_logger
from pydantic import ValidationError

from fluxstudio.preferences import Preferences, load_preferences, update_preferences

logger = get_logger(__name__)

router = APIRouter()


@router.get("/preferences")
def get_preferences() -> Preferences:
    return load_preferences()


@router.put("/preferences")
def put_preferences(partial: Dict[str, Any]) -> Preferences:
    try:
        return update_preferences(partial)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.errors())
