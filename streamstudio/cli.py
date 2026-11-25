"""The ``streamstudio`` command: serve the graph editor over a folder of YAML graphs.

Serving, ``stop``, ``restart`` and ``list`` are portail's — the framework a workspace web app is served by. This module
says who the editor is (:data:`SPEC`), what it serves (a :class:`Folder` of graphs) and what is its own:

* ``open`` (the default — bare ``streamstudio``): the editor (``docs/rewrite.md``) over the confluid documents of a
  folder — ``streamstudio [graph_dir | graph.yaml] [--port 8190] [--templates-dir DIR] [--background]``, the Flask
  app of :mod:`streamstudio.app`, standalone (a consuming Flask app mounts the same blueprint instead).
* ``stop [folder | port] [--all]`` / ``restart [folder | port]``: a server of the editor, found by what it answers —
  never a program that is not the editor. The editor stops by the same way out for SIGTERM (what they send) as for
  Ctrl+C, keeping its unsaved work in the folder's hidden ``.streamstudio-workspace.json``; ``stop`` names what was
  kept, and the next start offers it.
* ``list``: every running editor, its port, its folder and its address.
"""

import http.client
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, NamedTuple, Optional, Set

from liquifai import LiquifyApp
from loggair import get_logger
from portail import AppSpec, Hooks, Served, serving
from portail.cli import add_lifecycle_commands
from portail.serving import BrowserChoice, Server

if TYPE_CHECKING:
    from flask import Flask

logger = get_logger(__name__)

STUDIO_HOST = "127.0.0.1"
#: The editor's default port.
EDITOR_PORT = 8190
#: How long ``stop`` / ``restart`` wait for the editor to say which graphs hold unsaved work before stopping it anyway
#: (seconds). It answers in milliseconds.
ASK_TIMEOUT = 5.0


#: ``strict_flags``: every command here takes PLAIN values — there is no config document for an
#: override to fall through to, so a flag that reaches nothing reached nothing at all. Without it a
#: mistyped ``--templates-dri`` is indistinguishable from one that worked (liquifai's report-based
#: warning only speaks when something was materialized, and a launcher materializes nothing).
app = LiquifyApp(
    name="streamstudio",
    description="StreamStudio — a graph editor for YAML pipelines, over a folder of graphs (port 8190).",
    strict_flags=True,
)


# =======================================================================================
# What the editor serves
# =======================================================================================


class Folder:
    """A folder of graphs as portail serves it (its ``Served``): the folder IS the workspace — there is no workspace
    file.

    Args:
        path: The folder, absolute.
        templates_dir: The templates folder, absolute — ``""``: the default (``~/.streamstudio/templates``). The one
            serving option of the editor's own: a background start and a restart serve with it again.
    """

    def __init__(self, path: str, templates_dir: str = "") -> None:
        self.host = STUDIO_HOST
        self.port = EDITOR_PORT
        self.path = path
        self.template = ""
        self.title = Path(path).name or path
        self.options: Dict[str, str] = {"templates_dir": templates_dir} if templates_dir else {}
        #: What the browser's tab opens on (portail's optional ``page``): the graph a start named by its file.
        self.page = _graph_page(_NAMED_GRAPH[0]) if _NAMED_GRAPH else ""

    def state_dir(self) -> Path:
        """The folder itself: the recovery file is kept there."""
        return Path(self.path)

    def info(self) -> Dict[str, Any]:  # JSON-safe, what ``/api/meta`` answers under ``workspace``
        return {"path": self.path, "template": "", "state_dir": self.path, "options": dict(self.options)}


def open_folder(path: str, templates_dir: str = "") -> Folder:
    """The graph folder ``path`` (absolute — portail resolved it and refused one that is not there), served with
    ``templates_dir`` (resolved here, ONCE: every path the editor answers is then absolute)."""
    return Folder(path, str(Path(templates_dir).expanduser().resolve()) if templates_dir else "")


def open_here() -> Folder:
    """What a bare ``streamstudio`` serves: the folder it is typed in."""
    return Folder(str(Path.cwd().resolve()))


def _shown(path: Path, folder: Path) -> str:
    """How a message names a graph's file: by its way from the workspace folder — ``flow.yaml``, ``sub/x.yaml``,
    ``../other/flow.yaml`` for a file of another folder (measured before: two kept graphs named ``flow.yaml, flow.yaml``
    could not be told apart). The absolute path when there is no way (another drive)."""
    try:
        return os.path.relpath(path, folder)
    except ValueError:  # pragma: no cover - Windows: another drive has no relative path
        return str(path)


def _say(line: str) -> None:
    """Print one line of the way out. After SIGHUP the terminal is gone and a print fails (``EIO``) — the work is
    already kept by then, and a line nobody can read is no reason to end with a traceback."""
    try:
        print(line, flush=True)
    except OSError:
        _terminal_gone()


def _terminal_gone() -> None:
    """Point file descriptors 1 and 2 at /dev/null once the terminal is gone, so what the process still writes — a
    line left in a buffer, the interpreter's own flush at exit — succeeds. Measured without it: the editor kept the
    work but exited with code 120, Python's code for an exit whose final flush failed."""
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        for fd in (1, 2):
            os.dup2(devnull, fd)
    finally:
        os.close(devnull)


def _recovery_notice(workspace: Dict[str, Any]) -> Optional[str]:  # GET /api/workspace's answer
    """The one line a start prints when the folder holds unsaved work of the last session: when it was kept and the
    files (named by their way from the folder, :func:`_shown`) — the page asks what to do — or, for a file that cannot
    be used, why."""
    recovery = workspace["recovery"]
    if recovery is None:
        return None
    if recovery["error"] is not None:
        error = recovery["error"]
        return f"Unsaved work from the last session cannot be read — {error}. The page offers to move it aside."
    try:
        kept = datetime.fromisoformat(str(recovery["saved_at"])).astimezone()
        when = f"{kept.day} {kept:%b %H:%M}"
    except ValueError:
        when = str(recovery["saved_at"])
    folder = Path(str(workspace["folder"]))
    names = ", ".join(_shown(Path(str(graph["path"])), folder) for graph in recovery["graphs"])
    return f"Unsaved work from the last session ({when}): {names} — the page asks whether to restore it."


# =======================================================================================
# Who the editor is: the spec and its hooks
# =======================================================================================

#: The Flask app of each folder this process serves, by the folder object's id — what the way out keeps the work of.
_APPS: Dict[int, "Flask"] = {}


def _page_checked(_: Served) -> None:
    """The page is checked at a START, never when an app is made: when its source changed since its last build it is
    built now (a line says so), and without Node or with a failing build the server still starts. The outcome is handed
    to the serving process (a ``--background`` child) through the environment."""
    from streamstudio.app import ensure_page_built, page_check_environment

    ensure_page_built()
    os.environ.update(page_check_environment())


def _keep_unsaved_work(served: Served) -> None:
    """The way out — Ctrl+C, SIGTERM (what ``stop`` and ``restart`` send), SIGHUP (the terminal window closed): keep
    the unsaved work in the folder's recovery file (``Studio.stash``) and say so. Measured before the editor took this
    way out: SIGTERM ended the process at once and every unsaved edit was gone. A kill -9 or a crash still cannot keep
    anything."""
    from streamstudio.app import studio_of

    flask_app = _APPS.pop(id(served), None)
    if flask_app is None:
        return
    studio = studio_of(flask_app)
    try:
        kept = studio.stash()
    except OSError as exc:
        _say(f"Unsaved work could not be kept in {studio.recovery_path}: {exc.strerror or exc}")
        kept = []
    if kept:
        names = ", ".join(_shown(Path(path), studio.graph_dir) for path in kept)
        _say(f"Unsaved work ({names}) is kept in {studio.recovery_path} — the next start asks what to do.")
    for stream in (sys.stdout, sys.stderr):  # what the server logged may still sit in a buffer
        try:
            stream.flush()
        except OSError:
            _terminal_gone()


SPEC = AppSpec(
    name="streamstudio",
    cli_module="streamstudio.cli",
    default_port=EDITOR_PORT,
    default_host=STUDIO_HOST,
    workspace="folder",
    hooks=Hooks(prepare=_page_checked, leaving=_keep_unsaved_work),
)


def create_app(server: Server) -> "Flask":
    """The standalone editor's Flask app for ``server``, portail's shell mounted in it (``/api/meta`` names the app;
    ``/api/workspace`` stays the editor's). Prints the line about unsaved work of the last session."""
    # Imported here, not at module top: the app pulls in the document model and the palette, which `stop` and `list`
    # have no use for.
    from portail.web import mount_shell

    from streamstudio.app import create_app as editor_app
    from streamstudio.app import studio_of

    served = server.served
    templates = serving.options_of(served).get("templates_dir")
    flask_app = editor_app(Path(served.path), templates_dir=Path(templates) if templates else None)
    _APPS[id(served)] = flask_app
    notice = _recovery_notice(studio_of(flask_app).workspace())
    if notice is not None:
        print(notice, flush=True)
    if _NAMED_GRAPH:
        print(_graph_address(served.host, served.port, _NAMED_GRAPH.pop()), flush=True)
    return mount_shell(flask_app, server, index=False)


def server_of(folder: Served) -> Server:
    """The portail server of a folder — ``server_of(open_folder(path)).serve()`` is what the command line does."""
    return Server(SPEC, folder, create_app)


# =======================================================================================
# What stop and restart say about unsaved work
# =======================================================================================


class _Unsaved(NamedTuple):
    """What an editor said before it was stopped: the port it served on, the folder it serves (its recovery file is
    there) and the files of the graphs holding unsaved work."""

    port: int
    folder: Path
    paths: List[Path]

    @property
    def file(self) -> Path:
        """The recovery file the editor's stop writes."""
        from streamstudio.app import RECOVERY_FILE  # ~20 ms (measured): importing the package loads what the app needs

        return self.folder / RECOVERY_FILE

    @property
    def names(self) -> List[str]:
        """The files as a message names them (:func:`_shown`)."""
        return [_shown(path, self.folder) for path in self.paths]


def _ask(url: str) -> Any:  # the route's JSON answer, of any shape
    with urllib.request.urlopen(url, timeout=ASK_TIMEOUT) as response:
        return json.loads(response.read())


def _unsaved_work(port: int) -> Optional[_Unsaved]:
    """Ask the editor on ``port`` — ``GET /api/workspace`` (its folder) and ``GET /api/graphs`` (``dirty`` per graph)
    — which graphs hold unsaved work; ``None`` when it does not answer like an editor, so the stop goes on and says
    nothing extra. Best effort: ANY failure to get that answer is "nothing extra"."""
    base = f"http://{STUDIO_HOST}:{port}"
    try:
        workspace, graphs = _ask(f"{base}/api/workspace"), _ask(f"{base}/api/graphs")
    except (OSError, ValueError, http.client.HTTPException):  # refused, timed out, HTTP error; not JSON; not HTTP
        return None
    if not isinstance(workspace, dict) or not isinstance(workspace.get("folder"), str) or not isinstance(graphs, list):
        return None
    paths = [
        Path(str(entry["path"]))
        for entry in graphs
        if isinstance(entry, dict) and entry.get("dirty") is True and "path" in entry
    ]
    return _Unsaved(port, Path(workspace["folder"]), paths)


def _kept_in(file: Path) -> Set[Path]:
    """The files of the graphs a recovery file holds, resolved — empty when it is not there or cannot be read."""
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
        return {Path(str(entry["path"])).resolve() for entry in data["graphs"]}
    except (OSError, ValueError, KeyError, TypeError):  # absent, unreadable, not JSON, not the editor's shape
        return set()


def _asked_before_the_stop() -> List[_Unsaved]:
    """What every running editor holds unsaved, asked BEFORE anything is stopped (a stopped one cannot answer)."""
    asked = [_unsaved_work(int(server["port"])) for server in serving.running_servers(SPEC)]
    return [unsaved for unsaved in asked if unsaved is not None and unsaved.paths]


def _report_kept(asked: List[_Unsaved]) -> None:
    """For each editor that held unsaved work and is gone now: where that work is — checked in the recovery file its
    stop wrote, never assumed."""
    still = {int(server["port"]) for server in serving.running_servers(SPEC)}
    for unsaved in asked:
        if unsaved.port in still:
            continue
        shown = ", ".join(unsaved.names)
        if {path.resolve() for path in unsaved.paths} <= _kept_in(unsaved.file):
            print(f"Its unsaved work ({shown}) is kept in {unsaved.file} — the next start asks what to do.")
        else:
            print(f"Its unsaved work ({shown}) was not kept — the editor did not write it to {unsaved.file}.")


def _server_of(folder: str) -> Optional[Dict[str, Any]]:  # a row of portail's running servers
    """The running editor that serves ``folder`` (``""``: the current directory), or ``None``."""
    identity = str(Path(folder).expanduser().resolve()) if folder else str(Path.cwd().resolve())
    return next((server for server in serving.running_servers(SPEC) if server["identity"] == identity), None)


def _notice_of_the_server_of(folder: str) -> None:
    """After a ``--background`` start: the line about unsaved work of the last session, asked of the server that now
    serves ``folder`` (its own output went nowhere)."""
    server = _server_of(folder)
    if server is None:
        return
    try:
        notice = _recovery_notice(_ask(f"http://{server['host']}:{server['port']}/api/workspace"))
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        return
    if notice is not None:
        print(notice)


#: The graph a start named by its file (``streamstudio flow.yaml``), until its address is printed: by ``create_app``
#: for a server this process runs (the port is settled by then), else by ``open`` once portail returns.
_NAMED_GRAPH: List[str] = []


def _graph_page(graph: str) -> str:
    """The part of the editor's address that opens on ``graph``."""
    return f"?graph={urllib.parse.quote(graph, safe='')}"


def _graph_address(host: str, port: int, graph: str) -> str:
    """The line naming the page that opens on ``graph`` — the address alone on it, so a terminal links all of it."""
    return f"  http://{host}:{port}/{_graph_page(graph)}"


# =======================================================================================
# Commands
# =======================================================================================

lifecycle = add_lifecycle_commands(
    app, SPEC, open_file=open_folder, open_empty=open_here, create_app=create_app, commands=("list",)
)


def _options(templates_dir: str) -> Dict[str, str]:
    return {"templates_dir": templates_dir} if templates_dir else {}


@app.command(name="open", default=True, positionals=["graph_dir"], short={"p": "port", "b": "background"})
def open_(
    graph_dir: str = "",
    port: int = 0,
    templates_dir: str = "",
    auto_port: bool = False,
    background: bool = False,
    browser: str = "",
) -> None:
    """Serve the editor over a folder of graphs (the default command).

    A folder already being served is not started twice: its address is printed. A port another folder is served on
    asks whether to take the next free one; a port a program that is not the editor holds is refused.

    Args:
        graph_dir: The folder whose *.yaml files are the graphs (default: the current directory), or one of its
            graphs' files: the file's folder is served and the address that opens on that graph is printed. More
            can be opened from anywhere with File → Open.
        port: Port the editor listens on (default: 8190).
        templates_dir: The folder holding the templates (default: ~/.streamstudio/templates, created on the first
            template saved).
        auto_port: Take the next free port without asking when the port is taken.
        background: Serve detached and return once the editor answers (``streamstudio stop`` stops it).
        browser: Open the editor in the browser: ``always``, ``new`` (only for a server started now) or ``never``.
            Left out, a graph's file opens its tab (``always``, on that graph) and a folder opens none (``never``).
    """
    named = Path(graph_dir).expanduser() if graph_dir else None
    del _NAMED_GRAPH[:]
    if named is not None and named.suffix == ".yaml" and named.is_file():  # a graph's file: its folder, that graph
        _NAMED_GRAPH.append(named.stem)
        named = named.parent
    folder = str(named) if named is not None else ""
    lifecycle.open(
        folder,
        auto_port=auto_port,
        port=port,
        background=background,
        browser=browser or ("always" if _NAMED_GRAPH else "never"),
        options=_options(templates_dir),
    )
    if background:
        _notice_of_the_server_of(folder)
    server = _server_of(folder) if _NAMED_GRAPH else None  # a server this process did not run: already there, or -b
    if server is not None:
        print(_graph_address(server["host"], server["port"], _NAMED_GRAPH.pop()))


@app.command(name="stop", positionals=["target"])
def stop(target: str = "", all: bool = False) -> None:
    """Stop running StreamStudio server(s).

    Names one by its folder or its port; with none named, the running one (several: asked, or --all). A port a
    program that is not the editor holds is refused, never stopped. The editor keeps its unsaved work in its folder's
    hidden .streamstudio-workspace.json as it stops, and the message names the files kept there.

    Args:
        target: The folder or the port of the server to stop (default: the running one).
        all: Stop every running StreamStudio server.
    """
    asked = _asked_before_the_stop()
    lifecycle.stop(target, all=all)
    _report_kept(asked)


@app.command(name="restart", positionals=["target"], short={"p": "port", "b": "background"})
def restart(
    target: str = "",
    port: int = 0,
    templates_dir: str = "",
    background: bool = False,
    browser: BrowserChoice = "never",
) -> None:
    """Stop the server (if any), then serve the editor over the same folder again.

    A folder that is not there is refused before anything is stopped. The editor keeps its unsaved work as it stops,
    and the new one offers it on its page.

    Args:
        target: The folder or the port of the server to restart (default: the folder served last).
        port: Port to serve on again (default: the one it ran on).
        templates_dir: The templates folder (default: the one the running server has).
        background: Serve detached and return once the editor answers.
        browser: Open the editor's address in the browser: ``always``, ``new`` or ``never`` (the default).
    """
    named = str(Path(target).expanduser()) if target and not target.isdigit() else target
    lifecycle.restart(named, port=port, background=background, browser=browser, options=_options(templates_dir))
    if background:
        _notice_of_the_server_of(named if named and not named.isdigit() else serving.last_spec(SPEC)[len("file:") :])


def main() -> None:
    """Console-script entry point (``[project.scripts] streamstudio``)."""
    app.run()


if __name__ == "__main__":
    main()
