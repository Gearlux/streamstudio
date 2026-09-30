"""``streamstudio`` refuses a busy port up front, with a URL you can click.

ComfyUI binds ``127.0.0.1:8188`` (``comfy/cli_args.py`` — ``--listen`` / ``--port`` defaults) and,
when that port is taken, discovers it only at the very END of its boot: measured 2026-08-26 against
a studio that was already running, a launch spent 23 seconds and 91 lines of output to reach

    ERROR | server:start_multi_address:1437 | Port 8188 is already in use on address 127.0.0.1.

on line 85, then exited 0. Nothing in those 91 lines is a URL, so the instance that IS up — almost
always the reason the port is busy — was unreachable from the failure that named it.

``_run`` therefore probes the port BEFORE any of the launch work (clone, requirements sync, custom
nodes, symlink), prints the address as a plain clickable line, and exits 1. ``--port`` exists so the
alternative the message offers is a command this CLI actually accepts — the same rule the generated
Jenkinsfile headers follow, and pinned the same way: by PARSING the emitted line.
"""

import socket
import sys
from pathlib import Path
from typing import Any, Iterator, Tuple
from unittest.mock import MagicMock, patch

import pytest

from streamstudio import cli


@pytest.fixture
def busy_port() -> Iterator[int]:
    """A really-listening socket on a free port — the probe is exercised, never mocked."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind((cli.STUDIO_HOST, 0))
        held.listen(1)
        yield int(held.getsockname()[1])


@pytest.fixture
def free_port() -> int:
    """A port nothing listens on: bound, read, and released before the test runs."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((cli.STUDIO_HOST, 0))
        port = int(probe.getsockname()[1])
    return port


def _launch(comfy: Path, port: int, monkeypatch: pytest.MonkeyPatch) -> Tuple[MagicMock, Any]:
    """Run ``streamstudio --port <port>`` against a prepared ComfyUI dir; return the run mock."""
    monkeypatch.setattr(
        sys, "argv", ["streamstudio", "--editor", "comfy", "--comfy-dir", str(comfy), "--port", str(port), "--install-"]
    )
    with patch("subprocess.run") as run_mock:
        cli.main()
    return run_mock, None


@pytest.fixture
def comfy_dir(tmp_path: Path) -> Path:
    """A ComfyUI checkout that already exists, so ``_run`` skips the clone."""
    comfy = tmp_path / "ComfyUI"
    (comfy / "custom_nodes").mkdir(parents=True)
    (comfy / "requirements.txt").write_text("torch\n")
    return comfy


# --------------------------------------------------------------------------- #
# The probe
# --------------------------------------------------------------------------- #


def test_a_listening_socket_reads_as_in_use(busy_port: int) -> None:
    assert cli._port_in_use(busy_port) is True


def test_a_port_nothing_listens_on_reads_as_free(free_port: int) -> None:
    assert cli._port_in_use(free_port) is False


# --------------------------------------------------------------------------- #
# A busy port: stop before the work, name the URL, exit non-zero
# --------------------------------------------------------------------------- #


def test_a_busy_port_stops_before_any_launch_work(
    comfy_dir: Path, busy_port: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No clone, no requirements sync, no custom-node install, no ComfyUI — the probe is first."""
    monkeypatch.setattr(
        sys, "argv", ["streamstudio", "--editor", "comfy", "--comfy-dir", str(comfy_dir), "--port", str(busy_port)]
    )
    with patch("subprocess.run") as run_mock:
        with pytest.raises(SystemExit) as exit_info:
            cli.main()
    assert exit_info.value.code == 1
    run_mock.assert_not_called()


def test_the_busy_message_prints_a_clickable_url_for_the_running_studio(
    comfy_dir: Path, busy_port: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["streamstudio", "--editor", "comfy", "--comfy-dir", str(comfy_dir), "--port", str(busy_port)]
    )
    with patch("subprocess.run"):
        with pytest.raises(SystemExit):
            cli.main()
    out = capsys.readouterr().out
    # A bare http:// URL on its own is what terminals linkify — no markup, no log prefix.
    assert f"http://{cli.STUDIO_HOST}:{busy_port}" in out
    assert str(busy_port) in out


def test_the_busy_message_offers_a_port_the_cli_actually_accepts(
    comfy_dir: Path, busy_port: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Parse the suggested command with the REAL parser — a printed command that errors is the defect.

    The suggestion names ``--editor comfy``: bare ``streamstudio`` is the canvas editor now, so a
    suggestion without it would start the OTHER editor on the offered port."""
    monkeypatch.setattr(
        sys, "argv", ["streamstudio", "--editor", "comfy", "--comfy-dir", str(comfy_dir), "--port", str(busy_port)]
    )
    with patch("subprocess.run"):
        with pytest.raises(SystemExit):
            cli.main()
    out = capsys.readouterr().out
    suggested = [line for line in out.splitlines() if "streamstudio --editor comfy --port" in line]
    assert len(suggested) == 1, f"expected one suggested command, got {suggested}"
    argv = suggested[0].split("streamstudio", 1)[1].split()
    assert argv == ["--editor", "comfy", "--port", str(busy_port + 1)]
    monkeypatch.setattr(sys, "argv", ["streamstudio", *argv])
    with patch("subprocess.run"):
        with patch.object(cli, "_run") as run_mock:
            cli.main()  # parses cleanly, and reaches the ComfyUI launch on the offered port
    assert run_mock.call_args.kwargs["port"] == busy_port + 1


# --------------------------------------------------------------------------- #
# A free port: the launch carries the port through to ComfyUI
# --------------------------------------------------------------------------- #


def test_the_chosen_port_is_forwarded_to_comfyui(
    comfy_dir: Path, free_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_mock, _ = _launch(comfy_dir, free_port, monkeypatch)
    launches = [c for c in run_mock.call_args_list if "main.py" in list(c.args[0])]
    assert len(launches) == 1
    assert list(launches[0].args[0]) == [sys.executable, "main.py", "--port", str(free_port)]


def test_the_default_launch_names_comfyuis_own_default_port(comfy_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without --port the launch still states 8188 explicitly — the value ComfyUI would pick itself."""
    monkeypatch.setattr(sys, "argv", ["streamstudio", "--editor", "comfy", "--comfy-dir", str(comfy_dir), "--install-"])
    monkeypatch.setattr(cli, "_port_in_use", lambda port, host=cli.STUDIO_HOST: False)
    with patch("subprocess.run") as run_mock:
        cli.main()
    launches = [c for c in run_mock.call_args_list if "main.py" in list(c.args[0])]
    assert list(launches[0].args[0]) == [sys.executable, "main.py", "--port", str(cli.DEFAULT_PORT)]
