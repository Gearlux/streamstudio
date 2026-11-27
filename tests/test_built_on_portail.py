"""The standalone editor is served by portail — the CLI keeps no serving code of its own.

portail is the framework a workspace web app is served by: serving in the foreground or detached, ``stop`` /
``restart`` / ``list`` by the server's identity, a stop that never signals a program that is not the app. The editor
says who it is (an ``AppSpec``), what it serves (a folder of graphs) and what it does on its way out (keep the unsaved
work); everything else it imports. What those commands DO is pinned in portail's own suite.

Measured before: ``streamstudio stop`` ended whatever process listened on the port, without asking what it was.
"""

import ast
import importlib.util
import tomllib
from pathlib import Path
from typing import Any, Set

import pytest

import streamstudio

PACKAGE = Path(streamstudio.__file__).parent
PROJECT = PACKAGE.parent


def _names(path: Path) -> Set[str]:
    """Every function and class a module defines, at any depth, the leading underscores dropped."""
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    return {
        node.name.lstrip("_")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, kinds)
    }


def _portail_file(module: str) -> Path:
    spec = importlib.util.find_spec(module)
    assert spec is not None and spec.origin is not None, f"{module} is not installed"
    return Path(spec.origin)


class TestTheDependency:
    def test_pyproject_declares_portail_and_no_process_scanner(self) -> None:
        """The pids of a stop are portail's to find; the editor's own per-process scan is gone, and its package."""
        project = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        declared = [line.replace(" ", "") for line in project["dependencies"]]
        assert [line for line in declared if line.startswith("portail>=")]
        assert not [line for line in declared if line.startswith("psutil")]
        assert not [line for line in project["optional-dependencies"]["dev"] if line.startswith("types-psutil")]


class TestWhoTheEditorIs:
    def test_the_spec(self) -> None:
        from portail import AppSpec

        from streamstudio.cli import EDITOR_PORT, SPEC

        assert isinstance(SPEC, AppSpec)
        assert (SPEC.name, SPEC.cli_module, SPEC.default_port) == ("streamstudio", "streamstudio.cli", EDITOR_PORT)
        assert SPEC.workspace == "folder"
        assert SPEC.state_dir() == Path.home() / ".streamstudio"

    def test_the_unsaved_work_is_kept_through_the_leaving_hook(self) -> None:
        from portail import Hooks

        from streamstudio.cli import SPEC

        assert SPEC.hooks.leaving is not Hooks().leaving

    def test_a_folder_of_graphs_is_what_portail_serves(self, tmp_path: Path) -> None:
        from portail import Served

        from streamstudio.cli import open_folder

        served = open_folder(str(tmp_path))
        assert isinstance(served, Served)
        assert (served.path, served.template, served.port) == (str(tmp_path), "", 8190)
        assert {"path", "template", "state_dir"} <= set(served.info())

    def test_the_templates_folder_is_an_option_portail_carries(self, tmp_path: Path) -> None:
        """A background start and a restart serve with the templates folder the server was started with."""
        from portail import serving

        from streamstudio.cli import SPEC, open_folder

        served = open_folder(str(tmp_path), templates_dir=str(tmp_path / "t"))
        assert served.info()["options"] == {"templates_dir": str(tmp_path / "t")}
        argv = serving.background_command(SPEC, served)
        assert argv[argv.index("--templates-dir") + 1] == str(tmp_path / "t")


class TestTheCommands:
    def test_they_are_portails_with_the_editors_two_serving_ones(self) -> None:
        from streamstudio.cli import app

        assert set(app._commands) == {"open", "stop", "restart", "list"}
        assert app._default_cmd is app._commands["open"]
        assert getattr(app._commands["open"], "__liquifai_positionals__", []) == ["graph_dir"]

    @pytest.mark.parametrize("command", ["open", "restart"])
    def test_a_serving_command_takes_the_templates_folder_and_p(self, command: str) -> None:
        import inspect

        from streamstudio.cli import app

        assert {"port", "templates_dir", "background"} <= set(inspect.signature(app._commands[command]).parameters)
        assert getattr(app._commands[command], "__liquifai_short__", {}).get("p") == "port"

    def test_a_start_decides_the_browser_by_what_is_named_and_a_restart_opens_none(self) -> None:
        """``open`` leaves ``--browser`` blank: a folder opens no tab, a graph's file opens its own (pinned in
        ``tests/test_cli_editor.py::TestServingTheCanvas``). ``restart`` opens none unless asked."""
        import inspect

        from streamstudio.cli import app

        assert inspect.signature(app._commands["open"]).parameters["browser"].default == ""
        assert inspect.signature(app._commands["restart"]).parameters["browser"].default == "never"

    def test_no_function_of_portails_serving_is_defined_again(self) -> None:
        """Before the move ``cli.py`` held its own port probe, process scan, stop and signal handling."""
        portail = _names(_portail_file("portail.serving")) | _names(_portail_file("portail.cli"))
        assert sorted((portail & _names(PACKAGE / "cli.py")) - {"init__", "open_", "restart", "stop"}) == []
        source = (PACKAGE / "cli.py").read_text(encoding="utf-8")
        for module in ("psutil", "socket", "signal"):
            assert f"import {module}" not in source


class TestTheStandaloneApp:
    def _client(self, tmp_path: Path) -> Any:
        from streamstudio.cli import create_app, open_folder, server_of

        return create_app(server_of(open_folder(str(tmp_path)))).test_client()

    def test_it_answers_as_streamstudio(self, tmp_path: Path) -> None:
        """``/api/meta`` names the app — the identity ``stop``, ``restart`` and ``list`` find a server by."""
        meta = self._client(tmp_path).get("/api/meta").get_json()
        assert meta["app"] == "streamstudio" and meta["workspace"]["path"] == str(tmp_path)

    def test_the_workspace_route_stays_the_editors(self, tmp_path: Path) -> None:
        """The page joins through it: its folder, its session, the kept work of the last session."""
        answer = self._client(tmp_path).get("/api/workspace").get_json()
        assert {"folder", "session", "recovery", "host"} <= set(answer)

    def test_the_pages_stop_and_restart_buttons_find_their_shell(self, tmp_path: Path) -> None:
        """The page's ↻ and ⏻ are portail's shell: the page loads ``portail/shell.js`` from BESIDE itself
        (``frontend/src/shell.ts``) and the shell stops and restarts the server through its own routes. Nothing of
        the editor's ``/api/workspace`` makes the shell ask about unsaved changes: the stop keeps them."""
        import streamstudio

        client = self._client(tmp_path)
        assert client.get("/portail/shell.js").status_code == 200
        assert client.get("/portail/shell.css").status_code == 200
        workspace = client.get("/api/workspace").get_json()
        assert workspace["shell"] is True, "what tells the page its buttons can be loaded"
        assert not (workspace.get("path") and workspace.get("dirty")), "the shell would ask before a stop"
        built = "".join(
            path.read_text(encoding="utf-8")
            for path in (Path(streamstudio.__file__).parent / "canvas" / "assets").glob("*.js")
        )
        assert '"portail/shell.js"' in built and '"/portail/shell.js"' not in built, "relative to the page"

    def test_CON_mounted_in_a_host_the_page_finds_no_shell_and_draws_no_buttons(self, tmp_path: Path) -> None:
        """That server is the host's to stop: beside the mounted page there is no shell to load."""
        from flask import Flask

        from streamstudio.app import editor_blueprint, page_blueprint

        host = Flask("host")
        host.register_blueprint(editor_blueprint(graph_dir=tmp_path), url_prefix="/studio")
        host.register_blueprint(page_blueprint(), url_prefix="/studio")
        client = host.test_client()
        assert client.get("/studio/api/workspace").get_json()["shell"] is False, "the page then asks for no shell"
        assert client.get("/studio/portail/shell.js").status_code == 404

    def test_a_host_mounting_the_editor_gets_no_shell_routes(self, tmp_path: Path) -> None:
        """portail serves the STANDALONE editor only; a host's app is the host's."""
        from flask import Flask

        from streamstudio.app import editor_blueprint

        host = Flask("host")
        host.register_blueprint(editor_blueprint(graph_dir=tmp_path), url_prefix="/editor")
        assert host.test_client().get("/editor/api/meta").status_code == 404
