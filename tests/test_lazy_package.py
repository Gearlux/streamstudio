"""Importing the canvas editor does not run the ComfyUI node discovery.

The package's ``__init__`` used to discover every ComfyUI node at import — measured 6.6 s, torch and 432 node
classes — so ``import streamstudio.app`` (what a host app that mounts the editor imports) paid for a second editor
it never starts. The ComfyUI mappings are now resolved on first READ of ``NODE_CLASS_MAPPINGS`` /
``NODE_DISPLAY_NAME_MAPPINGS`` (a module ``__getattr__``, which ComfyUI's loader reaches through its own
``getattr``), and at import when the package is loaded INSIDE ComfyUI (its ``server`` module is imported before any
custom node, and the routes must be registered while it loads them — ``tests/test_init_discovery.py`` pins those).

Each check runs in a SUBPROCESS (a clean ``sys.modules``) from a temporary folder, so ``streamstudio`` resolves to
the installed package. The entry-point scan is stubbed to nothing where the mappings are read, so the check costs
the ComfyUI modules' import, not the whole workspace's.
"""

import subprocess
import sys
from pathlib import Path

#: The node discovery and the plugin seam find nothing; every other entry-point read (fsspec's, …) is answered.
_STUB_SCAN = (
    "import importlib.metadata as m\n"
    "real = m.entry_points\n"
    "m.entry_points = lambda **kw: [] if kw.get('group') in ('confluid.configurables', 'streamstudio.plugins')"
    " else real(**kw)\n"
)


def _run(code: str, cwd: Path) -> str:
    done = subprocess.run([sys.executable, "-c", code], cwd=str(cwd), capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_importing_the_editor_loads_no_comfyui_module_and_no_torch(tmp_path: Path) -> None:
    code = (
        "import sys\n"
        "import streamstudio.app\n"
        "loaded = [m for m in ('torch', 'streamstudio.bridge', 'streamstudio.nodes', 'streamstudio.export',"
        " 'streamstudio.graphio') if m in sys.modules]\n"
        "print(loaded)\n"
    )
    assert _run(code, tmp_path) == "[]"


def test_reading_the_mappings_loads_them_once(tmp_path: Path) -> None:
    code = (
        _STUB_SCAN + "import sys\n"
        "import streamstudio\n"
        "print('streamstudio.bridge' in sys.modules)\n"
        "mappings = streamstudio.NODE_CLASS_MAPPINGS\n"
        "print('streamstudio.bridge' in sys.modules, 'Taidal_ExtractRecord' in mappings)\n"
        "from streamstudio import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS\n"
        "print(NODE_CLASS_MAPPINGS is mappings, NODE_DISPLAY_NAME_MAPPINGS['Taidal_ExtractRecord'] != '')\n"
        "print(getattr(streamstudio, 'WEB_DIRECTORY'), hasattr(streamstudio, 'NODE_DISPLAY_NAME_MAPPINGS'))\n"
    )
    assert _run(code, tmp_path).splitlines() == ["False", "True True", "True True", "./web True"]


def test_an_unknown_name_is_still_an_attribute_error(tmp_path: Path) -> None:
    code = (
        "import sys, streamstudio\n"
        "try:\n"
        "    streamstudio.nosuch\n"
        "except AttributeError as exc:\n"
        "    print(exc)\n"
        "print('streamstudio.bridge' in sys.modules)\n"
    )
    assert _run(code, tmp_path).splitlines() == ["module 'streamstudio' has no attribute 'nosuch'", "False"]
