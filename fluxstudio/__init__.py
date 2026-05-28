"""ComfyUI Entry Point for Taidal/Waivefront nodes."""

import importlib.metadata
from typing import Any, Dict

from logflow import get_logger

from fluxstudio.bridge import register_module_nodes

logger = get_logger(__name__)

NODE_CLASS_MAPPINGS: Dict[str, Any] = {}
NODE_DISPLAY_NAME_MAPPINGS: Dict[str, str] = {}


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

except ImportError:
    # Not running inside ComfyUI
    pass

# Export for ComfyUI
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
