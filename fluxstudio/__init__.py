"""ComfyUI Entry Point for Taidal/Waivefront nodes."""

import importlib.metadata
from typing import Any, Dict

from logflow import get_logger

from fluxstudio.bridge import register_module_nodes
from fluxstudio.nodes import NODE_CLASS_MAPPINGS as _STATIC_NODE_CLASS_MAPPINGS
from fluxstudio.nodes import NODE_DISPLAY_NAME_MAPPINGS as _STATIC_NODE_DISPLAY_NAME_MAPPINGS

logger = get_logger(__name__)

NODE_CLASS_MAPPINGS: Dict[str, Any] = {}
NODE_DISPLAY_NAME_MAPPINGS: Dict[str, str] = {}


def list_cached_hf_datasets() -> list:
    """Return sorted repo ids of HuggingFace **datasets** already in the local cache.

    Backs the ``/taidal/hf_datasets`` route + the HF dataset-id picker
    (``web/hf_dataset_chooser.js``). Degrades to an empty list on ANY failure
    (``huggingface_hub`` not installed, no cache dir, scan error) — the picker is
    a convenience over a free-text field, so it must never break node discovery
    or the editor.
    """
    try:
        from huggingface_hub import scan_cache_dir

        info = scan_cache_dir()
        ids = {repo.repo_id for repo in info.repos if getattr(repo, "repo_type", None) == "dataset"}
        return sorted(ids)
    except Exception as e:
        logger.debug(f"list_cached_hf_datasets: returning [] ({type(e).__name__}: {e})")
        return []


def _discover_and_register_nodes() -> None:
    """Dynamically discover and register ComfyUI nodes via entry points."""
    try:
        # Python 3.10+ entry_points API
        eps = importlib.metadata.entry_points(group="confluid.configurables")
    except Exception as e:
        logger.error(f"Failed to load entry points: {e}")
        return

    for ep in eps:
        mod_path = ep.value
        # Create a nested category based on the module path (e.g., "waivefront.sinks" -> "Taidal/Waivefront/Sinks")
        parts = mod_path.split(".")
        category = "Taidal/" + "/".join(p.title() for p in parts)

        try:
            register_module_nodes(mod_path, NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS, category=category)
        except Exception as e:
            # We use debug instead of warning here because not all confluid modules
            # will have valid ComfyUI ops, and we don't want to spam the user's startup log.
            logger.debug(f"Skipped module {mod_path} during ComfyUI node generation: {e}")


_discover_and_register_nodes()

# Merge the statically-defined FluxStudio adapter nodes (dataset walker + sample viewer) on top of
# the auto-discovered DataFlux/Waivefront nodes.
NODE_CLASS_MAPPINGS.update(_STATIC_NODE_CLASS_MAPPINGS)
NODE_DISPLAY_NAME_MAPPINGS.update(_STATIC_NODE_DISPLAY_NAME_MAPPINGS)

WEB_DIRECTORY = "./web"

# Add backend route for the directory chooser if running inside ComfyUI
try:
    import subprocess

    import server  # type: ignore[import-not-found]
    from aiohttp import web

    route_exists = any(
        r.path == "/taidal/choose_directory" and r.method == "POST" for r in server.PromptServer.instance.routes
    )

    if not route_exists:

        @server.PromptServer.instance.routes.post("/taidal/choose_directory")
        async def choose_directory_handler(request: Any) -> Any:
            try:
                # Pop up native macOS folder picker on the host machine
                result = subprocess.run(
                    ["osascript", "-e", 'POSIX path of (choose folder with prompt "Select a directory:")'],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                folder_path = result.stdout.strip()
                return web.json_response({"path": folder_path})
            except subprocess.CalledProcessError:
                # User likely clicked Cancel in the dialog
                return web.json_response({"path": ""})
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)

    # List locally-cached HuggingFace datasets so the HF dataset-id field
    # (web/hf_dataset_chooser.js) can offer them as an optional picker. Free-text entry
    # is preserved on the widget itself; this only surfaces what's already downloaded.
    hf_route_exists = any(
        r.path == "/taidal/hf_datasets" and r.method == "GET" for r in server.PromptServer.instance.routes
    )
    if not hf_route_exists:

        @server.PromptServer.instance.routes.get("/taidal/hf_datasets")
        async def hf_datasets_handler(request: Any) -> Any:
            return web.json_response({"datasets": list_cached_hf_datasets()})

    # Serve each node's declared input/output type spec so the canvas connection-validator
    # (web/connection_validator.js) can reject incompatible Sample wires live in the editor.
    specs_route_exists = any(
        r.path == "/taidal/node_specs" and r.method == "GET" for r in server.PromptServer.instance.routes
    )
    if not specs_route_exists:

        @server.PromptServer.instance.routes.get("/taidal/node_specs")
        async def node_specs_handler(request: Any) -> Any:
            specs = {
                node_name: {
                    "accepts": getattr(node_cls, "TAIDAL_ACCEPTS", None),
                    "produces": getattr(node_cls, "TAIDAL_PRODUCES", None),
                }
                for node_name, node_cls in NODE_CLASS_MAPPINGS.items()
            }
            return web.json_response(specs)

except ImportError:
    # Not running inside ComfyUI
    pass

# Export for ComfyUI
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
