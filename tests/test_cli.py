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
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fluxstudio import cli

# The symlink target main() computes: this installed package's directory.
SOURCE = Path(cli.__file__).parent.resolve()


@pytest.fixture(autouse=True)
def _no_ambient_custom_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep call-count assertions hermetic — ignore any ambient FLUXSTUDIO_CUSTOM_NODES export."""
    monkeypatch.delenv(cli.CUSTOM_NODES_ENV_VAR, raising=False)


@pytest.fixture
def logger_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Silence logflow output and expose the logger for message assertions."""
    m = MagicMock()
    monkeypatch.setattr(cli, "logger", m)
    return m


def _preinstall_manager(comfy_dir: Path) -> Path:
    """Create the ComfyUI-Manager checkout dir so the default install is a no-op in this test."""
    manager = comfy_dir / "custom_nodes" / cli._custom_node_dir_name(cli.COMFYUI_MANAGER_URL)
    manager.mkdir(parents=True, exist_ok=True)
    return manager


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
    _preinstall_manager(comfy)  # default custom-node install already done -> no clone for it
    link = custom / "fluxstudio"
    stale_target = tmp_path / "somewhere_else"
    stale_target.mkdir()
    os.symlink(stale_target, link)  # pre-existing symlink pointing at the wrong place

    monkeypatch.setattr(sys, "argv", ["fluxstudio", "--comfy-dir", str(comfy)])

    with patch("subprocess.run") as run_mock:
        cli.main()

    # comfy + manager already exist -> no clone/pip, only the launch call
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
    comfy.mkdir()
    _preinstall_manager(comfy)  # exists -> only the launch subprocess.run call fires
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


# --------------------------------------------------------------------------- custom-node install


def test_custom_node_dir_name() -> None:
    # URL basename, with ``.git`` and trailing-slash forms all landing on the repo name.
    assert cli._custom_node_dir_name("https://github.com/Comfy-Org/ComfyUI-Manager") == "ComfyUI-Manager"
    assert cli._custom_node_dir_name("https://github.com/foo/Bar.git") == "Bar"
    assert cli._custom_node_dir_name("https://github.com/foo/Bar/") == "Bar"


def test_main_installs_comfyui_manager_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logger_mock: MagicMock
) -> None:
    comfy = tmp_path / "ComfyUI"
    comfy.mkdir()
    monkeypatch.setattr(sys, "argv", ["fluxstudio", "--comfy-dir", str(comfy)])

    with patch("subprocess.run") as run_mock:
        cli.main()

    clones = [c.args[0] for c in run_mock.call_args_list if c.args[0][:2] == ["git", "clone"]]
    target = comfy / "custom_nodes" / "ComfyUI-Manager"
    assert clones == [["git", "clone", cli.COMFYUI_MANAGER_URL, str(target)]]
    # The mocked clone creates no files -> no requirements.txt -> no pip call fires for it.
    assert all("pip" not in c.args[0] for c in run_mock.call_args_list)


def test_main_no_manager_skips_manager_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logger_mock: MagicMock
) -> None:
    comfy = tmp_path / "ComfyUI"
    comfy.mkdir()
    monkeypatch.setattr(sys, "argv", ["fluxstudio", "--comfy-dir", str(comfy), "--no-manager"])

    with patch("subprocess.run") as run_mock:
        cli.main()

    # No custom-node clone at all -> the launch is the only subprocess call.
    assert run_mock.call_count == 1
    assert run_mock.call_args.args[0] == [sys.executable, "main.py"]


def test_main_skips_already_installed_custom_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logger_mock: MagicMock
) -> None:
    comfy = tmp_path / "ComfyUI"
    comfy.mkdir()
    _preinstall_manager(comfy)
    monkeypatch.setattr(sys, "argv", ["fluxstudio", "--comfy-dir", str(comfy)])

    with patch("subprocess.run") as run_mock:
        cli.main()

    # Just the launch — an existing checkout is never re-cloned (update via ComfyUI-Manager).
    assert run_mock.call_count == 1
    assert run_mock.call_args.args[0] == [sys.executable, "main.py"]


def test_custom_node_flag_and_env_combine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logger_mock: MagicMock
) -> None:
    # Install order: bundled manager, then FLUXSTUDIO_CUSTOM_NODES entries, then --custom-node flags.
    comfy = tmp_path / "ComfyUI"
    comfy.mkdir()
    monkeypatch.setenv(cli.CUSTOM_NODES_ENV_VAR, " https://github.com/a/EnvNode.git , ,https://github.com/b/EnvNode2")
    monkeypatch.setattr(
        sys,
        "argv",
        ["fluxstudio", "--comfy-dir", str(comfy), "--custom-node", "https://github.com/c/FlagNode"],
    )

    with patch("subprocess.run") as run_mock:
        cli.main()

    clone_targets = [Path(c.args[0][3]).name for c in run_mock.call_args_list if c.args[0][:2] == ["git", "clone"]]
    assert clone_targets == ["ComfyUI-Manager", "EnvNode", "EnvNode2", "FlagNode"]
    assert all(
        Path(c.args[0][3]).parent == comfy / "custom_nodes" for c in run_mock.call_args_list if "git" in c.args[0]
    )


def test_custom_node_requirements_installed_after_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logger_mock: MagicMock
) -> None:
    # A cloned custom node that ships a requirements.txt gets it pip-installed into this env.
    comfy = tmp_path / "ComfyUI"
    comfy.mkdir()
    monkeypatch.setattr(sys, "argv", ["fluxstudio", "--comfy-dir", str(comfy)])

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:2] == ["git", "clone"]:  # simulate the clone materializing the repo
            repo = Path(cmd[3])
            repo.mkdir(parents=True)
            (repo / "requirements.txt").write_text("GitPython\n")
        return MagicMock()

    with patch("subprocess.run", side_effect=fake_run) as run_mock:
        cli.main()

    requirements = comfy / "custom_nodes" / "ComfyUI-Manager" / "requirements.txt"
    pip_calls = [c.args[0] for c in run_mock.call_args_list if "pip" in c.args[0]]
    assert pip_calls == [[sys.executable, "-m", "pip", "install", "-r", str(requirements)]]


def test_custom_node_clone_failure_warns_and_launch_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logger_mock: MagicMock
) -> None:
    comfy = tmp_path / "ComfyUI"
    comfy.mkdir()
    bad = "https://github.com/nowhere/Missing"
    monkeypatch.setattr(sys, "argv", ["fluxstudio", "--comfy-dir", str(comfy), "--no-manager", "--custom-node", bad])

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:2] == ["git", "clone"]:
            raise subprocess.CalledProcessError(128, cmd)
        return MagicMock()

    with patch("subprocess.run", side_effect=fake_run) as run_mock:
        cli.main()  # must not raise

    assert run_mock.call_args.args[0] == [sys.executable, "main.py"]  # launch still happened
    warnings = [str(c.args[0]) for c in logger_mock.warning.call_args_list]
    assert any("Failed to clone custom node" in w and bad in w for w in warnings)


def test_custom_node_pip_failure_warns_and_launch_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logger_mock: MagicMock
) -> None:
    comfy = tmp_path / "ComfyUI"
    comfy.mkdir()
    monkeypatch.setattr(sys, "argv", ["fluxstudio", "--comfy-dir", str(comfy)])

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:2] == ["git", "clone"]:
            repo = Path(cmd[3])
            repo.mkdir(parents=True)
            (repo / "requirements.txt").write_text("GitPython\n")
        elif "pip" in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        return MagicMock()

    with patch("subprocess.run", side_effect=fake_run) as run_mock:
        cli.main()  # must not raise

    assert run_mock.call_args.args[0] == [sys.executable, "main.py"]
    warnings = [str(c.args[0]) for c in logger_mock.warning.call_args_list]
    assert any("Failed to install requirements" in w and "ComfyUI-Manager" in w for w in warnings)


# --------------------------------------------------------------------------- export subcommand


def _register_rescale_node() -> None:
    """Put the generated RescaleOp op node into the live NODE_CLASS_MAPPINGS the CLI reads."""
    from dataflux.ops.torch import RescaleOp

    import fluxstudio
    from fluxstudio.bridge import create_comfy_node

    fluxstudio.NODE_CLASS_MAPPINGS["Taidal_RescaleOp"] = create_comfy_node(RescaleOp, "Taidal_RescaleOp")


def _rescale_prompt_node(sample_src: object, in_max: float) -> dict:
    return {"class_type": "Taidal_RescaleOp", "inputs": {"sample": sample_src, "in_min": 0.0, "in_max": in_max}}


def test_export_single_chain_writes_output_verbatim(tmp_path: Path, logger_mock: MagicMock) -> None:
    import argparse
    import json

    _register_rescale_node()
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps({"2": _rescale_prompt_node(["x", 0], 255.0)}))
    out = tmp_path / "ops.yaml"

    cli._export(argparse.Namespace(graph=graph, output=out))

    assert out.exists()
    assert "RescaleOp" in out.read_text()
    assert not (tmp_path / "ops_1.yaml").exists()


def test_export_multi_chain_writes_one_file_per_chain(tmp_path: Path, logger_mock: MagicMock) -> None:
    # Two disconnected op chains -> no error; ops.yaml fans out to ops_1.yaml + ops_2.yaml.
    import argparse
    import json

    _register_rescale_node()
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps({"2": _rescale_prompt_node(["x", 0], 255.0), "3": _rescale_prompt_node(["y", 0], 1.0)}))

    cli._export(argparse.Namespace(graph=graph, output=tmp_path / "ops.yaml"))

    assert not (tmp_path / "ops.yaml").exists()
    docs = [(tmp_path / name).read_text() for name in ("ops_1.yaml", "ops_2.yaml")]
    assert all("ops:" in d and "RescaleOp" in d for d in docs)
    assert "in_max: 255.0" in docs[0] and "in_max: 1.0" in docs[1]
    infos = [str(c.args[0]) for c in logger_mock.info.call_args_list]
    assert any("2 independent op chains" in i for i in infos)


def test_export_multi_chain_stdout_is_document_stream(
    tmp_path: Path, logger_mock: MagicMock, capsys: pytest.CaptureFixture
) -> None:
    # Without -o, several chains stream to stdout as ``---``-separated YAML documents.
    import argparse
    import json

    _register_rescale_node()
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps({"2": _rescale_prompt_node(["x", 0], 255.0), "3": _rescale_prompt_node(["y", 0], 1.0)}))

    cli._export(argparse.Namespace(graph=graph, output=None))

    out = capsys.readouterr().out
    assert out.count("ops:") == 2
    assert "\n---\n" in "\n" + out  # one separator between the two documents
