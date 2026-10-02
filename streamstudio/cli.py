"""CLI for StreamStudio.

Subcommands (the converters are offline — importing :mod:`streamstudio` populates
``NODE_CLASS_MAPPINGS`` via entry-point discovery, so they work WITHOUT a running ComfyUI):

* ``run`` (the default — bare ``streamstudio``): serve an editor, chosen by ``--editor`` (a closed
  ``Literal``, the user's decision of 2026-09-29):

  - ``canvas`` (the default): the graph EDITOR (``docs/rewrite.md``) over the confluid documents of a
    folder — ``streamstudio [graph_dir] [--port 8190] [--templates-dir DIR]``, the Flask app of
    :mod:`streamstudio.app`, standalone (a consuming Flask app mounts the same blueprint instead);
  - ``comfy``: the ComfyUI editor with the Taidal nodes auto-configured, port 8188. It also seeds
    ComfyUI custom nodes from three tiers — the ``streamstudio.custom_nodes`` entry-point group
    (nodes DECLARED by installed packages, including streamstudio's own bundled ComfyUI-Manager;
    ``--manager-`` filters the manager, ``--declared-nodes-`` skips the tier), the
    ``STREAMSTUDIO_CUSTOM_NODES`` env var (comma-separated git URLs), and ``--custom-node`` —
    idempotently: a repo already present under ``custom_nodes/`` is left alone.

  The port default FOLLOWS the editor, and an option of the other editor is refused naming it
  (``--comfy-dir only applies to --editor comfy``). Both refuse a taken port before any work.
* ``stop`` / ``restart``: the server on the editor's port (``--editor`` as above). The canvas editor stops by the
  same way out for SIGTERM (what they send) as for Ctrl+C, keeping its unsaved work in the folder's hidden
  ``.streamstudio-workspace.json``; ``stop`` names what was kept, and the next start offers it.
* ``export``: compile a saved ComfyUI **API-format** graph into Confluid ``flow:`` document(s)
  (``streamstudio export graph.json -o flow.yaml``) — the offline twin of the canvas Export-Flow
  button. Shares :func:`streamstudio.export.export_flow_yamls`; a graph with several disconnected
  op components writes one file per component (``flow_1.yaml`` …) / a ``---``-separated stream
  on stdout.
* ``import``: convert a Confluid YAML config into a ComfyUI ``workflow.json``
  (``streamstudio import config.yaml -o workflow.json [--as graph|group|subgraph]``) — the offline
  twin of the canvas "Import Taidal YAML" menu command. Uses :func:`streamstudio.graphio.yaml_to_workflow`,
  which AUTO-DETECTS a flow-export document (a ``{…constants…, flow: {...}, outputs: …}`` file from
  ``export`` / the canvas Export-Flow button) and rebuilds the connected op graph, colouring
  dead-end taps. A BRANCHY flow document does not import back yet (tracked in TASKS.md).
* ``export-workflow``: convert a WHOLE ``workflow.json`` (UI or API form) into a hoisted-``!ref:``
  YAML (``streamstudio export-workflow workflow.json -o config.yaml``) — the twin of the canvas
  "Export Graph → Taidal YAML". Uses :func:`streamstudio.graphio.workflow_to_yaml`.
"""

import http.client
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import urllib.request
from datetime import datetime
from importlib.metadata import entry_points
from pathlib import Path
from types import FrameType
from typing import Any, Dict, List, Literal, NamedTuple, Optional, Sequence, Set, Tuple, Union, cast, get_args

import psutil
from liquifai import LiquifyApp
from loggair import get_logger

from streamstudio.custom_nodes import COMFYUI_MANAGER_URL  # bundled default; --no-manager filters it

logger = get_logger(__name__)

# Entry-point group through which ANY installed package declares "generally available" custom
# nodes (installed on every launch, no flags needed). Each entry point resolves to a git-URL str
# or an iterable of them; streamstudio's own declaration is streamstudio/custom_nodes.py.
CUSTOM_NODES_ENTRY_POINT_GROUP = "streamstudio.custom_nodes"

# Env twin of the repeatable --custom-node flag: comma-separated git URLs of EXTRA custom nodes
# to install on launch. Install order is declared (entry points) -> env -> flags, deduped.
CUSTOM_NODES_ENV_VAR = "STREAMSTUDIO_CUSTOM_NODES"

# ComfyUI's own listen defaults (comfy/cli_args.py: ``--listen 127.0.0.1``, ``--port 8188``, both
# plain argparse defaults with no env/config fallback). We mirror them so the launcher can (a) probe
# the address BEFORE doing any launch work and (b) print the URL of whatever is already there.
DEFAULT_PORT = 8188
STUDIO_HOST = "127.0.0.1"
#: The graph editor's own default port: beside ComfyUI's 8188 (which ``--editor comfy`` serves) and past the
#: 8189 the busy-port message offers for a second ComfyUI, so the two servers coexist on one machine.
EDITOR_PORT = 8190

#: Which editor ``run`` / ``stop`` / ``restart`` mean: the new canvas editor, or the ComfyUI one.
EditorName = Literal["canvas", "comfy"]
EDITORS: Tuple[str, ...] = get_args(EditorName)
#: Each editor's default port — ``--port`` left out means this one.
EDITOR_PORTS: Dict[EditorName, int] = {"canvas": EDITOR_PORT, "comfy": DEFAULT_PORT}
#: How long ``stop`` / ``restart`` wait for the canvas editor to say which graphs hold unsaved work before stopping
#: it anyway (seconds). It answers in milliseconds; something on the port that is not a canvas editor may not answer.
ASK_TIMEOUT = 5.0


def default_comfy_dir() -> Path:
    """Where ComfyUI is cloned to when ``--comfy-dir`` is not given.

    A FUNCTION, not a module constant: a signature default is evaluated at IMPORT, which froze the
    path against ``Path.home()`` before anything could change it (argparse built its parser inside
    ``main()``, so it never had this problem). The commands therefore default the parameter to
    ``None`` and resolve here.
    """
    return Path.home() / ".streamstudio" / "ComfyUI"


#: ``strict_flags``: every command here takes PLAIN values — there is no config document for an
#: override to fall through to, so a flag that reaches nothing reached nothing at all. Without it a
#: mistyped ``--custom-nod`` is indistinguishable from one that worked (liquifai's report-based
#: warning only speaks when something was materialized, and a launcher materializes nothing).
app = LiquifyApp(
    name="streamstudio",
    description=(
        "StreamStudio — the canvas graph editor (default, port 8190) over a folder of YAML graphs; "
        "--editor comfy launches the ComfyUI editor with the Taidal nodes (port 8188)."
    ),
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


def studio_log_path(port: int) -> Path:
    """Where a ``--background`` studio writes its output — ``~/.streamstudio/logs/studio-<port>.log``.

    PER-PORT so two studios never interleave into one file, and under the studio's OWN home rather
    than the launch directory: this package has already been burned once by writing a ``logs/``
    directory into whatever folder the user happened to be standing in (see AGENTS, "The Package
    Speaks Its OWN Name"), and a detached server's log is not a per-run artifact belonging to a
    project checkout.
    """
    return Path.home() / ".streamstudio" / "logs" / f"studio-{port}.log"


def _studio_process(port: int, host: str = STUDIO_HOST) -> Optional[psutil.Process]:
    """The process LISTENING on ``host:port``, or ``None``.

    Located per-process on purpose: the whole-system ``psutil.net_connections()`` raises
    ``AccessDenied`` for an unprivileged caller on macOS, while asking each process for its own
    connections needs no privileges for processes you own. The port OWNER is the target — a
    foreground ``streamstudio run`` is the launcher, and the server is the ``main.py`` it spawned
    (measured: ``74841 streamstudio run`` -> ``74870 python main.py --port 8189``, and the launcher
    exits by itself once that child returns).
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
    listening, a ComfyUI editor, another program), so the stop goes on and says nothing extra. Best effort: ANY
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


def _stop_and_report(port: int, editor: EditorName) -> Optional[_Unsaved]:
    """``stop`` and the first half of ``restart``: ask a canvas editor what it holds unsaved, stop the server on
    ``port``, say what happened; answer what the canvas editor said (its folder is what a ``restart`` without one
    serves again). A ComfyUI editor is not asked — it keeps no unsaved work of this kind."""
    unsaved = _unsaved_work(port) if editor == "canvas" else None
    _report_stopped(port, _stop_studio(port), unsaved)
    return unsaved


def _report_port_in_use(
    port: int, editor: EditorName = "comfy", graph_dir: Optional[Path] = None, templates_dir: Optional[Path] = None
) -> None:
    """Say what holds the port and hand over a clickable way in, then stop the launch.

    Printed PLAIN (not through loggair) and on its own line: a terminal linkifies a bare ``http://``
    URL, and a log prefix in front of it buries the one thing the reader came for. Without this
    check ComfyUI discovers the clash only at the end of its own boot — measured 2026-08-26, 23
    seconds and 91 lines to reach one ERROR line on line 85, followed by exit code 0.

    The suggested command starts the SAME editor elsewhere, in the order the CLI accepts: the canvas
    editor on the same folder and templates folder (absolute, shell-quoted, the folder first — liquifai
    binds positionals up to the first flag), ``streamstudio --editor comfy --port`` for ComfyUI — without
    the flag it would start the other editor on the offered port. Measured before: the canvas suggestion
    was ``streamstudio --port 8195``, which served the CURRENT directory with the default templates folder.
    """
    print(f"Port {port} is already in use — StreamStudio may already be running there.")
    print()
    print(f"    Open it:  http://{STUDIO_HOST}:{port}")
    print()
    words = ["streamstudio"]
    if editor == "canvas":
        words += [str(graph_dir)] if graph_dir is not None else []
        words += ["--templates-dir", str(templates_dir)] if templates_dir is not None else []
    else:
        words += ["--editor", editor]
    words += ["--port", str(port + 1)]
    print(f"If that is a different program, run StreamStudio elsewhere:  {shlex.join(words)}")


def _as_list(value: Union[str, Sequence[str], None]) -> List[str]:
    """A repeatable option as a LIST, whatever spelling it arrived in.

    ``--custom-node URL`` binds a single string and ``--custom-node=[a,b]`` a list, so every
    consumer would otherwise have to guess — and splatting the string form iterates it CHARACTER BY
    CHARACTER, which fails as a wrong-looking clone URL rather than as a type error.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _custom_node_dir_name(url: str) -> str:
    """Directory name ``git clone <url>`` produces — the URL basename, ``.git`` stripped."""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    return name[: -len(".git")] if name.endswith(".git") else name


def _env_custom_nodes() -> list[str]:
    """Extra custom-node git URLs from ``STREAMSTUDIO_CUSTOM_NODES`` (comma-separated, blanks dropped)."""
    raw = os.environ.get(CUSTOM_NODES_ENV_VAR, "")
    return [url for url in (part.strip() for part in raw.split(",")) if url]


def _declared_custom_nodes() -> list[str]:
    """Custom-node git URLs declared by installed packages via ``streamstudio.custom_nodes``.

    Each entry point resolves to a ``str`` URL or an iterable of them. A broken declaration is
    skipped with an actionable warning (it is a packaging bug in the declaring package) — like
    every other custom-node failure, it must never block the launch.
    """
    urls: list[str] = []
    for ep in entry_points(group=CUSTOM_NODES_ENTRY_POINT_GROUP):
        try:
            value = ep.load()
        except Exception:  # arbitrary third-party metadata — anything can go wrong on load
            logger.warning(f"Failed to load custom-node entry point '{ep.name}' ({ep.value}); skipping it.")
            continue
        if isinstance(value, str):
            urls.append(value)
            continue
        try:
            items = list(value)
        except TypeError:
            logger.warning(
                f"Custom-node entry point '{ep.name}' must resolve to a str or an iterable of str, "
                f"got {type(value).__name__}; skipping it."
            )
            continue
        for item in items:
            if isinstance(item, str):
                urls.append(item)
            else:
                logger.warning(
                    f"Custom-node entry point '{ep.name}' contains a non-str item "
                    f"({type(item).__name__}); skipping that item."
                )
    return urls


def _install_custom_nodes(comfy_dir: Path, urls: Sequence[str]) -> None:
    """Clone each custom-node repo into ``custom_nodes/`` and install its ``requirements.txt``.

    Idempotent and launch-safe: a repo whose target directory already exists is skipped (update /
    remove it via ComfyUI-Manager), and a clone or pip failure of one node logs a warning and moves
    on — a broken or unreachable custom node must never block the studio launch.
    """
    custom_nodes_dir = comfy_dir / "custom_nodes"
    custom_nodes_dir.mkdir(parents=True, exist_ok=True)
    for url in urls:
        target = custom_nodes_dir / _custom_node_dir_name(url)
        if target.exists():
            logger.debug(f"Custom node '{target.name}' already installed at {target}; skipping.")
            continue
        logger.info(f"Installing custom node '{target.name}' from {url}...")
        try:
            subprocess.run(["git", "clone", url, str(target)], check=True)
        except subprocess.CalledProcessError:
            logger.warning(f"Failed to clone custom node {url} — skipping it; launch continues.")
            continue
        requirements = target / "requirements.txt"
        if requirements.exists():
            logger.info(f"Installing '{target.name}' dependencies into current environment...")
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", str(requirements)],
                    check=True,
                )
            except subprocess.CalledProcessError:
                logger.warning(
                    f"Failed to install requirements for custom node '{target.name}' — it may not "
                    "work until its dependencies are installed manually."
                )


def _run(
    comfy_dir: Optional[Path] = None,
    port: int = DEFAULT_PORT,
    background: bool = False,
    default_nodes: bool = True,
    only_taidal_nodes: bool = False,
    palette_config: Optional[Path] = None,
    install: bool = True,
    manager: bool = True,
    declared_nodes: bool = True,
    custom_node: Optional[List[str]] = None,
) -> None:
    """Clone ComfyUI if needed, install custom nodes, symlink the Taidal nodes in, and launch it.

    The shared launch body behind both the ``run`` and ``restart`` commands — plain parameters, so
    the two command functions declare the SAME surface and neither can drift from it.
    """
    # liquifai binds CLI values VERBATIM as strings (its documented contract: "a command that needs
    # another type coerces in its body"), so a `--comfy-dir /x` arrives as `str` while the default
    # is already a Path. Coerce once, here, rather than at every use.
    comfy_dir = Path(comfy_dir) if comfy_dir is not None else default_comfy_dir()
    port = int(port)
    palette_config = Path(palette_config) if palette_config is not None else None

    # 0. Refuse a taken port BEFORE any launch work. Everything below (clone, requirements sync,
    # custom nodes, symlink) is wasted when ComfyUI cannot bind, and ComfyUI itself only finds out
    # at the very end of its boot — see _report_port_in_use.
    if _port_in_use(port):
        _report_port_in_use(port, "comfy")
        raise SystemExit(1)

    # 1. Clone ComfyUI if it doesn't exist
    if not comfy_dir.exists():
        logger.info(f"ComfyUI not found at {comfy_dir}. Cloning...")
        comfy_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "git",
                "clone",
                "https://github.com/comfyanonymous/ComfyUI.git",
                str(comfy_dir),
            ],
            check=True,
        )
    else:
        logger.info(f"Using existing ComfyUI installation at {comfy_dir}")
    # 1b. Sync ComfyUI's requirements on EVERY run, not only after a clone: an existing checkout
    # drifts (ComfyUI-Manager updates it in place; a newer main.py then imports a module the venv
    # never received — `comfy_aimdo`, measured 2026-08-22 — and dies at startup). pip is a no-op
    # when everything is already satisfied, so this costs seconds, not a reinstall.
    if install:
        logger.info(f"Syncing ComfyUI requirements into {sys.executable}...")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(comfy_dir / "requirements.txt")],
                check=True,
            )
        except KeyboardInterrupt:  # Ctrl-C while pip resolves: stop as quietly as a Ctrl-C during the launch
            logger.info("StreamStudio stopped.")
            return
        except subprocess.CalledProcessError as exc:
            # A failed sync (offline, a resolver conflict) must not block an install that already
            # works — same continue-on-failure rule as the custom-node tier; only the CLONE is fatal.
            logger.warning(
                f"ComfyUI requirements sync failed ({exc}); launching anyway — "
                f"run `pip install -r {comfy_dir / 'requirements.txt'}` manually if startup fails."
            )

    # 2. Install declared + requested custom nodes (idempotent — an existing checkout is skipped).
    # Tiers: entry-point-declared (incl. streamstudio's own bundled ComfyUI-Manager) -> env -> flags.
    # --no-manager filters the manager URL from the DECLARED tier only — an explicit env/flag
    # request for it is the stronger signal and still installs.
    declared = _declared_custom_nodes() if declared_nodes else []
    if not manager:
        declared = [url for url in declared if url != COMFYUI_MANAGER_URL]
    custom_node_urls = list(dict.fromkeys([*declared, *_env_custom_nodes(), *_as_list(custom_node)]))
    _install_custom_nodes(comfy_dir, custom_node_urls)

    # 3. Symlink StreamStudio nodes into ComfyUI's custom_nodes directory
    source_module_dir = Path(__file__).parent.resolve()
    custom_nodes_dir = comfy_dir / "custom_nodes"
    custom_nodes_dir.mkdir(parents=True, exist_ok=True)
    link_path = custom_nodes_dir / "streamstudio"

    if link_path.exists():
        if link_path.is_symlink():
            if link_path.resolve() != source_module_dir:
                logger.info("Updating existing custom_node symlink...")
                link_path.unlink()
                os.symlink(source_module_dir, link_path)
        else:
            logger.warning(f"A non-symlink file/directory exists at {link_path}. Cannot auto-inject nodes.")
    else:
        logger.info(f"Symlinking Taidal nodes: {source_module_dir} -> {link_path}")
        os.symlink(source_module_dir, link_path)

    # 4. Launch ComfyUI
    # Pass the default-nodes choice to the launched ComfyUI via an env var (it's a subprocess, so an
    # env var is the channel). streamstudio's import inside ComfyUI reads STREAMSTUDIO_HIDE_DEFAULT_NODES
    # to (a) prune ComfyUI's built-in nodes and (b) surface the Taidal nodes at the palette root.
    # Set explicitly ("0" / "1") so the flag wins over any ambient shell export of the var.
    only_taidal = only_taidal_nodes
    hide_defaults = (not default_nodes) or only_taidal  # only-Taidal mode implies hiding built-ins
    # The palette keep-list lives in the config FILE (keep_nodes:), not an env var — pass only its
    # LOCATION (explicitly, so --config wins over any ambient STREAMSTUDIO_CONFIG; empty ⇒ the bridge's
    # default ~/.streamstudio/config.yaml).
    if palette_config is not None and not palette_config.exists():
        logger.warning(f"--palette-config file not found: {palette_config} (PreviewImage/PreviewAny are still kept).")
    env = {
        **os.environ,
        "STREAMSTUDIO_HIDE_DEFAULT_NODES": "1" if hide_defaults else "0",
        "STREAMSTUDIO_ONLY_TAIDAL_NODES": "1" if only_taidal else "0",
        "STREAMSTUDIO_CONFIG": str(palette_config) if palette_config is not None else "",
    }
    logger.info("🚀 Launching StreamStudio (ComfyUI) 🚀")
    if only_taidal:
        logger.info("Showing ONLY Taidal nodes (+ kept defaults) — hiding built-ins and other custom packs.")
    elif not default_nodes:
        logger.info("Hiding ComfyUI's built-in nodes — Taidal + other custom nodes appear (at the palette root).")
    command = [sys.executable, "main.py", "--port", str(port)]
    if background:
        # Detached: its own session, so it outlives the shell that started it, and its output goes
        # to a per-port file (this terminal is being handed back). We do NOT wait for the bind —
        # that is the whole point of --background; the log path is where a failed boot shows up.
        log = studio_log_path(port)
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "ab") as handle:
            subprocess.Popen(
                command, cwd=comfy_dir, env=env, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True
            )
        print(f"StreamStudio is starting in the background — log: {log}")
        print()
        print(f"    Open it:  http://{STUDIO_HOST}:{port}")
        return
    try:
        subprocess.run(command, cwd=comfy_dir, env=env)
    except KeyboardInterrupt:
        logger.info("StreamStudio stopped.")


def _chosen_editor(editor: str) -> EditorName:
    """The ``--editor`` value, checked: liquifai binds it verbatim, so a value outside the Literal is refused here."""
    if editor not in EDITORS:
        raise SystemExit(f"--editor must be {' or '.join(EDITORS)} — got {editor!r}")
    return cast(EditorName, editor)


class _ComfyOptions(NamedTuple):
    """The ComfyUI-only options as a command received them — ``None`` is "not typed" (each defaults to
    ``None`` so a canvas launch can tell a typed option from an absent one; :func:`_launch` fills in the
    ComfyUI defaults)."""

    comfy_dir: Optional[Path]
    background: Optional[bool]
    default_nodes: Optional[bool]
    only_taidal_nodes: Optional[bool]
    palette_config: Optional[Path]
    install: Optional[bool]
    manager: Optional[bool]
    declared_nodes: Optional[bool]
    custom_node: Optional[List[str]]


def _refuse_other_editors_options(
    editor: EditorName, graph_dir: Optional[Path], templates_dir: Optional[Path], comfy: _ComfyOptions
) -> None:
    """Refuse — before anything runs or stops — an option that belongs to the OTHER editor, naming it.

    ``strict_flags`` already refuses a flag that names no parameter; this refuses one that names a
    parameter the chosen editor ignores — the same "a flag that reaches nothing" failure, one level down.
    """
    if editor == "canvas":
        flags = [f"--{name.replace('_', '-')}" for name, value in comfy._asdict().items() if value is not None]
        other = "comfy"
    else:
        flags = [] if graph_dir is None else [f"a graph directory ({graph_dir})"]
        flags += [] if templates_dir is None else ["--templates-dir"]
        other = "canvas"
    if flags:
        verb = "applies" if len(flags) == 1 else "apply"
        raise SystemExit(f"{', '.join(flags)} only {verb} to --editor {other}")


def _canvas_folders(graph_dir: Optional[Path], templates_dir: Optional[Path]) -> Tuple[Path, Optional[Path]]:
    """The canvas editor's graph folder (default: the current directory) and templates folder, ABSOLUTE — a
    folder that is not there is refused naming it.

    Resolved ONCE, here: every graph path the editor answers is then absolute, so a path from ``GET
    /api/graphs`` can be sent back, and every dialog opens in the graph folder (measured before, from
    ``streamstudio graphs``: the list said ``graphs/chain.yaml`` and the file browser looked in
    ``graphs/graphs``; from ``streamstudio .`` both dialogs opened on ``/``). liquifai binds CLI values
    VERBATIM as strings (see `_run`), so they are coerced here too."""
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
    """
    directory, templates = _canvas_folders(graph_dir, templates_dir)
    if _port_in_use(port):
        _report_port_in_use(port, "canvas", directory, templates)
        raise SystemExit(1)
    # Imported here, not at module top: the app pulls in the document model and the palette, which
    # every other command (a converter, `stop`, the ComfyUI launch) has no use for.
    from streamstudio.app import create_app, studio_of

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


def _launch(
    editor: EditorName, port: int, graph_dir: Optional[Path], templates_dir: Optional[Path], comfy: _ComfyOptions
) -> None:
    """The body ``run`` and ``restart`` share once the editor and its port are settled: serve the canvas
    editor, or launch ComfyUI with every option it was not given at its own default."""
    if editor == "canvas":
        _serve_canvas(graph_dir, port, templates_dir)
        return
    _run(
        comfy_dir=comfy.comfy_dir,
        port=port,
        background=bool(comfy.background),
        default_nodes=comfy.default_nodes is not False,
        only_taidal_nodes=bool(comfy.only_taidal_nodes),
        palette_config=comfy.palette_config,
        install=comfy.install is not False,
        manager=comfy.manager is not False,
        declared_nodes=comfy.declared_nodes is not False,
        custom_node=comfy.custom_node,
    )


# =======================================================================================
# Commands
# =======================================================================================


@app.command(default=True, positionals=["graph_dir"], short={"p": "port", "b": "background"})
def run(
    graph_dir: Optional[Path] = None,
    editor: EditorName = "canvas",
    port: Optional[int] = None,
    templates_dir: Optional[Path] = None,
    comfy_dir: Optional[Path] = None,
    background: Optional[bool] = None,
    default_nodes: Optional[bool] = None,
    only_taidal_nodes: Optional[bool] = None,
    palette_config: Optional[Path] = None,
    install: Optional[bool] = None,
    manager: Optional[bool] = None,
    declared_nodes: Optional[bool] = None,
    custom_node: Optional[List[str]] = None,
) -> None:
    """Serve an editor (the default command): the canvas editor over a folder of graphs, or ComfyUI.

    Args:
        graph_dir: Canvas editor — the folder whose *.yaml files are the graphs (default: the current
            directory). More can be opened from anywhere with File → Open.
        editor: canvas (default) — the graph editor on port 8190; comfy — the ComfyUI editor with the
            Taidal nodes, on port 8188.
        port: Port the editor listens on (default: 8190 for canvas, 8188 for comfy). A port already in
            use is refused before any work, with the address of what is there printed as a clickable URL.
        templates_dir: Canvas editor — the folder holding the templates (default:
            ~/.streamstudio/templates, created on the first template saved).
        comfy_dir: ComfyUI — the installation (default: ~/.streamstudio/ComfyUI). Cloned there if it
            does not exist.
        background: ComfyUI — start it DETACHED and return immediately instead of holding this
            terminal. Output goes to a per-port log under ~/.streamstudio/logs/.
        default_nodes: ComfyUI — show its built-in nodes alongside the Taidal nodes (default on). Turn
            it off (--default-nodes-) to hide them and surface the Taidal nodes at the palette root.
        only_taidal_nodes: ComfyUI — show ONLY Taidal nodes, hiding the built-ins AND every other
            third-party pack. Implies --default-nodes-.
        palette_config: ComfyUI — palette config file (YAML). Its keep_nodes: list names extra
            default/custom nodes to KEEP when hiding the palette. Defaults to
            ~/.streamstudio/config.yaml. NOT --config, which liquifai reserves for the app's own
            configuration file.
        install: ComfyUI — sync its requirements.txt into this environment on every launch (default on;
            a no-op when satisfied). Turn it off (--install-) for an air-gapped run.
        manager: ComfyUI — install the ComfyUI-Manager custom node (default on), an in-app store for
            further custom nodes.
        declared_nodes: ComfyUI — install the custom nodes installed packages declare through the
            streamstudio.custom_nodes entry-point group (default on).
        custom_node: ComfyUI — git URL(s) of extra custom nodes to clone on launch. Several:
            --custom-node=[urlA,urlB].
    """
    chosen = _chosen_editor(editor)
    comfy = _ComfyOptions(
        comfy_dir,
        background,
        default_nodes,
        only_taidal_nodes,
        palette_config,
        install,
        manager,
        declared_nodes,
        custom_node,
    )
    _refuse_other_editors_options(chosen, graph_dir, templates_dir, comfy)
    _launch(chosen, int(port) if port is not None else EDITOR_PORTS[chosen], graph_dir, templates_dir, comfy)


@app.command(short={"p": "port"})
def stop(editor: EditorName = "canvas", port: Optional[int] = None) -> None:
    """Stop the StreamStudio server listening on the editor's port.

    An idle port is not an error — it reports that nothing was there. A canvas editor keeps its unsaved work in
    its folder's hidden .streamstudio-workspace.json as it stops, and the message names the files kept there.

    Args:
        editor: canvas (default) or comfy — whose default port to stop.
        port: Port whose server to stop (default: 8190 for canvas, 8188 for comfy).
    """
    chosen = _chosen_editor(editor)
    target = int(port) if port is not None else EDITOR_PORTS[chosen]
    _stop_and_report(target, chosen)


@app.command(positionals=["graph_dir"], short={"p": "port", "b": "background"})
def restart(
    graph_dir: Optional[Path] = None,
    editor: EditorName = "canvas",
    port: Optional[int] = None,
    templates_dir: Optional[Path] = None,
    comfy_dir: Optional[Path] = None,
    background: Optional[bool] = None,
    default_nodes: Optional[bool] = None,
    only_taidal_nodes: Optional[bool] = None,
    palette_config: Optional[Path] = None,
    install: Optional[bool] = None,
    manager: Optional[bool] = None,
    declared_nodes: Optional[bool] = None,
    custom_node: Optional[List[str]] = None,
) -> None:
    """Stop the server on the editor's port (if any), then serve it again — the full run flow.

    Deliberately re-walks the WHOLE launch (for ComfyUI: clone, requirements sync, custom nodes,
    symlink) rather than shortcutting to the relaunch: a restart is the moment a drifted checkout or
    a newly installed entry point bites, so it is also how you pick those up. An option of the other
    editor, and a graph folder that is not there, are refused before anything is stopped. The canvas
    editor keeps its unsaved work as it stops, and the new one offers it on its page.

    Args:
        graph_dir: Canvas editor — the folder of graphs (default: the folder the running editor serves, else
            the current directory).
        editor: canvas (default) or comfy.
        port: Port whose server to restart (default: 8190 for canvas, 8188 for comfy).
        templates_dir: Canvas editor — the templates folder (default: ~/.streamstudio/templates).
        comfy_dir: ComfyUI — the installation (default: ~/.streamstudio/ComfyUI).
        background: ComfyUI — start the new one DETACHED and return immediately.
        default_nodes: ComfyUI — show its built-in nodes alongside the Taidal nodes.
        only_taidal_nodes: ComfyUI — show ONLY Taidal nodes. Implies --default-nodes-.
        palette_config: ComfyUI — palette config file (YAML). NOT --config (liquifai's own).
        install: ComfyUI — sync its requirements.txt into this environment.
        manager: ComfyUI — install the ComfyUI-Manager custom node.
        declared_nodes: ComfyUI — install the entry-point-declared custom nodes.
        custom_node: ComfyUI — git URL(s) of extra custom nodes.
    """
    chosen = _chosen_editor(editor)
    comfy = _ComfyOptions(
        comfy_dir,
        background,
        default_nodes,
        only_taidal_nodes,
        palette_config,
        install,
        manager,
        declared_nodes,
        custom_node,
    )
    _refuse_other_editors_options(chosen, graph_dir, templates_dir, comfy)
    if chosen == "canvas":  # a folder that is not there is refused BEFORE the running editor is stopped
        _canvas_folders(graph_dir, templates_dir)
    target = int(port) if port is not None else EDITOR_PORTS[chosen]
    stopped = _stop_and_report(target, chosen)
    if chosen == "canvas" and graph_dir is None and stopped is not None:
        # the SAME workspace (R6): the folder the stopped editor served, so its start offers the work it kept there —
        # measured before, a restart typed in another folder served that one, and the kept work waited unseen
        graph_dir = stopped.folder
    _launch(chosen, target, graph_dir, templates_dir, comfy)


@app.command(name="export", positionals=["graph"], short={"o": "output"})
def export_flow(
    graph: Path,
    output: Optional[Path] = None,
    skip: Optional[List[str]] = None,
    target: Optional[List[str]] = None,
) -> None:
    """Compile a saved graph (API form or native workflow.json, auto-detected) to Confluid flow document(s).

    One document per independent op component. The offline twin of the canvas Export-Flow button.

    Args:
        graph: ComfyUI graph JSON to compile — API form, or a native workflow.json save.
        output: Write the YAML here (default: stdout). Several components become
            flow_1.yaml … flow_N.yaml.
        skip: Node id(s) to EXCLUDE from the export, with everything data-downstream of them —
            the CLI twin of the canvas export-skip mark. Several: --skip=[3,7].
        target: Export ONLY the ops that compute this node (its data ancestor cone). Several:
            --target=[12,19].
    """
    # Import here (not at module top): pulling in streamstudio runs entry-point node discovery,
    # which is unnecessary weight for the common ``run`` path.
    import streamstudio
    from streamstudio.export import chain_output_paths, collect_export_marks, export_flow_yamls

    graph_path = Path(graph)
    if not graph_path.exists():
        raise SystemExit(f"Graph file not found: {graph_path}")

    data = json.loads(graph_path.read_text())
    # Accept a bare prompt dict, or {"prompt": {...}} (the wrapper the route also tolerates).
    prompt = data.get("prompt", data) if isinstance(data, dict) else data
    # ``--skip`` mirrors the canvas export-skip mark; an API-format file carries no node
    # properties, so the marked ids must be named explicitly here (getattr: tolerate
    # programmatic Namespace callers that predate the flag).
    skip_ids = _as_list(skip)
    target_ids = _as_list(target)
    if isinstance(prompt, dict) and isinstance(prompt.get("nodes"), list):
        # AUTO-DETECTED by payload shape (only the UI form carries a top-level ``nodes`` list):
        # a UI-form workflow.json — the editor's native save, e.g. a workspace-owned graph file
        # its save mirror wrote. Convert it to the prompt form; a UI file DOES carry node
        # properties, so its canvas export marks are honored alongside the explicit flags.
        from streamstudio.graphio import ui_workflow_to_prompt

        wf_skip, wf_target = collect_export_marks(prompt)
        skip_ids += wf_skip
        target_ids += wf_target
        prompt = ui_workflow_to_prompt(prompt, streamstudio.NODE_CLASS_MAPPINGS)
    if not isinstance(prompt, dict) or any(not isinstance(v, dict) or "class_type" not in v for v in prompt.values()):
        raise SystemExit(
            f"{graph_path} is not a ComfyUI graph (expected the API form {{node_id: {{class_type, inputs}}}} "
            "or a native workflow.json save with a top-level 'nodes' list)."
        )

    yaml_docs = export_flow_yamls(
        prompt,
        streamstudio.NODE_CLASS_MAPPINGS,
        skip_ids=skip_ids or None,
        target_ids=target_ids or None,
    )

    if len(yaml_docs) > 1:
        logger.info(f"Graph contains {len(yaml_docs)} independent op components — exporting each individually.")
    if output:
        # One file per component: ``--output flow.yaml`` stays ``flow.yaml`` for a single one,
        # ``flow_1.yaml`` … ``flow_N.yaml`` for several.
        for chain_path, yaml_doc in zip(chain_output_paths(Path(output), len(yaml_docs)), yaml_docs):
            chain_path.write_text(yaml_doc)
            logger.info(f"Wrote flow YAML to {chain_path}")
    else:
        # Several components on stdout → a ``---``-separated YAML document stream (each document
        # is individually a complete ``{flow: {...}}`` config; split before feeding confluid.load).
        sys.stdout.write("---\n".join(yaml_docs))


@app.command(name="import", positionals=["document"], short={"o": "output"})
def import_config(
    document: Path,
    output: Optional[Path] = None,
    as_: str = "graph",
    instantiate: bool = False,
) -> None:
    """Convert a Confluid YAML config into a ComfyUI workflow.json, loadable on the canvas.

    Auto-detects a flow-export document and rebuilds the connected op graph. The offline twin of
    the canvas "Import Taidal YAML" menu command.

    Args:
        document: Confluid YAML to import — a whole-graph config, or a flow-export document.
        output: Write the workflow JSON here (default: stdout).
        as_: graph (default) lays the nodes out; group wraps them in one visual box; subgraph
            packages them as a reusable blueprint with a declared record boundary and an op output.
        instantiate: Walk live-but-inert objects (materialize, solidify suppressed) instead of
            un-built markers.
    """
    import json as _json

    import streamstudio
    from streamstudio.graphio import yaml_to_workflow

    yaml_path = Path(document)
    if not yaml_path.exists():
        raise SystemExit(f"Config file not found: {yaml_path}")
    workflow = yaml_to_workflow(
        str(yaml_path),
        streamstudio.NODE_CLASS_MAPPINGS,
        instantiate=instantiate,
        as_=as_,
        name=yaml_path.stem,
    )
    text = _json.dumps(workflow, indent=2)
    if output:
        Path(output).write_text(text)
        logger.info(f"Wrote workflow JSON to {output}")
    else:
        sys.stdout.write(text)


@app.command(name="export-workflow", positionals=["workflow"], short={"o": "output"})
def export_workflow(workflow: Path, output: Optional[Path] = None) -> None:
    """Convert a WHOLE ComfyUI workflow.json (UI or API form) into a hoisted-!ref: YAML.

    The twin of the canvas "Export Graph → Taidal YAML".

    Args:
        workflow: ComfyUI workflow JSON (UI or API form).
        output: Write the YAML here (default: stdout).
    """
    import streamstudio
    from streamstudio.export import UnsupportedTopologyError
    from streamstudio.graphio import workflow_to_yaml

    graph_path = Path(workflow)
    if not graph_path.exists():
        raise SystemExit(f"Workflow file not found: {graph_path}")
    graph = json.loads(graph_path.read_text())
    try:
        yaml_doc = workflow_to_yaml(graph, streamstudio.NODE_CLASS_MAPPINGS)
    except UnsupportedTopologyError as refusal:
        # A refusal is an ANSWER, not a crash — the message names the node and the remedy,
        # so print it the way a missing file is printed rather than as a traceback.
        raise SystemExit(f"Cannot export {graph_path}: {refusal}")
    if output:
        Path(output).write_text(yaml_doc)
        logger.info(f"Wrote YAML to {output}")
    else:
        sys.stdout.write(yaml_doc)


@app.command(name="convert-server")
def convert_server() -> None:
    """Serve workflow→YAML conversions over stdin/stdout — one process, many conversions.

    A one-shot ``export-workflow`` pays the node-registry import (~10 s) EVERY call; a
    consuming workspace that converts several graphs per user action keeps ONE of these
    alive instead. Protocol, line-oriented JSON: after the imports finish, one
    ``{"ready": true}`` line; then per request ``{"json": "<workflow path>", "yaml": "<out
    path>"}`` → the conversion is written to the out path and answered with
    ``{"ok": true}`` or ``{"error": "..."}``. EOF on stdin ends the server.
    """
    import streamstudio
    from streamstudio.graphio import workflow_to_yaml

    def answer(payload: dict) -> None:
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()

    answer({"ready": True})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            graph = json.loads(Path(request["json"]).read_text())
            yaml_doc = workflow_to_yaml(graph, streamstudio.NODE_CLASS_MAPPINGS)
            Path(request["yaml"]).write_text(yaml_doc)
            answer({"ok": True})
        except Exception as exc:  # noqa: BLE001 - every failure answers the requester, never kills the server
            answer({"error": f"{type(exc).__name__}: {exc}"})


def main() -> None:
    """Console-script entry point (``[project.scripts] streamstudio``)."""
    app.run()


if __name__ == "__main__":
    main()
