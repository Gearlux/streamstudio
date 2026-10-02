"""The editor's page builds itself at a server's START when its source changed (user, 2026-10-02: "automatically
build when it changes").

Measured before: after a ``git pull`` that changed ``frontend/src``, every start served the page built BEFORE the
pull — or, on a machine that never built it, the placeholder ``The canvas page is not built. Build it once — cd
streamstudio/frontend && npm install && npm run build — and reload`` — until someone ran that command by hand. And
``page_blueprint`` decided ONCE, when the app was made, whether a page existed: a page built after that was never
served.

Every check here runs over a page folder of its own (``tmp_path``) with a FAKE ``npm`` and ``node`` on a PATH of their
own: ``npm ci`` makes ``node_modules`` and npm's record of the install, ``npm run build`` empties ``dist`` and writes
the page from ``src/App.tsx`` (what vite does), ``FAKE_NPM_FAIL=<ci|build>`` makes that step fail with output,
``FAKE_NPM_HANG=<ci|build>`` makes it start a child of its own and never finish (a registry that drops the traffic),
and ``FAKE_NPM_INTERRUPT=<ci|build>`` makes it send Ctrl+C's signal to the start while it runs. No test here runs the
real npm or touches the checkout's ``frontend/``.
"""

import errno
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from werkzeug.serving import make_server

from streamstudio import app as app_module
from streamstudio import cli, page
from streamstudio.app import page_blueprint

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake npm and node are POSIX scripts")

NOT_BUILT = "streamstudio: the editor's page is not built yet — building it"
CHANGED = "streamstudio: the editor's page changed since its last build — building it"
NO_RECORD = "streamstudio: the editor's page has no record of the source it was built from — building it"
INSTALLING = "streamstudio: installing the page's libraries first (npm ci) — the first time takes about a minute"
BUILT = re.compile(r"^streamstudio: built the editor's page in \d+\.\d s$")
NO_NODE_NOT_BUILT = "streamstudio: the editor's page is not built and Node is not installed — run aisland setup"
NO_NODE_OUT_OF_DATE = "streamstudio: the editor's page is out of date and Node is not installed — run aisland setup"
NO_NODE_NO_RECORD = (
    "streamstudio: the editor's page has no record of the source it was built from and Node is not installed — "
    "the page built earlier is served"
)
WAITING = "streamstudio: another start is building the editor's page — waiting for it"
INTERRUPTED = "streamstudio: interrupted before the editor's page was built — the next start builds it"

#: The files of a small page folder: what the build reads, and what it does not (tests, a Finder file).
PAGE_FILES = {
    "package.json": '{"name": "page", "scripts": {"build": "tsc --noEmit && vite build"}}',
    "package-lock.json": '{"lockfileVersion": 3}',
    "index.html": '<div id="root"></div><script type="module" src="/src/main.tsx"></script>',
    "vite.config.ts": "export default {}",
    "tsconfig.json": '{"include": ["src"]}',
    "src/main.tsx": "import { App } from './App'",
    "src/App.tsx": "export const App = 'first'",
    "src/canvas/Node.tsx": "export const Node = 1",
    "src/styles.css": "body {}",
    "src/__tests__/app.test.ts": "test('app', () => {})",
    "src/model.test.ts": "test('model', () => {})",
    "src/.DS_Store": "finder",
}

FAKE_NPM = """#!{python}
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)  # npm's lines and the error in the order they are written
here = Path.cwd()
args = sys.argv[1:]
with open(os.environ["FAKE_NPM_LOG"], "a") as log:
    log.write(" ".join(args) + "\\n")
step = {{("ci",): "ci", ("run", "build"): "build"}}.get(tuple(args))
if step is None:
    sys.exit("fake npm: unexpected " + " ".join(args))
if os.environ.get("FAKE_NPM_HANG") == step:  # npm, and a child of its own (vite, a download), never finish
    print(step + " started")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    Path(os.environ["FAKE_NPM_PIDS"]).write_text(f"{{os.getpid()}} {{child.pid}}")
    time.sleep(600)
if os.environ.get("FAKE_NPM_INTERRUPT") == step:  # the person presses Ctrl+C while npm runs
    Path(os.environ["FAKE_NPM_PIDS"]).write_text(str(os.getpid()))
    os.kill(os.getppid(), signal.SIGINT)
    time.sleep(600)
if os.environ.get("FAKE_NPM_FAIL") == step:
    for number in range(1, 31):
        print(step + " line " + str(number))
    print("src/App.tsx(1,7): error TS2304: Cannot find name 'oops'.", file=sys.stderr)
    sys.exit(2)
if step == "ci":
    shutil.rmtree(here / "node_modules", ignore_errors=True)
    (here / "node_modules").mkdir()
    (here / "node_modules" / ".package-lock.json").write_text("{{}}")
else:
    shutil.rmtree(here / "dist", ignore_errors=True)
    (here / "dist").mkdir()
    source = (here / "src" / "App.tsx").read_text()
    (here / "dist" / "index.html").write_text("<!doctype html><title>built</title>" + source)
print("vite v5.4.21 building for production...")
"""


class FakeNpm:
    """The fake ``npm``/``node`` on a PATH of their own; ``calls()`` answers what npm was asked, in order."""

    def __init__(self, folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.bin = folder / "bin"
        self.bin.mkdir()
        self.log = folder / "npm.log"
        self.log.write_text("")
        self.pids_file = folder / "npm.pids"
        self._monkeypatch = monkeypatch
        npm = self.bin / "npm"
        npm.write_text(FAKE_NPM.format(python=sys.executable))
        node = self.bin / "node"
        node.write_text(f"#!{sys.executable}\n")
        for script in (npm, node):
            script.chmod(0o755)
        monkeypatch.setenv("PATH", str(self.bin))
        monkeypatch.setenv("FAKE_NPM_LOG", str(self.log))
        monkeypatch.setenv("FAKE_NPM_PIDS", str(self.pids_file))
        for name in ("FAKE_NPM_FAIL", "FAKE_NPM_HANG", "FAKE_NPM_INTERRUPT"):
            monkeypatch.delenv(name, raising=False)

    def calls(self) -> List[str]:
        return self.log.read_text().splitlines()

    def fail(self, step: Optional[str]) -> None:
        if step is None:
            self._monkeypatch.delenv("FAKE_NPM_FAIL", raising=False)
        else:
            self._monkeypatch.setenv("FAKE_NPM_FAIL", step)

    def hang(self, step: str) -> None:
        self._monkeypatch.setenv("FAKE_NPM_HANG", step)

    def interrupt(self, step: str) -> None:
        self._monkeypatch.setenv("FAKE_NPM_INTERRUPT", step)

    def pids(self) -> List[int]:
        """npm's pid (and its own child's, for a hang), as the fake wrote them."""
        return [int(word) for word in self.pids_file.read_text().split()]


def _still_running(pid: int) -> bool:
    """Whether ``pid`` is a process that has not exited — a zombie waiting to be reaped has exited."""
    ps = shutil.which("ps", path="/bin:/usr/bin") or "ps"  # the test's own PATH holds only the fake npm and node
    state = subprocess.run([ps, "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return state != "" and not state.startswith(("Z", "E"))


def _installed(frontend: Path) -> None:
    """What an install leaves: ``node_modules`` and npm's record of it, written after the lock."""
    record = frontend / "node_modules" / ".package-lock.json"
    record.parent.mkdir(exist_ok=True)
    record.write_text("{}")
    later = (frontend / "package-lock.json").stat().st_mtime_ns + 1_000_000_000
    os.utime(record, ns=(later, later))


@pytest.fixture(autouse=True)
def a_fresh_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts as a new process does: no check of this process before it, nothing handed down to it."""
    monkeypatch.setattr(page, "_UNSETTLED", {})
    monkeypatch.delenv(page.HANDED_DOWN, raising=False)


def _the_next_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the NEXT start begins with: a new process, which remembers no check of an earlier one."""
    monkeypatch.setattr(page, "_UNSETTLED", {})


@pytest.fixture
def frontend(tmp_path: Path) -> Path:
    folder = tmp_path / "frontend"
    for name, text in PAGE_FILES.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text)
    _installed(folder)
    return folder


@pytest.fixture
def npm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeNpm:
    return FakeNpm(tmp_path, monkeypatch)


@pytest.fixture
def no_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A PATH on which neither node nor npm is found."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    yield


def _printed(capsys: pytest.CaptureFixture[str]) -> List[str]:
    return capsys.readouterr().out.splitlines()


def _page(dist: Optional[Path] = None) -> Flask:
    """An app serving only the page — ``page_blueprint(dist)``, as a host mounts it."""
    app = Flask("host")
    app.register_blueprint(page_blueprint(dist))
    return app


# ------------------------------------------------------------------------------------------------
# what the check reads
# ------------------------------------------------------------------------------------------------


class TestWhatTheBuildReads:
    def test_the_inputs_are_the_source_the_config_and_the_lock_never_tests_or_hidden_files(
        self, frontend: Path
    ) -> None:
        (frontend / "public").mkdir()
        (frontend / "public" / "favicon.svg").write_text("<svg/>")
        (frontend / "vite.config.js").write_text("export default {}")
        (frontend / "tsconfig.node.json").write_text("{}")
        (frontend / ".env.production").write_text("VITE_X=1")
        assert [path.as_posix() for path in page.page_inputs(frontend)] == [
            ".env.production",
            "index.html",
            "package-lock.json",
            "package.json",
            "public/favicon.svg",
            "src/App.tsx",
            "src/canvas/Node.tsx",
            "src/main.tsx",
            "src/styles.css",
            "tsconfig.json",
            "tsconfig.node.json",
            "vite.config.js",
            "vite.config.ts",
        ]

    @pytest.mark.parametrize(
        "name", ["src/App.tsx", "src/canvas/Node.tsx", "index.html", "package.json", "vite.config.ts", "tsconfig.json"]
    )
    def test_a_change_to_an_input_changes_the_fingerprint(self, frontend: Path, name: str) -> None:
        before = page.fingerprint(frontend)
        (frontend / name).write_text((frontend / name).read_text() + " ")
        assert page.fingerprint(frontend) != before

    @pytest.mark.parametrize("name", [".env", ".env.local", ".env.production", ".env.production.local"])
    def test_an_env_file_the_production_build_loads_changes_the_fingerprint(self, frontend: Path, name: str) -> None:
        """``vite build`` runs in production mode: these four are the ``.env`` files it reads."""
        before = page.fingerprint(frontend)
        (frontend / name).write_text("VITE_X=1")
        assert page.fingerprint(frontend) != before

    @pytest.mark.parametrize(
        "name",
        [
            "src/__tests__/app.test.ts",
            "src/model.test.ts",
            "src/.DS_Store",
            "dist/x.js",
            ".env.test",
            ".env.development",
            ".env.development.local",
            ".env.test.local",
        ],
    )
    def test_a_change_to_a_test_a_hidden_file_or_the_output_leaves_the_fingerprint(
        self, frontend: Path, name: str
    ) -> None:
        before = page.fingerprint(frontend)
        (frontend / name).parent.mkdir(parents=True, exist_ok=True)
        (frontend / name).write_text("changed")
        assert page.fingerprint(frontend) == before


# ------------------------------------------------------------------------------------------------
# the check at a start
# ------------------------------------------------------------------------------------------------


class TestTheStartCheck:
    def test_a_page_never_built_is_built_and_says_so(
        self, frontend: Path, npm: FakeNpm, capsys: pytest.CaptureFixture[str]
    ) -> None:
        check = page.ensure_page_built(frontend)
        printed = _printed(capsys)
        assert printed[0] == NOT_BUILT and BUILT.match(printed[1]) and len(printed) == 2, printed
        assert check.state == "built" and list(check.said) == printed
        assert npm.calls() == ["run build"], "node_modules is installed from this lock: no npm ci"
        assert (frontend / "dist" / "index.html").read_text().endswith("export const App = 'first'")
        assert (frontend / "dist" / page.STAMP).read_text().strip() == page.fingerprint(frontend)

    def test_an_unchanged_page_is_not_built_again_and_nothing_is_printed(
        self, frontend: Path, npm: FakeNpm, capsys: pytest.CaptureFixture[str]
    ) -> None:
        page.ensure_page_built(frontend)
        capsys.readouterr()
        check = page.ensure_page_built(frontend)
        assert check.state == "current" and check.said == ()
        assert capsys.readouterr().out == ""
        assert npm.calls() == ["run build"]

    def test_a_changed_source_file_is_built_again(
        self, frontend: Path, npm: FakeNpm, capsys: pytest.CaptureFixture[str]
    ) -> None:
        page.ensure_page_built(frontend)
        (frontend / "src" / "App.tsx").write_text("export const App = 'after the pull'")
        capsys.readouterr()
        check = page.ensure_page_built(frontend)
        printed = _printed(capsys)
        assert printed[0] == CHANGED and BUILT.match(printed[1]) and len(printed) == 2, printed
        assert check.state == "built"
        assert npm.calls() == ["run build", "run build"]
        assert (frontend / "dist" / "index.html").read_text().endswith("'after the pull'")

    def test_a_changed_test_file_builds_nothing(
        self, frontend: Path, npm: FakeNpm, capsys: pytest.CaptureFixture[str]
    ) -> None:
        page.ensure_page_built(frontend)
        (frontend / "src" / "__tests__" / "app.test.ts").write_text("test('changed', () => {})")
        capsys.readouterr()
        assert page.ensure_page_built(frontend).state == "current"
        assert capsys.readouterr().out == "" and npm.calls() == ["run build"]

    def test_a_page_built_by_hand_without_a_record_is_built_once(
        self, frontend: Path, npm: FakeNpm, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A page built before this check existed (or by ``npm run build`` by hand) holds no record of its source."""
        (frontend / "dist").mkdir()
        (frontend / "dist" / "index.html").write_text("<!doctype html><title>built by hand</title>")
        check = page.ensure_page_built(frontend)
        printed = _printed(capsys)
        assert printed[0] == NO_RECORD and BUILT.match(printed[1]), printed
        assert check.state == "built" and npm.calls() == ["run build"]
        assert page.ensure_page_built(frontend).state == "current"

    def test_a_record_that_is_not_a_fingerprint_counts_as_no_record(
        self, frontend: Path, npm: FakeNpm, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Measured before: a record holding bytes that are not UTF-8 stopped the start with ``UnicodeDecodeError``."""
        (frontend / "dist").mkdir()
        (frontend / "dist" / "index.html").write_text("<!doctype html><title>the old page</title>")
        (frontend / "dist" / page.STAMP).write_bytes(b"\xff\xfe")
        check = page.ensure_page_built(frontend)
        printed = _printed(capsys)
        assert printed[0] == NO_RECORD and BUILT.match(printed[1]), printed
        assert check.state == "built"

    def test_a_folder_that_cannot_be_opened_for_the_lock_is_built_without_it(
        self, frontend: Path, npm: FakeNpm, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Measured before: ``PermissionError`` from ``os.open`` of the folder stopped the start with a traceback."""
        frontend.chmod(0o311)  # entered, not listed — the lock opens the folder for reading
        try:
            check = page.ensure_page_built(frontend)
        finally:
            frontend.chmod(0o755)
        assert check.state == "built", _printed(capsys)

    def test_a_lock_the_filesystem_refuses_is_no_reason_not_to_build(
        self, frontend: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(fd: int, operation: int) -> None:
            raise OSError(errno.ENOLCK, "No locks available")

        monkeypatch.setattr(page.fcntl, "flock", refuse)
        assert page.ensure_page_built(frontend).state == "built"

    def test_missing_libraries_are_installed_first(
        self, frontend: Path, npm: FakeNpm, capsys: pytest.CaptureFixture[str]
    ) -> None:
        shutil.rmtree(frontend / "node_modules")
        check = page.ensure_page_built(frontend)
        printed = _printed(capsys)
        assert printed[:2] == [NOT_BUILT, INSTALLING] and BUILT.match(printed[2]) and len(printed) == 3, printed
        assert check.state == "built" and npm.calls() == ["ci", "run build"]

    def test_the_libraries_are_installed_again_when_the_lock_changed_since_the_last_install(
        self, frontend: Path, npm: FakeNpm, capsys: pytest.CaptureFixture[str]
    ) -> None:
        page.ensure_page_built(frontend)
        lock = frontend / "package-lock.json"
        lock.write_text('{"lockfileVersion": 3, "packages": {"node_modules/vite": {"version": "5.4.22"}}}')
        newer = (frontend / "node_modules" / ".package-lock.json").stat().st_mtime_ns + 1_000_000_000
        os.utime(lock, ns=(newer, newer))  # what a pull that changes the lock leaves
        capsys.readouterr()
        page.ensure_page_built(frontend)
        printed = _printed(capsys)
        assert printed[:2] == [CHANGED, INSTALLING] and BUILT.match(printed[2]), printed
        assert npm.calls() == ["run build", "ci", "run build"]
        page.ensure_page_built(frontend)
        assert npm.calls() == ["run build", "ci", "run build"], "installed and built from this lock: nothing to do"

    def test_a_folder_without_page_source_is_never_checked(
        self, tmp_path: Path, npm: FakeNpm, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An install from a wheel: the package has no ``frontend/`` beside it."""
        nothing = tmp_path / "wheel-install" / "frontend"
        check = page.ensure_page_built(nothing)
        assert check.state == "no-source" and check.said == ()
        assert capsys.readouterr().out == "" and npm.calls() == []
        assert not nothing.exists()

    def test_the_default_folder_is_the_one_beside_the_package(self) -> None:
        assert page.FRONTEND == Path(page.__file__).resolve().parent.parent / "frontend"


class TestWithoutNode:
    def test_a_page_never_built_starts_anyway_and_says_to_run_aisland_setup(
        self, frontend: Path, no_node: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        check = page.ensure_page_built(frontend)
        assert _printed(capsys) == [NO_NODE_NOT_BUILT]
        assert check.state == "no-node" and not (frontend / "dist").exists()

    def test_an_out_of_date_page_starts_anyway_and_says_to_run_aisland_setup(
        self, frontend: Path, no_node: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (frontend / "dist").mkdir()
        (frontend / "dist" / "index.html").write_text("<!doctype html><title>the old page</title>")
        (frontend / "dist" / page.STAMP).write_text("0" * 64)
        check = page.ensure_page_built(frontend)
        assert _printed(capsys) == [NO_NODE_OUT_OF_DATE]
        assert check.state == "no-node"

    def test_a_current_page_needs_no_node(
        self, frontend: Path, npm: FakeNpm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page.ensure_page_built(frontend)
        empty = tmp_path / "nothing-on-path"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        assert page.ensure_page_built(frontend).state == "current"

    def test_a_page_with_no_record_is_served_as_it_is_and_the_start_says_so(
        self, frontend: Path, no_node: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A page built by hand (or before the record existed) may well be current: without Node the start cannot
        tell, so it keeps serving it — measured before: ``/`` stopped serving it and said it was out of date."""
        (frontend / "dist").mkdir()
        (frontend / "dist" / "index.html").write_text("<!doctype html><title>built by hand</title>")
        check = page.ensure_page_built(frontend)
        assert _printed(capsys) == [NO_NODE_NO_RECORD]
        assert check.state == "unverified"
        assert page.unbuildable(frontend / "dist") is None
        assert _page(frontend / "dist").test_client().get("/").get_data(as_text=True) == (
            "<!doctype html><title>built by hand</title>"
        )


class TestAFailingBuild:
    @pytest.mark.parametrize("step, command", [("build", "npm run build"), ("ci", "npm ci")])
    def test_the_start_goes_on_and_prints_npms_last_lines(
        self, frontend: Path, npm: FakeNpm, capsys: pytest.CaptureFixture[str], step: str, command: str
    ) -> None:
        if step == "ci":
            (frontend / "node_modules" / ".package-lock.json").unlink()
        npm.fail(step)
        check = page.ensure_page_built(frontend)
        printed = _printed(capsys)
        failed = printed.index(
            f"streamstudio: building the editor's page failed — {command} exited with code 2; its last lines:"
        )
        assert printed[failed + 1 :] == [f"  {step} line {number}" for number in range(12, 31)] + [
            "  src/App.tsx(1,7): error TS2304: Cannot find name 'oops'."
        ]
        assert check.state == "failed" and check.why == f"{command} exited with code 2"
        assert "error TS2304" in check.output
        assert not (frontend / "dist" / page.STAMP).exists()

    def test_an_npm_that_cannot_be_started_is_a_failure_not_a_traceback(
        self, frontend: Path, npm: FakeNpm, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (npm.bin / "npm").write_text("#!/no/such/interpreter\n")
        check = page.ensure_page_built(frontend)
        printed = _printed(capsys)
        assert printed[0] == NOT_BUILT
        assert printed[1].startswith("streamstudio: building the editor's page failed — "), printed
        assert check.state == "failed"

    def test_the_next_start_tries_again(
        self, frontend: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        npm.fail("build")
        page.ensure_page_built(frontend)
        npm.fail(None)
        _the_next_start(monkeypatch)
        assert page.ensure_page_built(frontend).state == "built"
        assert npm.calls() == ["run build", "run build"]

    @pytest.mark.parametrize("step, command", [("build", "npm run build"), ("ci", "npm ci")])
    def test_an_npm_that_never_finishes_is_stopped_with_everything_it_started(
        self,
        frontend: Path,
        npm: FakeNpm,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        step: page.NpmStep,
        command: str,
    ) -> None:
        """Measured before: with a registry that drops the traffic, ``npm ci`` still ran after 10 minutes and the
        server never started; when the start was killed, npm went on running on its own."""
        if step == "ci":
            (frontend / "node_modules" / ".package-lock.json").unlink()
        monkeypatch.setitem(page.TIMEOUTS, step, 1.0)
        npm.hang(step)
        check = page.ensure_page_built(frontend)
        printed = _printed(capsys)
        failed = printed.index(
            f"streamstudio: building the editor's page failed — {command} did not finish in 1 s; its last lines:"
        )
        assert printed[failed + 1 :] == [f"  {step} started"]
        assert check.state == "failed" and check.why == f"{command} did not finish in 1 s"
        npm_pid, its_child = npm.pids()
        assert not _still_running(npm_pid) and not _still_running(its_child)
        assert not (frontend / "dist" / page.STAMP).exists()

    def test_ctrl_c_while_npm_runs_ends_the_start_in_one_line_and_stops_npm(self, frontend: Path, npm: FakeNpm) -> None:
        """Measured before: a 45-line traceback ending ``KeyboardInterrupt`` and exit code -2."""
        npm.interrupt("build")
        start = (
            "import sys; from pathlib import Path; from streamstudio import page; "
            "page.ensure_page_built(Path(sys.argv[1]))"
        )
        done = subprocess.run(
            [sys.executable, "-c", start, str(frontend)], capture_output=True, text=True, timeout=120, cwd=frontend
        )
        said = [line for line in done.stdout.splitlines() if line.startswith("streamstudio:")]
        assert said == [NOT_BUILT, INTERRUPTED], done.stdout
        assert done.returncode == 130 and "Traceback" not in done.stderr, done.stderr
        assert not _still_running(npm.pids()[0])


class TestTwoStartsAtOnce:
    def test_the_second_waits_for_the_first_build_and_then_finds_the_page_current(
        self, frontend: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The test holds the folder's lock as the FIRST start's build would; the second start asks for it (a
        recorded ``flock``), waits, and — the first having built the page — finds it current: npm never runs."""
        real_flock = page.fcntl.flock
        arrived = threading.Event()
        second: List[threading.Thread] = []

        def flock(fd: int, operation: int) -> None:
            if threading.current_thread() in second:
                arrived.set()
            real_flock(fd, operation)

        monkeypatch.setattr(page.fcntl, "flock", flock)
        results: List[page.PageCheck] = []
        with page._held(frontend, lambda line: None):
            thread = threading.Thread(target=lambda: results.append(page.ensure_page_built(frontend)))
            second.append(thread)
            thread.start()
            assert arrived.wait(timeout=30), "the second start never asked for the folder's lock"
            assert thread.is_alive(), "the second start did not wait for the first"
            (frontend / "dist").mkdir()  # what the first start's build leaves
            (frontend / "dist" / "index.html").write_text("<!doctype html><title>built by the first</title>")
            (frontend / "dist" / page.STAMP).write_text(page.fingerprint(frontend))
        thread.join(timeout=30)
        assert [check.state for check in results] == ["current"]
        assert results[0].said == (WAITING,), "measured before: the second start waited 31 s and said nothing"
        assert npm.calls() == []

    def test_only_a_build_takes_the_lock(self, frontend: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch) -> None:
        """A current page (and one without Node) takes no lock: a second start never waits on a check that only
        compares, and the line a waiting start prints is true — another start IS building."""
        page.ensure_page_built(frontend)
        locks: List[int] = []
        real_flock = page.fcntl.flock

        def flock(fd: int, operation: int) -> None:
            locks.append(operation)
            real_flock(fd, operation)

        monkeypatch.setattr(page.fcntl, "flock", flock)
        assert page.ensure_page_built(frontend).state == "current"
        assert locks == []


class TestOneStartChecksOnce:
    """A start may ask more than once (a host's command line, then the server it starts): an answer that did not make
    the page current is not repeated — no second npm run, no line printed twice — and a process the start launches to
    serve takes the answer over instead of running npm again where its output is discarded."""

    def test_a_second_check_in_the_same_process_repeats_neither_npm_nor_its_lines(
        self, frontend: Path, npm: FakeNpm, capsys: pytest.CaptureFixture[str]
    ) -> None:
        npm.fail("build")
        first = page.ensure_page_built(frontend)
        capsys.readouterr()
        again = page.ensure_page_built(frontend)
        assert capsys.readouterr().out == ""
        assert npm.calls() == ["run build"]
        assert (again.state, again.why, again.output, again.said) == ("failed", first.why, first.output, ())

    def test_without_node_the_line_is_printed_once(
        self, frontend: Path, no_node: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        page.ensure_page_built(frontend)
        assert page.ensure_page_built(frontend).state == "no-node"
        assert _printed(capsys) == [NO_NODE_NOT_BUILT]

    def test_a_source_changed_since_is_checked_again(self, frontend: Path, npm: FakeNpm) -> None:
        npm.fail("build")
        page.ensure_page_built(frontend)
        npm.fail(None)
        (frontend / "src" / "App.tsx").write_text("export const App = 'fixed'")
        assert page.ensure_page_built(frontend).state == "built"

    def test_a_process_started_to_serve_takes_over_the_answer_instead_of_running_npm(
        self, frontend: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Measured before: a ``--background`` start whose ``npm ci`` failed in the terminal ran it AGAIN in the
        serving process, where its output is discarded — two ``npm ci`` 33 s apart."""
        npm.fail("build")
        first = page.ensure_page_built(frontend)
        handed = page.page_check_environment(frontend)
        assert list(handed) == [page.HANDED_DOWN]

        _the_next_start(monkeypatch)  # the serving process: a new one, with the environment it was given
        monkeypatch.setenv(page.HANDED_DOWN, handed[page.HANDED_DOWN])
        capsys.readouterr()
        taken = page.ensure_page_built(frontend)
        assert (taken.state, taken.why, taken.output) == ("failed", first.why, first.output)
        assert capsys.readouterr().out == "" and npm.calls() == ["run build"]
        problem = page.unbuildable(frontend / "dist")
        assert problem is not None and problem.state == "failed", "the served page says the build failed"
        assert page.HANDED_DOWN not in os.environ, "the answer is for that one process, not for what it starts"

    def test_nothing_is_handed_down_when_the_page_is_current(self, frontend: Path, npm: FakeNpm) -> None:
        assert page.page_check_environment(frontend) == {}, "no check yet"
        page.ensure_page_built(frontend)
        assert page.page_check_environment(frontend) == {}

    @pytest.mark.parametrize("field, value", [("source", "0" * 64), ("dist", "/elsewhere/dist")])
    def test_an_answer_about_another_source_or_folder_is_not_taken_over(
        self, frontend: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch, field: str, value: str
    ) -> None:
        npm.fail("build")
        page.ensure_page_built(frontend)
        handed: Dict[str, Any] = json.loads(page.page_check_environment(frontend)[page.HANDED_DOWN])
        handed[field] = value
        _the_next_start(monkeypatch)
        monkeypatch.setenv(page.HANDED_DOWN, json.dumps(handed))
        npm.fail(None)
        assert page.ensure_page_built(frontend).state == "built"

    def test_an_unreadable_answer_is_ignored(
        self, frontend: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(page.HANDED_DOWN, "not json")
        assert page.ensure_page_built(frontend).state == "built"
        assert page.HANDED_DOWN not in os.environ


# ------------------------------------------------------------------------------------------------
# the page served at / follows the check — per request
# ------------------------------------------------------------------------------------------------


class TestThePageServed:
    def test_a_page_built_after_the_app_was_made_is_served(self, frontend: Path) -> None:
        client = _page(frontend / "dist").test_client()
        assert "The canvas page is not built" in client.get("/").get_data(as_text=True)
        (frontend / "dist").mkdir()
        (frontend / "dist" / "index.html").write_text("<!doctype html><title>built</title>")
        (frontend / "dist" / "app.js").write_text("console.log(1)")
        assert client.get("/").get_data(as_text=True) == "<!doctype html><title>built</title>"
        assert client.get("/app.js").status_code == 200

    def test_without_node_the_page_says_so_instead_of_serving_the_old_page(self, frontend: Path, no_node: None) -> None:
        (frontend / "dist").mkdir()
        (frontend / "dist" / "index.html").write_text("<!doctype html><title>the old page</title>")
        (frontend / "dist" / page.STAMP).write_text("0" * 64)  # built from another source: out of date
        page.ensure_page_built(frontend)
        text = _page(frontend / "dist").test_client().get("/").get_data(as_text=True)
        assert "The editor&#x27;s page is out of date and Node is not installed — run aisland setup" in text, text
        assert "the old page" not in text

    def test_without_node_a_page_never_built_says_so(self, frontend: Path, no_node: None) -> None:
        page.ensure_page_built(frontend)
        text = _page(frontend / "dist").test_client().get("/").get_data(as_text=True)
        assert "The editor&#x27;s page is not built and Node is not installed — run aisland setup" in text, text

    def test_a_failed_build_says_so_names_the_command_that_shows_why_and_its_last_lines(
        self, frontend: Path, npm: FakeNpm
    ) -> None:
        npm.fail("build")
        page.ensure_page_built(frontend)
        text = _page(frontend / "dist").test_client().get("/").get_data(as_text=True)
        assert "Building the editor&#x27;s page failed when this server started" in text, text
        assert page.BUILD_COMMAND.replace("&", "&amp;") in text
        assert "error TS2304: Cannot find name &#x27;oops&#x27;." in text

    def test_a_later_good_build_serves_the_page_again(
        self, frontend: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _page(frontend / "dist").test_client()
        npm.fail("build")
        page.ensure_page_built(frontend)
        npm.fail(None)
        _the_next_start(monkeypatch)
        page.ensure_page_built(frontend)
        assert client.get("/").get_data(as_text=True).endswith("export const App = 'first'")

    def test_the_default_page_follows_the_check_of_the_checkouts_folder(
        self, frontend: Path, no_node: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(page, "FRONTEND", frontend)
        page.ensure_page_built()
        text = _page().test_client().get("/").get_data(as_text=True)
        assert "is not built and Node is not installed — run aisland setup" in text, text

    def test_a_check_of_another_folder_leaves_a_page_it_did_not_check(
        self, frontend: Path, no_node: None, tmp_path: Path
    ) -> None:
        """Without a start check of ITS folder (an app made in a test), a blueprint serves what its folder holds."""
        page.ensure_page_built(frontend)
        other = tmp_path / "other-dist"
        other.mkdir()
        (other / "index.html").write_text("<!doctype html><title>other</title>")
        assert _page(other).test_client().get("/").get_data(as_text=True) == "<!doctype html><title>other</title>"


#: A host page holding the editor's page in a panel, as a host app does (``<iframe src="/studio/?embed=1">``), and
#: keeping every message the frame posts it. ``FRAME`` is replaced by the frame's link.
HOST_PAGE = """<!doctype html><meta charset="utf-8"><title>host</title>
<script>
  window.got = [];
  addEventListener("message", (event) => { if (event.origin === location.origin) window.got.push(event.data); });
</script>
<iframe id="panel" src="FRAME"></iframe>
"""


class TestAHostPanelIsToldThePageIsUnavailable:
    """The page served instead of the editor — the placeholder, no Node, a failed build — tells a host's panel
    (``?embed=1``) that no editor will answer: ``{source: "streamstudio", type: "unavailable"}``. Measured before: a
    host covering its panel until the editor's first ``state`` message covered these pages for good — the person saw
    ``loading sink…`` over ``…Node is not installed — run aisland setup…``."""

    @staticmethod
    def _messages(dist: Path, frame: str) -> List[Any]:
        """What the host page received once the panel's page has loaded, in a headless chromium."""
        playwright_api = pytest.importorskip("playwright.sync_api")
        host = Flask("host")
        host.register_blueprint(page_blueprint(dist), url_prefix="/studio")
        host.add_url_rule("/", "host", lambda: HOST_PAGE.replace("FRAME", frame))
        # The placeholder lists the app's api/ routes, and the editor's own include `…/templates/<template>`: written
        # unescaped, `<template>` swallowed the rest of the page, the message script included (measured in a host).
        host.add_url_rule("/studio/api/templates/<template>", "a_route", lambda template: "")
        httpd = make_server("127.0.0.1", 0, host, threaded=True)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with playwright_api.sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch()
                except playwright_api.Error as exc:  # the browser binary is absent
                    pytest.skip(f"headless chromium is not installed (playwright install chromium): {exc}")
                try:
                    tab = browser.new_page()
                    tab.goto(f"http://127.0.0.1:{httpd.server_port}/")
                    tab.wait_for_function(
                        "() => { const d = document.getElementById('panel').contentDocument;"
                        " return !!d && d.readyState === 'complete' && d.title === 'streamstudio editor'; }"
                    )
                    # A message the host posts itself now arrives AFTER anything the frame posted while it loaded
                    # (one queue, first in first out): what `got` holds then is everything the frame said.
                    return tab.evaluate(  # type: ignore[no-any-return]
                        """() => new Promise((resolve) => {
                          addEventListener("message", function done(event) {
                            if (event.data !== "the end") return;
                            removeEventListener("message", done);
                            resolve(window.got.filter((data) => data !== "the end"));
                          });
                          postMessage("the end", location.origin);
                        })"""
                    )
                finally:
                    browser.close()
        finally:
            httpd.shutdown()
            thread.join(timeout=10)

    def test_the_placeholder_tells_the_panel(self, frontend: Path) -> None:
        assert self._messages(frontend / "dist", "/studio/?embed=1") == [
            {"source": "streamstudio", "type": "unavailable"}
        ]

    def test_the_no_node_page_tells_the_panel(self, frontend: Path, tmp_path: Path) -> None:
        with pytest.MonkeyPatch.context() as only_here:  # no Node for the check; the browser keeps its own PATH
            empty = tmp_path / "empty-bin"
            empty.mkdir()
            only_here.setenv("PATH", str(empty))
            assert page.ensure_page_built(frontend).state == "no-node"
        assert self._messages(frontend / "dist", "/studio/?graph=sink&embed=1") == [
            {"source": "streamstudio", "type": "unavailable"}
        ]

    def test_the_failed_build_page_tells_the_panel(self, frontend: Path, tmp_path: Path) -> None:
        with pytest.MonkeyPatch.context() as only_here:
            npm = FakeNpm(tmp_path, only_here)
            npm.fail("build")
            assert page.ensure_page_built(frontend).state == "failed"
        assert self._messages(frontend / "dist", "/studio/?embed=1") == [
            {"source": "streamstudio", "type": "unavailable"}
        ]

    def test_a_page_outside_a_hosts_panel_posts_nothing(self, frontend: Path) -> None:
        """The con case: without ``?embed=1`` the page is not in a host's panel, and says nothing to its parent."""
        assert self._messages(frontend / "dist", "/studio/") == []


# ------------------------------------------------------------------------------------------------
# who calls it: a START, never the making of an app
# ------------------------------------------------------------------------------------------------


class TestWhoChecks:
    def test_the_host_finds_the_check_where_it_imports_the_editor(self) -> None:
        assert "ensure_page_built" in app_module.__all__

    def test_making_an_app_checks_nothing(self, tmp_path: Path, page_checks: List[str]) -> None:
        app_module.create_app(tmp_path)
        _page()
        assert page_checks == []

    def test_the_canvas_editor_checks_its_page_before_it_serves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, page_checks: List[str]
    ) -> None:
        monkeypatch.setattr(cli, "_port_in_use", lambda port, host=cli.STUDIO_HOST: False)
        monkeypatch.setattr(cli, "_unsaved_work", lambda port: None)
        double = MagicMock()
        double.extensions = {app_module.EXTENSION: app_module.Studio(tmp_path)}
        double.run.side_effect = lambda **kwargs: page_checks.append("serving")

        def create(*args: object, **kwargs: object) -> MagicMock:
            page_checks.append("app made")
            return double

        monkeypatch.setattr(sys, "argv", ["streamstudio", str(tmp_path), "--port", "9123"])
        with patch("streamstudio.app.create_app", side_effect=create):
            cli.main()
        assert page_checks == ["checked", "app made", "serving"]

    def test_a_port_taken_while_the_page_was_built_is_refused_in_streamstudios_words(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        page_checks: List[str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Measured before: two starts on one port both passed the port check during a 5 s build, and the second
        died in werkzeug's words — ``Address already in use`` / ``Port 8193 is in use by another program…``."""
        probes: List[str] = []

        def in_use(port: int, host: str = cli.STUDIO_HOST) -> bool:
            probes.append("port probed")
            page_checks.append("port probed")
            return len(probes) > 1  # free before the page was checked, taken after

        monkeypatch.setattr(cli, "_port_in_use", in_use)
        monkeypatch.setattr(cli, "_unsaved_work", lambda port: None)
        monkeypatch.setattr(sys, "argv", ["streamstudio", str(tmp_path), "--port", "9123"])
        with patch("streamstudio.app.create_app") as create_app_mock, pytest.raises(SystemExit) as ended:
            cli.main()
        assert ended.value.code == 1
        create_app_mock.assert_not_called()
        assert page_checks[-3:] == ["port probed", "checked", "port probed"]
        assert "9123" in capsys.readouterr().out

    def test_a_refused_port_checks_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, page_checks: List[str]
    ) -> None:
        monkeypatch.setattr(cli, "_port_in_use", lambda port, host=cli.STUDIO_HOST: True)
        monkeypatch.setattr(cli, "_unsaved_work", lambda port: None)
        monkeypatch.setattr(sys, "argv", ["streamstudio", str(tmp_path), "--port", "9123"])
        with patch("streamstudio.app.create_app") as create_app_mock, pytest.raises(SystemExit):
            cli.main()
        create_app_mock.assert_not_called()
        assert page_checks == []
