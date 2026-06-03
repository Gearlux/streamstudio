"""CLI for FluxStudio.

Subcommands (all offline — importing :mod:`fluxstudio` populates ``NODE_CLASS_MAPPINGS`` via
entry-point discovery, so the converters work WITHOUT a running ComfyUI):

* ``run`` (the default — bare ``fluxstudio`` still works): launch ComfyUI with the Taidal
  nodes auto-configured.
* ``export``: compile a saved ComfyUI **API-format** graph's OPS into a Confluid YAML op-list
  (``fluxstudio export graph.json -o ops.yaml``) — the offline twin of the canvas Export-Ops
  button. Shares :func:`fluxstudio.export.export_ops_yaml`.
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
from pathlib import Path

from logflow import get_logger

logger = get_logger(__name__)


def _run(args: argparse.Namespace) -> None:
    """Clone ComfyUI if needed, symlink the Taidal nodes in, and launch it."""
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

    # 2. Symlink FluxStudio nodes into ComfyUI's custom_nodes directory
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

    # 3. Launch ComfyUI
    logger.info("🚀 Launching FluxStudio (ComfyUI) 🚀")
    try:
        subprocess.run([sys.executable, "main.py"], cwd=comfy_dir)
    except KeyboardInterrupt:
        logger.info("FluxStudio stopped.")


def _export(args: argparse.Namespace) -> None:
    """Compile a saved API-format graph's ops into a Confluid YAML op-list."""
    # Import here (not at module top): pulling in fluxstudio runs entry-point node discovery,
    # which is unnecessary weight for the common ``run`` path.
    import fluxstudio
    from fluxstudio.export import export_ops_yaml

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

    yaml_doc = export_ops_yaml(prompt, fluxstudio.NODE_CLASS_MAPPINGS)

    if args.output:
        args.output.write_text(yaml_doc)
        logger.info(f"Wrote ops YAML to {args.output}")
    else:
        sys.stdout.write(yaml_doc)


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
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="Launch ComfyUI with Taidal nodes (default).")

    export_parser = sub.add_parser("export", help="Compile a saved graph's ops to a Confluid YAML op-list.")
    export_parser.add_argument("graph", type=Path, help="ComfyUI API-format graph JSON.")
    export_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Write the YAML here (default: stdout).",
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
