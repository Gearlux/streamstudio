"""``streamstudio stop`` — find the studio by the port it holds and signal it.

Two facts shape this:

* **The port owner is the target, never a pid file.** Whatever started the server (a terminal, a host app, a
  script), the process LISTENING on the port is the one to stop.
* **SIGTERM is enough.** The port went free **64 ms** after the signal (measured 2026-08-26), so the stop is a
  `terminate()` plus a deterministic `wait()`, never a sleep-and-hope. `kill()` is the escalation for a process
  that ignores SIGTERM, pinned below with a child that really does ignore it.

The owner is located with a PER-PROCESS psutil scan: the whole-system ``psutil.net_connections()``
raises ``AccessDenied`` unprivileged on this platform, while iterating processes and asking each for
its own connections does not.

Every listener in this file is a REAL child process on a real ephemeral port that signals readiness
over a pipe — no mocked sockets, and no sleeping to wait for one. A listener accepts but never answers HTTP, so
the question ``stop`` asks a canvas editor first (which graphs hold unsaved work) times out on it — the case of
a port held by something else — and the stop goes on saying nothing extra; the wait is shortened here.
"""

import socket
import subprocess
import sys
from pathlib import Path
from typing import Iterator, Tuple
from unittest.mock import patch

import pytest

from streamstudio import cli

# A child that binds, listens, announces itself, then idles. ``ignore`` makes it deaf to SIGTERM so
# the kill-escalation branch can be exercised for real.
_LISTENER = """
import signal, socket, sys, time
if {ignore}:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(("{host}", {port}))
s.listen(1)
print("ready", flush=True)
time.sleep(120)
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((cli.STUDIO_HOST, 0))
        return int(probe.getsockname()[1])


def _spawn_listener(port: int, *, ignore_sigterm: bool = False) -> subprocess.Popen:
    """Start a real listener and return once it has ANNOUNCED it is listening (no sleeping)."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _LISTENER.format(host=cli.STUDIO_HOST, port=port, ignore=ignore_sigterm)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready", "listener failed to start"
    return proc


@pytest.fixture(autouse=True)
def _short_question(monkeypatch: pytest.MonkeyPatch) -> None:
    """The listeners never answer the unsaved-work question: wait 0.2 s for that answer, not the 5 s a stop waits."""
    monkeypatch.setattr(cli, "ASK_TIMEOUT", 0.2)


@pytest.fixture
def listener() -> Iterator[Tuple[int, subprocess.Popen]]:
    port = _free_port()
    proc = _spawn_listener(port)
    yield port, proc
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)


# --------------------------------------------------------------------------- #
# Locating the studio
# --------------------------------------------------------------------------- #


def test_the_port_owner_is_found_by_the_port_it_holds(listener: Tuple[int, subprocess.Popen]) -> None:
    port, proc = listener
    found = cli._studio_process(port)
    assert found is not None and found.pid == proc.pid


def test_nothing_is_found_on_a_port_nobody_holds() -> None:
    assert cli._studio_process(_free_port()) is None


# --------------------------------------------------------------------------- #
# Stopping it
# --------------------------------------------------------------------------- #


def test_stopping_reports_the_pid_and_frees_the_port(listener: Tuple[int, subprocess.Popen]) -> None:
    port, proc = listener
    assert cli._stop_studio(port) == proc.pid
    assert proc.wait(timeout=10) is not None  # really gone, not just unbound
    assert cli._port_in_use(port) is False


def test_stopping_an_idle_port_reports_nothing_stopped() -> None:
    assert cli._stop_studio(_free_port()) is None


def test_a_studio_deaf_to_sigterm_is_killed(tmp_path: Path) -> None:
    """The escalation branch, against a child that really ignores SIGTERM."""
    port = _free_port()
    proc = _spawn_listener(port, ignore_sigterm=True)
    try:
        assert cli._stop_studio(port, grace=1.0) == proc.pid
        assert cli._port_in_use(port) is False
    finally:
        if proc.poll() is None:  # pragma: no cover - only if the kill escalation regressed
            proc.kill()
            proc.wait(timeout=10)


# --------------------------------------------------------------------------- #
# `streamstudio stop`
# --------------------------------------------------------------------------- #


def test_stop_command_names_the_port_and_the_pid(
    listener: Tuple[int, subprocess.Popen], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    port, proc = listener
    monkeypatch.setattr(sys, "argv", ["streamstudio", "--port", str(port), "stop"])
    cli.main()
    out = capsys.readouterr().out
    assert str(port) in out and str(proc.pid) in out


#: A listener that answers every connection with an SSH banner — a program that speaks, but not HTTP.
_BANNER_LISTENER = """
import socket, time
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(("{host}", {port}))
s.listen(8)
print("ready", flush=True)
held = []
while True:
    connection, _ = s.accept()
    connection.sendall(b"SSH-2.0-OpenSSH_9.6\\r\\n")
    held.append(connection)
"""


def test_stop_stops_a_listener_that_does_not_speak_http_and_says_nothing_extra(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Measured before (R7): the question ``stop`` asks first crashed on the banner (``http.client.BadStatusLine:
    SSH-2.0-OpenSSH_9.6``), the command exited 1 and the listener kept running. The question is best effort: any
    answer that is not a canvas editor's is "nothing extra", and the stop goes on."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-c", _BANNER_LISTENER.format(host=cli.STUDIO_HOST, port=port)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
        assert cli._unsaved_work(port) is None
        monkeypatch.setattr(sys, "argv", ["streamstudio", "stop", "--port", str(port)])
        cli.main()
        assert capsys.readouterr().out.splitlines() == [f"Stopped StreamStudio on port {port} (pid {proc.pid})."]
        assert proc.wait(timeout=10) is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_stop_command_says_so_when_nothing_is_running(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    port = _free_port()
    monkeypatch.setattr(sys, "argv", ["streamstudio", "--port", str(port), "stop"])
    cli.main()  # exits 0 — nothing to stop is not an error
    assert str(port) in capsys.readouterr().out


def test_stop_command_never_launches_anything(
    listener: Tuple[int, subprocess.Popen], monkeypatch: pytest.MonkeyPatch
) -> None:
    port, _ = listener
    monkeypatch.setattr(sys, "argv", ["streamstudio", "--port", str(port), "stop"])
    with patch("subprocess.run") as run_mock, patch("subprocess.Popen") as popen_mock:
        cli.main()
    run_mock.assert_not_called()
    popen_mock.assert_not_called()
