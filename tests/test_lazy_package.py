"""The package is the canvas editor: importing it loads no torch, and it carries no second editor.

``import streamstudio.app`` is what a host app that mounts the editor imports, so its cost is the host's start-up cost
(measured 2026-10-01: 0.2 s). The package root answers ``__version__`` and the HuggingFace lookups the pickers call;
it registers nothing and exposes no node mappings.

Each check runs in a SUBPROCESS (a clean ``sys.modules``) from a temporary folder, so ``streamstudio`` resolves to
the installed package.
"""

import subprocess
import sys
from pathlib import Path

import pytest


def _run(code: str, cwd: Path) -> str:
    done = subprocess.run([sys.executable, "-c", code], cwd=str(cwd), capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_importing_the_editor_loads_no_torch(tmp_path: Path) -> None:
    code = "import sys\nimport streamstudio.app\nprint('torch' in sys.modules)\n"
    assert _run(code, tmp_path) == "False"


@pytest.mark.parametrize("module", ["bridge", "nodes", "export", "graphio", "custom_nodes"])
def test_no_module_of_a_second_editor_is_installed(tmp_path: Path, module: str) -> None:
    code = f"import importlib.util\nprint(importlib.util.find_spec('streamstudio.{module}'))\n"
    assert _run(code, tmp_path) == "None"


def test_the_package_root_registers_no_nodes(tmp_path: Path) -> None:
    code = (
        "import streamstudio\n"
        "print([name for name in ('NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS', 'WEB_DIRECTORY')"
        " if hasattr(streamstudio, name)])\n"
        "print(callable(streamstudio.cached_hf_datasets), isinstance(streamstudio.__version__, str))\n"
    )
    assert _run(code, tmp_path).splitlines() == ["[]", "True True"]
