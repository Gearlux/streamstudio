"""Graph validate / YAML round-trip routes."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from logflow import get_logger
from pydantic import BaseModel

from fluxstudio.graph import Graph

logger = get_logger(__name__)

router = APIRouter()


class YamlBody(BaseModel):
    yaml: str


@router.post("/graph/from-yaml")
def from_yaml(body: YamlBody) -> Dict[str, Any]:
    try:
        graph = Graph.from_confluid_yaml(body.yaml)
    except (ValueError, Exception) as exc:
        raise HTTPException(status_code=400, detail=f"Failed to parse YAML: {exc}")
    return graph.model_dump()


@router.post("/graph/to-yaml")
def to_yaml(graph: Graph) -> Dict[str, Any]:
    try:
        text = graph.to_confluid_yaml()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"yaml": text}
