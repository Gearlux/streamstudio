"""``streamstudio`` serves the canvas editor over a folder of graphs; ``stop`` and ``restart`` address it by port.

Three commands — ``run`` (the default), ``stop``, ``restart`` — and one default port, 8190. The CLI is
``strict_flags``: a flag that names no parameter is refused before anything runs or stops, so an option of no
command here (``--editor``, ``--comfy-dir``) is an error in liquifai's words, never silently ignored.

Every serve is intercepted at ``streamstudio.app.create_app`` — its double holds a REAL ``Studio`` over the folder,
so the recovery file's start line and the stash on the way out run for real — so nothing binds a port; the busy-port
probe is exercised against a REAL listening socket. No test here asks a port what it holds unsaved
(``cli._unsaved_work`` is replaced for the whole file): a bare ``stop`` means port 8190, where the person's own editor
may be running.
"""

import inspect
import json
import shlex
import signal
import socket
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, List
from unittest.mock import MagicMock, patch

import pytest
from confluid import parse_param_docs

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
    """The default ports may be busy on a developer's machine: every test that is not ABOUT the probe
    sees them free."""
    monkeypatch.setattr(cli, "_port_in_use", lambda port, host=cli.STUDIO_HOST: False)


#: The real question ``stop`` asks, kept before the autouse fixture replaces it.
_ASK = cli._unsaved_work


@pytest.fixture(autouse=True)
def _never_ask_a_real_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_unsaved_work", lambda port: None)


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
    def test_the_commands_are_run_stop_and_restart_on_port_8190(self) -> None:
        assert sorted(cli.app._commands) == ["restart", "run", "stop"]
        assert cli.EDITOR_PORT == 8190

    @pytest.mark.parametrize("command", ["run", "stop", "restart"])
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
        create_app_mock.return_value.run.assert_called_once_with(host=cli.STUDIO_HOST, port=cli.EDITOR_PORT)

    @pytest.mark.usefixtures("free")
    @pytest.mark.parametrize("command", [[], ["run"]])
    def test_a_directory_a_port_and_a_templates_folder_reach_the_canvas(
        self, graph_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: List[str]
    ) -> None:
        templates = tmp_path / "tpl"
        with _serving(graph_dir) as create_app_mock:
            _main(monkeypatch, *command, str(graph_dir), "--templates-dir", str(templates), "-p", "9123")
        create_app_mock.assert_called_once_with(graph_dir, templates_dir=templates)
        create_app_mock.return_value.run.assert_called_once_with(host=cli.STUDIO_HOST, port=9123)

    @pytest.mark.parametrize(
        "argv, option",
        [
            (["--editor", "comfy"], "editor"),
            (["--comfy-dir", "/x"], "comfy-dir"),
            (["--custom-node", "https://example.invalid/x.git"], "custom-node"),
            (["--background"], "background"),
        ],
    )
    def test_an_option_no_command_has_is_refused_before_anything_is_served(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: List[str], option: str
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
    def test_a_missing_directory_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        missing = tmp_path / "nowhere"
        with patch("streamstudio.app.create_app") as create_app_mock:
            with pytest.raises(SystemExit) as exit_info:
                _main(monkeypatch, str(missing))
        assert str(exit_info.value) == f"Graph directory not found: {missing}"
        create_app_mock.assert_not_called()

    def test_a_busy_port_is_refused_before_serving_with_a_command_the_cli_accepts(
        self, graph_dir: Path, busy_port: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("streamstudio.app.create_app") as create_app_mock:
            with pytest.raises(SystemExit) as exit_info:
                _main(monkeypatch, str(graph_dir), "--port", str(busy_port))
        assert exit_info.value.code == 1
        create_app_mock.assert_not_called()
        out = capsys.readouterr().out
        assert f"http://{cli.STUDIO_HOST}:{busy_port}" in out
        suggested = [line for line in out.splitlines() if "run StreamStudio elsewhere:" in line]
        assert len(suggested) == 1, out
        argv = shlex.split(suggested[0].split("elsewhere:", 1)[1])
        assert argv == ["streamstudio", str(graph_dir), "--port", str(busy_port + 1)]
        # pasted EXACTLY as printed, it serves the same folder on the next port
        monkeypatch.setattr(cli, "_port_in_use", lambda port, host=cli.STUDIO_HOST: False)
        with _serving(graph_dir) as create_app_mock:
            _main(monkeypatch, *argv[1:])
        create_app_mock.assert_called_once_with(graph_dir, templates_dir=None)
        create_app_mock.return_value.run.assert_called_once_with(host=cli.STUDIO_HOST, port=busy_port + 1)

    def test_the_busy_port_suggestion_keeps_a_relative_folder_and_the_templates_folder(
        self, tmp_path: Path, busy_port: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Measured before, from ``streamstudio graphs --templates-dir tpl``: the suggestion was ``streamstudio
        --port 8195`` — pasted as printed it served the current directory with the default templates folder,
        and ``streamstudio --port 8195 graphs`` is refused (``unrecognized argument 'graphs'``)."""
        (tmp_path / "my graphs").mkdir()
        monkeypatch.chdir(tmp_path)
        with patch("streamstudio.app.create_app"):
            with pytest.raises(SystemExit):
                _main(monkeypatch, "my graphs", "--templates-dir", "tpl", "--port", str(busy_port))
        line = next(line for line in capsys.readouterr().out.splitlines() if "elsewhere:" in line)
        argv = shlex.split(line.split("elsewhere:", 1)[1])
        assert argv == [
            "streamstudio",
            str(tmp_path / "my graphs"),
            "--templates-dir",
            str(tmp_path / "tpl"),
            "--port",
            str(busy_port + 1),
        ]
        monkeypatch.setattr(cli, "_port_in_use", lambda port, host=cli.STUDIO_HOST: False)
        with _serving(tmp_path / "my graphs") as create_app_mock:
            _main(monkeypatch, *argv[1:])
        create_app_mock.assert_called_once_with(tmp_path / "my graphs", templates_dir=tmp_path / "tpl")

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
    @pytest.mark.parametrize(
        "argv, port",
        [
            (["stop"], cli.EDITOR_PORT),
            (["stop", "-p", "9002"], 9002),
            (["--port", "9003", "stop"], 9003),
        ],
    )
    def test_stop_defaults_to_the_editors_port_and_asks_it_first(
        self, monkeypatch: pytest.MonkeyPatch, argv: List[str], port: int
    ) -> None:
        asked: List[int] = []
        monkeypatch.setattr(cli, "_unsaved_work", lambda port: asked.append(port))
        with patch.object(cli, "_stop_studio", return_value=None) as stop_mock:
            _main(monkeypatch, *argv)
        stop_mock.assert_called_once_with(port)
        assert asked == [port]

    def test_stop_refuses_an_option_it_does_not_have_and_stops_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with patch.object(cli, "_stop_studio") as stop_mock:
            with pytest.raises(SystemExit):
                _main(monkeypatch, "stop", "--editor", "comfy")
        stop_mock.assert_not_called()

    @pytest.mark.usefixtures("free")
    def test_restart_of_the_canvas_stops_its_port_then_serves_the_directory(
        self, graph_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: List[str] = []

        def stopped(port: int) -> None:
            calls.append(f"stop {port}")

        def served(*args: Any, **kwargs: Any) -> MagicMock:
            calls.append("serve")
            return _double(graph_dir)

        with patch.object(cli, "_stop_studio", side_effect=stopped):
            with patch("streamstudio.app.create_app", side_effect=served) as create_app_mock:
                _main(monkeypatch, "restart", str(graph_dir))
        assert calls == [f"stop {cli.EDITOR_PORT}", "serve"]
        create_app_mock.assert_called_once_with(graph_dir, templates_dir=None)

    def test_restart_checks_the_folder_before_stopping_anything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Measured before: ``streamstudio restart grpahs`` stopped the running editor, THEN said ``Graph directory
        not found: grpahs`` — a typo took the editor down and started nothing."""
        monkeypatch.chdir(tmp_path)
        with patch.object(cli, "_stop_studio") as stop_mock, patch("streamstudio.app.create_app") as create_app_mock:
            with pytest.raises(SystemExit) as exit_info:
                _main(monkeypatch, "restart", "grpahs")
        assert str(exit_info.value) == f"Graph directory not found: {tmp_path / 'grpahs'}"
        stop_mock.assert_not_called()
        create_app_mock.assert_not_called()

    @pytest.mark.usefixtures("free")
    def test_restart_with_no_folder_restarts_the_folder_the_running_editor_serves(
        self, graph_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Measured before (R6): ``streamstudio restart --port 8193`` typed in ANOTHER folder served that folder —
        an empty list, and the kept work waited unseen in the folder the stopped editor served."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        monkeypatch.setattr(cli, "_unsaved_work", lambda port: cli._Unsaved(graph_dir, []))
        with patch.object(cli, "_stop_studio", return_value=4780), _serving(graph_dir) as create_app_mock:
            _main(monkeypatch, "restart", "--port", "9007")
        create_app_mock.assert_called_once_with(graph_dir, templates_dir=None)

    @pytest.mark.usefixtures("free")
    def test_restart_with_no_folder_and_no_canvas_editor_answering_serves_the_current_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        with patch.object(cli, "_stop_studio", return_value=None), _serving(tmp_path) as create_app_mock:
            _main(monkeypatch, "restart", "--port", "9007")
        create_app_mock.assert_called_once_with(tmp_path.resolve(), templates_dir=None)

    @pytest.mark.usefixtures("free")
    def test_restart_with_a_folder_serves_that_folder_whatever_the_running_editor_served(
        self, graph_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "_unsaved_work", lambda port: cli._Unsaved(tmp_path, []))
        with patch.object(cli, "_stop_studio", return_value=4780), _serving(graph_dir) as create_app_mock:
            _main(monkeypatch, "restart", str(graph_dir), "--port", "9007")
        create_app_mock.assert_called_once_with(graph_dir, templates_dir=None)

    def test_restart_refuses_an_option_it_does_not_have_before_stopping_anything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with patch.object(cli, "_stop_studio") as stop_mock, patch("streamstudio.app.create_app") as create_app_mock:
            with pytest.raises(SystemExit) as exit_info:
                _main(monkeypatch, "restart", "--comfy-dir", "/x")
        assert exit_info.value.code == 1
        stop_mock.assert_not_called()
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
    assert studio.stash() == [str(graph_dir / "demo.yaml"), str(graph_dir / "fresh.yaml")]
    data = json.loads(studio.recovery_path.read_text())
    studio.recovery_path.write_text(json.dumps({**data, "saved_at": saved_at}))
    return studio.recovery_path


class TestUnsavedWorkOfTheLastSession:
    @pytest.mark.usefixtures("free")
    def test_the_start_names_when_and_which_files_before_serving(
        self, graph_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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
        self, graph_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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
        self, graph_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Measured before (R2): ``PermissionError`` from the start itself — the editor did not come up, and every
        later start the same way, while a file of ANOTHER folder the kept work names was unreadable."""
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
        self, graph_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with _serving(graph_dir):
            _main(monkeypatch, str(graph_dir))
        assert capsys.readouterr().out == ""

    @pytest.mark.usefixtures("free")
    def test_the_way_out_keeps_the_unsaved_work_and_says_where(
        self, graph_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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

    @pytest.mark.usefixtures("free")
    def test_sigterm_and_sighup_are_turned_into_the_ctrl_c_way_out_only_while_serving(
        self, graph_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ways_out = [getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)]
        assert list(cli._WAYS_OUT) == ways_out
        double = _double(graph_dir)
        seen: List[Any] = []
        double.run.side_effect = lambda **_kwargs: seen.extend(signal.getsignal(signum) for signum in ways_out)
        before = [signal.getsignal(signum) for signum in ways_out]
        with patch("streamstudio.app.create_app", return_value=double):
            _main(monkeypatch, str(graph_dir))
        assert seen == [cli._interrupt] * len(ways_out)
        assert [signal.getsignal(signum) for signum in ways_out] == before  # the process's handlers again
        with pytest.raises(KeyboardInterrupt):
            cli._interrupt(signal.SIGTERM, None)


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
        self, graph_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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
        self, graph_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        kept = _keep_an_edit(graph_dir)
        monkeypatch.setattr(cli, "_unsaved_work", lambda port: cli._Unsaved(graph_dir, [graph_dir / "demo.yaml"]))
        with patch.object(cli, "_stop_studio", return_value=4780):
            _main(monkeypatch, "stop", "--port", "9007")
        assert capsys.readouterr().out.splitlines() == [
            f"Stopped StreamStudio on port 9007 (pid 4780). Its unsaved work (demo.yaml) is kept in {kept} — the next "
            "start asks what to do."
        ]

    def test_work_the_editor_did_not_write_is_said_to_be_lost(
        self, graph_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An editor from before this change, or one killed after ignoring SIGTERM, writes no file — the message
        must not claim it did."""
        file = graph_dir / app_module.RECOVERY_FILE
        monkeypatch.setattr(cli, "_unsaved_work", lambda port: cli._Unsaved(graph_dir, [graph_dir / "demo.yaml"]))
        with patch.object(cli, "_stop_studio", return_value=4780):
            _main(monkeypatch, "stop", "--port", "9007")
        assert capsys.readouterr().out.splitlines() == [
            f"Stopped StreamStudio on port 9007 (pid 4780). Its unsaved work (demo.yaml) was not kept — the editor did "
            f"not write it to {file}."
        ]

    def test_nothing_unsaved_nothing_extra(
        self, graph_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(cli, "_unsaved_work", lambda port: cli._Unsaved(graph_dir, []))
        with patch.object(cli, "_stop_studio", return_value=4780):
            _main(monkeypatch, "stop", "--port", "9007")
        assert capsys.readouterr().out.splitlines() == ["Stopped StreamStudio on port 9007 (pid 4780)."]

    def test_restart_says_it_too_then_the_start_offers_the_work(
        self, graph_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _keep_an_edit(graph_dir)
        monkeypatch.setattr(cli, "_unsaved_work", lambda port: cli._Unsaved(graph_dir, [graph_dir / "demo.yaml"]))
        monkeypatch.setattr(cli, "_port_in_use", lambda port, host=cli.STUDIO_HOST: False)
        with patch.object(cli, "_stop_studio", return_value=4780), _serving(graph_dir):
            _main(monkeypatch, "restart", str(graph_dir), "--port", "9007")
        stopped, started = capsys.readouterr().out.splitlines()
        assert stopped.startswith("Stopped StreamStudio on port 9007 (pid 4780). Its unsaved work (demo.yaml) is kept")
        assert started.startswith("Unsaved work from the last session (")

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
        client.post("/api/graphs/demo/param", json={"node": "probe", "param": "label", "value": "x"})
        answers = {"/api/workspace": client.get("/api/workspace"), "/api/graphs": client.get("/api/graphs")}
        monkeypatch.setattr(cli, "_get_json", lambda url: answers[url.split(":9007", 1)[1]].get_json())
        assert _ASK(9007) == cli._Unsaved(graph_dir, [graph_dir / "demo.yaml"])

    @pytest.mark.parametrize("answer", [["not", "a", "mapping"], {"folder": 3}])
    def test_something_that_does_not_answer_like_a_canvas_editor_is_not_asked_further(
        self, answer: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "_get_json", lambda url: answer)
        assert _ASK(9007) is None

    def test_a_kept_file_that_cannot_be_read_holds_nothing(self, tmp_path: Path) -> None:
        (tmp_path / "broken.json").write_text("{not json")
        assert cli._kept_in(tmp_path / "broken.json") == set()
        assert cli._kept_in(tmp_path / "absent.json") == set()

    def test_a_graph_of_another_folder_is_named_by_its_way_from_the_folder(
        self, graph_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Measured before (R11): ``Its unsaved work (flow.yaml, flow.yaml) is kept …`` — two graphs of one file name,
        one of them from another folder, could not be told apart (nor at the start, nor in the page's question)."""
        other = tmp_path / "other"
        other.mkdir()
        (graph_dir / "flow.yaml").write_text(DEMO_TEXT)
        (other / "flow.yaml").write_text(DEMO_TEXT)
        studio = app_module.Studio(graph_dir)
        studio.document("flow").set_param("probe", "label", "here")
        studio.open_graph(str(other / "flow.yaml"))
        studio.document("flow~2").set_param("probe", "label", "there")
        kept = studio.stash()
        unsaved = cli._Unsaved(graph_dir, [Path(path) for path in kept])
        assert unsaved.names == ["flow.yaml", "../other/flow.yaml"]
        monkeypatch.setattr(cli, "_unsaved_work", lambda port: unsaved)
        with patch.object(cli, "_stop_studio", return_value=4780):
            _main(monkeypatch, "stop", "--port", "9007")
        assert capsys.readouterr().out.splitlines() == [
            "Stopped StreamStudio on port 9007 (pid 4780). Its unsaved work (flow.yaml, ../other/flow.yaml) is kept "
            f"in {graph_dir / app_module.RECOVERY_FILE} — the next start asks what to do."
        ]
        notice = cli._recovery_notice(app_module.Studio(graph_dir).workspace())
        assert notice is not None and ": flow.yaml, ../other/flow.yaml — the page asks" in notice
