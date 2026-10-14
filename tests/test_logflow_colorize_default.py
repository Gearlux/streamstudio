"""Importing StreamStudio leaves the importing process's console colors and environment alone.

A host that mounts the canvas editor imports ``streamstudio.app``. Measured before 2026-10-01, that import set
``NO_COLOR=1`` in the host's own process — and in every child process it started — and reconfigured its loggair. The
package touches neither, whatever else the process has imported.

Each check runs in a SUBPROCESS (isolated loggair state) with ``cwd`` set to a throwaway dir so ``import streamstudio``
resolves to the installed package (not the workspace-root namespace-package shadow). ``PYCHARM_HOSTED=1`` forces
loguru's color auto-detect ON for the real stderr, so a still-colorized sink is distinguishable from an
auto-detected-off one.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPORT = (
    "import os\n"
    "from loguru import logger\n"
    "vals = [h._colorize for h in logger._core.handlers.values() if type(h._sink).__name__ == 'StreamSink']\n"
    "print('RESULT:' + str(vals[0] if vals else None))\n"
    "print('NO_COLOR:' + repr(os.environ.get('NO_COLOR')))\n"
)

_PRECONFIGURED = "from loggair import get_logger\nget_logger('early').info('pre-config -> colors baked in')\n"

_CHILD = (
    "import subprocess, sys\n"
    "print('CHILD:' + subprocess.run([sys.executable, '-c', 'import os; print(os.environ.get(\"NO_COLOR\"))'],"
    " capture_output=True, text=True).stdout.strip())\n"
)

#: A module named ``server`` already imported — what some web hosts have; it changes nothing here.
_A_SERVER_MODULE = "import sys, types\nsys.modules['server'] = types.ModuleType('server')\n"


def _env() -> dict:
    env = dict(os.environ)
    env.pop("NO_COLOR", None)
    env["PYCHARM_HOSTED"] = "1"  # auto-detect would say "colorize"
    return env


@pytest.mark.parametrize("before", ["", _A_SERVER_MODULE], ids=["plain", "a-server-module-loaded"])
def test_a_host_mounting_the_editor_keeps_its_environment_and_its_colors(tmp_path: Path, before: str) -> None:
    """``import streamstudio.app`` leaves ``NO_COLOR`` unset, so the host's own console and every child process it
    starts keep their colors."""
    code = before + _PRECONFIGURED + "import streamstudio.app\n" + _REPORT + _CHILD
    proc = subprocess.run([sys.executable, "-c", code], env=_env(), cwd=str(tmp_path), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert {"RESULT:True", "NO_COLOR:None", "CHILD:None"} <= set(lines), proc.stdout
