"""FluxStudio CLI entry point.

    fluxstudio discover                 # print discovered op palette as JSON
    fluxstudio run <yaml>               # step through a Confluid pipeline, NDJSON to stdout
    fluxstudio serve [--port 8765]      # start the FastAPI server
"""

from __future__ import annotations

import dataclasses
import json
import sys
from typing import Any

import uvicorn
from confluid.loader import load as confluid_load
from liquifai import LiquifyApp
from logflow import get_logger

from fluxstudio.discovery import scan_packages
from fluxstudio.executor import SteppingExecutor
from fluxstudio.server import create_app

logger = get_logger(__name__)

app = LiquifyApp(name="fluxstudio", description="Visual editor + step debugger for DataFlux pipelines.")


@app.command(name="discover")
def discover() -> None:
    """Print op palette JSON to stdout."""
    schemas = scan_packages()
    json.dump(schemas, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


@app.script_command(name="run", flow_mode="manual")
def run() -> None:
    """Step through a Confluid pipeline and emit one NDJSON event per op application.

    The pipeline YAML is supplied as the positional argument right after
    ``run`` and is picked up by Liquify's config-promotion convention
    (see ``LiquifyApp.script_command``).
    """
    if app.context is None or app.context.config_path is None:
        logger.error("Pipeline YAML required: fluxstudio run <yaml>")
        sys.exit(2)
    path = app.context.config_path
    flux: Any = confluid_load(path.read_text())
    executor = SteppingExecutor(flux)
    for event in executor.iter_events():
        json.dump(dataclasses.asdict(event), sys.stdout, default=str)
        sys.stdout.write("\n")


@app.command(name="serve")
def serve(host: str = "127.0.0.1", port: int = 8765, reload: bool = False) -> None:
    """Start the FluxStudio HTTP/WebSocket server."""
    if reload:
        # Uvicorn's reload loader needs an import string, not an app instance.
        uvicorn.run("fluxstudio.server:create_app", host=host, port=port, reload=True, factory=True)
    else:
        uvicorn.run(create_app(), host=host, port=port)


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()
