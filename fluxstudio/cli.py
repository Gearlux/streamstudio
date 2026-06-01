"""CLI for FluxStudio.

Two subcommands:

* ``run`` (the default — bare ``fluxstudio`` still works): launch ComfyUI with the Taidal
  nodes auto-configured.
* ``export``: compile a saved ComfyUI **API-format** graph's ops into a Confluid YAML op-list
  (``fluxstudio export graph.json -o ops.yaml``) — the offline twin of the canvas Export button
  / the ``/taidal/export_ops`` route. It shares the same compiler
  (:func:`fluxstudio.export.export_ops_yaml`); importing :mod:`fluxstudio` populates
  ``NODE_CLASS_MAPPINGS`` via entry-point discovery, so this works WITHOUT a running ComfyUI.
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

    args = parser.parse_args()

    if args.command == "export":
        _export(args)
    else:  # None (bare `fluxstudio`) or explicit `run`
        _run(args)


if __name__ == "__main__":
    main()
