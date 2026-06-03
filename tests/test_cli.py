"""Tests for ``fluxstudio.cli.main`` — the ComfyUI bootstrap + launch flow.

``main`` does three things on each run: clone ComfyUI if missing, (re)point the
``custom_nodes/fluxstudio`` symlink at this package, then launch ``main.py``
under the active interpreter. ``subprocess.run`` is always mocked so no real
clone / pip install / ComfyUI launch happens, but the symlink branches use a
real ``tmp_path`` and real ``os.symlink`` — the create / update / already-correct
/ non-symlink-warning logic is exactly the kind of filesystem reasoning that
mocking ``os.symlink`` would paper over, so we assert on the resulting link
state instead.
"""

import os
import runpy
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fluxstudio import cli

# The symlink target main() computes: this installed package's directory.
SOURCE = Path(cli.__file__).parent.resolve()


@pytest.fixture
def logger_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Silence logflow output and expose the logger for message assertions."""
    m = MagicMock()
    monkeypatch.setattr(cli, "logger", m)
    return m


def test_main_clones_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logger_mock: MagicMock) -> None:
    # No --comfy-dir -> the default is built from Path.home(); point it at tmp.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["fluxstudio"])
    comfy = tmp_path / ".fluxstudio" / "ComfyUI"

    with patch("subprocess.run") as run_mock:
        cli.main()

    calls = run_mock.call_args_list
    # 1) git clone into the default comfy dir
    assert calls[0].args[0][:3] == ["git", "clone", "https://github.com/comfyanonymous/ComfyUI.git"]
    assert calls[0].args[0][3] == str(comfy)
    # 2) pip install -r requirements.txt
    assert calls[1].args[0][1:4] == ["-m", "pip", "install"]
    assert calls[1].args[0][-1] == str(comfy / "requirements.txt")
    # 3) launch main.py from inside the comfy dir
    assert calls[-1].args[0] == [sys.executable, "main.py"]
    assert calls[-1].kwargs["cwd"] == comfy
    # symlink freshly created, pointing at this package
    link = comfy / "custom_nodes" / "fluxstudio"
    assert link.is_symlink()
    assert link.resolve() == SOURCE


def test_main_uses_existing_and_updates_stale_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logger_mock: MagicMock
) -> None:
    comfy = tmp_path / "ComfyUI"
    custom = comfy / "custom_nodes"
    custom.mkdir(parents=True)
    link = custom / "fluxstudio"
    stale_target = tmp_path / "somewhere_else"
    stale_target.mkdir()
    os.symlink(stale_target, link)  # pre-existing symlink pointing at the wrong place

    monkeypatch.setattr(sys, "argv", ["fluxstudio", "--comfy-dir", str(comfy)])

    with patch("subprocess.run") as run_mock:
        cli.main()

    # comfy already exists -> no clone/pip, only the launch call
    assert run_mock.call_count == 1
    assert run_mock.call_args.args[0] == [sys.executable, "main.py"]
    assert run_mock.call_args.kwargs["cwd"] == comfy
    # the stale symlink was repointed at this package
    assert link.is_symlink()
    assert link.resolve() == SOURCE


def test_main_leaves_correct_symlink_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logger_mock: MagicMock
) -> None:
    comfy = tmp_path / "ComfyUI"
    custom = comfy / "custom_nodes"
    custom.mkdir(parents=True)
    link = custom / "fluxstudio"
    os.symlink(SOURCE, link)  # already correct

    monkeypatch.setattr(sys, "argv", ["fluxstudio", "--comfy-dir", str(comfy)])

    with patch("subprocess.run"), patch("fluxstudio.cli.os.symlink") as symlink_mock:
        cli.main()

    # An already-correct link must not be unlinked/recreated.
    symlink_mock.assert_not_called()
    assert link.resolve() == SOURCE


def test_main_warns_on_non_symlink_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logger_mock: MagicMock
) -> None:
    comfy = tmp_path / "ComfyUI"
    custom = comfy / "custom_nodes"
    custom.mkdir(parents=True)
    link = custom / "fluxstudio"
    link.mkdir()  # a real directory squatting where the symlink should go

    monkeypatch.setattr(sys, "argv", ["fluxstudio", "--comfy-dir", str(comfy)])

    with patch("subprocess.run"), patch("fluxstudio.cli.os.symlink") as symlink_mock:
        cli.main()

    symlink_mock.assert_not_called()
    assert link.is_dir() and not link.is_symlink()
    warnings = [str(c.args[0]) for c in logger_mock.warning.call_args_list]
    assert any("non-symlink" in w for w in warnings)


def test_main_handles_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logger_mock: MagicMock
) -> None:
    comfy = tmp_path / "ComfyUI"
    comfy.mkdir()  # exists -> only the launch subprocess.run call fires
    monkeypatch.setattr(sys, "argv", ["fluxstudio", "--comfy-dir", str(comfy)])

    with patch("subprocess.run", side_effect=KeyboardInterrupt):
        cli.main()  # Ctrl-C during launch must be swallowed, not propagated

    infos = [str(c.args[0]) for c in logger_mock.info.call_args_list]
    assert any("stopped" in i.lower() for i in infos)


def test_main_default_nodes_shown_exports_env_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logger_mock: MagicMock
) -> None:
    # Default (no flag): ComfyUI's built-in nodes stay visible -> the launched ComfyUI gets
    # FLUXSTUDIO_HIDE_DEFAULT_NODES="0" (explicit, so the flag wins over any ambient shell export).
    comfy = tmp_path / "ComfyUI"
    comfy.mkdir()
    monkeypatch.setattr(sys, "argv", ["fluxstudio", "--comfy-dir", str(comfy)])
    # An ambient export of the var must NOT leak through when the flag defaults to shown.
    monkeypatch.setenv("FLUXSTUDIO_HIDE_DEFAULT_NODES", "1")

    with patch("subprocess.run") as run_mock:
        cli.main()

    launch = run_mock.call_args
    assert launch.args[0] == [sys.executable, "main.py"]
    assert launch.kwargs["env"]["FLUXSTUDIO_HIDE_DEFAULT_NODES"] == "0"


def test_main_no_default_nodes_exports_env_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logger_mock: MagicMock
) -> None:
    # --no-default-nodes hides ComfyUI's built-ins -> FLUXSTUDIO_HIDE_DEFAULT_NODES="1" so the
    # fluxstudio import inside ComfyUI prunes the defaults and surfaces Taidal nodes at the root.
    comfy = tmp_path / "ComfyUI"
    comfy.mkdir()
    monkeypatch.setattr(sys, "argv", ["fluxstudio", "--comfy-dir", str(comfy), "--no-default-nodes"])

    with patch("subprocess.run") as run_mock:
        cli.main()

    launch = run_mock.call_args
    assert launch.kwargs["env"]["FLUXSTUDIO_HIDE_DEFAULT_NODES"] == "1"
    # The rest of the parent environment is forwarded, not replaced.
    assert "PATH" in launch.kwargs["env"]
    infos = [str(c.args[0]) for c in logger_mock.info.call_args_list]
    assert any("Hiding ComfyUI's built-in nodes" in i for i in infos)


def test_module_run_as_script_invokes_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Exercise the ``if __name__ == "__main__": main()`` entry guard.
    comfy = tmp_path / "ComfyUI"
    comfy.mkdir()
    monkeypatch.setattr(sys, "argv", ["fluxstudio", "--comfy-dir", str(comfy)])

    with patch("logflow.get_logger", return_value=MagicMock()), patch("subprocess.run"), patch("os.symlink"):
        runpy.run_path(cli.__file__, run_name="__main__")
