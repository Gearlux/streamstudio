"""CLI for FluxStudio. Launches ComfyUI with Taidal nodes auto-configured."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from logflow import get_logger

logger = get_logger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch FluxStudio (ComfyUI with Taidal nodes).")
    parser.add_argument(
        "--comfy-dir",
        type=Path,
        default=Path.home() / ".fluxstudio" / "ComfyUI",
        help="Path to ComfyUI installation. Will be cloned if it doesn't exist.",
    )
    args = parser.parse_args()

    comfy_dir: Path = args.comfy_dir

    # 1. Clone ComfyUI if it doesn't exist
    if not comfy_dir.exists():
        logger.info(f"ComfyUI not found at {comfy_dir}. Cloning...")
        comfy_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "https://github.com/comfyanonymous/ComfyUI.git", str(comfy_dir)],
            check=True,
        )
        logger.info("Installing ComfyUI dependencies into current environment...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(comfy_dir / "requirements.txt")],
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


if __name__ == "__main__":
    main()
