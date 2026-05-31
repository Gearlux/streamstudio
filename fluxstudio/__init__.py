"""ComfyUI Entry Point for Taidal/Waivefront nodes."""

import importlib.metadata
from typing import Any, Dict, List, Optional

from logflow import get_logger

from fluxstudio.bridge import register_module_nodes
from fluxstudio.export import UnsupportedTopologyError, export_ops_yaml
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


def list_hf_dataset_features(path: str, name: Optional[str] = None, split: Optional[str] = None) -> List[str]:
    """Return the feature/column names of a HuggingFace dataset (e.g. ``["image", "label"]``).

    Backs the ``/taidal/hf_dataset_features`` route + the feature picker
    (``web/hf_feature_chooser.js``), which lets a ``HuggingFace*`` source node's
    ``input_feature`` / ``target_feature`` STRING fields be filled from a dropdown of
    the *selected* dataset's actual columns instead of typed blind — the cached-dataset
    picker one level down (the columns of the chosen dataset, not the list of datasets).

    Tries the cheap metadata-only path first (``load_dataset_builder().info.features``,
    no row download), then falls back to streaming a single example (handles datasets
    whose ``info.features`` is unset until a split is prepared, e.g. local imagefolders).
    Column order is preserved (more useful than alphabetical for picking input vs target).
    Degrades to an empty list on ANY failure (``datasets`` missing, unknown id, offline) —
    the picker is a convenience over a free-text field and must never break the editor.
    """
    if not path:
        return []
    # 1. Cheap path: the dataset builder's info carries the feature schema without
    #    downloading any rows.
    try:
        from datasets import load_dataset_builder

        builder = load_dataset_builder(path, name=name or None)
        features = getattr(builder.info, "features", None)
        if features:
            return list(features.keys())
    except Exception as e:
        logger.debug(f"list_hf_dataset_features: builder path failed ({type(e).__name__}: {e})")
    # 2. Fallback: stream one example and read its keys.
    try:
        from datasets import load_dataset

        stream = load_dataset(path, name=name or None, split=split or "train", streaming=True)
        first = next(iter(stream))
        return list(first.keys())
    except Exception as e:
        logger.debug(f"list_hf_dataset_features: streaming fallback failed ({type(e).__name__}: {e})")
        return []


def list_hf_dataset_splits(path: str, name: Optional[str] = None) -> List[str]:
    """Return the available split names of a HuggingFace dataset (e.g. ``["train", "test"]``).

    Backs the ``/taidal/hf_dataset_splits`` route + the split picker
    (``web/hf_split_chooser.js``), which fills a ``HuggingFace*`` source node's ``split`` STRING
    field from a dropdown of the *selected* dataset's actual splits. The field stays free-text
    (HF supports slice syntax like ``train[:90%]`` / ``train+test``); this only offers the
    enumerated splits. Metadata-only (``datasets.get_dataset_split_names``, no row download).
    Degrades to an empty list on ANY failure (``datasets`` missing, unknown id, offline) — the
    picker is a convenience over a free-text field and must never break the editor.
    """
    if not path:
        return []
    try:
        from datasets import get_dataset_split_names

        return list(get_dataset_split_names(path, config_name=name or None))
    except Exception as e:
        logger.debug(f"list_hf_dataset_splits: returning [] ({type(e).__name__}: {e})")
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
        # The palette path per node is ``Taidal/<Package>/<Category>[/<group>]`` — built inside
        # register_module_nodes from the module's top-level package + each class's confluid
        # category/group (see bridge._node_category). We only pass the module to scan.
        try:
            register_module_nodes(mod_path, NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS)
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

    # List the feature/column names of the dataset named in a node's HF dataset-id field so the
    # feature picker (web/hf_feature_chooser.js) can offer them for input_feature / target_feature
    # instead of free-text. Query params: path (required), name (HF subset), split.
    features_route_exists = any(
        r.path == "/taidal/hf_dataset_features" and r.method == "GET" for r in server.PromptServer.instance.routes
    )
    if not features_route_exists:

        @server.PromptServer.instance.routes.get("/taidal/hf_dataset_features")
        async def hf_dataset_features_handler(request: Any) -> Any:
            query = request.query
            path = query.get("path", "")
            name = query.get("name") or None
            split = query.get("split") or None
            return web.json_response({"features": list_hf_dataset_features(path, name=name, split=split)})

    # List the available split names of the dataset named in a node's HF dataset-id field so the
    # split picker (web/hf_split_chooser.js) can offer them for the `split` field. Query params:
    # path (required), name (HF subset).
    splits_route_exists = any(
        r.path == "/taidal/hf_dataset_splits" and r.method == "GET" for r in server.PromptServer.instance.routes
    )
    if not splits_route_exists:

        @server.PromptServer.instance.routes.get("/taidal/hf_dataset_splits")
        async def hf_dataset_splits_handler(request: Any) -> Any:
            query = request.query
            path = query.get("path", "")
            name = query.get("name") or None
            return web.json_response({"splits": list_hf_dataset_splits(path, name=name)})

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

    # Compile the current canvas's ops into a Confluid YAML op-list (drops the source +
    # Walk/Extract adapters) so it can be reattached to any source via
    # ``Flux.from_ops_yaml`` / ``confluid.materialize(confluid.load(path)["ops"])``.
    # Backs web/export_ops.js: the button POSTs ``{prompt: graphToPrompt(), path?: ...}``.
    export_route_exists = any(
        r.path == "/taidal/export_ops" and r.method == "POST" for r in server.PromptServer.instance.routes
    )
    if not export_route_exists:

        @server.PromptServer.instance.routes.post("/taidal/export_ops")
        async def export_ops_handler(request: Any) -> Any:
            try:
                body = await request.json()
                prompt = body.get("prompt", body)  # accept a bare prompt or {prompt: ...}
                yaml_doc = export_ops_yaml(prompt, NODE_CLASS_MAPPINGS)
                out_path = body.get("path")
                if out_path:
                    with open(out_path, "w") as fh:
                        fh.write(yaml_doc)
                    return web.json_response({"yaml": yaml_doc, "path": out_path})
                return web.json_response({"yaml": yaml_doc})
            except UnsupportedTopologyError as e:
                # 400: the graph is structurally not exportable (fan-in, multi-head, …) —
                # an actionable user error, not a server fault.
                return web.json_response({"error": str(e)}, status=400)
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)

except ImportError:
    # Not running inside ComfyUI
    pass

# Export for ComfyUI
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
