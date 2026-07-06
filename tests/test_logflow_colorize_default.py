"""FluxStudio forces Loggair console coloring OFF — even if loggair was already
configured (with colors) before fluxstudio was imported.

`fluxstudio/__init__.py` calls ``loggair.force_no_color()`` at import, which sets
the standard ``NO_COLOR=1`` AND reloads loggair when it is already configured.
Merely inheriting/copying the env var is NOT enough once a coloring decision is
baked into the live console sink — these tests pin the reload, and that a pre-set
``NO_COLOR=`` (empty) still re-allows colors.

Each check runs in a SUBPROCESS (isolated loggair state) with ``cwd`` set to a
throwaway dir so ``import fluxstudio`` resolves to the installed package (not the
workspace-root namespace-package shadow). ``PYCHARM_HOSTED=1`` forces loguru's
color auto-detect ON for the real stderr, so a still-colorized sink is
distinguishable from an auto-detected-off one.
"""

import os
import subprocess
import sys
from pathlib import Path

_SNIPPET = (
    "from loggair import get_logger\n"
    "get_logger('early').info('pre-config -> colors baked in')\n"
    "import fluxstudio  # must force colorless via force_no_color()\n"
    "from loguru import logger\n"
    "vals = [h._colorize for h in logger._core.handlers.values()"
    " if type(h._sink).__name__ == 'StreamSink']\n"
    "print('RESULT:' + str(vals[0] if vals else None))\n"
)


def _stream_colorize(env: dict, cwd: Path) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", _SNIPPET],
        env=env,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT:"):
            return line[len("RESULT:") :]
    raise AssertionError(f"no RESULT line:\n{proc.stdout}\n{proc.stderr}")


def test_fluxstudio_forces_colorless_even_if_preconfigured(tmp_path: Path) -> None:
    env = dict(os.environ)
    env.pop("NO_COLOR", None)
    env["PYCHARM_HOSTED"] = "1"  # auto-detect would say "colorize" — but we forced off
    assert _stream_colorize(env, tmp_path) == "False"


def test_fluxstudio_empty_no_color_re_allows_colors(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["NO_COLOR"] = ""  # explicit "allow colors" — force_no_color must not clobber it
    env["PYCHARM_HOSTED"] = "1"
    assert _stream_colorize(env, tmp_path) == "True"
