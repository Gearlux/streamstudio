"""ComfyUI Entry Point for Taidal/Waivefront nodes."""

import importlib.metadata
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import loggair

# StreamStudio runs inside ComfyUI: its logs land in ComfyUI's own console/log
# capture, where ANSI color escapes are noise. force_no_color() sets the standard
# NO_COLOR=1 AND reloads loggair if some earlier ComfyUI import already configured
# it (inheriting/copying the env is NOT enough once a coloring decision is baked
# into the console sink). Must run before the get_logger() / streamstudio.* imports
# below that pull loggair in. To re-allow colors, pre-set NO_COLOR= (empty).
# See loggair's force_no_color.
loggair.force_no_color()

from loggair import get_logger  # (import follows force_no_color above)

from streamstudio.bridge import (
    hide_default_comfy_nodes,
    kept_default_node_names,
    only_taidal_nodes,
    register_module_nodes,
)
from streamstudio.export import UnsupportedTopologyError, chain_output_paths, collect_export_marks, export_flow_yamls
from streamstudio.nodes import NODE_CLASS_MAPPINGS as _STATIC_NODE_CLASS_MAPPINGS
from streamstudio.nodes import NODE_DISPLAY_NAME_MAPPINGS as _STATIC_NODE_DISPLAY_NAME_MAPPINGS
from streamstudio.nodes import build_item_type_nodes as _build_item_type_nodes

logger = get_logger(__name__)

NODE_CLASS_MAPPINGS: Dict[str, Any] = {}
NODE_DISPLAY_NAME_MAPPINGS: Dict[str, str] = {}


def cached_hf_datasets() -> "tuple[List[str], str]":
    """``(dataset repo ids, reason)`` — the local HF cache, and why it is empty when it is.

    Backs the ``/taidal/hf_datasets`` route + the HF dataset-id picker
    (``web/hf_dataset_chooser.js``). Still degrades to an empty list on ANY failure — the
    picker is a convenience over a free-text field and must never break the editor — but the
    REASON travels with it, because an empty picker that explains nothing is indistinguishable
    from a broken one. The canonical case: ``HF_HOME`` changed, the process still holds the
    OLD value (``huggingface_hub`` reads it at import), and the picker silently shows nothing.
    """
    try:
        from huggingface_hub import scan_cache_dir

        info = scan_cache_dir()
        ids = {repo.repo_id for repo in info.repos if getattr(repo, "repo_type", None) == "dataset"}
        if not ids:
            return [], f"no datasets cached at {_hf_cache_dir()}"
        return sorted(ids), ""
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        # A MISSING cache directory is actionable and is what a changed HF_HOME looks like from
        # here, so it is a warning naming the path this process is actually reading — the value
        # is read at import, so a running editor keeps the one it was started with.
        if type(exc).__name__ == "CacheNotFound":
            logger.warning(
                f"the HuggingFace cache directory does not exist: {_hf_cache_dir()} "
                f"(this process was started with HF_HOME={os.environ.get('HF_HOME', '<unset>')}; "
                "restart the editor after changing it)"
            )
        else:
            logger.debug(f"cached_hf_datasets: returning [] ({reason})")
        return [], reason


def _hf_cache_dir() -> str:
    """The hub cache path THIS process resolved — read at import, so it can be stale."""
    try:
        from huggingface_hub import constants

        return str(constants.HF_HUB_CACHE)
    except Exception:  # pragma: no cover - huggingface_hub absent
        return "<huggingface_hub not installed>"


def list_cached_hf_datasets() -> list:
    """Sorted repo ids of the locally cached HuggingFace datasets (the reason is dropped)."""
    return cached_hf_datasets()[0]


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


def _prune_default_comfy_nodes() -> int:
    """Remove ComfyUI's built-in nodes from the GLOBAL registry, leaving only custom (Taidal) nodes.

    Called at import time (inside ComfyUI) when the CLI's ``--no-default-nodes`` flag is active
    (``hide_default_comfy_nodes()``). ComfyUI loads its built-in (``nodes``) + ``comfy_extras`` +
    ``comfy_api_nodes`` node sets BEFORE it imports ``custom_nodes/`` — so by the time THIS streamstudio
    import runs, ComfyUI's global ``nodes.NODE_CLASS_MAPPINGS`` holds exactly those defaults (our own
    Taidal nodes are merged in AFTER this import returns, so they are untouched here).

    A default node is told apart from a custom one by ``RELATIVE_PYTHON_MODULE`` — the attribute
    ComfyUI stamps on every loaded node class (``server.py`` defaults a missing one to ``"nodes"``):
    a built-in/extra node's top-level module is one of ``{nodes, comfy_extras, comfy_api_nodes}``; a
    custom node's is ``custom_nodes``. We keep the latter so OTHER third-party extensions loaded
    before us survive. The configurable KEEP-LIST (``kept_default_node_names()`` — PreviewImage /
    PreviewAny by default, + ``STREAMSTUDIO_KEEP_NODES``) is also spared, so the native viewers the user
    relies on stay available. Returns the number of nodes removed (0 on a second call — idempotent).

    NOTE: hiding the defaults removes most of ComfyUI's native viewers, so the keep-list exists to
    retain the ones in use; this whole prune is opt-in via the CLI flag, never the default.
    """
    # ComfyUI's GLOBAL node registry — NOT streamstudio.nodes (absolute import). Only importable inside
    # ComfyUI, like ``server`` below, so ignore the type-check-time missing-stub error.
    import nodes as comfy_nodes  # type: ignore[import-not-found]

    default_roots = {"nodes", "comfy_extras", "comfy_api_nodes"}
    keep = kept_default_node_names()
    removed = 0
    for key, cls in list(comfy_nodes.NODE_CLASS_MAPPINGS.items()):
        module = getattr(cls, "RELATIVE_PYTHON_MODULE", "nodes")
        if module.split(".")[0] in default_roots and key not in keep:
            del comfy_nodes.NODE_CLASS_MAPPINGS[key]
            comfy_nodes.NODE_DISPLAY_NAME_MAPPINGS.pop(key, None)
            removed += 1
    return removed


def _discover_and_register_nodes() -> None:
    """Dynamically discover and register ComfyUI nodes via entry points — **once per process**.

    StreamStudio is commonly imported under TWO module identities: editable-installed in the ``.venv``
    as ``streamstudio`` AND symlinked into ComfyUI's ``custom_nodes/`` (and the package's own
    ``from streamstudio.bridge import …`` triggers the top-level import from inside the custom-node
    import). So this module's ``__init__`` runs twice, and a naive implementation re-scans every
    entry-point module and double-logs "Scanning module …".

    To stay correct AND quiet: scan ONCE (caching the discovered mappings on the ``sys`` singleton,
    which is shared across module identities) and, on any subsequent call, just copy the cached
    mappings into THIS identity's module globals — so whichever ``streamstudio`` ComfyUI actually reads
    still gets the full node set, while the scan + its logs happen a single time. The per-module
    ``seen`` set additionally dedups any duplicate entry points.
    """
    cached = getattr(sys, "_streamstudio_discovered_nodes", None)
    if cached is not None:
        NODE_CLASS_MAPPINGS.update(cached[0])
        NODE_DISPLAY_NAME_MAPPINGS.update(cached[1])
        return

    try:
        # Python 3.10+ entry_points API
        eps = importlib.metadata.entry_points(group="confluid.configurables")
    except Exception as e:
        logger.error(f"Failed to load entry points: {e}")
        return

    seen: set = set()
    for ep in eps:
        mod_path = ep.value
        if mod_path in seen:  # belt-and-suspenders: skip a duplicate entry point
            continue
        seen.add(mod_path)
        # The palette path per node is ``Taidal/<Package>/<Category>[/<group>]`` — built inside
        # register_module_nodes from the module's top-level package + each class's confluid
        # category/group (see bridge._node_category). ep.dist.name is the distribution name —
        # passed as dist_name so the [PKG] label tag is derived from the dist, not the top-level
        # Python package (which would collapse a companion namespace distribution back into its
        # parent package's tag).
        try:
            ep_dist = getattr(ep, "dist", None)
            dist_name = ep_dist.name if ep_dist is not None else None
            register_module_nodes(mod_path, NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS, dist_name=dist_name)
        except Exception as e:
            # We use debug instead of warning here because not all confluid modules
            # will have valid ComfyUI ops, and we don't want to spam the user's startup log.
            logger.debug(f"Skipped module {mod_path} during ComfyUI node generation: {e}")

    # Cache the discovered (pre-static-merge) mappings so a second module identity reuses them
    # without re-scanning. ``sys`` is a true process singleton shared across identities.
    setattr(
        sys,
        "_streamstudio_discovered_nodes",
        (dict(NODE_CLASS_MAPPINGS), dict(NODE_DISPLAY_NAME_MAPPINGS)),
    )


def _register_plugin_routes(mod: Any, dist_name: str) -> int:
    """Merge a plugin module's ``ROUTES`` onto ComfyUI's route table; return the number added.

    ``ROUTES`` is an aiohttp ``web.RouteTableDef`` (or any iterable of ``RouteDef``-shaped
    entries). aiohttp has no public table-append, so each entry is RE-registered via the
    ``routes.route(method, path, **kwargs)(handler)`` decorator form. Per-(method, path) dedup
    keeps the FIRST registration (this module runs under two identities — see
    :func:`_discover_and_register_nodes` — and two plugins may collide): a duplicate is skipped at
    debug. A table entry without method/path/handler (e.g. a ``StaticDef``) is skipped with a
    warning — static assets belong in the plugin's ``WEB_DIRECTORY``, not ``ROUTES``. Outside
    ComfyUI (no ``server`` module) this is a silent no-op.
    """
    routes = getattr(mod, "ROUTES", None)
    if not routes:
        return 0
    try:
        import server  # type: ignore[import-not-found]
    except ImportError:
        return 0  # not inside ComfyUI — the routes have nowhere to land
    table = server.PromptServer.instance.routes
    added = 0
    for rd in routes:
        method = getattr(rd, "method", None)
        path = getattr(rd, "path", None)
        handler = getattr(rd, "handler", None)
        if method is None or path is None or handler is None:
            logger.warning(
                f"StreamStudio plugin {dist_name}: skipped a non-route ROUTES entry ({rd!r}) — "
                "static assets belong in WEB_DIRECTORY, not ROUTES."
            )
            continue
        if any(getattr(r, "path", None) == path and getattr(r, "method", None) == method for r in table):
            logger.debug(
                f"StreamStudio plugin {dist_name}: route {method} {path} already registered; keeping the first."
            )
            continue
        table.route(method, path, **dict(getattr(rd, "kwargs", {}) or {}))(handler)
        added += 1
    return added


def _register_plugin_web_directory(mod: Any, dist_name: str) -> bool:
    """Register a plugin module's ``WEB_DIRECTORY`` with ComfyUI; return True when registered.

    ``WEB_DIRECTORY`` is a directory name relative to the plugin module's own directory (ComfyUI's
    own custom-node convention). It is inserted into ComfyUI's ``nodes.EXTENSION_WEB_DIRS`` under
    the plugin's DISTRIBUTION name, so its ``*.js`` files are listed by ``GET /extensions`` and
    served at ``/extensions/<dist>/…`` — the same URL depth as any custom node's extension, so
    relative imports of ComfyUI's ``scripts/*.js`` keep resolving. Timing is safe by construction:
    this module imports while ComfyUI loads custom nodes, BEFORE the server adds its static routes
    from that dict. Outside ComfyUI (no ``nodes`` module) this is a silent no-op.
    """
    web_dir_name = getattr(mod, "WEB_DIRECTORY", None)
    if not web_dir_name:
        return False
    try:
        import nodes  # type: ignore[import-not-found]
    except ImportError:
        return False  # not inside ComfyUI — nothing serves web assets here
    ext_dirs = getattr(nodes, "EXTENSION_WEB_DIRS", None)
    if ext_dirs is None:
        return False
    mod_file = getattr(mod, "__file__", None)
    if mod_file is None:
        logger.warning(
            f"StreamStudio plugin {dist_name}: WEB_DIRECTORY declared but the module has no __file__; skipped."
        )
        return False
    web_dir = (Path(mod_file).parent / str(web_dir_name)).resolve()
    if not web_dir.is_dir():
        logger.warning(f"StreamStudio plugin {dist_name}: WEB_DIRECTORY {web_dir} does not exist; skipped.")
        return False
    if dist_name in ext_dirs:
        logger.debug(f"StreamStudio plugin {dist_name}: web dir already registered; keeping the first.")
        return False
    ext_dirs[dist_name] = str(web_dir)
    return True


def _discover_and_register_plugins() -> None:
    """Discover ``streamstudio.plugins`` entry points and mount each plugin — once per process.

    A plugin is an installed package that contributes editor-side surface WITHOUT streamstudio
    knowing it: its entry point names a MODULE (a bare module path, like ``confluid.configurables``)
    whose module-level attributes form the contract —

    - ``ROUTES``: an aiohttp ``web.RouteTableDef`` of extra ``/taidal/*``-style routes, merged by
      :func:`_register_plugin_routes`;
    - ``WEB_DIRECTORY``: a directory of frontend ``*.js`` extensions, served by ComfyUI via
      :func:`_register_plugin_web_directory`.

    (A plugin's NODES ride the separate ``confluid.configurables`` group, and a per-param widget
    kind rides the ``__taidal_widget_params__`` class stamp — neither needs this group.)

    Unlike the node scan (debug-level skips — most confluid modules legitimately aren't node
    modules), a ``streamstudio.plugins`` entry is an EXPLICIT opt-in, so a failing plugin is
    isolated at WARNING with the reason — actionable, and one broken plugin never blocks the
    editor or the other plugins. Registrations are side effects on ComfyUI's process-global
    router/dict, so the second module identity (see :func:`_discover_and_register_nodes`) has
    nothing to redo: a ``sys`` sentinel makes the scan one-shot.
    """
    if getattr(sys, "_streamstudio_discovered_plugins", None) is not None:
        return
    try:
        eps = importlib.metadata.entry_points(group="streamstudio.plugins")
    except Exception as e:
        logger.error(f"Failed to load streamstudio.plugins entry points: {e}")
        return

    seen: set = set()
    loaded: List[str] = []
    for ep in eps:
        mod_path = ep.value
        if mod_path in seen:  # belt-and-suspenders: skip a duplicate entry point
            continue
        seen.add(mod_path)
        ep_dist = getattr(ep, "dist", None)
        dist_name = (ep_dist.name if ep_dist is not None else None) or ep.name
        try:
            mod = importlib.import_module(mod_path)
            n_routes = _register_plugin_routes(mod, dist_name)
            web_registered = _register_plugin_web_directory(mod, dist_name)
        except Exception as e:
            logger.warning(f"StreamStudio plugin {dist_name} ({mod_path}) failed to load: {type(e).__name__}: {e}")
            continue
        loaded.append(dist_name)
        logger.debug(
            f"StreamStudio plugin {dist_name}: {n_routes} route(s), "
            f"web dir {'registered' if web_registered else 'not registered'}."
        )
    setattr(sys, "_streamstudio_discovered_plugins", tuple(loaded))


_discover_and_register_nodes()
_discover_and_register_plugins()

# Re-apply no-color after discovery: some scanned packages configure their own colorized handlers
# (logger.remove() + logger.add(colorize=True)) at import time, undoing the force_no_color()
# above.  NO_COLOR=1 is already set; this call only triggers the reconfigure() branch that strips
# any stray handlers and re-adds loggair's sinks with colorize=False.
loggair.force_no_color()

# Merge the statically-defined StreamStudio adapter nodes (dataset walker + record viewer) on top of
# the auto-discovered RecordStream/Waivefront nodes.
NODE_CLASS_MAPPINGS.update(_STATIC_NODE_CLASS_MAPPINGS)
NODE_DISPLAY_NAME_MAPPINGS.update(_STATIC_NODE_DISPLAY_NAME_MAPPINGS)

# The item TYPE family is built HERE, not at nodes.py import: item types are registered BY the
# scan above, so building it earlier would carry only the record engine's own types and silently
# miss every domain one. Rebuilding also refreshes ``Extract from Record``'s item outputs, so a
# type can never have a node without the extract output that feeds it.
for _key, _cls in _build_item_type_nodes().items():
    NODE_CLASS_MAPPINGS[_key] = _cls
    NODE_DISPLAY_NAME_MAPPINGS[_key] = getattr(_cls, "__confluid_display_name__", _key)

WEB_DIRECTORY = "./web"


# Add backend route for the directory chooser if running inside ComfyUI
try:
    import subprocess

    import server  # type: ignore[import-not-found]
    from aiohttp import web

    # Hide ComfyUI's built-in nodes when the CLI's --no-default-nodes flag is active, so only the
    # Taidal nodes remain in the palette. Idempotent across the two streamstudio module identities
    # (editable install + custom_nodes symlink), so only log when something was actually removed.
    if hide_default_comfy_nodes():
        _pruned = _prune_default_comfy_nodes()
        if _pruned:
            logger.info(f"StreamStudio: hid {_pruned} default ComfyUI nodes (--no-default-nodes).")

    # Palette config for web/palette_filter.js. When --only-taidal-nodes is set, the frontend removes
    # every non-Taidal node from the Add-Node menu (built-ins AND other third-party custom packs)
    # EXCEPT the keep-list. This is done frontend-side, post-load, because custom packs that load AFTER
    # streamstudio aren't in the global registry at our import time but ARE by the time the browser asks.
    palette_route_exists = any(
        r.path == "/taidal/palette_config" and r.method == "GET" for r in server.PromptServer.instance.routes
    )
    if not palette_route_exists:

        @server.PromptServer.instance.routes.get("/taidal/palette_config")
        async def palette_config_handler(request: Any) -> Any:
            return web.json_response({"only_taidal": only_taidal_nodes(), "keep": sorted(kept_default_node_names())})

    route_exists = any(
        r.path == "/taidal/choose_directory" and r.method == "POST" for r in server.PromptServer.instance.routes
    )

    if not route_exists:

        @server.PromptServer.instance.routes.post("/taidal/choose_directory")
        async def choose_directory_handler(request: Any) -> Any:
            try:
                # Pop up native macOS folder picker on the host machine
                result = subprocess.run(
                    [
                        "osascript",
                        "-e",
                        'POSIX path of (choose folder with prompt "Select a directory:")',
                    ],
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
            datasets, reason = cached_hf_datasets()
            return web.json_response({"datasets": datasets, "reason": reason})

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

    # Compile the current canvas into Confluid ``flow:`` documents (drops the source +
    # Walk/Extract adapters) so the graph can be reattached to any source via
    # ``FlowGraph.from_yaml(path)``. A graph with several disconnected op components yields
    # ONE document per component.
    # Backs web/export_flow.js: the button POSTs ``{prompt: graphToPrompt(), path?: ...}``.
    export_route_exists = any(
        r.path == "/taidal/export_flow" and r.method == "POST" for r in server.PromptServer.instance.routes
    )
    if not export_route_exists:

        @server.PromptServer.instance.routes.post("/taidal/export_flow")
        async def export_flow_handler(request: Any) -> Any:
            try:
                body = await request.json()
                prompt = body.get("prompt", body)  # accept a bare prompt or {prompt: ...}
                workflow = body.get("workflow") if isinstance(body, dict) else None
                # AUTO-DETECT by payload shape: a UI-form workflow (a dict carrying a top-level
                # ``nodes`` list — the editor's native save, e.g. a consuming workspace's owned
                # graph file) may arrive as {"workflow": ...} with no prompt, or as the bare
                # body. The flow compiler consumes prompt form only, so it is converted first
                # (graphio owns the workflow.json knowledge).
                if isinstance(prompt, dict) and isinstance(prompt.get("nodes"), list):
                    workflow = workflow if workflow is not None else prompt
                    prompt = None
                elif prompt is body and workflow is not None:
                    prompt = None  # {"workflow": ...} alone — nothing prompt-shaped was posted
                if prompt is None and workflow is not None:
                    from streamstudio.graphio import ui_workflow_to_prompt

                    prompt = ui_workflow_to_prompt(workflow, NODE_CLASS_MAPPINGS)
                # Export marks (properties["taidal_export"] == "skip"/"target") are read in PYTHON
                # from the full UI ``workflow`` web/export_flow.js posts alongside the prompt — the
                # prompt strips node properties, and only the workflow carries the subgraph
                # definitions, so a mark placed INSIDE a subgraph is collected here (subgraph-aware,
                # one source of truth) and resolved to the expanded prompt ids by the compiler.
                # Explicit skip_ids/target_ids (older clients / programmatic callers) still merge in.
                skip_ids = list(body.get("skip_ids") or []) if isinstance(body, dict) else []
                target_ids = list(body.get("target_ids") or []) if isinstance(body, dict) else []
                if workflow is not None:
                    wf_skip, wf_target = collect_export_marks(workflow)
                    skip_ids += wf_skip
                    target_ids += wf_target
                yaml_docs = export_flow_yamls(
                    prompt, NODE_CLASS_MAPPINGS, skip_ids=skip_ids or None, target_ids=target_ids or None
                )
                # ``yaml`` (first doc) kept alongside ``yamls`` for older clients of the route.
                payload: Dict[str, Any] = {"yamls": yaml_docs, "yaml": yaml_docs[0]}
                out_path = body.get("path")
                if out_path:
                    paths = [str(p) for p in chain_output_paths(out_path, len(yaml_docs))]
                    for chain_path, yaml_doc in zip(paths, yaml_docs):
                        with open(chain_path, "w") as fh:
                            fh.write(yaml_doc)
                    payload.update({"paths": paths, "path": paths[0]})
                return web.json_response(payload)
            except UnsupportedTopologyError as e:
                # 400: the graph is structurally not exportable (fan-in, cycle, link-driven
                # widget, …) — an actionable user error, not a server fault.
                return web.json_response({"error": str(e)}, status=400)
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)

    # Extract JUST the op-DAG nodes from the current canvas as a standalone workflow.json — the
    # GRAPH counterpart of /taidal/export_flow (which compiles them to Confluid YAML). Same node set
    # (op-kind nodes + the Mix fan-in), but the RAW DAG as drawn (no Stash/Unstash/Mix compilation);
    # the source / Walk / Extract / viewer / value nodes are dropped. Backs web/export_flow.js's
    # "Export Taidal Ops → Graph" command, which POSTs ``{workflow: graphToPrompt().workflow}`` and
    # downloads the returned graph. An optional ``path`` writes it server-side (a future CLI twin).
    export_ops_graph_route_exists = any(
        r.path == "/taidal/export_ops_graph" and r.method == "POST" for r in server.PromptServer.instance.routes
    )
    if not export_ops_graph_route_exists:
        from streamstudio.graphio import extract_ops_workflow

        @server.PromptServer.instance.routes.post("/taidal/export_ops_graph")
        async def export_ops_graph_handler(request: Any) -> Any:
            try:
                body = await request.json()
                workflow = body.get("workflow") if isinstance(body, dict) else None
                if not isinstance(workflow, dict):
                    return web.json_response({"error": "no workflow provided"}, status=400)
                # flatten=False keeps subgraph instances intact; flatten=True inlines + contracts to raw ops.
                flatten = bool(body.get("flatten")) if isinstance(body, dict) else False
                graph = extract_ops_workflow(workflow, NODE_CLASS_MAPPINGS, flatten=flatten)
                payload: Dict[str, Any] = {"workflow": graph}
                out_path = body.get("path") if isinstance(body, dict) else None
                if out_path:
                    with open(out_path, "w") as fh:
                        json.dump(graph, fh, indent=2)
                    payload["path"] = str(out_path)
                return web.json_response(payload)
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)

    # Convert a Confluid YAML config → a whole ComfyUI workflow.json (nodes + links + groups +
    # subgraphs). Backs web/import_yaml.js ("Import Taidal YAML" menu commands). The POST body is
    # ``{yaml: "<text>", as: "graph"|"group"|"subgraph", name?: "<title>"}``.
    import_route_exists = any(
        r.path == "/taidal/import_yaml" and r.method == "POST" for r in server.PromptServer.instance.routes
    )
    if not import_route_exists:
        from streamstudio.graphio import workflow_to_yaml, yaml_to_workflow

        @server.PromptServer.instance.routes.post("/taidal/import_yaml")
        async def import_yaml_handler(request: Any) -> Any:
            try:
                body = await request.json()
                yaml_text = body.get("yaml", "")
                name = body.get("name", "Imported")
                if not yaml_text and body.get("path"):
                    # A server-side config path instead of inline text — how a launching
                    # viewer reopens the graph it was last handed (web/open_graph.js).
                    config = Path(str(body["path"]))
                    if not config.is_file():
                        return web.json_response({"error": f"no such file: {config}"}, status=400)
                    yaml_text = config.read_text()
                    if name == "Imported":
                        name = config.stem
                workflow = yaml_to_workflow(
                    yaml_text,
                    NODE_CLASS_MAPPINGS,
                    as_=body.get("as", "graph"),
                    name=name,
                )
                return web.json_response({"workflow": workflow})
            except Exception as e:
                return web.json_response({"error": str(e)}, status=400)

        @server.PromptServer.instance.routes.get("/taidal/read_graph")
        async def read_graph_handler(request: Any) -> Any:
            # A launching viewer names its workspace's source-graph FILE (?taidal_graph=…/source.json);
            # this serves it raw so web/open_graph.js can put it on the canvas.
            target = Path(str(request.query.get("path", "")))
            if not target.is_file():
                return web.json_response({"error": f"no such file: {target}"}, status=404)
            try:
                return web.json_response({"workflow": json.loads(target.read_text())})
            except ValueError as e:
                return web.json_response({"error": f"{target.name}: not a workflow JSON — {e}"}, status=400)

        @server.PromptServer.instance.routes.post("/taidal/write_graph")
        async def write_graph_handler(request: Any) -> Any:
            # The mirror half: every native workflow SAVE in a launched editor also lands in
            # the workspace's source-graph file (web/open_graph.js posts it here). The FILE,
            # not a network push, is how the canvas reaches the viewer.
            try:
                body = await request.json()
                target = Path(str(body.get("path", "")))
                workflow = body.get("workflow")
                if not str(target) or str(target) == ".":
                    return web.json_response({"error": "a 'path' is required"}, status=400)
                if not isinstance(workflow, dict) or "nodes" not in workflow:
                    return web.json_response({"error": "a 'workflow' (UI form, with nodes) is required"}, status=400)
                if not target.parent.exists():
                    target.parent.mkdir(parents=True)
                target.write_text(json.dumps(workflow))
                return web.json_response({"written": str(target)})
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)

        # Convert a WHOLE workflow.json (UI form) → a hoisted-!ref: Confluid YAML. Backs
        # web/export_workflow.js ("Export Graph → Taidal YAML"). Body: ``{workflow: {...}}``.
        @server.PromptServer.instance.routes.post("/taidal/export_workflow")
        async def export_workflow_handler(request: Any) -> Any:
            try:
                body = await request.json()
                graph = body.get("workflow", body)
                return web.json_response({"yaml": workflow_to_yaml(graph, NODE_CLASS_MAPPINGS)})
            except UnsupportedTopologyError as e:
                return web.json_response({"error": str(e)}, status=400)
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)

except ImportError:
    # Not running inside ComfyUI
    pass

# Export for ComfyUI
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
