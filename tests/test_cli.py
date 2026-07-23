"""Tests for ``fluxstudio.cli.main`` — the ComfyUI bootstrap + launch flow.

``main`` does four things on each run: clone ComfyUI if missing, install custom
nodes (entry-point-declared / env / flags), (re)point the ``custom_nodes/fluxstudio``
symlink at this package, then launch ``main.py`` under the active interpreter.
``subprocess.run`` is always mocked so no real clone / pip install / ComfyUI launch
happens, but the symlink branches use a real ``tmp_path`` and real ``os.symlink`` —
the create / update / already-correct / non-symlink-warning logic is exactly the
kind of filesystem reasoning that mocking ``os.symlink`` would paper over, so we
assert on the resulting link state instead.
"""

import os
import runpy
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fluxstudio import cli
from fluxstudio.custom_nodes import CUSTOM_NODE_URLS

# The symlink target main() computes: this installed package's directory.
SOURCE = Path(cli.__file__).parent.resolve()


class _FakeEntryPoint:
    """Stand-in for an importlib.metadata EntryPoint: load() returns the payload or raises it."""

    def __init__(self, name: str, payload: object) -> None:
        self.name = name
        self.value = f"fake.module:{name}"  # the declaration string warnings quote
        self._payload = payload

    def load(self) -> object:
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


@pytest.fixture(autouse=True)
def _no_ambient_custom_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep call-count assertions hermetic — ignore any ambient FLUXSTUDIO_CUSTOM_NODES export."""
    monkeypatch.delenv(cli.CUSTOM_NODES_ENV_VAR, raising=False)


@pytest.fixture(autouse=True)
def _hermetic_declared_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the declared tier to fluxstudio's own bundled list.

    The real ``entry_points()`` reads the whole venv — another installed package declaring
    ``fluxstudio.custom_nodes`` would leak into call-count assertions. The real loading logic
    still runs, fed fluxstudio's actual ``CUSTOM_NODE_URLS`` declaration.
    """
    eps = [_FakeEntryPoint("fluxstudio", CUSTOM_NODE_URLS)]
    monkeypatch.setattr(cli, "entry_points", lambda group: eps if group == cli.CUSTOM_NODES_ENTRY_POINT_GROUP else [])


@pytest.fixture
def logger_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Silence loggair output and expose the logger for message assertions."""
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


def test_main_config_exports_config_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logger_mock: MagicMock
) -> None:
    # --config <file> is forwarded as FLUXSTUDIO_CONFIG (the PATH; the keep-list lives IN the file).
    comfy = tmp_path / "ComfyUI"
    comfy.mkdir()
    cfg = tmp_path / "palette.yaml"
    cfg.write_text("keep_nodes: [MaskToImage]\n")
    monkeypatch.setattr(sys, "argv", ["fluxstudio", "--comfy-dir", str(comfy), "--config", str(cfg)])
    with patch("subprocess.run") as run_mock:
        cli.main()
    assert run_mock.call_args.kwargs["env"]["FLUXSTUDIO_CONFIG"] == str(cfg)


def test_main_no_config_clears_config_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logger_mock: MagicMock
) -> None:
    # Without --config the flag is authoritative: FLUXSTUDIO_CONFIG is set to "" (clearing any ambient
    # export) so the bridge uses its default ~/.fluxstudio/config.yaml.
    comfy = tmp_path / "ComfyUI"
    comfy.mkdir()
    monkeypatch.setenv("FLUXSTUDIO_CONFIG", "/ambient/leak.yaml")
    monkeypatch.setattr(sys, "argv", ["fluxstudio", "--comfy-dir", str(comfy)])
    with patch("subprocess.run") as run_mock:
        cli.main()
    assert run_mock.call_args.kwargs["env"]["FLUXSTUDIO_CONFIG"] == ""


def test_main_only_taidal_nodes_exports_both_envs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logger_mock: MagicMock
) -> None:
    # --only-taidal-nodes sets FLUXSTUDIO_ONLY_TAIDAL_NODES=1 AND implies hiding built-ins
    # (FLUXSTUDIO_HIDE_DEFAULT_NODES=1) so the backend prunes defaults + the palette roots.
    comfy = tmp_path / "ComfyUI"
    comfy.mkdir()
    monkeypatch.setattr(sys, "argv", ["fluxstudio", "--comfy-dir", str(comfy), "--only-taidal-nodes"])
    with patch("subprocess.run") as run_mock:
        cli.main()
    env = run_mock.call_args.kwargs["env"]
    assert env["FLUXSTUDIO_ONLY_TAIDAL_NODES"] == "1"
    assert env["FLUXSTUDIO_HIDE_DEFAULT_NODES"] == "1"
    infos = [str(c.args[0]) for c in logger_mock.info.call_args_list]
    assert any("ONLY Taidal nodes" in i for i in infos)


def test_module_run_as_script_invokes_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Exercise the ``if __name__ == "__main__": main()`` entry guard.
    comfy = tmp_path / "ComfyUI"
    comfy.mkdir()
    monkeypatch.setattr(sys, "argv", ["fluxstudio", "--comfy-dir", str(comfy)])

    with patch("loggair.get_logger", return_value=MagicMock()), patch("subprocess.run"), patch("os.symlink"):
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
    # Install order: declared tier (fluxstudio's bundled manager), then FLUXSTUDIO_CUSTOM_NODES
    # entries, then --custom-node flags.
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


# ------------------------------------------------------------------- entry-point-declared nodes


def test_fluxstudio_declares_manager_via_entry_point() -> None:
    # The REAL installed metadata (bypassing the autouse fake): fluxstudio itself declares the
    # group, so the manager is "generally available" with no flags. A failure here means the
    # editable install's *.dist-info is stale — reinstall (`uv pip install -e ./fluxstudio`).
    import importlib.metadata as md

    ours = [ep for ep in md.entry_points(group=cli.CUSTOM_NODES_ENTRY_POINT_GROUP) if ep.name == "fluxstudio"]
    assert ours, "fluxstudio.custom_nodes entry point missing from installed metadata (stale editable?)"
    assert cli.COMFYUI_MANAGER_URL in tuple(ours[0].load())


def test_declared_custom_nodes_str_and_iterable_forms(monkeypatch: pytest.MonkeyPatch, logger_mock: MagicMock) -> None:
    # An entry point may resolve to a single str URL or any iterable of them; order is preserved.
    eps = [
        _FakeEntryPoint("one", "https://github.com/a/One"),
        _FakeEntryPoint("many", ("https://github.com/b/Two", "https://github.com/c/Three")),
    ]
    monkeypatch.setattr(cli, "entry_points", lambda group: eps)

    assert cli._declared_custom_nodes() == [
        "https://github.com/a/One",
        "https://github.com/b/Two",
        "https://github.com/c/Three",
    ]
    logger_mock.warning.assert_not_called()


def test_declared_custom_nodes_broken_or_junk_entries_warn_and_skip(
    monkeypatch: pytest.MonkeyPatch, logger_mock: MagicMock
) -> None:
    # A broken declaration (import error, wrong type, non-str item) warns and is skipped — the
    # remaining declarations still install; nothing raises.
    eps = [
        _FakeEntryPoint("bad_load", ImportError("boom")),
        _FakeEntryPoint("bad_type", 42),
        _FakeEntryPoint("mixed", ["https://github.com/x/Ok", 7]),
        _FakeEntryPoint("good", "https://github.com/y/Good"),
    ]
    monkeypatch.setattr(cli, "entry_points", lambda group: eps)

    assert cli._declared_custom_nodes() == ["https://github.com/x/Ok", "https://github.com/y/Good"]
    warnings = [str(c.args[0]) for c in logger_mock.warning.call_args_list]
    assert any("bad_load" in w for w in warnings)
    assert any("bad_type" in w and "int" in w for w in warnings)
    assert any("mixed" in w and "non-str" in w for w in warnings)


def test_main_installs_declared_nodes_from_any_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logger_mock: MagicMock
) -> None:
    # Any installed package's declaration installs alongside fluxstudio's bundled manager.
    comfy = tmp_path / "ComfyUI"
    comfy.mkdir()
    eps = [
        _FakeEntryPoint("fluxstudio", CUSTOM_NODE_URLS),
        _FakeEntryPoint("acme", ["https://github.com/acme/AcmeNodes"]),
    ]
    monkeypatch.setattr(cli, "entry_points", lambda group: eps)
    monkeypatch.setattr(sys, "argv", ["fluxstudio", "--comfy-dir", str(comfy)])

    with patch("subprocess.run") as run_mock:
        cli.main()

    clone_targets = [Path(c.args[0][3]).name for c in run_mock.call_args_list if c.args[0][:2] == ["git", "clone"]]
    assert clone_targets == ["ComfyUI-Manager", "AcmeNodes"]


def test_main_no_declared_nodes_skips_package_declared_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logger_mock: MagicMock
) -> None:
    # --no-declared-nodes drops the whole entry-point tier (incl. the bundled manager — it IS a
    # declared node); ad-hoc --custom-node requests still install.
    comfy = tmp_path / "ComfyUI"
    comfy.mkdir()
    eps = [
        _FakeEntryPoint("fluxstudio", CUSTOM_NODE_URLS),
        _FakeEntryPoint("acme", ["https://github.com/acme/AcmeNodes"]),
    ]
    monkeypatch.setattr(cli, "entry_points", lambda group: eps)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fluxstudio",
            "--comfy-dir",
            str(comfy),
            "--no-declared-nodes",
            "--custom-node",
            "https://github.com/c/FlagNode",
        ],
    )

    with patch("subprocess.run") as run_mock:
        cli.main()

    clone_targets = [Path(c.args[0][3]).name for c in run_mock.call_args_list if c.args[0][:2] == ["git", "clone"]]
    assert clone_targets == ["FlagNode"]


def test_main_dedups_urls_across_tiers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logger_mock: MagicMock) -> None:
    # The same URL declared by a package AND given via env/flag clones exactly once.
    comfy = tmp_path / "ComfyUI"
    comfy.mkdir()
    monkeypatch.setenv(cli.CUSTOM_NODES_ENV_VAR, cli.COMFYUI_MANAGER_URL)
    monkeypatch.setattr(
        sys, "argv", ["fluxstudio", "--comfy-dir", str(comfy), "--custom-node", cli.COMFYUI_MANAGER_URL]
    )

    with patch("subprocess.run") as run_mock:
        cli.main()

    clones = [c.args[0] for c in run_mock.call_args_list if c.args[0][:2] == ["git", "clone"]]
    assert len(clones) == 1 and clones[0][2] == cli.COMFYUI_MANAGER_URL


def test_no_manager_filters_declared_tier_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logger_mock: MagicMock
) -> None:
    # --no-manager drops the manager from the DECLARED tier; an explicit --custom-node request
    # for the same URL is the stronger signal and still installs.
    comfy = tmp_path / "ComfyUI"
    comfy.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        ["fluxstudio", "--comfy-dir", str(comfy), "--no-manager", "--custom-node", cli.COMFYUI_MANAGER_URL],
    )

    with patch("subprocess.run") as run_mock:
        cli.main()

    clones = [c.args[0] for c in run_mock.call_args_list if c.args[0][:2] == ["git", "clone"]]
    assert len(clones) == 1 and clones[0][2] == cli.COMFYUI_MANAGER_URL


# --------------------------------------------------------------------------- export subcommand


def _register_rescale_node() -> None:
    """Put the generated RescaleOp op node into the live NODE_CLASS_MAPPINGS the CLI reads."""
    from sampleflux.ops.torch import ToTensor as RescaleOp

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
    assert "ToTensor" in out.read_text()
    assert not (tmp_path / "ops_1.yaml").exists()


def test_export_skip_flag_excludes_marked_node(tmp_path: Path, logger_mock: MagicMock) -> None:
    # ``--skip 2`` (repeatable) is the CLI twin of the canvas export-skip mark: the node
    # (and anything data-downstream) leaves the export.
    import argparse
    import json

    _register_rescale_node()
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps({"2": _rescale_prompt_node(["x", 0], 255.0)}))
    out = tmp_path / "ops.yaml"

    cli._export(argparse.Namespace(graph=graph, output=out, skip=["2"]))

    text = out.read_text()
    assert "ToTensor" not in text  # the marked op is gone — only the empty ops doc remains
    assert "ops: []" in text


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
    assert all("ops:" in d and "ToTensor" in d for d in docs)
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
