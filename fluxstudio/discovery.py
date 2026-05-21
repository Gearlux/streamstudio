"""Discover ops, sources, sinks and visualizers across DataFlux + Waivefront.

Wraps `dataflux.discovery.scan_module` and tags each result with a category
(DataSource | DataOp | DataSink | Visualizer) so the FluxStudio palette can
group nodes the same way navigaitor's MCP server does.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from dataflux.discovery import scan_module
from logflow import get_logger

logger = get_logger(__name__)

DEFAULT_MODULES: tuple[str, ...] = (
    "dataflux.ops.torch",
    "dataflux.ops.numpy",
    "dataflux.ops.tee",
    "dataflux.ops.copy",
    "dataflux.ops.swap",
    "dataflux.ops.stash",
    "dataflux.storage.hdf5",
    "dataflux.storage.zarr",
    "dataflux.storage.directory",
    "waivefront.processing",
    "waivefront.visualizers",
    "waivefront.sinks",
)


def _categorize(module: str, name: str) -> str:
    lower_name = name.lower()
    if "source" in module.lower() or lower_name.endswith("source"):
        return "DataSource"
    if lower_name.endswith("sink"):
        return "DataSink"
    if "visualiz" in module.lower() or lower_name.startswith(
        ("render", "plot", "saveimage", "spectrogram")
    ):
        return "Visualizer"
    return "DataOp"


def scan_packages(modules: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    """Return op schemas for every module in `modules` (or DEFAULT_MODULES).

    Modules that fail to import are logged and skipped — DataFlux is mandatory,
    Waivefront is optional, and a missing optional module is not a fatal error.
    """
    targets = tuple(modules) if modules is not None else DEFAULT_MODULES
    schemas: List[Dict[str, Any]] = []
    for module in targets:
        results = scan_module(module)
        if not results:
            logger.debug(
                f"discovery: module {module!r} produced no schemas (missing or empty)"
            )
            continue
        for schema in results:
            path = schema.get("path", "")
            mod_part = path.split(":", 1)[0] if ":" in path else module
            schema_with_meta: Dict[str, Any] = {
                **schema,
                "module": mod_part,
                "category": _categorize(mod_part, schema.get("name", "")),
            }
            schemas.append(schema_with_meta)
    return schemas
