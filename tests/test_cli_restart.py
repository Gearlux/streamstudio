"""``streamstudio stop`` / ``restart`` against a port a program that is NOT the editor holds: refused, never stopped.

Measured before the editor was served by portail: ``streamstudio stop --port N`` ended whatever process listened on
the port, without asking what it was — a database, another tool. Now a server is found by what it ANSWERS
(``/api/meta`` names the app), and a listener that does not answer as the editor is refused in one line and keeps
running. How the pids of a stop are found, and the kill of a process that ignores SIGTERM, are pinned in portail's own
suite.

Every listener in this file is a REAL child process on a real ephemeral port that signals readiness over a pipe — no
mocked sockets, and no sleeping to wait for one.
"""

import socket
import subprocess
import sys
import urllib.request
from typing import Iterator, Tuple

import pytest
from portail import serving

from streamstudio import cli

#: A listener that accepts and never answers — a program that is not HTTP at all.
_SILENT = """
import socket, time
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(("{host}", {port}))
s.listen(8)
print("ready", flush=True)
time.sleep(120)
"""
#: A listener that answers every connection with an SSH banner — a program that speaks, but not HTTP.
_BANNER = """
import socket
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((cli.STUDIO_HOST, 0))
        return int(probe.getsockname()[1])


@pytest.fixture(autouse=True)
def _live_and_short(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests ask the port THEY opened for real; a listener that never answers is waited for 0.2 s, not 5 s."""
    monkeypatch.setattr(serving, "_urlopen", urllib.request.urlopen)
    monkeypatch.setattr(cli, "ASK_TIMEOUT", 0.2)


@pytest.fixture(params=[_SILENT, _BANNER], ids=["silent", "an SSH banner"])
def listener(request: pytest.FixtureRequest) -> Iterator[Tuple[int, "subprocess.Popen[str]"]]:
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-c", request.param.format(host=cli.STUDIO_HOST, port=port)], stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout is not None and proc.stdout.readline().strip() == "ready", "listener failed to start"
    yield port, proc
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)


def _main(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["streamstudio", *argv])
    cli.main()


@pytest.mark.parametrize("command", ["stop", "restart"])
def test_a_program_that_is_not_the_editor_is_refused_and_keeps_running(
    listener: Tuple[int, "subprocess.Popen[str]"],
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Measured before (R7), for the banner: the question ``stop`` asks first crashed on it
    (``http.client.BadStatusLine``). The question is still best effort — and the stop no longer goes on."""
    port, proc = listener
    assert cli._unsaved_work(port) is None
    with pytest.raises(SystemExit) as exit_info:
        _main(monkeypatch, command, str(port))
    assert exit_info.value.code == 1
    said = " ".join(capsys.readouterr().out.split())
    assert f"port {port}" in said and "not streamstudio" in said and "Traceback" not in said
    assert proc.poll() is None, "the listener was signalled"


def test_stop_says_so_when_nothing_is_running(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    port = _free_port()
    _main(monkeypatch, "stop", str(port))  # exits 0 — nothing to stop is not an error
    assert f"nothing listening on port {port}" in capsys.readouterr().out
