"""The signal path, against a REAL editor process: a stop keeps the unsaved work, the next start offers it.

Each test starts ``python -m streamstudio.cli run <folder> --port <free port>`` as a child process over a temporary
folder, waits until it LISTENS (werkzeug's own ``Running on http://…`` line — read from the pipe, never a sleep),
makes an edit through the API, and stops it the way a person does:

* ``SIGTERM`` — what ``streamstudio stop`` and ``streamstudio restart`` send (``cli._stop_studio``);
* ``SIGINT`` — Ctrl+C in the terminal the editor runs in;
* ``SIGHUP`` — the terminal window the editor runs in is closed (POSIX only).

Measured before this change: SIGTERM ended the editor at once (exit code ``-15``) and its unsaved edits were gone.
Now both signals take the same way out — the server stops serving, ``Studio.stash`` writes
``<folder>/.streamstudio-workspace.json`` when something is unsaved, and the process exits 0.

Only the port a test picked is stopped, and only the process the test started is signalled or killed.
"""

import json
import os
import pty
import queue
import select
import signal
import socket
import subprocess
import sys
import threading
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest

from streamstudio import cli
from streamstudio.app import RECOVERY_FILE

PROBE = "recordstream.ops.debug.PrintRecordOp"
THRESHOLD = "recordstream.ops.numpy.Threshold"
FLOW_TEXT = f"""\
# a two-step flow
flow:
  probe:
    op: !class:{PROBE} {{label: probe}}
  mask:
    op: !class:{THRESHOLD} {{low_level: 0.5}}   # the mask
outputs: mask
"""
EDIT = {"node": "mask", "param": "low_level", "value": 0.9}
#: How long a start (imports, ~6 s measured) and a stop may take before the test fails instead of hanging.
START_TIMEOUT = 180.0
STOP_TIMEOUT = 60.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((cli.STUDIO_HOST, 0))
        return int(probe.getsockname()[1])


class _Editor:
    """A canvas editor in a child process, serving ``folder`` on a free port; ``out`` collects what it prints."""

    def __init__(self, folder: Path, work: Path) -> None:
        self.folder = folder
        self.port = _free_port()
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "streamstudio.cli",
                "run",
                str(folder),
                "--port",
                str(self.port),
                "--templates-dir",
                str(work / "templates"),
            ],
            cwd=work,  # never the workspace root: its liquifai/ folder shadows the installed package
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.out: List[str] = []
        self.err: List[str] = []
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._readers = [
            threading.Thread(target=self._drain, args=(self.proc.stdout, self.out), daemon=True),
            threading.Thread(target=self._drain, args=(self.proc.stderr, self.err), daemon=True),
        ]
        for reader in self._readers:
            reader.start()
        ready = f"Running on http://{cli.STUDIO_HOST}:{self.port}"
        while True:
            line = self._lines.get(timeout=START_TIMEOUT)
            if line is None and self.proc.poll() is not None:
                raise AssertionError(f"the editor exited before listening:\n{''.join(self.out + self.err)}")
            if line is not None and ready in line:
                return

    def _drain(self, stream: Any, into: List[str]) -> None:  # stream: the child's text pipe
        for line in stream:
            into.append(line)
            self._lines.put(line)
        self._lines.put(None)

    def call(self, method: str, route: str, body: Optional[Dict[str, Any]] = None) -> Any:  # the JSON answer
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            f"http://{cli.STUDIO_HOST}:{self.port}/{route}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=STOP_TIMEOUT) as response:
            return json.loads(response.read())

    def stop(self, signum: int) -> int:
        """Signal THIS process and wait for it to exit (``Popen.wait`` with a timeout — never a sleep)."""
        self.proc.send_signal(signum)
        return self.wait()

    def wait(self) -> int:
        code = self.proc.wait(timeout=STOP_TIMEOUT)
        for reader in self._readers:
            reader.join(timeout=STOP_TIMEOUT)
        return code

    def printed(self) -> str:
        return "".join(self.out)

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=STOP_TIMEOUT)


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    graphs = tmp_path / "graphs"
    graphs.mkdir()
    (graphs / "demo.yaml").write_text(FLOW_TEXT)
    return graphs


@pytest.fixture
def start(folder: Path, tmp_path: Path) -> Iterator[Any]:
    """Start an editor over ``folder``; every editor a test started is killed at the end if it is still running."""
    started: List[_Editor] = []

    def starting() -> _Editor:
        editor = _Editor(folder, tmp_path)
        started.append(editor)
        return editor

    yield starting
    for editor in started:
        editor.kill()


def _kept(folder: Path) -> Dict[str, Any]:
    data: Dict[str, Any] = json.loads((folder / RECOVERY_FILE).read_text())
    return data


def _kept_line(folder: Path) -> str:
    return f"Unsaved work (demo.yaml) is kept in {folder / RECOVERY_FILE} — the next start asks what to do."


#: The ways a person stops the editor. SIGHUP (the terminal window closed) exists on POSIX only; measured before
#: 2026-09-30: it ended the editor at once (exit code -1) and the unsaved edit was gone.
WAYS_OUT = [
    pytest.param(signal.SIGTERM, id="SIGTERM (stop, restart)"),
    pytest.param(signal.SIGINT, id="SIGINT (Ctrl+C)"),
    pytest.param(
        getattr(signal, "SIGHUP", signal.SIGTERM),
        id="SIGHUP (the terminal window closed)",
        marks=pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="SIGHUP exists on POSIX only"),
    ),
]


@pytest.mark.parametrize("signum", WAYS_OUT)
def test_a_stop_keeps_the_unsaved_edit_and_the_next_start_offers_it(folder: Path, start: Any, signum: int) -> None:
    first = start()
    first.call("POST", "api/graphs/demo/param", EDIT)
    assert first.stop(signum) == 0
    kept = _kept(folder)
    [entry] = kept["graphs"]
    assert entry["name"] == "demo"
    assert "low_level: 0.9" in entry["texts"][str((folder / "demo.yaml").resolve())]
    assert (folder / "demo.yaml").read_text() == FLOW_TEXT  # nothing was saved
    assert _kept_line(folder) in first.printed()

    second = start()
    recovery = second.call("GET", "api/workspace")["recovery"]
    assert recovery["graphs"] == [
        {
            "name": "demo",
            "path": str(folder / "demo.yaml"),
            "disk": "unchanged",
            "reason": None,
            "changed_on_disk": False,
            "saved_at": kept["saved_at"],
        }
    ]
    before = (folder / RECOVERY_FILE).read_bytes()
    assert second.stop(signal.SIGTERM) == 0
    # nobody answered and nothing is unsaved: the file waits for the next start, as it was
    assert (folder / RECOVERY_FILE).read_bytes() == before
    when = datetime.fromisoformat(kept["saved_at"]).astimezone()
    assert (
        f"Unsaved work from the last session ({when.day} {when:%b %H:%M}): demo.yaml — the page asks whether to "
        "restore it."
    ) in second.printed()


def test_a_stop_with_nothing_unsaved_writes_no_file(folder: Path, start: Any) -> None:
    editor = start()
    editor.call("GET", "api/graphs/demo")
    assert editor.stop(signal.SIGTERM) == 0
    assert not (folder / RECOVERY_FILE).exists()
    assert "Unsaved work" not in editor.printed()


def test_restore_then_save_then_stop_leaves_no_file(folder: Path, start: Any) -> None:
    first = start()
    first.call("POST", "api/graphs/demo/param", EDIT)
    assert first.stop(signal.SIGTERM) == 0
    second = start()
    listed = second.call("POST", "api/workspace/recovery/restore")
    assert [(entry["name"], entry["dirty"]) for entry in listed] == [("demo", True)]
    second.call("POST", "api/graphs/demo/save")
    assert second.stop(signal.SIGTERM) == 0
    assert not (folder / RECOVERY_FILE).exists()
    assert "low_level: 0.9" in (folder / "demo.yaml").read_text()


def test_streamstudio_stop_names_the_unsaved_work_it_kept(
    folder: Path, start: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    editor = start()
    editor.call("POST", "api/graphs/demo/param", EDIT)
    monkeypatch.setattr(sys, "argv", ["streamstudio", "stop", "--port", str(editor.port)])
    cli.main()
    assert editor.wait() == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        f"Stopped StreamStudio on port {editor.port} (pid {editor.proc.pid}). Its unsaved work (demo.yaml) is kept "
        f"in {folder / RECOVERY_FILE} — the next start asks what to do."
    )
    assert os.path.exists(folder / RECOVERY_FILE)


def test_two_editors_on_one_folder_stopped_at_once_both_keep_their_work(
    folder: Path, tmp_path: Path, start: Any
) -> None:
    """Measured before (R3, 4 of 4 trials): two editors of one folder sent SIGTERM at once (a logout, ``killall``) —
    one editor's work was lost while its terminal said it was kept, the other's said it was NOT kept although it was,
    and once the file came out ``Extra data: line 21`` (not JSON). Now each stop merges under the folder's lock through
    a file of its own."""
    (folder / "flow.yaml").write_text(FLOW_TEXT)
    first, second = start(), start()
    first.call("POST", "api/graphs/demo/param", EDIT)
    second.call("POST", "api/graphs/flow/param", {**EDIT, "value": 0.99})
    first.proc.send_signal(signal.SIGTERM)
    second.proc.send_signal(signal.SIGTERM)
    assert (first.wait(), second.wait()) == (0, 0)
    kept = _kept(folder)
    assert sorted(entry["name"] for entry in kept["graphs"]) == ["demo", "flow"]
    assert [path.name for path in folder.iterdir() if path.name.startswith(".")] == [RECOVERY_FILE]
    for editor, name in ((first, "demo"), (second, "flow")):
        line = f"Unsaved work ({name}.yaml) is kept in {folder / RECOVERY_FILE} — the next start asks what to do."
        assert line in editor.printed(), editor.printed()


def test_a_restart_with_no_folder_serves_the_folder_the_running_editor_served(
    folder: Path, tmp_path: Path, start: Any
) -> None:
    """Measured before (R6): ``streamstudio restart --port N`` typed in another folder served THAT folder — an empty
    list, no question — while the work the stop kept waited in the folder the stopped editor had served."""
    first = start()
    first.call("POST", "api/graphs/demo/param", EDIT)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    restarted = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamstudio.cli",
            "restart",
            "--port",
            str(first.port),
            "--templates-dir",
            str(tmp_path / "templates"),
        ],
        cwd=elsewhere,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # werkzeug's "Running on" line and the CLI's own lines, in one pipe
        text=True,
    )
    try:
        assert first.wait() == 0
        ready = f"Running on http://{cli.STUDIO_HOST}:{first.port}"
        seen: List[str] = []
        assert restarted.stdout is not None
        for line in restarted.stdout:
            seen.append(line)
            if ready in line:
                break
        else:
            raise AssertionError(f"the restarted editor never listened:\n{''.join(seen)}")
        workspace = first.call("GET", "api/workspace")
        assert workspace["folder"] == str(folder)
        assert [graph["name"] for graph in workspace["recovery"]["graphs"]] == ["demo"]
    finally:
        restarted.send_signal(signal.SIGTERM)
        restarted.wait(timeout=STOP_TIMEOUT)


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="SIGHUP and pseudo-terminals exist on POSIX only")
def test_a_closed_terminal_window_keeps_the_work_although_nothing_can_be_printed(folder: Path, tmp_path: Path) -> None:
    """The editor runs in a terminal (a pseudo-terminal here) that is CLOSED before it stops: every line it prints
    after that fails (``EIO``), and the kept work must not depend on those lines. The terminal's side is closed first,
    then SIGHUP is sent — what closing the window does."""
    port = _free_port()
    terminal, editor_side = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-m", "streamstudio.cli", "run", str(folder), "--port", str(port)]
        + ["--templates-dir", str(tmp_path / "templates")],
        cwd=tmp_path,  # never the workspace root: its liquifai/ folder shadows the installed package
        stdin=editor_side,
        stdout=editor_side,
        stderr=editor_side,
        close_fds=True,
    )
    os.close(editor_side)
    try:
        ready = f"Running on http://{cli.STUDIO_HOST}:{port}".encode()
        seen = b""
        while ready not in seen:
            readable, _, _ = select.select([terminal], [], [], START_TIMEOUT)
            assert readable, f"the editor never listened:\n{seen.decode(errors='replace')}"
            chunk = os.read(terminal, 4096)
            assert chunk, f"the editor exited before listening:\n{seen.decode(errors='replace')}"
            seen += chunk
        request = urllib.request.Request(
            f"http://{cli.STUDIO_HOST}:{port}/api/graphs/demo/param",
            data=json.dumps(EDIT).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=STOP_TIMEOUT) as response:
            assert json.loads(response.read())["dirty"] is True
        os.close(terminal)
        terminal = -1
        proc.send_signal(signal.SIGHUP)
        assert proc.wait(timeout=STOP_TIMEOUT) == 0
        [entry] = _kept(folder)["graphs"]
        assert "low_level: 0.9" in entry["texts"][str((folder / "demo.yaml").resolve())]
        assert (folder / "demo.yaml").read_text() == FLOW_TEXT
    finally:
        if terminal != -1:
            os.close(terminal)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=STOP_TIMEOUT)
