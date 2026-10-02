"""StreamStudio forces Loggair console coloring OFF on the COMFYUI path — and leaves a host that mounts the canvas
editor alone.

The ComfyUI half's logs land in ComfyUI's own console capture, where ANSI color escapes are noise, so the package calls
``loggair.force_no_color()`` — which sets the standard ``NO_COLOR=1`` AND reloads loggair when it is already configured
— when it is loaded INSIDE ComfyUI (its ``server`` module is imported before any custom node) and when the ComfyUI half
is made (``_load_comfy``, on the first read of ``NODE_CLASS_MAPPINGS``). Merely inheriting/copying the env var is NOT
enough once a coloring decision is baked into the live console sink — these tests pin the reload, and that a pre-set
``NO_COLOR=`` (empty) still re-allows colors.

What a host that mounts the canvas editor imports (``streamstudio.app``) must NOT touch: measured before 2026-10-01,
``import streamstudio.app`` set ``NO_COLOR=1`` in the host's own process — and in every child process it started — and
reconfigured its loggair, for a ComfyUI it never starts.

Each check runs in a SUBPROCESS (isolated loggair state) with ``cwd`` set to a throwaway dir so ``import streamstudio``
resolves to the installed package (not the workspace-root namespace-package shadow). ``PYCHARM_HOSTED=1`` forces
loguru's color auto-detect ON for the real stderr, so a still-colorized sink is distinguishable from an
auto-detected-off one.
"""

import os
import subprocess
import sys
from pathlib import Path

#: The node discovery and the plugin seam find nothing (the mappings' read costs the ComfyUI modules' import, not the
#: whole workspace's); every other entry-point read is answered.
_STUB_SCAN = (
    "import importlib.metadata as m\n"
    "real = m.entry_points\n"
    "m.entry_points = lambda **kw: [] if kw.get('group') in ('confluid.configurables', 'streamstudio.plugins')"
    " else real(**kw)\n"
)

#: Inside ComfyUI: its ``server`` module is imported before any custom node. This stand-in has no route table, so the
#: ``/taidal/*`` registration takes its not-inside-ComfyUI way out (an ``ImportError`` from the attribute read).
_FAKE_COMFY_SERVER = (
    "import sys, types\n"
    "class _Server(types.ModuleType):\n"
    "    def __getattr__(self, name):\n"
    "        if name == 'PromptServer':\n"
    "            raise ImportError('no route table in this stand-in')\n"
    "        raise AttributeError(name)\n"
    "sys.modules['server'] = _Server('server')\n"
)

_REPORT = (
    "import os\n"
    "from loguru import logger\n"
    "vals = [h._colorize for h in logger._core.handlers.values() if type(h._sink).__name__ == 'StreamSink']\n"
    "print('RESULT:' + str(vals[0] if vals else None))\n"
    "print('NO_COLOR:' + repr(os.environ.get('NO_COLOR')))\n"
)

_PRECONFIGURED = "from loggair import get_logger\nget_logger('early').info('pre-config -> colors baked in')\n"


def _run(code: str, env: dict, cwd: Path) -> dict:
    proc = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(cwd), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    said = dict(line.split(":", 1) for line in proc.stdout.splitlines() if line.startswith(("RESULT:", "NO_COLOR:")))
    assert set(said) == {"RESULT", "NO_COLOR"}, f"no report:\n{proc.stdout}\n{proc.stderr}"
    return said


def _env(**extra: str) -> dict:
    env = dict(os.environ)
    env.pop("NO_COLOR", None)
    env["PYCHARM_HOSTED"] = "1"  # auto-detect would say "colorize"
    env.update(extra)
    return env


def test_inside_comfyui_the_import_forces_colorless_even_if_preconfigured(tmp_path: Path) -> None:
    code = _STUB_SCAN + _FAKE_COMFY_SERVER + _PRECONFIGURED + "import streamstudio\n" + _REPORT
    assert _run(code, _env(), tmp_path) == {"RESULT": "False", "NO_COLOR": "'1'"}


def test_the_comfyui_half_made_on_first_read_forces_colorless(tmp_path: Path) -> None:
    code = _STUB_SCAN + _PRECONFIGURED + "import streamstudio\nstreamstudio.NODE_CLASS_MAPPINGS\n" + _REPORT
    assert _run(code, _env(), tmp_path) == {"RESULT": "False", "NO_COLOR": "'1'"}


def test_an_empty_no_color_still_re_allows_colors_on_the_comfyui_path(tmp_path: Path) -> None:
    code = _STUB_SCAN + _FAKE_COMFY_SERVER + _PRECONFIGURED + "import streamstudio\n" + _REPORT
    assert _run(code, _env(NO_COLOR=""), tmp_path) == {"RESULT": "True", "NO_COLOR": "''"}


def test_a_host_mounting_the_editor_keeps_its_environment_and_its_colors(tmp_path: Path) -> None:
    """CON: ``import streamstudio.app`` (what a host imports) leaves ``NO_COLOR`` unset, so the host's own console and
    every child process it starts keep their colors."""
    child = (
        "import subprocess, sys\n"
        "print('CHILD:' + subprocess.run([sys.executable, '-c', 'import os; print(os.environ.get(\"NO_COLOR\"))'],"
        " capture_output=True, text=True).stdout.strip())\n"
    )
    code = _PRECONFIGURED + "import streamstudio.app\n" + _REPORT + child
    proc = subprocess.run([sys.executable, "-c", code], env=_env(), cwd=str(tmp_path), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert {"RESULT:True", "NO_COLOR:None", "CHILD:None"} <= set(lines), proc.stdout
