"""``streamstudio`` serves the canvas editor over a folder of graphs; ``stop`` and ``restart`` name it by folder or
port.

Four commands — ``open`` (the default), ``stop``, ``restart``, ``list`` — and one default port, 8190. Serving and the
lifecycle are portail's (pinned there, and in ``tests/test_built_on_portail.py``); here is what is the editor's own:
which folder and templates folder a command serves, the line a start prints about unsaved work of the last session,
the work kept on the way out, and what ``stop`` says about it. The CLI is ``strict_flags``: a flag that names no
parameter is refused before anything runs or stops.

Every serve is intercepted at ``streamstudio.app.create_app`` — its double holds a REAL ``Studio`` over the folder,
so the recovery file's start line and the stash on the way out run for real — so nothing binds a port. No test here
reaches a live server (``tests/conftest.py::_no_live_server``): the running editors a ``stop`` or ``restart`` finds are
the ones a test lists (:func:`_running`).
"""

import inspect
import json
import signal
import socket
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from confluid import parse_param_docs
from portail import serving

from streamstudio import app as app_module
from streamstudio import cli


@pytest.fixture
def busy_port() -> Iterator[int]:
    """A really-listening socket on a free port — the probe is exercised, never mocked."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind((cli.STUDIO_HOST, 0))
        held.listen(1)
        yield int(held.getsockname()[1])


@pytest.fixture
def graph_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "graphs"
    folder.mkdir()
    return folder


@pytest.fixture
def free(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every port reads free: a test that is not ABOUT a busy port never depends on the machine's listeners."""
    monkeypatch.setattr(serving, "port_in_use", lambda host, port: False)


#: The real question ``stop`` asks, kept before the autouse fixture replaces it.
_ASK = cli._unsaved_work


@pytest.fixture(autouse=True)
def _never_ask_a_real_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_unsaved_work", lambda port: None)


def _running(monkeypatch: pytest.MonkeyPatch, editors: Dict[int, Path]) -> List[int]:
    """The editors running, by port → the folder each serves; answers the ports stopped, each stop taking its editor
    out of the running ones — as a real stop does. Nothing of the machine is asked: no port, no process.
    """
    alive = dict(editors)
    stopped: List[int] = []
    servers = cli.SPEC.state_dir() / "servers"
    servers.mkdir(parents=True, exist_ok=True)
    for port in editors:
        (servers / f"{port}.json").write_text(json.dumps({"host": cli.STUDIO_HOST, "port": port, "pid": 4780}))

    def meta(host: str, port: int) -> Optional[Dict[str, Any]]:
        if port not in alive:
            return None
        folder = str(alive[port])
        info = {"path": folder, "template": "", "state_dir": folder, "options": {}}
        return {"app": "streamstudio", "title": "x", "pid": 4780, "workspace": info}

    def stop(name: str, port: int) -> str:
        stopped.append(port)
        alive.pop(port, None)
        return f"{name}: stopped pid 4780 (port {port})"

    monkeypatch.setattr(serving, "meta_at", meta)
    monkeypatch.setattr(serving, "stop_port", stop)
    monkeypatch.setattr(serving, "port_in_use", lambda host, port: port in alive)
    monkeypatch.setattr(serving, "pids_listening", lambda port: [4780] if port in alive else [])
    return stopped


def _served_on(create_app_mock: MagicMock) -> int:
    """The port the intercepted app was run on."""
    create_app_mock.return_value.run.assert_called_once()
    return int(create_app_mock.return_value.run.call_args.kwargs["port"])


def _double(folder: Path) -> MagicMock:
    """What ``create_app`` answers here: an app double (``run`` binds nothing) holding a REAL Studio over ``folder``."""
    app = MagicMock()
    app.extensions = {app_module.EXTENSION: app_module.Studio(folder)}
    return app


def _serving(folder: Path) -> Any:  # the patch context manager
    return patch("streamstudio.app.create_app", return_value=_double(folder))


def _main(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["streamstudio", *argv])
    cli.main()


# ------------------------------------------------------------------------------------------------
# the switch
# ------------------------------------------------------------------------------------------------


class TestTheCommands:
    def test_the_commands_are_open_stop_restart_and_list_on_port_8190(self) -> None:
        assert sorted(cli.app._commands) == ["list", "open", "restart", "stop"]
        assert cli.EDITOR_PORT == 8190

    @pytest.mark.parametrize("command", ["open", "stop", "restart"])
    def test_every_parameter_is_documented_for_the_help_liquifai_renders(self, command: str) -> None:
        """liquifai renders ``--help`` from the ``Args:`` block — an undocumented option is an unexplained one."""
        func = cli.app._commands[command]
        documented = parse_param_docs(func)
        assert list(documented) == list(inspect.signature(func).parameters)
        assert all(documented.values())

    @pytest.mark.usefixtures("free")
    def test_bare_streamstudio_serves_the_canvas_over_the_current_directory_on_8190(
        self, graph_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(graph_dir)
        with _serving(graph_dir) as create_app_mock:
            _main(monkeypatch)
        create_app_mock.assert_called_once_with(Path.cwd(), templates_dir=None)
        assert _served_on(create_app_mock) == cli.EDITOR_PORT

    @pytest.mark.usefixtures("free")
    @pytest.mark.parametrize("command", [[], ["open"]])
    def test_a_directory_a_port_and_a_templates_folder_reach_the_canvas(
        self,
        graph_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        command: List[str],
    ) -> None:
        templates = tmp_path / "tpl"
        with _serving(graph_dir) as create_app_mock:
            _main(
                monkeypatch,
                *command,
                str(graph_dir),
                "--templates-dir",
                str(templates),
                "-p",
                "9123",
            )
        create_app_mock.assert_called_once_with(graph_dir, templates_dir=templates)
        assert _served_on(create_app_mock) == 9123

    @pytest.mark.parametrize(
        "argv, option",
        [
            (["--editor", "comfy"], "editor"),
            (["--comfy-dir", "/x"], "comfy-dir"),
            (["--custom-node", "https://example.invalid/x.git"], "custom-node"),
        ],
    )
    def test_an_option_no_command_has_is_refused_before_anything_is_served(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        argv: List[str],
        option: str,
    ) -> None:
        """``strict_flags``: the option reaches nothing, so it is an error — the editor is never started on it."""
        with patch("streamstudio.app.create_app") as create_app_mock:
            with pytest.raises(SystemExit) as exit_info:
                _main(monkeypatch, *argv)
        assert exit_info.value.code == 1
        captured = capsys.readouterr()
        said = " ".join((captured.out + captured.err).split())
        assert f"unknown option '{option.replace('-', '_')}'" in said or f"unknown option '{option}'" in said, said
        create_app_mock.assert_not_called()


# ------------------------------------------------------------------------------------------------
# the canvas server's own refusals
# ------------------------------------------------------------------------------------------------


class TestServingTheCanvas:
    def test_a_missing_directory_is_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        missing = tmp_path / "nowhere"
        with patch("streamstudio.app.create_app") as create_app_mock:
            with pytest.raises(SystemExit) as exit_info:
                _main(monkeypatch, str(missing))
        assert exit_info.value.code == 1
        said = " ".join(capsys.readouterr().out.split())
        assert "workspace folder" in said and "nowhere does not exist" in said
        create_app_mock.assert_not_called()

    def test_a_port_another_program_holds_is_refused_before_the_editor_is_made(
        self,
        graph_dir: Path,
        busy_port: int,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The probe runs against a REAL listening socket that does not answer as the editor."""
        with patch("streamstudio.app.create_app") as create_app_mock:
            with pytest.raises(SystemExit) as exit_info:
                _main(monkeypatch, str(graph_dir), "--port", str(busy_port))
        assert exit_info.value.code == 1
        create_app_mock.assert_not_called()
        said = " ".join(capsys.readouterr().out.split())
        assert f"port {busy_port} on {cli.STUDIO_HOST} is in use" in said and "--port" in said

    def test_a_folder_already_being_served_is_not_started_twice(
        self,
        graph_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _running(monkeypatch, {cli.EDITOR_PORT: graph_dir})
        with patch("streamstudio.app.create_app") as create_app_mock:
            _main(monkeypatch, str(graph_dir))
        create_app_mock.assert_not_called()
        assert f"http://{cli.STUDIO_HOST}:{cli.EDITOR_PORT}" in capsys.readouterr().out

    @pytest.mark.usefixtures("free")
    def test_a_relative_folder_and_templates_folder_are_served_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Measured before: ``streamstudio graphs`` served ``Path('graphs')``, so every graph path it answered
        was relative and the page read it against the folder a second time."""
        (tmp_path / "graphs").mkdir()
        monkeypatch.chdir(tmp_path)
        with _serving(tmp_path / "graphs") as create_app_mock:
            _main(monkeypatch, "graphs", "--templates-dir", "tpl")
        create_app_mock.assert_called_once_with(tmp_path / "graphs", templates_dir=tmp_path / "tpl")

    @pytest.mark.usefixtures("free")
    @pytest.mark.parametrize("typed_from_its_folder", [True, False])
    def test_a_graph_file_serves_its_folder_and_names_the_graphs_address(
        self,
        graph_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        typed_from_its_folder: bool,
    ) -> None:
        """Measured before: ``streamstudio convert.yaml``, typed in the graph's folder, was refused — ``workspace
        folder convert.yaml does not exist``."""
        (graph_dir / "my flow.yaml").write_text("flow: {}\n")
        monkeypatch.chdir(graph_dir if typed_from_its_folder else tmp_path)
        typed = "my flow.yaml" if typed_from_its_folder else str(graph_dir / "my flow.yaml")
        with _serving(graph_dir) as create_app_mock:
            _main(monkeypatch, typed)
        create_app_mock.assert_called_once_with(graph_dir, templates_dir=None)
        lines = capsys.readouterr().out.splitlines()
        assert f"  http://{cli.STUDIO_HOST}:{cli.EDITOR_PORT}/?graph=my%20flow" in lines  # alone on its line

    def test_a_graph_file_of_a_folder_already_served_names_the_graphs_address_there(
        self,
        graph_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (graph_dir / "flow.yaml").write_text("flow: {}\n")
        _running(monkeypatch, {9123: graph_dir})
        with patch("streamstudio.app.create_app") as create_app_mock:
            _main(monkeypatch, str(graph_dir / "flow.yaml"), "-p", "9123")
        create_app_mock.assert_not_called()
        assert f"  http://{cli.STUDIO_HOST}:9123/?graph=flow" in capsys.readouterr().out.splitlines()

    def test_a_folder_names_no_graphs_address(
        self,
        graph_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with _serving(graph_dir):
            _main(monkeypatch, str(graph_dir))
        assert "?graph=" not in capsys.readouterr().out

    @pytest.mark.parametrize("name", ["nowhere.yaml", "notes.txt"])
    def test_a_file_that_is_no_graph_file_there_is_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        name: str,
    ) -> None:
        (tmp_path / "notes.txt").write_text("not a graph\n")
        with patch("streamstudio.app.create_app") as create_app_mock:
            with pytest.raises(SystemExit) as exit_info:
                _main(monkeypatch, str(tmp_path / name))
        assert exit_info.value.code == 1
        assert f"{name} does not exist" in " ".join(capsys.readouterr().out.split())
        create_app_mock.assert_not_called()

    def test_the_default_templates_folder_is_under_the_home_resolved_when_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert app_module.default_templates_dir() == tmp_path / ".streamstudio" / "templates"
        studio = app_module.Studio(tmp_path)
        assert studio.templates_dir == tmp_path / ".streamstudio" / "templates"


# ------------------------------------------------------------------------------------------------
# stop and restart follow the editor
# ------------------------------------------------------------------------------------------------


class TestStopAndRestart:
    @pytest.mark.parametrize("argv", [["stop"], ["stop", "9002"]])
    def test_stop_asks_the_editor_what_it_holds_first(
        self, graph_dir: Path, monkeypatch: pytest.MonkeyPatch, argv: List[str]
    ) -> None:
        stopped = _running(monkeypatch, {9002: graph_dir})
        happened: List[str] = []
        monkeypatch.setattr(cli, "_unsaved_work", lambda port: happened.append(f"asked {port}"))
        monkeypatch.setattr(
            serving,
            "stop_servers",
            lambda spec, chosen: happened.append(f"stopped {chosen[0]['port']}"),
        )
        _main(monkeypatch, *argv)
        assert happened == ["asked 9002", "stopped 9002"] and stopped == []

    def test_stop_names_an_editor_by_its_folder(
        self, graph_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stopped = _running(monkeypatch, {9002: graph_dir, 9003: tmp_path})
        _main(monkeypatch, "stop", str(graph_dir))
        assert stopped == [9002]

    def test_stop_refuses_an_option_it_does_not_have_and_stops_nothing(
        self, graph_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stopped = _running(monkeypatch, {cli.EDITOR_PORT: graph_dir})
        with pytest.raises(SystemExit):
            _main(monkeypatch, "stop", "--editor", "comfy")
        with pytest.raises(SystemExit):
            _main(monkeypatch, "stop", "--port", "8190")  # a port is named as `stop 8190`
        assert stopped == []

    def test_restart_of_the_canvas_stops_its_port_then_serves_the_directory(
        self, graph_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stopped = _running(monkeypatch, {cli.EDITOR_PORT: graph_dir})
        calls: List[str] = []

        def served(*args: Any, **kwargs: Any) -> MagicMock:
            calls.append(f"serve after {len(stopped)} stop")
            return _double(graph_dir)

        with patch("streamstudio.app.create_app", side_effect=served) as create_app_mock:
            _main(monkeypatch, "restart", str(graph_dir))
        assert stopped == [cli.EDITOR_PORT] and calls == ["serve after 1 stop"]
        create_app_mock.assert_called_once_with(graph_dir, templates_dir=None)

    def test_restart_checks_the_folder_before_stopping_anything(
        self,
        graph_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Measured before: ``streamstudio restart grpahs`` stopped the running editor, THEN said ``Graph directory
        not found: grpahs`` — a typo took the editor down and started nothing."""
        monkeypatch.chdir(tmp_path)
        stopped = _running(monkeypatch, {cli.EDITOR_PORT: graph_dir})
        with patch("streamstudio.app.create_app") as create_app_mock:
            with pytest.raises(SystemExit) as exit_info:
                _main(monkeypatch, "restart", "grpahs")
        assert exit_info.value.code == 1
        assert "'grpahs' is not a workspace folder" in " ".join(capsys.readouterr().out.split())
        assert stopped == []
        create_app_mock.assert_not_called()

    def test_restart_of_a_port_serves_the_folder_the_editor_on_it_serves(
        self, graph_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Measured before (R6): ``streamstudio restart --port 8193`` typed in ANOTHER folder served that folder —
        an empty list, and the kept work waited unseen in the folder the stopped editor served.
        """
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        stopped = _running(monkeypatch, {9007: graph_dir})
        with _serving(graph_dir) as create_app_mock:
            _main(monkeypatch, "restart", "9007")
        create_app_mock.assert_called_once_with(graph_dir, templates_dir=None)
        assert stopped == [9007] and _served_on(create_app_mock) == 9007

    def test_a_bare_restart_serves_the_folder_served_last(
        self, graph_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        _running(monkeypatch, {})
        with _serving(graph_dir):
            _main(monkeypatch, str(graph_dir))
        monkeypatch.chdir(elsewhere)
        with _serving(graph_dir) as create_app_mock:
            _main(monkeypatch, "restart")
        create_app_mock.assert_called_once_with(graph_dir, templates_dir=None)

    def test_a_bare_restart_with_nothing_served_before_serves_the_current_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _running(monkeypatch, {})
        with _serving(tmp_path) as create_app_mock:
            _main(monkeypatch, "restart")
        create_app_mock.assert_called_once_with(tmp_path.resolve(), templates_dir=None)

    def test_restart_keeps_the_templates_folder_the_editor_runs_with(
        self, graph_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _running(monkeypatch, {9007: graph_dir})
        real = serving.meta_at

        def with_templates(host: str, port: int) -> Optional[Dict[str, Any]]:
            answer = real(host, port)
            if answer is not None:
                answer["workspace"]["options"] = {"templates_dir": str(tmp_path / "tpl")}
            return answer

        monkeypatch.setattr(serving, "meta_at", with_templates)
        with _serving(graph_dir) as create_app_mock:
            _main(monkeypatch, "restart", "9007")
        create_app_mock.assert_called_once_with(graph_dir, templates_dir=tmp_path / "tpl")

    def test_restart_refuses_an_option_it_does_not_have_before_stopping_anything(
        self, graph_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stopped = _running(monkeypatch, {cli.EDITOR_PORT: graph_dir})
        with patch("streamstudio.app.create_app") as create_app_mock:
            with pytest.raises(SystemExit) as exit_info:
                _main(monkeypatch, "restart", "--comfy-dir", "/x")
        assert exit_info.value.code == 1
        assert stopped == []
        create_app_mock.assert_not_called()


class TestWhatTheCliSaysAboutItself:
    def test_the_description_names_the_editor_and_its_port(self) -> None:
        expected = "StreamStudio — a graph editor for YAML pipelines, over a folder of graphs (port 8190)."
        assert cli.app.description == expected

    @pytest.mark.parametrize("word", ["comfy", "taidal"])
    def test_no_help_text_names_a_removed_editor_or_the_workspace(self, word: str) -> None:
        """What ``streamstudio --help`` and each command's help render: the app's description, the module docstring
        and every command's docstring."""
        texts = [cli.app.description, cli.__doc__ or ""] + [func.__doc__ or "" for func in cli.app._commands.values()]
        assert not [text for text in texts if word in text.lower()]


# ------------------------------------------------------------------------------------------------
# unsaved work of the last session: the start line, the line on the way out, and what `stop` says
# ------------------------------------------------------------------------------------------------

PROBE = "recordstream.ops.debug.PrintRecordOp"
DEMO_TEXT = f"flow:\n  probe:\n    op: !class:{PROBE} {{label: probe}}\noutputs: probe\n"


def _keep_an_edit(graph_dir: Path, saved_at: str = "2026-09-29T13:25:04+02:00") -> Path:
    """A folder whose last session stopped with ``demo.yaml`` and ``fresh.yaml`` (made with New…, which wrote it)
    changed; answers the file."""
    (graph_dir / "demo.yaml").write_text(DEMO_TEXT)
    studio = app_module.Studio(graph_dir)
    studio.document("demo").set_param("probe", "label", "changed")
    studio.new_graph("fresh", "flow")
    studio.document("fresh").set_param("probe", "label", "edited")
    assert studio.stash() == [
        str(graph_dir / "demo.yaml"),
        str(graph_dir / "fresh.yaml"),
    ]
    data = json.loads(studio.recovery_path.read_text())
    studio.recovery_path.write_text(json.dumps({**data, "saved_at": saved_at}))
    return studio.recovery_path


class TestUnsavedWorkOfTheLastSession:
    @pytest.mark.usefixtures("free")
    def test_the_start_names_when_and_which_files_before_serving(
        self,
        graph_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _keep_an_edit(graph_dir)
        with _serving(graph_dir):
            _main(monkeypatch, str(graph_dir))
        when = datetime.fromisoformat("2026-09-29T13:25:04+02:00").astimezone()
        assert capsys.readouterr().out.splitlines() == [
            f"Unsaved work from the last session ({when.day} {when:%b %H:%M}): demo.yaml, fresh.yaml — the page asks "
            "whether to restore it."
        ]
        assert (graph_dir / app_module.RECOVERY_FILE).exists()  # nothing unsaved now: the file waits for an answer

    @pytest.mark.usefixtures("free")
    def test_a_file_that_cannot_be_read_is_named_and_the_editor_still_serves(
        self,
        graph_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (graph_dir / app_module.RECOVERY_FILE).write_text("{not json")
        with _serving(graph_dir) as create_app_mock:
            _main(monkeypatch, str(graph_dir))
        create_app_mock.return_value.run.assert_called_once()
        [line] = capsys.readouterr().out.splitlines()
        file = graph_dir / app_module.RECOVERY_FILE
        assert line.startswith(f"Unsaved work from the last session cannot be read — {file} cannot be read: ")
        assert line.endswith(". The page offers to move it aside.")

    @pytest.mark.usefixtures("free")
    def test_a_kept_graph_whose_file_cannot_be_read_never_stops_the_start(
        self,
        graph_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Measured before (R2): ``PermissionError`` from the start itself — the editor did not come up, and every
        later start the same way, while a file of ANOTHER folder the kept work names was unreadable.
        """
        other = tmp_path / "other"
        other.mkdir()
        (other / "away.yaml").write_text(DEMO_TEXT)
        studio = app_module.Studio(graph_dir)
        studio.open_graph(str(other / "away.yaml"))
        studio.document("away").set_param("probe", "label", "moved")
        studio.stash()
        (other / "away.yaml").chmod(0)
        try:
            with _serving(graph_dir) as create_app_mock:
                _main(monkeypatch, str(graph_dir))
        finally:
            (other / "away.yaml").chmod(0o644)
        create_app_mock.return_value.run.assert_called_once()
        [line] = capsys.readouterr().out.splitlines()
        assert line.startswith("Unsaved work from the last session (") and "../other/away.yaml" in line

    @pytest.mark.usefixtures("free")
    def test_no_file_no_line(
        self,
        graph_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with _serving(graph_dir):
            _main(monkeypatch, str(graph_dir))
        assert capsys.readouterr().out == ""

    @pytest.mark.usefixtures("free")
    def test_the_way_out_keeps_the_unsaved_work_and_says_where(
        self,
        graph_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (graph_dir / "demo.yaml").write_text(DEMO_TEXT)
        double = _double(graph_dir)
        studio = double.extensions[app_module.EXTENSION]
        double.run.side_effect = lambda **_kwargs: studio.document("demo").set_param("probe", "label", "x")
        with patch("streamstudio.app.create_app", return_value=double):
            _main(monkeypatch, str(graph_dir))
        assert capsys.readouterr().out.splitlines() == [
            f"Unsaved work (demo.yaml) is kept in {studio.recovery_path} — the next start asks what to do."
        ]
        assert (
            "label: x"
            in json.loads(studio.recovery_path.read_text())["graphs"][0]["texts"][
                str((graph_dir / "demo.yaml").resolve())
            ]
        )

    @pytest.mark.usefixtures("free")
    def test_a_signal_outside_the_serve_loop_takes_the_same_way_out(
        self, graph_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """werkzeug's serve loop returns quietly on Ctrl+C; one that arrives elsewhere in ``app.run`` is caught too."""
        (graph_dir / "demo.yaml").write_text(DEMO_TEXT)
        double = _double(graph_dir)
        studio = double.extensions[app_module.EXTENSION]

        def interrupted(**_kwargs: Any) -> None:
            studio.document("demo").set_param("probe", "label", "x")
            raise KeyboardInterrupt

        double.run.side_effect = interrupted
        before = signal.getsignal(signal.SIGTERM)
        with patch("streamstudio.app.create_app", return_value=double):
            _main(monkeypatch, str(graph_dir))
        assert studio.recovery_path.exists()
        assert signal.getsignal(signal.SIGTERM) == before  # the handler is the process's again


class TestTheWayOutWhenTheTerminalIsGone:
    """After SIGHUP (the terminal window closed) every print fails with ``EIO``. The work is kept first; a line nobody
    can read then points the output at /dev/null instead of ending in a traceback or exit code 120 (measured with a
    real closed pseudo-terminal: ``tests/test_recovery_process.py``). ``os.dup2`` is replaced here — the test process's
    own file descriptors are never touched."""

    def test_a_line_is_printed_while_the_terminal_is_there(self, capsys: pytest.CaptureFixture[str]) -> None:
        cli._say("Unsaved work (demo.yaml) is kept")
        assert capsys.readouterr().out == "Unsaved work (demo.yaml) is kept\n"

    def test_a_line_that_cannot_be_printed_points_both_outputs_at_devnull(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Gone:
            def write(self, _text: str) -> int:
                raise OSError(5, "Input/output error")

            def flush(self) -> None:
                raise OSError(5, "Input/output error")

        pointed: List[int] = []
        monkeypatch.setattr(sys, "stdout", Gone())
        monkeypatch.setattr(cli.os, "dup2", lambda _devnull, fd: pointed.append(fd))
        cli._say("Unsaved work (demo.yaml) is kept")
        assert pointed == [1, 2]

    @pytest.mark.usefixtures("free")
    def test_output_left_in_a_buffer_at_the_way_out_cannot_fail_the_exit(
        self, graph_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Gone:
            def write(self, _text: str) -> int:
                raise OSError(5, "Input/output error")

            def flush(self) -> None:
                raise OSError(5, "Input/output error")

        double = _double(graph_dir)
        pointed: List[int] = []
        with patch("streamstudio.app.create_app", return_value=double):
            monkeypatch.setattr(cli.os, "dup2", lambda _devnull, fd: pointed.append(fd))
            monkeypatch.setattr(sys, "stderr", Gone())
            _main(monkeypatch, str(graph_dir))
        assert pointed == [1, 2]

    @pytest.mark.usefixtures("free")
    def test_a_folder_the_editor_cannot_write_says_the_work_was_not_kept(
        self,
        graph_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (graph_dir / "demo.yaml").write_text(DEMO_TEXT)
        double = _double(graph_dir)
        studio = double.extensions[app_module.EXTENSION]

        def edit_then_lock(**_kwargs: Any) -> None:
            studio.document("demo").set_param("probe", "label", "x")
            graph_dir.chmod(0o500)

        double.run.side_effect = edit_then_lock
        try:
            with patch("streamstudio.app.create_app", return_value=double):
                _main(monkeypatch, str(graph_dir))
        finally:
            graph_dir.chmod(0o700)
        assert capsys.readouterr().out.splitlines() == [
            f"Unsaved work could not be kept in {studio.recovery_path}: Permission denied"
        ]


class TestWhatStopSays:
    def test_the_unsaved_work_a_canvas_editor_kept_is_named(
        self,
        graph_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        kept = _keep_an_edit(graph_dir)
        _running(monkeypatch, {9007: graph_dir})
        monkeypatch.setattr(
            cli,
            "_unsaved_work",
            lambda port: cli._Unsaved(port, graph_dir, [graph_dir / "demo.yaml"]),
        )
        _main(monkeypatch, "stop", "9007")
        assert capsys.readouterr().out.splitlines() == [
            f"streamstudio ({graph_dir}): stopped pid 4780 (port 9007)",
            f"Its unsaved work (demo.yaml) is kept in {kept} — the next start asks what to do.",
        ]

    def test_work_the_editor_did_not_write_is_said_to_be_lost(
        self,
        graph_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An editor killed after ignoring SIGTERM writes no file — the message must not claim it did."""
        file = graph_dir / app_module.RECOVERY_FILE
        _running(monkeypatch, {9007: graph_dir})
        monkeypatch.setattr(
            cli,
            "_unsaved_work",
            lambda port: cli._Unsaved(port, graph_dir, [graph_dir / "demo.yaml"]),
        )
        _main(monkeypatch, "stop", "9007")
        assert capsys.readouterr().out.splitlines()[-1] == (
            f"Its unsaved work (demo.yaml) was not kept — the editor did not write it to {file}."
        )

    def test_nothing_unsaved_nothing_extra(
        self,
        graph_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _running(monkeypatch, {9007: graph_dir})
        monkeypatch.setattr(cli, "_unsaved_work", lambda port: cli._Unsaved(port, graph_dir, []))
        _main(monkeypatch, "stop", "9007")
        assert capsys.readouterr().out.splitlines() == [f"streamstudio ({graph_dir}): stopped pid 4780 (port 9007)"]

    def test_an_editor_that_was_not_stopped_is_said_nothing_about(
        self,
        graph_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Two editors hold unsaved work and ONE is stopped: only that one's work is spoken of."""
        _keep_an_edit(graph_dir)
        _running(monkeypatch, {9007: graph_dir, 9008: tmp_path})
        folders = {9007: graph_dir, 9008: tmp_path}
        monkeypatch.setattr(
            cli,
            "_unsaved_work",
            lambda port: cli._Unsaved(port, folders[port], [folders[port] / "demo.yaml"]),
        )
        _main(monkeypatch, "stop", "9007")
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 2 and str(tmp_path / app_module.RECOVERY_FILE) not in lines[1]

    def test_restart_starts_with_the_line_that_offers_the_work(
        self,
        graph_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _keep_an_edit(graph_dir)
        _running(monkeypatch, {9007: graph_dir})
        with _serving(graph_dir):
            _main(monkeypatch, "restart", "9007")
        lines = capsys.readouterr().out.splitlines()
        assert lines[-1].startswith("Unsaved work from the last session (")

    def test_nothing_listening_answers_nothing(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((cli.STUDIO_HOST, 0))
            port = int(probe.getsockname()[1])
        assert _ASK(port) is None

    def test_a_canvas_editor_answers_its_folder_and_the_files_it_holds_unsaved(
        self, graph_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two routes the question reads, answered by a real app over the folder."""
        (graph_dir / "demo.yaml").write_text(DEMO_TEXT)
        client = app_module.create_app(graph_dir).test_client()
        client.post(
            "/api/graphs/demo/param",
            json={"node": "probe", "param": "label", "value": "x"},
        )
        answers = {
            "/api/workspace": client.get("/api/workspace"),
            "/api/graphs": client.get("/api/graphs"),
        }
        monkeypatch.setattr(cli, "_ask", lambda url: answers[url.split(":9007", 1)[1]].get_json())
        assert _ASK(9007) == cli._Unsaved(9007, graph_dir, [graph_dir / "demo.yaml"])

    @pytest.mark.parametrize("answer", [["not", "a", "mapping"], {"folder": 3}])
    def test_something_that_does_not_answer_like_a_canvas_editor_is_not_asked_further(
        self, answer: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "_ask", lambda url: answer)
        assert _ASK(9007) is None

    def test_a_kept_file_that_cannot_be_read_holds_nothing(self, tmp_path: Path) -> None:
        (tmp_path / "broken.json").write_text("{not json")
        assert cli._kept_in(tmp_path / "broken.json") == set()
        assert cli._kept_in(tmp_path / "absent.json") == set()

    def test_a_graph_of_another_folder_is_named_by_its_way_from_the_folder(
        self,
        graph_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Measured before (R11): ``Its unsaved work (flow.yaml, flow.yaml) is kept …`` — two graphs of one file name,
        one of them from another folder, could not be told apart (nor at the start, nor in the page's question).
        """
        other = tmp_path / "other"
        other.mkdir()
        (graph_dir / "flow.yaml").write_text(DEMO_TEXT)
        (other / "flow.yaml").write_text(DEMO_TEXT)
        studio = app_module.Studio(graph_dir)
        studio.document("flow").set_param("probe", "label", "here")
        studio.open_graph(str(other / "flow.yaml"))
        studio.document("flow~2").set_param("probe", "label", "there")
        kept = studio.stash()
        unsaved = cli._Unsaved(9007, graph_dir, [Path(path) for path in kept])
        assert unsaved.names == ["flow.yaml", "../other/flow.yaml"]
        _running(monkeypatch, {9007: graph_dir})
        monkeypatch.setattr(cli, "_unsaved_work", lambda port: unsaved)
        _main(monkeypatch, "stop", "9007")
        assert capsys.readouterr().out.splitlines()[-1] == (
            "Its unsaved work (flow.yaml, ../other/flow.yaml) is kept "
            f"in {graph_dir / app_module.RECOVERY_FILE} — the next start asks what to do."
        )
        notice = cli._recovery_notice(app_module.Studio(graph_dir).workspace())
        assert notice is not None and ": flow.yaml, ../other/flow.yaml — the page asks" in notice
