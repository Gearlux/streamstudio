"""CLI for FluxStudio.

Subcommands (all offline — importing :mod:`fluxstudio` populates ``NODE_CLASS_MAPPINGS`` via
entry-point discovery, so the converters work WITHOUT a running ComfyUI):

* ``run`` (the default — bare ``fluxstudio`` still works): launch ComfyUI with the Taidal
  nodes auto-configured. Also installs ComfyUI-Manager by default (``--no-manager`` skips it)
  plus any extra custom nodes requested via the repeatable ``--custom-node <git-url>`` flag or
  the ``FLUXSTUDIO_CUSTOM_NODES`` env var (comma-separated git URLs) — idempotently: a repo
  already present under ``custom_nodes/`` is left alone.
* ``export``: compile a saved ComfyUI **API-format** graph's OPS into Confluid YAML op-list(s)
  (``fluxstudio export graph.json -o ops.yaml``) — the offline twin of the canvas Export-Ops
  button. Shares :func:`fluxstudio.export.export_ops_yamls`; a graph with several disconnected
  op chains writes one file per chain (``ops_1.yaml`` …) / a ``---``-separated stream on stdout.
* ``import``: convert a Confluid YAML config into a ComfyUI ``workflow.json``
  (``fluxstudio import config.yaml -o workflow.json [--as graph|group|subgraph]``) — the offline
  twin of the canvas "Import Taidal YAML" menu command. Uses :func:`fluxstudio.graphio.yaml_to_workflow`.
* ``export-workflow``: convert a WHOLE ``workflow.json`` (UI or API form) into a hoisted-``!ref:``
  YAML (``fluxstudio export-workflow workflow.json -o config.yaml``) — the twin of the canvas
  "Export Graph → Taidal YAML". Uses :func:`fluxstudio.graphio.workflow_to_yaml`.
"""

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from logflow import get_logger

logger = get_logger(__name__)

# ComfyUI-Manager is installed into custom_nodes/ by default (skip with --no-manager): it gives
# the launched ComfyUI an in-app store to install / update / remove further custom nodes, so the
# bootstrap only has to seed it — everything else is manageable from the GUI.
COMFYUI_MANAGER_URL = "https://github.com/Comfy-Org/ComfyUI-Manager"

# Env twin of the repeatable --custom-node flag: comma-separated git URLs of EXTRA custom nodes
# to install on launch. Both feed _install_custom_nodes (flag values after env values).
CUSTOM_NODES_ENV_VAR = "FLUXSTUDIO_CUSTOM_NODES"


def _custom_node_dir_name(url: str) -> str:
    """Directory name ``git clone <url>`` produces — the URL basename, ``.git`` stripped."""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    return name[: -len(".git")] if name.endswith(".git") else name


def _env_custom_nodes() -> list[str]:
    """Extra custom-node git URLs from ``FLUXSTUDIO_CUSTOM_NODES`` (comma-separated, blanks dropped)."""
    raw = os.environ.get(CUSTOM_NODES_ENV_VAR, "")
    return [url for url in (part.strip() for part in raw.split(",")) if url]


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
        logger.info("Installing ComfyUI dependencies into current environment...")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "-r",
                str(comfy_dir / "requirements.txt"),
            ],
            check=True,
        )
    else:
        logger.info(f"Using existing ComfyUI installation at {comfy_dir}")

    # 2. Install bundled + requested custom nodes (idempotent — an existing checkout is skipped).
    custom_node_urls: list[str] = [COMFYUI_MANAGER_URL] if args.manager else []
    custom_node_urls.extend(_env_custom_nodes())
    custom_node_urls.extend(args.custom_node or [])
    _install_custom_nodes(comfy_dir, custom_node_urls)

    # 3. Symlink FluxStudio nodes into ComfyUI's custom_nodes directory
    source_module_dir = Path(__file__).parent.resolve()
    custom_nodes_dir = comfy_dir / "custom_nodes"
    custom_nodes_dir.mkdir(parents=True, exist_ok=True)
    link_path = custom_nodes_dir / "fluxstudio"

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
    # env var is the channel). fluxstudio's import inside ComfyUI reads FLUXSTUDIO_HIDE_DEFAULT_NODES
    # to (a) prune ComfyUI's built-in nodes and (b) surface the Taidal nodes at the palette root.
    # Set explicitly ("0" / "1") so the flag wins over any ambient shell export of the var.
    env = {**os.environ, "FLUXSTUDIO_HIDE_DEFAULT_NODES": "0" if args.default_nodes else "1"}
    logger.info("🚀 Launching FluxStudio (ComfyUI) 🚀")
    if not args.default_nodes:
        logger.info("Hiding ComfyUI's built-in nodes — only Taidal nodes will appear (at the palette root).")
    try:
        subprocess.run([sys.executable, "main.py"], cwd=comfy_dir, env=env)
    except KeyboardInterrupt:
        logger.info("FluxStudio stopped.")


def _export(args: argparse.Namespace) -> None:
    """Compile a saved API-format graph's ops into Confluid YAML op-list(s) — one per chain."""
    # Import here (not at module top): pulling in fluxstudio runs entry-point node discovery,
    # which is unnecessary weight for the common ``run`` path.
    import fluxstudio
    from fluxstudio.export import chain_output_paths, export_ops_yamls

    graph_path: Path = args.graph
    if not graph_path.exists():
        raise SystemExit(f"Graph file not found: {graph_path}")

    data = json.loads(graph_path.read_text())
    # Accept a bare prompt dict, or {"prompt": {...}} (the wrapper the route also tolerates).
    prompt = data.get("prompt", data) if isinstance(data, dict) else data
    if not isinstance(prompt, dict) or any(not isinstance(v, dict) or "class_type" not in v for v in prompt.values()):
        raise SystemExit(
            f"{graph_path} is not a ComfyUI API-format graph (expected {{node_id: {{class_type, inputs}}}}). "
            "Export it from ComfyUI via the dev-mode 'Save (API Format)' button."
        )

    yaml_docs = export_ops_yamls(prompt, fluxstudio.NODE_CLASS_MAPPINGS)

    if len(yaml_docs) > 1:
        logger.info(f"Graph contains {len(yaml_docs)} independent op chains — exporting each individually.")
    if args.output:
        # One file per chain: ``-o ops.yaml`` stays ``ops.yaml`` for a single chain,
        # ``ops_1.yaml`` … ``ops_N.yaml`` for several.
        for chain_path, yaml_doc in zip(chain_output_paths(args.output, len(yaml_docs)), yaml_docs):
            chain_path.write_text(yaml_doc)
            logger.info(f"Wrote ops YAML to {chain_path}")
    else:
        # Several chains on stdout → a ``---``-separated YAML document stream (each document is
        # individually a complete ``{ops: [...]}`` config; split before feeding confluid.load).
        sys.stdout.write("---\n".join(yaml_docs))


def _import(args: argparse.Namespace) -> None:
    """Convert a Confluid YAML config into a ComfyUI ``workflow.json`` (loadable on the canvas)."""
    import json as _json

    import fluxstudio
    from fluxstudio.graphio import yaml_to_workflow

    yaml_path: Path = args.config
    if not yaml_path.exists():
        raise SystemExit(f"Config file not found: {yaml_path}")
    workflow = yaml_to_workflow(
        str(yaml_path),
        fluxstudio.NODE_CLASS_MAPPINGS,
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
    import fluxstudio
    from fluxstudio.graphio import workflow_to_yaml

    graph_path: Path = args.workflow
    if not graph_path.exists():
        raise SystemExit(f"Workflow file not found: {graph_path}")
    graph = json.loads(graph_path.read_text())
    yaml_doc = workflow_to_yaml(graph, fluxstudio.NODE_CLASS_MAPPINGS)
    if args.output:
        args.output.write_text(yaml_doc)
        logger.info(f"Wrote YAML to {args.output}")
    else:
        sys.stdout.write(yaml_doc)


def main() -> None:
    parser = argparse.ArgumentParser(description="FluxStudio (ComfyUI with Taidal nodes).")
    parser.add_argument(
        "--comfy-dir",
        type=Path,
        default=Path.home() / ".fluxstudio" / "ComfyUI",
        help="Path to ComfyUI installation. Will be cloned if it doesn't exist.",
    )
    parser.add_argument(
        "--default-nodes",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Show ComfyUI's built-in nodes alongside the Taidal nodes (default). "
            "Use --no-default-nodes to hide them so only Taidal nodes appear, surfaced at the "
            "palette root rather than under a Taidal/ folder."
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
        "--custom-node",
        action="append",
        default=None,
        metavar="GIT_URL",
        help=(
            "Git URL of an extra ComfyUI custom node to clone into custom_nodes/ on launch "
            "(repeatable). The FLUXSTUDIO_CUSTOM_NODES env var (comma-separated URLs) adds more. "
            "Already-installed nodes are skipped — update or remove them via ComfyUI-Manager."
        ),
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="Launch ComfyUI with Taidal nodes (default).")

    export_parser = sub.add_parser(
        "export", help="Compile a saved graph's ops to Confluid YAML op-list(s) — one per independent chain."
    )
    export_parser.add_argument("graph", type=Path, help="ComfyUI API-format graph JSON.")
    export_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help=(
            "Write the YAML here (default: stdout). With several disconnected op chains, one "
            "file per chain is written (ops.yaml -> ops_1.yaml ... ops_N.yaml)."
        ),
    )

    import_parser = sub.add_parser("import", help="Convert a Confluid YAML config into a ComfyUI workflow.json.")
    import_parser.add_argument("config", type=Path, help="Confluid YAML config to import.")
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

    args = parser.parse_args()

    if args.command == "export":
        _export(args)
    elif args.command == "import":
        _import(args)
    elif args.command == "export-workflow":
        _export_workflow(args)
    else:  # None (bare `fluxstudio`) or explicit `run`
        _run(args)


if __name__ == "__main__":
    main()
