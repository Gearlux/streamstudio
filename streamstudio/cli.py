"""CLI for StreamStudio.

Subcommands (all offline — importing :mod:`streamstudio` populates ``NODE_CLASS_MAPPINGS`` via
entry-point discovery, so the converters work WITHOUT a running ComfyUI):

* ``run`` (the default — bare ``streamstudio`` still works): launch ComfyUI with the Taidal
  nodes auto-configured. Also seeds ComfyUI custom nodes from three tiers — the
  ``streamstudio.custom_nodes`` entry-point group (nodes DECLARED by installed packages,
  including streamstudio's own bundled ComfyUI-Manager; ``--no-manager`` filters the manager,
  ``--no-declared-nodes`` skips the tier), the ``STREAMSTUDIO_CUSTOM_NODES`` env var
  (comma-separated git URLs), and the repeatable ``--custom-node <git-url>`` flag —
  idempotently: a repo already present under ``custom_nodes/`` is left alone.
* ``export``: compile a saved ComfyUI **API-format** graph into Confluid ``flow:`` document(s)
  (``streamstudio export graph.json -o flow.yaml``) — the offline twin of the canvas Export-Flow
  button. Shares :func:`streamstudio.export.export_flow_yamls`; a graph with several disconnected
  op components writes one file per component (``flow_1.yaml`` …) / a ``---``-separated stream
  on stdout.
* ``import``: convert a Confluid YAML config into a ComfyUI ``workflow.json``
  (``streamstudio import config.yaml -o workflow.json [--as graph|group|subgraph]``) — the offline
  twin of the canvas "Import Taidal YAML" menu command. Uses :func:`streamstudio.graphio.yaml_to_workflow`,
  which AUTO-DETECTS a flow-export document (a ``{…constants…, flow: {...}, outputs: …}`` file from
  ``export`` / the canvas Export-Flow button) and rebuilds the connected op graph, colouring
  dead-end taps. A BRANCHY flow document does not import back yet (tracked in TASKS.md).
* ``export-workflow``: convert a WHOLE ``workflow.json`` (UI or API form) into a hoisted-``!ref:``
  YAML (``streamstudio export-workflow workflow.json -o config.yaml``) — the twin of the canvas
  "Export Graph → Taidal YAML". Uses :func:`streamstudio.graphio.workflow_to_yaml`.
"""

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from importlib.metadata import entry_points
from pathlib import Path

from loggair import get_logger

from streamstudio.custom_nodes import COMFYUI_MANAGER_URL  # bundled default; --no-manager filters it

logger = get_logger(__name__)

# Entry-point group through which ANY installed package declares "generally available" custom
# nodes (installed on every launch, no flags needed). Each entry point resolves to a git-URL str
# or an iterable of them; streamstudio's own declaration is streamstudio/custom_nodes.py.
CUSTOM_NODES_ENTRY_POINT_GROUP = "streamstudio.custom_nodes"

# Env twin of the repeatable --custom-node flag: comma-separated git URLs of EXTRA custom nodes
# to install on launch. Install order is declared (entry points) -> env -> flags, deduped.
CUSTOM_NODES_ENV_VAR = "STREAMSTUDIO_CUSTOM_NODES"


def _custom_node_dir_name(url: str) -> str:
    """Directory name ``git clone <url>`` produces — the URL basename, ``.git`` stripped."""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    return name[: -len(".git")] if name.endswith(".git") else name


def _env_custom_nodes() -> list[str]:
    """Extra custom-node git URLs from ``STREAMSTUDIO_CUSTOM_NODES`` (comma-separated, blanks dropped)."""
    raw = os.environ.get(CUSTOM_NODES_ENV_VAR, "")
    return [url for url in (part.strip() for part in raw.split(",")) if url]


def _declared_custom_nodes() -> list[str]:
    """Custom-node git URLs declared by installed packages via ``streamstudio.custom_nodes``.

    Each entry point resolves to a ``str`` URL or an iterable of them. A broken declaration is
    skipped with an actionable warning (it is a packaging bug in the declaring package) — like
    every other custom-node failure, it must never block the launch.
    """
    urls: list[str] = []
    for ep in entry_points(group=CUSTOM_NODES_ENTRY_POINT_GROUP):
        try:
            value = ep.load()
        except Exception:  # arbitrary third-party metadata — anything can go wrong on load
            logger.warning(f"Failed to load custom-node entry point '{ep.name}' ({ep.value}); skipping it.")
            continue
        if isinstance(value, str):
            urls.append(value)
            continue
        try:
            items = list(value)
        except TypeError:
            logger.warning(
                f"Custom-node entry point '{ep.name}' must resolve to a str or an iterable of str, "
                f"got {type(value).__name__}; skipping it."
            )
            continue
        for item in items:
            if isinstance(item, str):
                urls.append(item)
            else:
                logger.warning(
                    f"Custom-node entry point '{ep.name}' contains a non-str item "
                    f"({type(item).__name__}); skipping that item."
                )
    return urls


def _install_custom_nodes(comfy_dir: Path, urls: Sequence[str]) -> None:
    """Clone each custom-node repo into ``custom_nodes/`` and install its ``requirements.txt``.

    Idempotent and launch-safe: a repo whose target directory already exists is skipped (update /
    remove it via ComfyUI-Manager), and a clone or pip failure of one node logs a warning and moves
    on — a broken or unreachable custom node must never block the studio launch.
    """
    custom_nodes_dir = comfy_dir / "custom_nodes"
    custom_nodes_dir.mkdir(parents=True, exist_ok=True)
    for url in urls:
        target = custom_nodes_dir / _custom_node_dir_name(url)
        if target.exists():
            logger.debug(f"Custom node '{target.name}' already installed at {target}; skipping.")
            continue
        logger.info(f"Installing custom node '{target.name}' from {url}...")
        try:
            subprocess.run(["git", "clone", url, str(target)], check=True)
        except subprocess.CalledProcessError:
            logger.warning(f"Failed to clone custom node {url} — skipping it; launch continues.")
            continue
        requirements = target / "requirements.txt"
        if requirements.exists():
            logger.info(f"Installing '{target.name}' dependencies into current environment...")
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", str(requirements)],
                    check=True,
                )
            except subprocess.CalledProcessError:
                logger.warning(
                    f"Failed to install requirements for custom node '{target.name}' — it may not "
                    "work until its dependencies are installed manually."
                )


def _run(args: argparse.Namespace) -> None:
    """Clone ComfyUI if needed, install custom nodes, symlink the Taidal nodes in, and launch it."""
    comfy_dir: Path = args.comfy_dir

    # 1. Clone ComfyUI if it doesn't exist
    if not comfy_dir.exists():
        logger.info(f"ComfyUI not found at {comfy_dir}. Cloning...")
        comfy_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "git",
                "clone",
                "https://github.com/comfyanonymous/ComfyUI.git",
                str(comfy_dir),
            ],
            check=True,
        )
    else:
        logger.info(f"Using existing ComfyUI installation at {comfy_dir}")
    # 1b. Sync ComfyUI's requirements on EVERY run, not only after a clone: an existing checkout
    # drifts (ComfyUI-Manager updates it in place; a newer main.py then imports a module the venv
    # never received — `comfy_aimdo`, measured 2026-08-22 — and dies at startup). pip is a no-op
    # when everything is already satisfied, so this costs seconds, not a reinstall.
    if getattr(args, "install", True):
        logger.info(f"Syncing ComfyUI requirements into {sys.executable}...")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(comfy_dir / "requirements.txt")],
                check=True,
            )
        except KeyboardInterrupt:  # Ctrl-C while pip resolves: stop as quietly as a Ctrl-C during the launch
            logger.info("StreamStudio stopped.")
            return
        except subprocess.CalledProcessError as exc:
            # A failed sync (offline, a resolver conflict) must not block an install that already
            # works — same continue-on-failure rule as the custom-node tier; only the CLONE is fatal.
            logger.warning(
                f"ComfyUI requirements sync failed ({exc}); launching anyway — "
                f"run `pip install -r {comfy_dir / 'requirements.txt'}` manually if startup fails."
            )

    # 2. Install declared + requested custom nodes (idempotent — an existing checkout is skipped).
    # Tiers: entry-point-declared (incl. streamstudio's own bundled ComfyUI-Manager) -> env -> flags.
    # --no-manager filters the manager URL from the DECLARED tier only — an explicit env/flag
    # request for it is the stronger signal and still installs.
    declared = _declared_custom_nodes() if args.declared_nodes else []
    if not args.manager:
        declared = [url for url in declared if url != COMFYUI_MANAGER_URL]
    custom_node_urls = list(dict.fromkeys([*declared, *_env_custom_nodes(), *(args.custom_node or [])]))
    _install_custom_nodes(comfy_dir, custom_node_urls)

    # 3. Symlink StreamStudio nodes into ComfyUI's custom_nodes directory
    source_module_dir = Path(__file__).parent.resolve()
    custom_nodes_dir = comfy_dir / "custom_nodes"
    custom_nodes_dir.mkdir(parents=True, exist_ok=True)
    link_path = custom_nodes_dir / "streamstudio"

    if link_path.exists():
        if link_path.is_symlink():
            if link_path.resolve() != source_module_dir:
                logger.info("Updating existing custom_node symlink...")
                link_path.unlink()
                os.symlink(source_module_dir, link_path)
        else:
            logger.warning(f"A non-symlink file/directory exists at {link_path}. Cannot auto-inject nodes.")
    else:
        logger.info(f"Symlinking Taidal nodes: {source_module_dir} -> {link_path}")
        os.symlink(source_module_dir, link_path)

    # 4. Launch ComfyUI
    # Pass the default-nodes choice to the launched ComfyUI via an env var (it's a subprocess, so an
    # env var is the channel). streamstudio's import inside ComfyUI reads STREAMSTUDIO_HIDE_DEFAULT_NODES
    # to (a) prune ComfyUI's built-in nodes and (b) surface the Taidal nodes at the palette root.
    # Set explicitly ("0" / "1") so the flag wins over any ambient shell export of the var.
    only_taidal = getattr(args, "only_taidal_nodes", False)
    hide_defaults = (not args.default_nodes) or only_taidal  # only-Taidal mode implies hiding built-ins
    # The palette keep-list lives in the config FILE (keep_nodes:), not an env var — pass only its
    # LOCATION (explicitly, so --config wins over any ambient STREAMSTUDIO_CONFIG; empty ⇒ the bridge's
    # default ~/.streamstudio/config.yaml).
    if args.config is not None and not args.config.exists():
        logger.warning(f"--config file not found: {args.config} (PreviewImage/PreviewAny are still kept).")
    env = {
        **os.environ,
        "STREAMSTUDIO_HIDE_DEFAULT_NODES": "1" if hide_defaults else "0",
        "STREAMSTUDIO_ONLY_TAIDAL_NODES": "1" if only_taidal else "0",
        "STREAMSTUDIO_CONFIG": str(args.config) if args.config is not None else "",
    }
    logger.info("🚀 Launching StreamStudio (ComfyUI) 🚀")
    if only_taidal:
        logger.info("Showing ONLY Taidal nodes (+ kept defaults) — hiding built-ins and other custom packs.")
    elif not args.default_nodes:
        logger.info("Hiding ComfyUI's built-in nodes — Taidal + other custom nodes appear (at the palette root).")
    try:
        subprocess.run([sys.executable, "main.py"], cwd=comfy_dir, env=env)
    except KeyboardInterrupt:
        logger.info("StreamStudio stopped.")


def _export(args: argparse.Namespace) -> None:
    """Compile a saved API-format graph's ops into Confluid YAML op-list(s) — one per chain."""
    # Import here (not at module top): pulling in streamstudio runs entry-point node discovery,
    # which is unnecessary weight for the common ``run`` path.
    import streamstudio
    from streamstudio.export import chain_output_paths, collect_export_marks, export_flow_yamls

    graph_path: Path = args.graph
    if not graph_path.exists():
        raise SystemExit(f"Graph file not found: {graph_path}")

    data = json.loads(graph_path.read_text())
    # Accept a bare prompt dict, or {"prompt": {...}} (the wrapper the route also tolerates).
    prompt = data.get("prompt", data) if isinstance(data, dict) else data
    # ``--skip`` mirrors the canvas export-skip mark; an API-format file carries no node
    # properties, so the marked ids must be named explicitly here (getattr: tolerate
    # programmatic Namespace callers that predate the flag).
    skip_ids = list(getattr(args, "skip", None) or [])
    target_ids = list(getattr(args, "target", None) or [])
    if isinstance(prompt, dict) and isinstance(prompt.get("nodes"), list):
        # AUTO-DETECTED by payload shape (only the UI form carries a top-level ``nodes`` list):
        # a UI-form workflow.json — the editor's native save, e.g. a workspace-owned graph file
        # its save mirror wrote. Convert it to the prompt form; a UI file DOES carry node
        # properties, so its canvas export marks are honored alongside the explicit flags.
        from streamstudio.graphio import ui_workflow_to_prompt

        wf_skip, wf_target = collect_export_marks(prompt)
        skip_ids += wf_skip
        target_ids += wf_target
        prompt = ui_workflow_to_prompt(prompt, streamstudio.NODE_CLASS_MAPPINGS)
    if not isinstance(prompt, dict) or any(not isinstance(v, dict) or "class_type" not in v for v in prompt.values()):
        raise SystemExit(
            f"{graph_path} is not a ComfyUI graph (expected the API form {{node_id: {{class_type, inputs}}}} "
            "or a native workflow.json save with a top-level 'nodes' list)."
        )

    yaml_docs = export_flow_yamls(
        prompt,
        streamstudio.NODE_CLASS_MAPPINGS,
        skip_ids=skip_ids or None,
        target_ids=target_ids or None,
    )

    if len(yaml_docs) > 1:
        logger.info(f"Graph contains {len(yaml_docs)} independent op components — exporting each individually.")
    if args.output:
        # One file per component: ``-o flow.yaml`` stays ``flow.yaml`` for a single one,
        # ``flow_1.yaml`` … ``flow_N.yaml`` for several.
        for chain_path, yaml_doc in zip(chain_output_paths(args.output, len(yaml_docs)), yaml_docs):
            chain_path.write_text(yaml_doc)
            logger.info(f"Wrote flow YAML to {chain_path}")
    else:
        # Several components on stdout → a ``---``-separated YAML document stream (each document
        # is individually a complete ``{flow: {...}}`` config; split before feeding confluid.load).
        sys.stdout.write("---\n".join(yaml_docs))


def _import(args: argparse.Namespace) -> None:
    """Convert a Confluid YAML config into a ComfyUI ``workflow.json`` (loadable on the canvas)."""
    import json as _json

    import streamstudio
    from streamstudio.graphio import yaml_to_workflow

    yaml_path: Path = args.config
    if not yaml_path.exists():
        raise SystemExit(f"Config file not found: {yaml_path}")
    workflow = yaml_to_workflow(
        str(yaml_path),
        streamstudio.NODE_CLASS_MAPPINGS,
        instantiate=args.instantiate,
        as_=args.as_,
        name=yaml_path.stem,
    )
    text = _json.dumps(workflow, indent=2)
    if args.output:
        args.output.write_text(text)
        logger.info(f"Wrote workflow JSON to {args.output}")
    else:
        sys.stdout.write(text)


def _export_workflow(args: argparse.Namespace) -> None:
    """Convert a whole ComfyUI ``workflow.json`` (UI or API form) into a hoisted-``!ref:`` YAML."""
    import streamstudio
    from streamstudio.graphio import workflow_to_yaml

    graph_path: Path = args.workflow
    if not graph_path.exists():
        raise SystemExit(f"Workflow file not found: {graph_path}")
    graph = json.loads(graph_path.read_text())
    yaml_doc = workflow_to_yaml(graph, streamstudio.NODE_CLASS_MAPPINGS)
    if args.output:
        args.output.write_text(yaml_doc)
        logger.info(f"Wrote YAML to {args.output}")
    else:
        sys.stdout.write(yaml_doc)


def main() -> None:
    parser = argparse.ArgumentParser(description="StreamStudio (ComfyUI with Taidal nodes).")
    parser.add_argument(
        "--comfy-dir",
        type=Path,
        default=Path.home() / ".streamstudio" / "ComfyUI",
        help="Path to ComfyUI installation. Will be cloned if it doesn't exist.",
    )
    parser.add_argument(
        "--default-nodes",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Show ComfyUI's built-in nodes alongside the Taidal nodes (default). "
            "Use --no-default-nodes to hide them (keeping other custom nodes + the keep-list), "
            "surfaced at the palette root rather than under a Taidal/ folder."
        ),
    )
    parser.add_argument(
        "--only-taidal-nodes",
        action="store_true",
        help=(
            "Show ONLY Taidal/StreamStudio nodes — hide ComfyUI's built-ins AND every other "
            "third-party custom-node pack. Implies --no-default-nodes. Extra nodes to keep are listed "
            "in the config file's keep_nodes (PreviewImage and PreviewAny are always kept)."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="FILE",
        help=(
            "Palette config file (YAML). Its 'keep_nodes:' list names extra default/custom nodes to "
            "KEEP when hiding the palette (PreviewImage / PreviewAny are always kept). "
            "Defaults to ~/.streamstudio/config.yaml when omitted."
        ),
    )
    parser.add_argument(
        "--install",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Sync ComfyUI's requirements.txt into this environment on every launch (default; a "
            "no-op when satisfied). --no-install skips it — for an air-gapped run, or when the "
            "environment is managed elsewhere."
        ),
    )
    parser.add_argument(
        "--manager",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Install the ComfyUI-Manager custom node on launch (default), giving the GUI an "
            "in-app store to install/update/remove further custom nodes. --no-manager skips it."
        ),
    )
    parser.add_argument(
        "--declared-nodes",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Install the custom nodes installed packages declare via the streamstudio.custom_nodes "
            "entry-point group (default; includes streamstudio's own bundled ComfyUI-Manager). "
            "--no-declared-nodes skips the whole tier — --custom-node / STREAMSTUDIO_CUSTOM_NODES "
            "still install."
        ),
    )
    parser.add_argument(
        "--custom-node",
        action="append",
        default=None,
        metavar="GIT_URL",
        help=(
            "Git URL of an extra ComfyUI custom node to clone into custom_nodes/ on launch "
            "(repeatable). The STREAMSTUDIO_CUSTOM_NODES env var (comma-separated URLs) adds more. "
            "Already-installed nodes are skipped — update or remove them via ComfyUI-Manager."
        ),
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="Launch ComfyUI with Taidal nodes (default).")

    export_parser = sub.add_parser(
        "export",
        help=(
            "Compile a saved graph (API form or native workflow.json, auto-detected) to Confluid "
            "`flow:` document(s) — one per independent component."
        ),
    )
    export_parser.add_argument(
        "graph",
        type=Path,
        help="ComfyUI graph JSON — API form OR a native workflow.json save (auto-detected by shape).",
    )
    export_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help=(
            "Write the YAML here (default: stdout). With several disconnected op components, one "
            "file per component is written (flow.yaml -> flow_1.yaml ... flow_N.yaml)."
        ),
    )
    export_parser.add_argument(
        "--target",
        action="append",
        metavar="NODE_ID",
        default=None,
        help=(
            "Export ONLY the ops that compute this node (its data ancestor cone; the node is the "
            "final op) — the CLI twin of the canvas export-target mark; repeatable (one document "
            "per target)."
        ),
    )
    export_parser.add_argument(
        "--skip",
        action="append",
        metavar="NODE_ID",
        default=None,
        help=(
            "Exclude this node (and everything data-downstream of it) from the export — the CLI "
            "twin of the canvas export-skip mark (right-click > 'Taidal: exclude from export'); "
            "repeatable. A subgraph-instance id excludes the whole inlined instance."
        ),
    )

    import_parser = sub.add_parser(
        "import",
        help=(
            "Convert a Confluid YAML config into a ComfyUI workflow.json. Auto-detects an ops-export "
            "document ({...,ops:[...]}) and rebuilds the connected op graph (auto-stash removed, taps coloured)."
        ),
    )
    import_parser.add_argument(
        "config", type=Path, help="Confluid YAML config (whole-graph OR flow-export doc) to import."
    )
    import_parser.add_argument(
        "-o", "--output", type=Path, default=None, help="Write the workflow JSON here (default: stdout)."
    )
    import_parser.add_argument(
        "--as",
        dest="as_",
        choices=["graph", "group", "subgraph"],
        default="graph",
        help="graph (default) lays nodes out; group wraps them in a box; subgraph packages a reusable blueprint.",
    )
    import_parser.add_argument(
        "--instantiate",
        action="store_true",
        help="Walk live-but-inert objects (materialize, solidify suppressed) instead of un-built markers.",
    )

    expw_parser = sub.add_parser(
        "export-workflow", help="Convert a whole ComfyUI workflow.json (UI or API form) into a hoisted-!ref: YAML."
    )
    expw_parser.add_argument("workflow", type=Path, help="ComfyUI workflow JSON (UI or API form).")
    expw_parser.add_argument("-o", "--output", type=Path, default=None, help="Write the YAML here (default: stdout).")

    # Shell completion. StreamStudio is an argparse app, so it gets NEITHER liquifai's
    # completion (that walks the `liquifai.apps` entry-point group — see aisland/setup.sh)
    # NOR click's built-in `_PROG_COMPLETE` protocol; argcomplete is the argparse
    # equivalent. It must run AFTER the parser is fully built (it introspects it) and
    # BEFORE parse_args. The import is guarded so a venv without argcomplete still runs
    # the CLI normally — completion is a convenience, never a hard requirement.
    try:
        import argcomplete
    except ImportError:
        pass
    else:
        argcomplete.autocomplete(parser)

    args = parser.parse_args()

    if args.command == "export":
        _export(args)
    elif args.command == "import":
        _import(args)
    elif args.command == "export-workflow":
        _export_workflow(args)
    else:  # None (bare `streamstudio`) or explicit `run`
        _run(args)


if __name__ == "__main__":
    main()
