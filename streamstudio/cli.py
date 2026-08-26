"""CLI for StreamStudio.

Subcommands (all offline — importing :mod:`streamstudio` populates ``NODE_CLASS_MAPPINGS`` via
entry-point discovery, so the converters work WITHOUT a running ComfyUI):

* ``run`` (the default — bare ``streamstudio`` still works): launch ComfyUI with the Taidal
  nodes auto-configured. Also seeds ComfyUI custom nodes from three tiers — the
  ``streamstudio.custom_nodes`` entry-point group (nodes DECLARED by installed packages,
  including streamstudio's own bundled ComfyUI-Manager; ``--no-manager`` filters the manager,
  ``--no-declared-nodes`` skips the tier), the ``STREAMSTUDIO_CUSTOM_NODES`` env var
  (comma-separated git URLs), and the repeatable ``--custom-node <git-url>`` flag —
  idempotently: a repo already present under ``custom_nodes/`` is left alone.
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

import json
import os
import socket
import subprocess
import sys
from importlib.metadata import entry_points
from pathlib import Path
from typing import List, Optional, Sequence, Union

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
    description="StreamStudio — ComfyUI with the Taidal nodes.",
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


def _report_stopped(port: int, pid: Optional[int]) -> None:
    """Say what was stopped, in the same plain voice as the port-in-use message."""
    if pid is None:
        print(f"Nothing is listening on port {port} — no StreamStudio to stop.")
    else:
        print(f"Stopped StreamStudio on port {port} (pid {pid}).")


def _report_port_in_use(port: int) -> None:
    """Say what holds the port and hand over a clickable way in, then stop the launch.

    Printed PLAIN (not through loggair) and on its own line: a terminal linkifies a bare ``http://``
    URL, and a log prefix in front of it buries the one thing the reader came for. Without this
    check ComfyUI discovers the clash only at the end of its own boot — measured 2026-08-26, 23
    seconds and 91 lines to reach one ERROR line on line 85, followed by exit code 0.
    """
    print(f"Port {port} is already in use — StreamStudio may already be running there.")
    print()
    print(f"    Open it:  http://{STUDIO_HOST}:{port}")
    print()
    print(f"If that is a different program, run StreamStudio elsewhere:  streamstudio --port {port + 1}")


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
        _report_port_in_use(port)
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


# =======================================================================================
# Commands
# =======================================================================================


@app.command(default=True, short={"p": "port", "b": "background"})
def run(
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
    """Launch ComfyUI with the Taidal nodes (the default command).

    Args:
        comfy_dir: Path to the ComfyUI installation (default: ~/.streamstudio/ComfyUI).
            Cloned there if it does not exist.
        port: Port ComfyUI listens on. A port already in use is refused before any launch work,
            with the address of what is there printed as a clickable URL.
        background: Start the studio DETACHED and return immediately instead of holding this
            terminal. Output goes to a per-port log under ~/.streamstudio/logs/.
        default_nodes: Show ComfyUI's built-in nodes alongside the Taidal nodes. Turn it off
            (--default-nodes-) to hide them and surface the Taidal nodes at the palette root.
        only_taidal_nodes: Show ONLY Taidal nodes — hide the built-ins AND every other
            third-party pack. Implies --default-nodes-.
        palette_config: Palette config file (YAML). Its keep_nodes: list names extra
            default/custom nodes to KEEP when hiding the palette. Defaults to
            ~/.streamstudio/config.yaml. NOT --config, which liquifai reserves for the app's own
            configuration file.
        install: Sync ComfyUI's requirements.txt into this environment on every launch (a no-op
            when satisfied). Turn it off (--install-) for an air-gapped run.
        manager: Install the ComfyUI-Manager custom node, giving the GUI an in-app store for
            further custom nodes.
        declared_nodes: Install the custom nodes that installed packages declare through the
            streamstudio.custom_nodes entry-point group.
        custom_node: Git URL(s) of extra ComfyUI custom nodes to clone on launch. Several:
            --custom-node=[urlA,urlB].
    """
    _run(
        comfy_dir,
        port,
        background,
        default_nodes,
        only_taidal_nodes,
        palette_config,
        install,
        manager,
        declared_nodes,
        custom_node,
    )


@app.command(short={"p": "port"})
def stop(port: int = DEFAULT_PORT) -> None:
    """Stop the StreamStudio server listening on --port.

    An idle port is not an error — it reports that nothing was there.

    Args:
        port: Port whose studio to stop.
    """
    _report_stopped(port, _stop_studio(port))


@app.command(short={"p": "port", "b": "background"})
def restart(
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
    """Stop the server on --port (if any), then launch it again — the full run flow.

    Deliberately re-walks the WHOLE launch (clone, requirements sync, custom nodes, symlink)
    rather than shortcutting to the relaunch: a restart is the moment a drifted checkout or a
    newly installed entry point bites, so it is also how you pick those up.

    Args:
        comfy_dir: Path to the ComfyUI installation (default: ~/.streamstudio/ComfyUI).
        port: Port whose studio to restart.
        background: Start the new studio DETACHED and return immediately.
        default_nodes: Show ComfyUI's built-in nodes alongside the Taidal nodes.
        only_taidal_nodes: Show ONLY Taidal nodes. Implies --default-nodes-.
        palette_config: Palette config file (YAML). NOT --config (liquifai's own).
        install: Sync ComfyUI's requirements.txt into this environment.
        manager: Install the ComfyUI-Manager custom node.
        declared_nodes: Install the entry-point-declared custom nodes.
        custom_node: Git URL(s) of extra ComfyUI custom nodes.
    """
    _report_stopped(port, _stop_studio(port))
    _run(
        comfy_dir,
        port,
        background,
        default_nodes,
        only_taidal_nodes,
        palette_config,
        install,
        manager,
        declared_nodes,
        custom_node,
    )


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
    from streamstudio.graphio import workflow_to_yaml

    graph_path = Path(workflow)
    if not graph_path.exists():
        raise SystemExit(f"Workflow file not found: {graph_path}")
    graph = json.loads(graph_path.read_text())
    yaml_doc = workflow_to_yaml(graph, streamstudio.NODE_CLASS_MAPPINGS)
    if output:
        Path(output).write_text(yaml_doc)
        logger.info(f"Wrote YAML to {output}")
    else:
        sys.stdout.write(yaml_doc)


def main() -> None:
    """Console-script entry point (``[project.scripts] streamstudio``)."""
    app.run()


if __name__ == "__main__":
    main()
