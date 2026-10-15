"""The ``streamstudio`` command: serve the graph editor over a folder of YAML graphs.

* ``run`` (the default — bare ``streamstudio``): the editor (``docs/rewrite.md``) over the confluid documents of a
  folder — ``streamstudio [graph_dir] [--port 8190] [--templates-dir DIR]``, the Flask app of :mod:`streamstudio.app`,
  standalone (a consuming Flask app mounts the same blueprint instead). A taken port is refused before any work.
* ``stop`` / ``restart``: the server on the port. The editor stops by the same way out for SIGTERM (what they send) as
  for Ctrl+C, keeping its unsaved work in the folder's hidden ``.streamstudio-workspace.json``; ``stop`` names what was
  kept, and the next start offers it.
"""

import http.client
import json
import os
import shlex
import signal
import socket
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import Any, Dict, List, NamedTuple, Optional, Set, Tuple

import psutil
from liquifai import LiquifyApp
from loggair import get_logger

logger = get_logger(__name__)

STUDIO_HOST = "127.0.0.1"
#: The editor's default port.
EDITOR_PORT = 8190
#: How long ``stop`` / ``restart`` wait for the editor to say which graphs hold unsaved work before stopping it anyway
#: (seconds). It answers in milliseconds; something on the port that is not the editor may not answer.
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


def _port_in_use(port: int, host: str = STUDIO_HOST) -> bool:
    """True when something already listens on ``host:port``.

    Probed by BINDING rather than connecting: a connect test cannot tell "nothing is listening"
    apart from "listening but not accepting yet", and a refused connection is the same errno as a
    host that is simply slow. ``SO_REUSEADDR`` IS set on purpose — on this platform family it lets a
    bind succeed beside a socket left in ``TIME_WAIT`` (a studio killed seconds ago, whose port is
    genuinely free) while still failing against a live LISTENING socket, which is the only state we
    mean to report. Pins: ``tests/test_cli_port.py`` (both directions, against a real socket).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return True
    return False


def _studio_process(port: int, host: str = STUDIO_HOST) -> Optional[psutil.Process]:
    """The process LISTENING on ``host:port``, or ``None``.

    Located per-process on purpose: the whole-system ``psutil.net_connections()`` raises
    ``AccessDenied`` for an unprivileged caller on macOS, while asking each process for its own
    connections needs no privileges for processes you own. The port OWNER is the target, whatever started it — a
    terminal, a host app, a script.
    """
    for proc in psutil.process_iter(["pid"]):
        try:
            for conn in proc.net_connections(kind="tcp"):
                if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port:
                    return proc
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue  # someone else's process, or one that died mid-scan
    return None


def _stop_studio(port: int, grace: float = 10.0) -> Optional[int]:
    """Stop the studio holding ``port``; return the pid stopped, or ``None`` if nothing was there.

    ``terminate()`` then a DETERMINISTIC ``wait()`` — never a sleep-and-recheck. A clean studio frees
    the port in well under a second (measured 64 ms); ``grace`` seconds later a process that ignored
    the signal is killed outright, which is the only way to hand the port to the relaunch.
    """
    proc = _studio_process(port)
    if proc is None:
        return None
    pid = int(proc.pid)  # psutil types .pid loosely; the caller and every message want a plain int
    try:
        proc.terminate()
        try:
            proc.wait(timeout=grace)
        except psutil.TimeoutExpired:
            logger.warning(f"StreamStudio (pid {pid}) ignored SIGTERM after {grace}s — killing it.")
            proc.kill()
            proc.wait(timeout=grace)
    except psutil.NoSuchProcess:  # exited between the scan and the signal
        pass
    return pid


def _shown(path: Path, folder: Path) -> str:
    """How a message names a graph's file: by its way from the workspace folder — ``flow.yaml``, ``sub/x.yaml``,
    ``../other/flow.yaml`` for a file of another folder (measured before: two kept graphs named ``flow.yaml, flow.yaml``
    could not be told apart). The absolute path when there is no way (another drive)."""
    try:
        return os.path.relpath(path, folder)
    except ValueError:  # pragma: no cover - Windows: another drive has no relative path
        return str(path)


class _Unsaved(NamedTuple):
    """What a canvas editor said before it was stopped: the folder it serves (its recovery file is there, and a
    ``restart`` without a folder serves it again) and the files of the graphs holding unsaved work."""

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


def _get_json(url: str) -> Any:  # the route's JSON answer, of any shape
    with urllib.request.urlopen(url, timeout=ASK_TIMEOUT) as response:
        return json.loads(response.read())


def _unsaved_work(port: int) -> Optional[_Unsaved]:
    """Ask the canvas editor on ``port`` — ``GET /api/workspace`` (its folder) and ``GET /api/graphs`` (``dirty``
    per graph) — which graphs hold unsaved work; ``None`` when nothing there answers like a canvas editor (nothing
    listening, another program), so the stop goes on and says nothing extra. Best effort: ANY
    failure to get that answer is "nothing extra" — measured before, a listener that answered with an SSH banner
    (``http.client.BadStatusLine``) crashed ``stop`` with a traceback and left the listener running."""
    base = f"http://{STUDIO_HOST}:{port}"
    try:
        workspace, graphs = _get_json(f"{base}/api/workspace"), _get_json(f"{base}/api/graphs")
    except (OSError, ValueError, http.client.HTTPException):  # refused, timed out, HTTP error; not JSON; not HTTP
        return None
    if not isinstance(workspace, dict) or not isinstance(workspace.get("folder"), str) or not isinstance(graphs, list):
        return None
    paths = [
        Path(str(entry["path"]))
        for entry in graphs
        if isinstance(entry, dict) and entry.get("dirty") is True and "path" in entry
    ]
    return _Unsaved(Path(workspace["folder"]), paths)


def _kept_in(file: Path) -> Set[Path]:
    """The files of the graphs a recovery file holds, resolved — empty when it is not there or cannot be read."""
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
        return {Path(str(entry["path"])).resolve() for entry in data["graphs"]}
    except (OSError, ValueError, KeyError, TypeError):  # absent, unreadable, not JSON, not the editor's shape
        return set()


def _report_stopped(port: int, pid: Optional[int], unsaved: Optional[_Unsaved] = None) -> None:
    """Say what was stopped, in the same plain voice as the port-in-use message — and, for a canvas editor that
    held unsaved work, where that work is now: checked in the recovery file its stop wrote, never assumed."""
    if pid is None:
        print(f"Nothing is listening on port {port} — no StreamStudio to stop.")
        return
    line = f"Stopped StreamStudio on port {port} (pid {pid})."
    if unsaved is not None and unsaved.paths:
        shown = ", ".join(unsaved.names)
        if {path.resolve() for path in unsaved.paths} <= _kept_in(unsaved.file):
            line += f" Its unsaved work ({shown}) is kept in {unsaved.file} — the next start asks what to do."
        else:
            line += f" Its unsaved work ({shown}) was not kept — the editor did not write it to {unsaved.file}."
    print(line)


def _stop_and_report(port: int) -> Optional[_Unsaved]:
    """``stop`` and the first half of ``restart``: ask the editor what it holds unsaved, stop the server on ``port``,
    say what happened; answer what the editor said (its folder is what a ``restart`` without one serves again)."""
    unsaved = _unsaved_work(port)
    _report_stopped(port, _stop_studio(port), unsaved)
    return unsaved


def _report_port_in_use(port: int, graph_dir: Path, templates_dir: Optional[Path] = None) -> None:
    """Say what holds the port and hand over a clickable way in, then stop the start.

    Printed PLAIN (not through loggair) and on its own line: a terminal linkifies a bare ``http://`` URL, and a log
    prefix in front of it buries the one thing the reader came for.

    The suggested command serves the SAME folder and templates folder elsewhere, in the order the CLI accepts
    (absolute, shell-quoted, the folder first — liquifai binds positionals up to the first flag). Measured before: the
    suggestion was ``streamstudio --port 8195``, which served the CURRENT directory with the default templates folder.
    """
    print(f"Port {port} is already in use — StreamStudio may already be running there.")
    print()
    print(f"    Open it:  http://{STUDIO_HOST}:{port}")
    print()
    words = ["streamstudio", str(graph_dir)]
    words += ["--templates-dir", str(templates_dir)] if templates_dir is not None else []
    words += ["--port", str(port + 1)]
    print(f"If that is a different program, run StreamStudio elsewhere:  {shlex.join(words)}")


def _canvas_folders(graph_dir: Optional[Path], templates_dir: Optional[Path]) -> Tuple[Path, Optional[Path]]:
    """The canvas editor's graph folder (default: the current directory) and templates folder, ABSOLUTE — a
    folder that is not there is refused naming it.

    Resolved ONCE, here: every graph path the editor answers is then absolute, so a path from ``GET
    /api/graphs`` can be sent back, and every dialog opens in the graph folder (measured before, from
    ``streamstudio graphs``: the list said ``graphs/chain.yaml`` and the file browser looked in
    ``graphs/graphs``; from ``streamstudio .`` both dialogs opened on ``/``). liquifai binds CLI values
    VERBATIM as strings, so they are coerced here too."""
    directory = (Path(graph_dir) if graph_dir is not None else Path.cwd()).expanduser().resolve()
    templates = Path(templates_dir).expanduser().resolve() if templates_dir is not None else None
    if not directory.is_dir():
        raise SystemExit(f"Graph directory not found: {directory}")
    return directory, templates


#: The signals that end the canvas editor the way Ctrl+C (SIGINT) does, so its unsaved work is kept: SIGTERM (what
#: ``stop`` and ``restart`` send) and SIGHUP (the terminal window it runs in was closed — POSIX only, hence the
#: lookup). Measured before 2026-09-30: SIGHUP ended the editor at once (exit code -1) and the unsaved edit was gone.
_WAYS_OUT: Tuple[signal.Signals, ...] = tuple(
    getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)
)


def _interrupt(signum: int, frame: Optional[FrameType]) -> None:
    """SIGTERM or SIGHUP handled like Ctrl+C: the server stops serving and the editor takes the same way out."""
    raise KeyboardInterrupt


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


def _serve_canvas(graph_dir: Optional[Path], port: int, templates_dir: Optional[Path]) -> None:
    """Serve the canvas editor over ``graph_dir`` (default: the current directory) on ``port``.

    The way OUT is the point of the shape (the user's item 1, 2026-09-29): Ctrl+C (SIGINT), SIGTERM — what ``stop``
    and ``restart`` send — and SIGHUP — the terminal window closed (2026-09-30) — all end ``app.run`` the same way,
    and the ``finally`` keeps the unsaved work in the folder's recovery file (``Studio.stash``) before the process
    exits. Measured before: SIGTERM ended the process at once (exit -15), SIGHUP too (exit -1), and every unsaved edit
    was gone. A kill -9 or a crash still cannot keep anything.

    Before it serves — after a taken port was refused — the start builds the canvas page when its source changed
    since its last build (``streamstudio.page.ensure_page_built``; silent when it did not), then asks for the port
    again: another start may have taken it while the page was built.
    """
    directory, templates = _canvas_folders(graph_dir, templates_dir)
    if _port_in_use(port):
        _report_port_in_use(port, directory, templates)
        raise SystemExit(1)
    # Imported here, not at module top: the app pulls in the document model and the palette, which `stop` has no
    # use for.
    from streamstudio.app import create_app, ensure_page_built, studio_of

    # The page is checked HERE, at the start, never when an app is made: when its source changed since its last
    # build it is built now (a line says so), and without Node or with a failing build the server still starts.
    ensure_page_built()
    # Asked again after the check: a build takes seconds (about a minute for the first install), and another start may
    # have taken the port meanwhile — measured before: the second of two starts on one port passed the first probe
    # during a 5 s build and died in werkzeug's words, "Address already in use".
    if _port_in_use(port):
        _report_port_in_use(port, directory, templates)
        raise SystemExit(1)
    app = create_app(directory, templates_dir=templates)
    studio = studio_of(app)
    notice = _recovery_notice(studio.workspace())
    if notice is not None:
        print(notice, flush=True)
    previous = {signum: signal.signal(signum, _interrupt) for signum in _WAYS_OUT}
    try:
        app.run(host=STUDIO_HOST, port=port)
    except KeyboardInterrupt:
        pass  # a signal outside werkzeug's serve loop (which returns quietly on one): the same way out
    finally:
        try:
            kept = studio.stash()
        except OSError as exc:
            _say(f"Unsaved work could not be kept in {studio.recovery_path}: {exc.strerror or exc}")
            kept = []
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    if kept:
        names = ", ".join(_shown(Path(path), studio.graph_dir) for path in kept)
        _say(f"Unsaved work ({names}) is kept in {studio.recovery_path} — the next start asks what to do.")
    for stream in (sys.stdout, sys.stderr):  # what the server logged may still sit in a buffer
        try:
            stream.flush()
        except OSError:
            _terminal_gone()


# =======================================================================================
# Commands
# =======================================================================================


@app.command(default=True, positionals=["graph_dir"], short={"p": "port"})
def run(graph_dir: Optional[Path] = None, port: Optional[int] = None, templates_dir: Optional[Path] = None) -> None:
    """Serve the editor over a folder of graphs (the default command).

    Args:
        graph_dir: The folder whose *.yaml files are the graphs (default: the current directory). More can be opened
            from anywhere with File → Open.
        port: Port the editor listens on (default: 8190). A port already in use is refused before any work, with the
            address of what is there printed as a clickable URL.
        templates_dir: The folder holding the templates (default: ~/.streamstudio/templates, created on the first
            template saved).
    """
    _serve_canvas(graph_dir, int(port) if port is not None else EDITOR_PORT, templates_dir)


@app.command(short={"p": "port"})
def stop(port: Optional[int] = None) -> None:
    """Stop the StreamStudio server listening on the port.

    An idle port is not an error — it reports that nothing was there. The editor keeps its unsaved work in its
    folder's hidden .streamstudio-workspace.json as it stops, and the message names the files kept there.

    Args:
        port: Port whose server to stop (default: 8190).
    """
    _stop_and_report(int(port) if port is not None else EDITOR_PORT)


@app.command(positionals=["graph_dir"], short={"p": "port"})
def restart(graph_dir: Optional[Path] = None, port: Optional[int] = None, templates_dir: Optional[Path] = None) -> None:
    """Stop the server on the port (if any), then serve the editor again.

    A graph folder that is not there is refused before anything is stopped. The editor keeps its unsaved work as it
    stops, and the new one offers it on its page.

    Args:
        graph_dir: The folder of graphs (default: the folder the running editor serves, else the current directory).
        port: Port whose server to restart (default: 8190).
        templates_dir: The templates folder (default: ~/.streamstudio/templates).
    """
    _canvas_folders(graph_dir, templates_dir)  # a folder that is not there is refused BEFORE the editor is stopped
    target = int(port) if port is not None else EDITOR_PORT
    stopped = _stop_and_report(target)
    if graph_dir is None and stopped is not None:
        # the SAME workspace (R6): the folder the stopped editor served, so its start offers the work it kept there —
        # measured before, a restart typed in another folder served that one, and the kept work waited unseen
        graph_dir = stopped.folder
    _serve_canvas(graph_dir, target, templates_dir)


def main() -> None:
    """Console-script entry point (``[project.scripts] streamstudio``)."""
    app.run()


if __name__ == "__main__":
    main()
