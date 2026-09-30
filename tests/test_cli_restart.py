"""``streamstudio stop`` / ``restart`` — find the studio by the port it holds, signal it, relaunch.

Measured 2026-08-26 on a live studio, and the two facts that shape this:

* **The launcher is not the server.** ``streamstudio run`` blocks in ``subprocess.run`` while the
  process that actually holds the port is the ComfyUI it spawned — ``74841 streamstudio run`` ->
  ``74870 python main.py --port 8189``. Signalling the launcher is the wrong target; the port owner
  is the thing to stop, and the launcher then exits by itself when its child returns (verified).
* **SIGTERM is enough.** The port went free **64 ms** after the signal, so the stop is a `terminate()`
  plus a deterministic `wait()`, never a sleep-and-hope. `kill()` is the escalation for a process
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
from typing import Any, Iterator, List, Tuple
from unittest.mock import MagicMock, patch

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
def _a_home_of_its_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test here gets a temporary home, so a ``--background`` launch writes its per-port log
    (``~/.streamstudio/logs/studio-<port>.log``) there. Measured before 2026-09-30: one run of this file left 3 empty
    ``studio-<port>.log`` files in the person's real ``~/.streamstudio/logs`` (259 → 262; 262 had piled up). Scoped to
    THIS file on purpose: the browser tests find headless chromium through the real home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # what Path.home() reads on Windows
    return home


def test_a_background_log_of_a_test_lands_in_the_tests_own_home(_a_home_of_its_own: Path) -> None:
    """The guard for the fixture above: remove it and this fails, instead of the person's logs folder filling up."""
    assert _a_home_of_its_own in cli.studio_log_path(8188).parents
    assert Path("~").expanduser() == _a_home_of_its_own


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


@pytest.fixture
def comfy_dir(tmp_path: Path) -> Path:
    comfy = tmp_path / "ComfyUI"
    (comfy / "custom_nodes").mkdir(parents=True)
    (comfy / "requirements.txt").write_text("torch\n")
    return comfy


def _launch_calls(run_mock: MagicMock) -> List[Any]:
    """The ComfyUI launches among a mock's calls. ``Any`` because mock's call type is private."""
    return [c for c in run_mock.call_args_list if "main.py" in list(c.args[0])]


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


# --------------------------------------------------------------------------- #
# `streamstudio restart`
# --------------------------------------------------------------------------- #


def test_restart_stops_the_running_studio_then_launches(
    comfy_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stop first, then the full launch flow — the freed port is what lets `_run`'s probe pass."""
    port = _free_port()
    proc = _spawn_listener(port)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "streamstudio",
            "--editor",
            "comfy",
            "--comfy-dir",
            str(comfy_dir),
            "--port",
            str(port),
            "--install-",
            "restart",
        ],
    )
    try:
        with patch("subprocess.run") as run_mock:
            cli.main()
    finally:
        if proc.poll() is None:  # pragma: no cover - only if the stop regressed
            proc.kill()
            proc.wait(timeout=10)
    assert str(proc.pid) in capsys.readouterr().out
    launches = _launch_calls(run_mock)
    assert len(launches) == 1
    assert list(launches[0].args[0]) == [sys.executable, "main.py", "--port", str(port)]


def test_restart_with_nothing_running_still_launches(comfy_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ "Restart" means "make it running" — an idle port is not an error."""
    port = _free_port()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "streamstudio",
            "--editor",
            "comfy",
            "--comfy-dir",
            str(comfy_dir),
            "--port",
            str(port),
            "--install-",
            "restart",
        ],
    )
    with patch("subprocess.run") as run_mock:
        cli.main()
    assert len(_launch_calls(run_mock)) == 1


# --------------------------------------------------------------------------- #
# Foreground (default) vs --background
# --------------------------------------------------------------------------- #


def test_the_default_launch_blocks_in_the_foreground(comfy_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port = _free_port()
    monkeypatch.setattr(
        sys,
        "argv",
        ["streamstudio", "--editor", "comfy", "--comfy-dir", str(comfy_dir), "--port", str(port), "--install-"],
    )
    with patch("subprocess.run") as run_mock, patch("subprocess.Popen") as popen_mock:
        cli.main()
    assert len(_launch_calls(run_mock)) == 1  # subprocess.run == blocking
    popen_mock.assert_not_called()


def test_background_detaches_and_writes_a_per_port_log(
    comfy_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    port = _free_port()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "streamstudio",
            "--editor",
            "comfy",
            "--comfy-dir",
            str(comfy_dir),
            "--port",
            str(port),
            "--install-",
            "--background",
        ],
    )
    with patch("subprocess.run") as run_mock, patch("subprocess.Popen") as popen_mock:
        cli.main()
    assert _launch_calls(run_mock) == []  # never blocks
    popen_mock.assert_called_once()
    assert list(popen_mock.call_args.args[0]) == [sys.executable, "main.py", "--port", str(port)]
    # Detached from this terminal's session, so it survives the shell that started it.
    assert popen_mock.call_args.kwargs["start_new_session"] is True
    out = capsys.readouterr().out
    assert f"http://{cli.STUDIO_HOST}:{port}" in out
    assert str(cli.studio_log_path(port)) in out


def test_background_is_spelled_in_full(comfy_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """There is no ``--bg`` / ``-b`` alias any more.

    The argparse CLI declared ``--background``/``--bg``/``-b``; liquifai derives every option from
    the parameter NAME, so a parameter has exactly one spelling (in either hyphen or underscore
    form). That is the trade the migration made in exchange for per-command help that cannot go
    stale — and an unknown flag is now REFUSED rather than silently ignored, so a stale ``--bg``
    in a script fails loudly instead of launching in the foreground.
    """
    from liquifai.exceptions import UnknownFlagError

    port = _free_port()
    monkeypatch.setattr(
        sys,
        "argv",
        ["streamstudio", "-d", "--editor", "comfy", "--comfy-dir", str(comfy_dir), "--port", str(port), "--bg", "run"],
    )
    with patch("subprocess.run"), patch("subprocess.Popen"):
        with pytest.raises(UnknownFlagError) as exc:
            cli.main()
    assert "bg" in str(exc.value)


def test_the_background_log_is_per_port_and_outside_the_working_directory() -> None:
    """Two studios must not interleave, and a launcher must never write into the user's cwd."""
    assert cli.studio_log_path(8188) != cli.studio_log_path(8189)
    assert Path.cwd() not in cli.studio_log_path(8188).parents


def test_the_short_spelling_reaches_background(comfy_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``-b`` is DECLARED (``short={"b": "background"}``), not derived from the first letter.

    Deriving would have shadowed liquifai's own single-dash globals — ``-c``/``-s``/``-d``/``-h``
    are config/scope/debug/help — and ``comfy_dir``, ``declared_nodes`` and ``default_nodes`` all
    start with a letter one of those owns.
    """
    port = _free_port()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "streamstudio",
            "--editor",
            "comfy",
            "--comfy-dir",
            str(comfy_dir),
            "-p",
            str(port),
            "--install-",
            "-b",
            "run",
        ],
    )
    with patch("subprocess.run") as run_mock, patch("subprocess.Popen") as popen_mock:
        cli.main()
    assert _launch_calls(run_mock) == []  # -b took effect: detached, never blocking
    popen_mock.assert_called_once()
    assert list(popen_mock.call_args.args[0]) == [sys.executable, "main.py", "--port", str(port)]


def test_the_short_spelling_works_after_the_command_too(comfy_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The placement that was refused outright before the migration."""
    port = _free_port()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "streamstudio",
            "restart",
            "--editor",
            "comfy",
            "--comfy-dir",
            str(comfy_dir),
            "-p",
            str(port),
            "--install-",
            "-b",
        ],
    )
    with patch("subprocess.run"), patch("subprocess.Popen") as popen_mock:
        cli.main()
    popen_mock.assert_called_once()
    assert list(popen_mock.call_args.args[0]) == [sys.executable, "main.py", "--port", str(port)]
