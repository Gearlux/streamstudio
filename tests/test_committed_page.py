"""The editor's page is COMMITTED, built, inside the package — ``streamstudio/canvas`` (user decision B, 2026-10-04).

Every install serves the page: a checkout without Node, an install from GitHub (``pip install
git+https://github.com/Gearlux/streamstudio.git@main``, which carries no ``frontend/``), a wheel. Node is needed only
by whoever edits the page's source. Measured before: the build landed in ``frontend/dist`` (gitignored, not in the
wheel), so an install from GitHub answered ``/`` with the placeholder ``The canvas page is not built. Build it once —
…`` and a host app's CI skipped every browser test of the editor.

What keeps the committed page honest is THE GUARD (the first test here): the build carries the fingerprint of the
source it was made from (``.built-from``, written with :func:`streamstudio.page.fingerprint`), and a page source
changed without its rebuilt page fails here — in CI too, which has no Node — before an out-of-date page can ship.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import List

import pytest

from streamstudio import page

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "streamstudio"

#: What the guard says — the one build command and the folder to commit, so the failure is its own instruction.
STALE = (
    "the committed editor page is older than its source — start streamstudio once, or run `{command}`, "
    "and commit streamstudio/streamstudio/canvas"
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)


def _in_a_git_checkout() -> bool:
    return shutil.which("git") is not None and _git("rev-parse", "--is-inside-work-tree").stdout.strip() == "true"


requires_page_source = pytest.mark.skipif(
    not (page.FRONTEND / "package.json").is_file(), reason="no page source here (an install): nothing was edited"
)
requires_git = pytest.mark.skipif(not _in_a_git_checkout(), reason="not a git checkout")


@requires_page_source
def test_the_committed_editor_page_was_built_from_its_current_source() -> None:
    """THE GUARD. Fails when a file the build reads changed and the page was not rebuilt (or not committed)."""
    built = page.built_folder()
    recorded = page._built_from(built)
    assert recorded == page.fingerprint(page.FRONTEND), STALE.format(command=page.BUILD_COMMAND) + (
        f" (the page in {built} records source {recorded!r}; the source now is {page.fingerprint(page.FRONTEND)})"
    )


def test_the_page_is_built_into_a_folder_inside_the_package() -> None:
    """``streamstudio/canvas``: not ``page`` (``streamstudio/page.py`` is the module that checks it), not ``web`` (the
    ComfyUI editor's JavaScript), not ``static`` (Flask serves a ``static`` folder beside ``app.py`` by default), and
    not ``dist``/``build`` (this repository's ``.gitignore`` drops every folder of those names — they are Python's)."""
    assert page.built_folder() == PACKAGE / "canvas"
    assert page.OUT_DIR == "../streamstudio/canvas"


@requires_page_source
def test_vite_writes_where_the_check_reads_with_no_source_map() -> None:
    """``build.outDir`` in ``frontend/vite.config.ts`` is :data:`streamstudio.page.OUT_DIR`; vite empties it as a build
    starts (``emptyOutDir`` — outside vite's own folder it would otherwise refuse to), and writes no source map: the
    committed ``.js`` would name a map that no install carries."""
    config = (page.FRONTEND / "vite.config.ts").read_text()
    build = re.search(r"build:\s*\{([^}]*)\}", config)
    assert build is not None, config
    assert re.search(r'outDir:\s*"([^"]+)"', build.group(1)).group(1) == page.OUT_DIR  # type: ignore[union-attr]
    assert re.search(r"emptyOutDir:\s*true", build.group(1)), build.group(1)
    assert re.search(r"sourcemap:\s*false", build.group(1)), build.group(1)


@requires_page_source
def test_a_build_by_hand_begins_builds_and_records_in_that_order() -> None:
    """``npm run build`` brackets the page's own build with streamstudio's steps — the ONE fingerprint, never a second
    one in the page's tooling — so a page built by hand passes the guard (measured before: it left no record, and the
    next start built it once more). The first step takes the fingerprint BEFORE vite reads a file: a record made
    afterwards vouched for a line saved during the build that was not in the bundle."""
    scripts = json.loads((page.FRONTEND / "package.json").read_text())["scripts"]
    assert [step.strip() for step in scripts["build"].split("&&")] == [
        "python3 ../streamstudio/page.py begin",
        "npm run build:page",
        "python3 ../streamstudio/page.py",
    ]


@requires_page_source
def test_the_script_a_start_runs_needs_no_python() -> None:
    """A start's rebuild needs Node and nothing else: it asks npm for ``build:page`` — ``tsc --noEmit && vite build`` —
    and writes the record itself. Measured before: it ran ``build``, which ended with ``python3 ../streamstudio/
    page.py``, so a machine with Node and no ``python3`` command (python.org's Windows install has ``python`` and
    ``py``) built the page and was told ``npm run build exited with code 127``."""
    scripts = json.loads((page.FRONTEND / "package.json").read_text())["scripts"]
    command, args = page._STEPS["build"]
    assert (command, args) == ("npm run build:page", ("run", "build:page"))
    assert scripts["build:page"] == "tsc --noEmit && vite build"


@requires_git
@requires_page_source
def test_no_file_the_fingerprint_reads_is_one_git_ignores() -> None:
    """The record is compared with the source of EVERY clone, and a clone has no ignored file. Fails here — on the
    machine that holds one — instead of in CI, where the guard would fail for a reason this machine cannot show."""
    inputs = [f"frontend/{relative.as_posix()}" for relative in page.page_inputs(page.FRONTEND)]
    ignored = _git("check-ignore", "--no-index", "--", *inputs)
    assert ignored.returncode == 1 and ignored.stdout == "", ignored.stdout


@requires_git
@requires_page_source
@pytest.mark.parametrize("name", [".env.local", ".env.production.local", "src/notes.local", "public/favicon.svg.local"])
def test_the_local_files_the_fingerprint_skips_are_the_ones_git_ignores(name: str) -> None:
    """The two halves of one rule: ``page.page_inputs`` leaves out a name ending ``.local`` because
    ``frontend/.gitignore`` ignores ``*.local`` — change one and this fails until the other follows."""
    assert _git("check-ignore", "-q", "--no-index", f"frontend/{name}").returncode == 0


@requires_git
@pytest.mark.parametrize(
    "path, ignored",
    [
        ("streamstudio/canvas/index.html", False),
        ("streamstudio/canvas/.built-from", False),
        ("streamstudio/canvas/assets/index-AbCd1234.js", False),
        ("streamstudio/canvas/assets/index-AbCd1234.css", False),
        ("streamstudio/canvas/assets/index-AbCd1234.js.map", True),
        ("streamstudio/canvas/index.js.map", True),
    ],
)
def test_the_built_page_is_committed_and_a_source_map_never_is(path: str, ignored: bool) -> None:
    assert (_git("check-ignore", "-q", "--no-index", path).returncode == 0) is ignored, path


@requires_git
@requires_page_source
def test_the_page_source_is_checked_out_with_lf_line_endings_everywhere() -> None:
    """The fingerprint reads BYTES: a checkout that turned ``\\n`` into ``\\r\\n`` (git on Windows) would find every
    committed page out of date. ``frontend/.gitattributes`` makes git check the source out with LF on every machine."""
    inputs = [f"frontend/{relative.as_posix()}" for relative in page.page_inputs(page.FRONTEND)]
    answered = _git("check-attr", "eol", "--", *inputs).stdout.splitlines()
    not_lf: List[str] = [line for line in answered if not line.endswith(": eol: lf")]
    assert len(answered) == len(inputs) and not not_lf, not_lf
