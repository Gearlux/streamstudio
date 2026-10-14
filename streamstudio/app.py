"""The editor's HTTP surface: a Flask blueprint whose every route is ONE call on the document, the
palette, the layout or the engine — JSON in, JSON out, a refusal as ``400 {error}`` carrying the
located message, an unknown graph / node / run as ``404 {error}`` naming what there is.

Why a blueprint and a factory, not a module-level ``app``: the editor is served two ways
(``docs/rewrite.md`` §0) — standalone by ``streamstudio`` (the canvas editor, the CLI's default), and
MOUNTED by a consuming Flask app under a prefix of its own. :func:`editor_blueprint` is the mountable
half; :func:`create_app` wraps it for the standalone case. All state — the graph list, the open
documents with their undo history, the runs — lives on ``app.extensions["streamstudio"]``
(:class:`Studio`), never on this module: two apps in one process are two editors, and a test never
leaks a run into the next test.

Why the routes hold no logic: the same operations become LLM tools in phase 5 (``run_graph``,
``rerun_node``, ``set_param``, ``add_step``, …) and a canvas action in phase 3. A route parses its
request and calls one :class:`Studio` method, so a tool calls the same method and cannot drift
from what the page does.

Three rules of the document carry into the routes. **An edit changes the EDITOR's copy; Save writes
it** (the user's decision 1, explicit Save): ``param`` / ``steps`` / ``rename`` / ``undo`` / ``redo`` /
``positions`` / ``layout`` / ``groups`` / ``paste`` / ``PUT`` change what the :class:`Studio` holds and
answer the graph with ``dirty: true``; ``POST …/save`` writes the files and the sidecar, ``…/save-as``
writes them elsewhere, ``…/revert`` drops what is held. **A refusal changes nothing**:
:class:`~streamstudio.document.Document` refuses an edit that makes the document stop loading or gain a
problem, a ``PUT`` of a whole text is held to the same test, and a Save that would overwrite a file
someone else changed on disk — or write a file the disk will not take — writes nothing and names the file.
**The run is what the canvas shows**: a trace builds the document from the held texts
(:meth:`Document.load`), never from the file, so an unsaved value is the value that runs.

The graph list is a NAME → FILE registry (:class:`Studio`): every ``*.yaml`` of the folder the editor
was started on — rescanned on every list, a ``*.layout.yaml`` sidecar and a ``*.steps.yaml`` steps file
left out — plus every file opened from anywhere on this machine (File → Open, over ``GET api/files``)
and every file File → New… wrote (New writes its starter at once: there is never a graph that exists
only in the editor). A name is the file's stem, made unique with ``~2``, ``~3`` … .
Templates are steps files in a folder of their own (``templates_dir``), written at once — they belong
to no graph.

Why the trace maps names: the engine (:class:`recordstream.flow.Tracer`) names a ``Stream``'s ops
``ops[0]``, ``ops[1]``, … relative to the stream; the document names the same nodes by their path,
``stream.ops[1]`` (§3: one naming scheme for the engine, the sidecar, the tools and the canvas).
A :class:`Run` keeps the stream node's name as the prefix and translates both ways, so a page or a
tool never sees ``ops[1]`` — nor ``ops[1]/grey``: the tracer descends into a subgraph and names its inner
nodes ``<step>/<inner>``, which the prefix turns into the document's ``stream.ops[1]/grey`` (a trace row's
``parent`` and a subgraph row's ``inner`` too), and ``until`` / a rerun take that name back.

Subgraphs (a ``recordstream.flow.subgraph.Subgraph`` step, drawn as one node whose inside is
``model.inside[<node>]``): every route that names a node takes a QUALIFIED name (``prep/grey``; in a URL
through ``<path:node>``), and four routes build them — ``POST …/subgraph`` (*Convert selection to
subgraph*), ``POST …/subgraph/<node>/extract`` (*Move to its own file*, a ``*.steps.yaml`` held until Save),
``POST …/include`` (*Insert steps file*) and ``POST …/templates/<template>/insert`` (a COPY of a template).
The record a trace runs is drawn from the document's OWN source (its record ``#record``). A graph without a
source — a view chain, a ``flow:`` alone, a ``Stream`` whose ``source`` is empty — runs the record a HOST supplies:

The HOST is what an app that mounts the editor gives it (``editor_blueprint(host=…)``, :class:`EditorHost` — three
methods, no base class): ``folder()`` names the folder the editor serves, asked at each request, so a host that
moves its state folder is followed and the graphs the editor holds move with it (:meth:`Studio.graph_dir`);
``record(graph, path)`` answers a :class:`HostRecord` — the record, the values the host fills into the graph's ops
(a window, a band) and its label — which the run takes through the texts the editor holds, unsaved values included,
on the same tracer; ``saved(paths)`` is told which document files every Save wrote, so the host re-runs what reads
them. A standalone editor has no host, and such a graph's run is refused in words: a host supplies the record
(mount the editor in one) or wire a source. The host's own stop calls :meth:`Studio.stash` (reached with
:func:`studio_of`), so a mounted editor keeps unsaved work across it as the standalone one does.
"""

import html
import itertools
import json
import os
import re
import secrets
import sys
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterator,
    List,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
    cast,
    get_args,
    runtime_checkable,
)

import yaml
from flask import Blueprint, Flask, Response, abort, current_app, g, jsonify, request, send_from_directory
from loggair import get_logger
from recordstream.core.stream import Stream
from recordstream.flow import FlowGraph, Tracer
from recordstream.items import Record
from recordstream.ops.contract import ChainContractError

import streamstudio
from streamstudio import catalogue as catalogue_module
from streamstudio import document as document_module
from streamstudio import layout, lookups
from streamstudio import page as page_module
from streamstudio import palette
from streamstudio._yamltext import YamlText
from streamstudio.catalogue import CatalogueUnavailable
from streamstudio.document import (
    ALREADY_EXISTS,
    GATE_PARAM,
    SEPARATOR,
    STEPS_SUFFIX,
    Document,
    Model,
    Node,
    WireEnds,
    rebase_path,
)
from streamstudio.page import BUILD_COMMAND, PageCheck, ensure_page_built, page_check_environment
from streamstudio.settings import is_settings_file

if sys.platform != "win32":  # the folder lock between editors (:func:`_folder_lock`) is POSIX's flock
    import fcntl

#: The key under ``app.extensions`` where the :class:`Studio` of an app lives.
EXTENSION = "streamstudio"

#: What a document's root says it is: a ``flow:`` graph, a contract-rooted graph (a template twin),
#: or any other configuration (a ``Stream``, a processor …).
GraphKind = Literal["flow", "graph", "contract"]
GRAPH_KINDS: Tuple[str, ...] = get_args(GraphKind)

#: What File → New makes: a ``flow:`` graph, a ``Stream`` pipeline, or a flow with its graph contract.
NewGraphKind = Literal["flow", "pipeline", "source"]
NEW_GRAPH_KINDS: Tuple[NewGraphKind, ...] = get_args(NewGraphKind)

#: What an entry of the file browser (``GET /api/files``) is: a directory to enter, a graph file to open, or — in a
#: listing of every file (a file picker's, :data:`FileShow`) — any file.
FileEntryKind = Literal["dir", "yaml", "file"]
#: What ``GET /api/files`` lists besides the directories: the graph files (the File ▸ Open browser), or every file (the
#: file picker of a parameter that names one, ``checkpoint_path``).
FileShow = Literal["graphs", "all"]
FILE_SHOWS: Tuple[str, ...] = get_args(FileShow)

#: The RECOVERY FILE: what a stop keeps of the unsaved work, in the folder the editor was started on (the user's
#: item 1, 2026-09-29: "save the file to a tmp workspace file (hidden)"). Hidden (a leading dot — the graph list
#: reads ``*.yaml`` and the file browser leaves hidden entries out) and JSON, because it is editor state, not a
#: confluid document. Written by :meth:`Studio.stash` only when something is unsaved; the next start offers it.
RECOVERY_FILE = ".streamstudio-workspace.json"
#: Where a recovery file that cannot be used is moved by *discard* (or by a stop that finds one) — kept, never
#: deleted, since it may be the only copy of someone's work. A name already taken gets the next free one
#: (:func:`_aside_name`: ``….broken-2.json``, ``….broken-3.json`` …) — measured before: the second set-aside
#: overwrote the first.
BROKEN_RECOVERY_FILE = ".streamstudio-workspace.broken.json"
#: The request header a page sends the SESSION it joined in (``GET /api/workspace`` answers it): a request of an
#: earlier editor's session — a tab left open across ``streamstudio restart`` — is refused, changing nothing
#: (:data:`RESTARTED`).
SESSION_HEADER = "X-Streamstudio-Session"
#: The refusal (``409``) a request of another session gets. Measured before: a tab left open across a restart edited
#: the NEW editor's document — read from disk — so the kept edit vanished on the next change, Save wrote the file
#: without it and said clean, and the next stop replaced the kept version.
RESTARTED = (
    "the editor was restarted after this page was loaded — nothing was changed; the page asks the new editor what it "
    "holds, and the unsaved work the stop kept is offered again"
)

#: A node position as the JSON carries it: ``[x, y]``.
PositionList = List[float]

#: The suffixes of files that sit beside graphs and are never one: the positions sidecar
#: (``streamstudio.layout``) and a subgraph's steps file (``streamstudio.document.STEPS_SUFFIX`` — a bare
#: steps mapping, what ``include:`` reads inside a subgraph and what a template is).
SIDECAR_SUFFIX = ".layout.yaml"

_RUN_ID_BYTES = 4
#: What a steps file's name may hold: a node name's last part with anything else made ``_``
#: (``stream.ops[1]`` → ``stream.ops_1_``).
_FILE_UNSAFE = re.compile(r"[^\w.-]")
_SEGMENT = re.compile(r"^(\w+)(?:\[(\d+)\])?$")
#: A ``$VAR`` / ``${VAR}`` that :func:`os.path.expandvars` left as written — a variable this process does not have.
_UNSET_VARIABLE = re.compile(r"\$(?:\{(\w+)\}|(\w+))")
#: A name that can be a file stem: a word character first, then word characters, spaces, dots, dashes.
_STEM = re.compile(r"[\w][\w .-]*")
#: The one op every starter holds: it reads and writes no record entry, so each starter derives clean
#: whatever the host provides, and a run of it prints what reached it.
_STARTER_OP = "recordstream.ops.debug.PrintRecordOp"

#: What a Save may write alone — the graph's settings file (``POST …/save {only: "settings"}``, a settings sheet's).
SaveOnly = Literal["settings"]
SAVE_ONLY: Tuple[str, ...] = get_args(SaveOnly)

logger = get_logger(__name__)


# ================================================================================================
# the host: what an app that mounts the editor gives it
# ================================================================================================


@dataclass(frozen=True)
class HostRecord:
    """What a host answers for a Run of a graph that has no source of its own (:meth:`EditorHost.record`).

    Args:
        record: The record the graph's first node receives — as the host's own chains would get it. The run hands it a
            shallow copy (``dict(record)``), so the host may give the same mapping again.
        fills: The values the host sets on the graph's ops before the run, the way it fills its own chains — groups of
            ``{attribute: value}``, e.g. ``({"start_s": 0.1, "stop_s": 0.2}, {"f0_hz": 2.40e9, "f1_hz": 2.48e9})``. A
            group is set on every op that has EVERY attribute in it (an op with ``start_s`` alone gets none of the
            window), on the graph's own level: a step of a ``flow:``, a member of the ``Stream``'s ``ops`` — never an
            op inside a subgraph. Set through the attributes, so the tracer's rerun (which rebuilds a node from its
            CURRENT values) keeps them.
        label: How the run panel names the record (``rec_0042 · 0.10–0.12 s · 2.40–2.48 GHz``).
    """

    record: Record
    fills: Sequence[Mapping[str, Any]] = ()  # a value is whatever the op's attribute takes (a float window, a band …)
    label: str = ""


@runtime_checkable
class EditorHost(Protocol):
    """What an app that mounts the editor gives it — ``editor_blueprint(host=…)``; any object with a ``name`` and
    these four methods (no base class; :func:`isinstance` checks the shape). Each is called on a request's thread.

    ``name`` is how the page names the host in what it says (``record: annotaide's selection``); ``GET /api/workspace``
    answers it as ``host: {name}`` (``null`` for a standalone editor), which is also how the page knows a Run of a
    graph without a source asks the host.

    ``folder()`` names the folder the editor serves; it is asked at EACH request (and at :meth:`Studio.stash`), so
    it must be cheap. When it answers another folder than before — the host moved or copied its state folder —
    every graph of the old folder is the same file in the new one: the list follows, and a graph held unsaved keeps
    its edits and its undo history and saves into the new folder (:meth:`Document.moved`).

    ``record(graph, path)`` answers the record a Run of ``graph`` (its list name and file) runs, for a graph with no
    source of its own (:class:`HostRecord`). A host that has none to give raises ``ValueError`` with the reason in
    the page's words — it is the run's refusal, ``400 {error}``, as written.

    ``saved(paths)`` is told, after every Save and Save As that wrote a document file, which ones it wrote (resolved,
    the graph's own last; a Save of positions alone writes only the layout sidecar and tells nothing). A failure
    raised here does not undo the Save: the answer is the saved graph plus ``host_error`` in words.

    ``shipped()`` names the graphs the host SHIPS, by name, each with the file it ships it in — outside the folder (a
    package's template). Such a graph is listed and opened while the folder holds no file of its name; a change of
    one of its settings writes only its settings file into the folder, and the first Save of a change to the graph
    itself writes the graph there — the folder's copy, which wins from then on. The shipped file is never written.
    A host that ships nothing answers ``{}``. Asked whenever the list is read, so it must be cheap.
    """

    name: str

    def folder(self) -> Path:
        """The folder the editor serves now."""
        ...

    def record(self, graph: str, path: Path) -> HostRecord:
        """The record a Run of ``graph`` (at ``path``) runs; ``ValueError`` in words when there is none."""
        ...

    def saved(self, paths: Sequence[Path]) -> None:
        """The document files a Save wrote."""
        ...

    def shipped(self) -> Mapping[str, Path]:
        """The graphs the host ships: name → the file it ships it in."""
        ...


def default_templates_dir() -> Path:
    """Where templates live when none is named: ``~/.streamstudio/templates`` — a FUNCTION, resolved when
    used, never at import (a signature default would freeze ``Path.home()`` — the CLI's rule)."""
    return Path.home() / ".streamstudio" / "templates"


def starter_text(kind: NewGraphKind, name: str) -> str:
    """The text a New graph of ``kind`` starts with — each pinned to derive with no problem.

    ``source`` APPENDS its ``GraphContract`` root after the flow: measured 2026-09-29, a contract written
    BEFORE ``flow:`` receives the file's ``outputs:`` line as its own ``outputs`` and does not build.
    """
    if kind not in NEW_GRAPH_KINDS:
        raise ValueError(f"kind must be one of {', '.join(NEW_GRAPH_KINDS)} — got {kind!r}")
    flow = (
        f"# {name} — a flow graph: each step reads the record the step above it returns\n"
        "flow:\n"
        "  probe:\n"
        f"    op: !class:{_STARTER_OP} {{label: probe}}\n"
        "outputs: probe\n"
    )
    if kind == "flow":
        return flow
    if kind == "pipeline":
        return (
            f"# {name} — a pipeline: a stream runs its ops, in list order, on every record its source yields\n"
            "stream: !class:recordstream.core.stream.Stream\n"
            "  ops:\n"
            f"    - !class:{_STARTER_OP} {{label: probe}}\n"
        )
    return (
        flow.replace("a flow graph:", "a source graph (with its contract):", 1)
        + "contract: !class:recordstream.ops.contract.GraphContract\n"
        + f"  name: {name}\n"
        + "  inputs: {}\n"
        + "  outputs: {}\n"
    )


def _refuse_stem(name: str, what: str) -> None:
    """Refuse a name that cannot be a file stem, or that would read as a sidecar / steps file's stem."""
    if not _STEM.fullmatch(name) or name.endswith((".layout", ".steps")):
        raise ValueError(
            f"{name!r} cannot name {what} — use letters, digits, '_', '-', '.' or spaces, not starting with "
            "'.' and not ending in '.layout' or '.steps'"
        )


def _file_entry(path: Path, kind: FileEntryKind) -> Dict[str, str]:
    """One entry of the file browser: ``{name, path, kind}``."""
    return {"name": path.name, "path": str(path), "kind": kind}


def _is_graph_file(path: Path) -> bool:
    """A ``*.yaml`` file of a graph folder that is a graph: not hidden, not a sidecar, not a steps file, not a graph's
    settings file (``streamstudio.settings``)."""
    name = path.name
    return (
        path.is_file()
        and not name.startswith(".")
        and not name.endswith(SIDECAR_SUFFIX)
        and not name.endswith(STEPS_SUFFIX)
        and not is_settings_file(path)
    )


def _step_names(text: str) -> List[str]:
    """The step names of a steps file's text (its ROOT mapping's keys, ``include`` left out)."""
    try:
        root = YamlText(text).root
    except yaml.YAMLError:
        return []
    if not isinstance(root, yaml.MappingNode):
        return []
    return [key for key in YamlText.keys(root) if key != "include"]


def _first_comment(text: str) -> str:
    """The first comment line of a text, without its ``#`` — a template's one-line description."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _json_kind(value: Any) -> str:  # any JSON value
    """How a message names a JSON value's kind."""
    kinds = {list: "a list", str: "a string", int: "a number", float: "a number", bool: "true or false"}
    return "null" if value is None else kinds.get(type(value), type(value).__name__)


def _is_xy(value: Any) -> bool:  # any JSON value
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
    )


def _aside_name(number: int) -> str:
    """The name the ``number``-th unreadable recovery file is moved aside to: :data:`BROKEN_RECOVERY_FILE`, then
    ``.streamstudio-workspace.broken-2.json`` … — every one matched by the ``.streamstudio-workspace*.json`` line of a
    project's ``.gitignore``."""
    return BROKEN_RECOVERY_FILE if number == 1 else BROKEN_RECOVERY_FILE.replace(".json", f"-{number}.json")


def _writing_name() -> str:
    """The file a stop writes before renaming it over :data:`RECOVERY_FILE` — its OWN name (the process and a random
    token): measured before, two editors stopped at once wrote through one shared ``.writing.json`` and one of them
    renamed the other's half-written file into place (``Extra data: line 21``) or found it gone."""
    return f".streamstudio-workspace.writing-{os.getpid()}-{secrets.token_hex(4)}.json"


@contextmanager
def _folder_lock(folder: Path) -> Iterator[None]:
    """Hold the folder's exclusive lock (``flock`` on the folder itself — no lock file is left behind) while a stop
    merges into the recovery file or a restore / discard takes it: two editors of one folder read-merge-write in turn,
    never interleaved. Where ``fcntl`` does not exist (Windows) there is no lock between processes."""
    if sys.platform == "win32":  # pragma: no cover - POSIX only
        yield
        return
    handle = os.open(folder, os.O_RDONLY)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)  # closing the last descriptor of the open file releases the lock


def _check_recovery(data: Any) -> None:  # the recovery file's JSON, of any shape
    """Refuse — a ``ValueError`` saying what is wrong — JSON the editor did not write, before any of it is used: a
    mapping of ``saved_at`` and ``graphs``, each graph what :meth:`Document.held` answers plus its list ``name``."""
    if not isinstance(data, dict):
        raise ValueError(f"it holds {_json_kind(data)}, not the editor's mapping")
    if not isinstance(data.get("saved_at"), str):
        raise ValueError("'saved_at' must be the time it was written")
    graphs = data.get("graphs")
    if not isinstance(graphs, list):
        raise ValueError("'graphs' must be a list")
    for number, entry in enumerate(graphs, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"graph {number} is {_json_kind(entry)}, not a mapping")
        for key in ("name", "path"):
            if not isinstance(entry.get(key), str) or not entry[key]:
                raise ValueError(f"graph {number} has no {key!r}")
        for key in ("texts", "saved"):
            texts = entry.get(key)
            if not isinstance(texts, dict) or not all(isinstance(text, str) for text in texts.values()):
                raise ValueError(f"graph {number}'s {key!r} must map each file to its text")
        if str(Path(entry["path"]).resolve()) not in entry["texts"]:
            raise ValueError(f"graph {number}'s 'texts' holds no text of {entry['path']}")
        positions = entry.get("positions")
        if positions is not None and not (isinstance(positions, dict) and all(map(_is_xy, positions.values()))):
            raise ValueError(f"graph {number}'s 'positions' must map each node to [x, y]")
        groups = entry.get("groups")
        if groups is not None and not (isinstance(groups, list) and all(isinstance(g, dict) for g in groups)):
            raise ValueError(f"graph {number}'s 'groups' must be a list of groups")
        if "saved_at" in entry and not isinstance(entry["saved_at"], str):
            raise ValueError(f"graph {number}'s 'saved_at' must be the time it was kept")


def _rebased(data: Dict[str, Any], folder: Path) -> Dict[str, Any]:  # the checked recovery JSON → the same, rebased
    """The recovery file's graphs with every path under the folder the file NAMES moved under ``folder`` — the one
    it now sits in, which the editor serves: the folder was moved (or copied) since the stop, and the kept work is
    about the files beside it. A file of another folder stays where it is. Each graph also gets its own ``saved_at``
    (the file's, for a graph kept before graphs carried one).

    Measured before (R4): after ``mv C C-moved`` the restore attached the work to ``C/flow.yaml`` as a second entry
    ``flow~2`` whose Save could never write."""
    named = data.get("folder")
    old = [Path(named), Path(named).resolve()] if isinstance(named, str) and Path(named) != folder else []
    new = [folder, folder.resolve()]

    def moved(given: str) -> str:
        path = Path(given)
        for before, after in zip(old, new):
            if path.is_relative_to(before):
                return str(after / path.relative_to(before))
        return given

    graphs = []
    for entry in data["graphs"]:
        rebased = {
            **entry,
            "saved_at": entry.get("saved_at", data["saved_at"]),
            "path": moved(entry["path"]),
            "texts": {moved(key): text for key, text in entry["texts"].items()},
            "saved": {moved(key): text for key, text in entry["saved"].items()},
        }
        graphs.append(rebased)
    return {**data, "folder": str(folder), "graphs": graphs}


# ================================================================================================
# JSON shapes
# ================================================================================================


def _plain(value: Any) -> Any:  # any Python value the model holds → what JSON can carry
    """A parameter's value or default as JSON: containers recursed, scalars as they are, anything
    else (a class, an enum, a marker) as its ``repr`` — the page shows it, never edits it."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return repr(value)


def _model_dict(model: Model) -> Dict[str, Any]:
    """The model as plain dicts — nodes (with their params and sockets), wires, problems, outputs, and
    ``inside``: per subgraph node of the level, the same shape for its inside (recursive)."""
    return {
        "nodes": [_plain(asdict(node)) for node in model.nodes],
        "wires": [asdict(wire) for wire in model.wires],
        "problems": [asdict(problem) for problem in model.problems],
        "outputs": list(model.outputs),
        "inside": {name: _model_dict(inner) for name, inner in model.inside.items()},
    }


def _positions_dict(positions: Mapping[str, Tuple[float, float]]) -> Dict[str, PositionList]:
    return {name: [float(x), float(y)] for name, (x, y) in positions.items()}


def _positions_from_json(body: Mapping[str, Any], model: Model) -> Dict[str, Tuple[float, float]]:
    """``{name: [x, y]}`` → positions of nodes on ANY level (``prep/grey`` inside a subgraph); an unknown node
    is a ``KeyError`` (404), a malformed pair a ``ValueError``."""
    known = model.all_names()
    positions: Dict[str, Tuple[float, float]] = {}
    for name, xy in body.items():
        if name not in known:
            raise KeyError(f"no node {name!r} (the nodes are: {known})")
        if not isinstance(xy, (list, tuple)) or len(xy) != 2:
            raise ValueError(f"the position of {name!r} must be [x, y], got {xy!r}")
        try:
            positions[str(name)] = (float(xy[0]), float(xy[1]))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"the position of {name!r} must be two numbers, got {xy!r}") from exc
    return positions


# ================================================================================================
# a run
# ================================================================================================


class Run:
    """One trace of one graph: the engine's tracer plus the name mapping between the document and the engine.

    Args:
        run_id: The short id the routes and the tools refer to it by.
        graph: The graph's name.
        record: Which record of the graph's own source was drawn; ``None`` when a host supplied it.
        tracer: The engine, already holding the run.
        prefix: What the document puts before the engine's node names (``stream.`` for a stream named ``stream``,
            ``""`` for a flow's steps).
        where: What refusals name as the location.
        host: For a record a host supplied, ``{label, filled}``: its label, and per node the values it set
            (``{"window": {"start_s": 0.1, "stop_s": 0.2}}``); ``None`` for the graph's own record.
    """

    def __init__(
        self,
        run_id: str,
        graph: str,
        record: Optional[int],
        tracer: Tracer,
        prefix: str,
        where: str,
        host: Optional[Dict[str, Any]] = None,
    ) -> None:
        # Stores only: the run happened (or is paused) inside the tracer already.
        self.run_id = run_id
        self.graph = graph
        self.record = record
        self.tracer = tracer
        self.prefix = prefix
        self.where = where
        self.host = host

    def document_names(self) -> List[str]:
        return [self.prefix + name for name in self.tracer.names]

    def document_name(self, engine_name: str) -> str:
        return self.prefix + engine_name

    def engine_name(self, node: str) -> str:
        """The engine's name of a document node; an unknown node is a ``KeyError`` naming the document's nodes."""
        names = self.document_names()
        if node not in names:
            raise KeyError(f"{self.where}: no node {node!r} — the nodes are {names}")
        return node[len(self.prefix) :]

    def rerun(self, node: str, params: Dict[str, Any]) -> None:  # params: the constructor's kwargs, of any type
        """Rebuild ``node`` with ``params`` and recompute from there on.

        The constructor is the authority: a value it refuses (a pydantic ``ValidationError``, which is
        a ``ValueError``; a ``TypeError``) leaves the generation counter where it was and is re-raised
        as a ``ValueError`` — the trace is untouched. A node that RAISES while running is not a refusal:
        the generation advanced, the trace records the error, and the run is answered as it stands.
        """
        engine_node = self.engine_name(node)
        before = self.tracer.generation
        try:
            self.tracer.rerun_from(engine_node, **params)
        except Exception as exc:  # noqa: BLE001 — refused before anything ran, or a node raised while running
            if self.tracer.generation == before:
                raise ValueError(f"{self.where}:{node}: {exc}") from exc

    def _row(self, row: Mapping[str, Any]) -> Dict[str, Any]:  # one row of the tracer's JSON: values of any type
        """A trace or check row with every node it names in the document's names — its own, and a subgraph
        row's ``inner`` / an inner row's ``parent`` (``ops[0]/grey`` → ``stream.ops[0]/grey``)."""
        out = {**row, "node": self.document_name(str(row["node"]))}
        if "parent" in row:
            out["parent"] = self.document_name(str(row["parent"]))
        if "inner" in row:
            out["inner"] = [self.document_name(str(name)) for name in row["inner"]]
        return out

    def to_dict(self) -> Dict[str, Any]:
        """``{run, graph, record, host, trace, statuses, generations, paused_at}`` — every node by its document name;
        ``record`` is ``None`` and ``host`` is ``{label, filled}`` when a host supplied the record."""
        trace = self.tracer.to_dict()
        trace["nodes"] = [self._row(node) for node in trace["nodes"]]
        trace["check"] = [self._row(row) for row in trace["check"]]
        paused = self.tracer.paused_at
        trace["paused_at"] = None if paused is None else self.document_name(paused)
        return {
            "run": self.run_id,
            "graph": self.graph,
            "record": self.record,
            "host": self.host,
            "trace": trace,
            "statuses": {self.document_name(name): status for name, status in self.tracer.statuses.items()},
            "generations": {self.document_name(name): gen for name, gen in self.tracer.generations.items()},
            "paused_at": trace["paused_at"],
        }


# ================================================================================================
# the studio: the documents of one directory, and the runs
# ================================================================================================


def _check_folder_or_host(graph_dir: Optional[Path], host: Optional[EditorHost]) -> None:
    """Refuse an editor given both a folder and a host, or neither: a host names the folder at each request."""
    if graph_dir is None and host is None:
        raise ValueError("the editor needs a graph folder or a host that names one (editor_blueprint(host=…))")
    if graph_dir is not None and host is not None:
        raise ValueError(
            f"pass a graph folder ({graph_dir}) or a host, not both — a host names the folder at each request"
        )


class Studio:
    """The editor's state: the graph list (a name → file registry), the documents the editor holds (opened
    on first use, kept with their undo history and unsaved changes), the runs, and the templates folder.

    Args:
        graph_dir: The folder the editor was started on — its ``*.yaml`` files are listed, and File → New
            and the file browser start there. ``None`` with a ``host``, which names it.
        history_depth: How many edits each document's ``undo`` can walk back.
        templates_dir: The folder holding the templates (``<name>.steps.yaml``); ``None`` =
            :func:`default_templates_dir`. Created on the first template saved.
        host: The app that mounts the editor (:class:`EditorHost`): it names the folder at each use, supplies the
            record of a graph with no source, and is told what a Save wrote. ``None`` for the standalone editor.
    """

    def __init__(
        self,
        graph_dir: Optional[Path] = None,
        *,
        history_depth: int = 50,
        templates_dir: Optional[Path] = None,
        host: Optional[EditorHost] = None,
    ) -> None:
        # Stores only: the folder is scanned when a route first asks, a document opened when one names it; a host's
        # folder is asked when first used. Both folders are made ABSOLUTE, once: every path an answer carries is then
        # absolute and can be sent back as it is (measured before, started on `graphs`: the list said
        # `graphs/chain.yaml`, which the routes then read against the folder again — `graphs/graphs/chain.yaml`).
        _check_folder_or_host(graph_dir, host)
        self.host = host
        self._folder: Optional[Path] = None if graph_dir is None else Path(graph_dir).expanduser().absolute()
        self.history_depth = int(history_depth)
        chosen = Path(templates_dir) if templates_dir is not None else default_templates_dir()
        self.templates_dir = chosen.expanduser().absolute()
        # Which editor process this is, as a page learns it (``GET /api/workspace``) and sends it back
        # (:data:`SESSION_HEADER`): a page of an earlier editor on this port — a tab left open across a restart — is
        # told so instead of editing a document it never saw.
        self.session = secrets.token_hex(8)
        # Whose turn it is: every editor request holds it from its first hook to its teardown (editor_blueprint), and a
        # host that reads, changes and saves a document from a route of its own holds it across all three. A host's
        # page serves the editor's panel and the host's routes from a THREADED server, so two requests reach this one
        # Studio at once — measured before 2026-10-13: a host's history pick racing the panel's load died in `_scan`,
        # "dictionary changed size during iteration". Reentrant: a request's own `save` tells the host, which may ask
        # the editor again on the same thread.
        self.lock = threading.RLock()
        self._paths: Dict[str, Path] = {}
        self._documents: Dict[str, Document] = {}
        self._runs: Dict[str, Run] = {}
        # The graphs a host ships (``EditorHost.shipped``), by list name: the file each is shipped in, while the
        # folder holds no file of that name (its entry in ``_paths`` is where the copy would be written).
        self._shipped: Dict[str, Path] = {}

    # -- the folder --------------------------------------------------------------------------

    @property
    def graph_dir(self) -> Path:
        """The folder the editor serves: the one it was started on, or the one its host names NOW — asked at each
        use, so a host that moved (or copied) its folder since the last request is followed (:meth:`_follow`)."""
        if self.host is None:
            assert self._folder is not None  # _check_folder_or_host: a standalone editor has its folder
            return self._folder
        try:
            named = self.host.folder()
        except Exception as exc:  # noqa: BLE001 — whatever the host raised: the request's refusal, in words
            raise ValueError(f"{self.host.name} could not name its folder: {type(exc).__name__}: {exc}") from exc
        current = Path(named).expanduser().absolute()
        if self._folder is not None and current != self._folder:
            self._follow(self._folder, current)
        self._folder = current
        return current

    def follow(self) -> Path:
        """Ask the host where its folder is now and follow it there (every request does, first); answers it."""
        return self.graph_dir

    def _follow(self, old: Path, new: Path) -> None:
        """The host's folder moved from ``old`` to ``new``: every entry of the list under ``old`` names the same file
        under ``new``, and every document the editor holds moves with its unsaved edits and its undo history
        (:meth:`Document.moved`) — its Save then writes into ``new``. Entries and documents of other folders (File ▸
        Open) stay. Before 2026-10-01 the folder was fixed when the blueprint was registered, so after a host's
        first save moved its state folder a mounted editor would have listed and saved into the old one
        (``tests/test_host.py::TestTheHostsFolder``)."""
        for name, path in list(self._paths.items()):
            self._paths[name] = rebase_path(path, old, new)
        for document in self._documents.values():
            document.moved(old, new)

    # -- the graph list ----------------------------------------------------------------------

    def _scan(self) -> None:
        """Bring the registry up to date with the folder: a file that appeared is added under a free name, an
        entry whose file is gone is dropped — unless the editor holds it (open: its Save writes the file again) or the
        host ships it. A graph the host ships (:meth:`EditorHost.shipped`) is listed under its own name while no entry
        holds that name — its entry is ``<folder>/<name>.yaml``, where its copy would be written."""
        folder = self.graph_dir  # first: a host's folder that moved is followed before a file is missed
        self._shipped = self._host_ships()
        for name in [n for n, path in self._paths.items() if n not in self._documents and not path.is_file()]:
            if name not in self._shipped:
                del self._paths[name]
        known = {path.resolve() for path in self._paths.values()}
        if folder.is_dir():
            for path in sorted(folder.glob("*.yaml")):
                if _is_graph_file(path) and path.resolve() not in known:
                    self._paths[self._free_name(path.stem)] = path
                    known.add(path.resolve())
        for name in self._shipped:
            if name not in self._paths and (folder / f"{name}.yaml").resolve() not in known:
                self._paths[name] = folder / f"{name}.yaml"

    def _host_ships(self) -> Dict[str, Path]:
        """What the host ships (:meth:`EditorHost.shipped`) — ``{}`` for a standalone editor; a host that fails to say
        is a refusal in words naming it, like a folder it cannot name."""
        if self.host is None:
            return {}
        try:
            return {str(name): Path(path) for name, path in self.host.shipped().items()}
        except Exception as exc:  # noqa: BLE001 — whatever the host raised: the request's refusal, in words
            raise ValueError(
                f"{self.host.name} could not name the graphs it ships: {type(exc).__name__}: {exc}"
            ) from exc

    def shipped(self, name: str) -> bool:
        """Whether the graph ``name`` is the one its host ships — the folder holds no file of it yet."""
        held = self._documents.get(name)
        if held is not None:
            return held.is_shipped
        return name in self._shipped and not self._paths[name].is_file()

    def _free_name(self, stem: str) -> str:
        """``stem`` when no entry holds it, else ``stem~2``, ``stem~3`` … — two files of one stem side by side."""
        if stem not in self._paths:
            return stem
        index = 2
        while f"{stem}~{index}" in self._paths:
            index += 1
        return f"{stem}~{index}"

    def _entry_of(self, path: Path) -> Optional[str]:
        """The name of the entry that IS ``path`` (compared resolved), or ``None``."""
        wanted = path.resolve()
        return next((name for name, known in self._paths.items() if known.resolve() == wanted), None)

    def _absolute(self, given: str) -> Path:
        """A path the page sent: ``~`` expanded, a relative one read against the graph folder."""
        path = Path(given).expanduser()
        return path if path.is_absolute() else self.graph_dir / path

    def names(self) -> List[str]:
        """Every graph of the list, by name, sorted."""
        self._scan()
        return sorted(self._paths)

    def path_of(self, name: str) -> Path:
        """The file of the graph ``name``; a miss is a ``KeyError`` naming the graphs there are."""
        self.follow()
        if name not in self._paths:
            self._scan()
        if name not in self._paths:
            raise KeyError(f"no graph named {name!r} (the graphs are: {self.names()})")
        return self._paths[name]

    def document(self, name: str) -> Document:
        """The document of the graph ``name`` as the editor holds it — opened on first use, and READ AGAIN when it holds
        nothing unsaved but a file it spans says something else on disk now (:meth:`Document.changed_on_disk`): a host
        writes graph files itself (a conversion, an edit of its own source graph), and someone may edit one in another
        editor. Measured before 2026-10-01: the first text read was kept for the life of the process, so the panel, a
        reload and Run showed ``label: one`` after the file said ``label: two`` — and only an edit's Save found out,
        refused as changed on disk. A document holding unsaved work is never re-read: its Save is what refuses."""
        self.follow()
        held = self._documents.get(name)
        readable = held is not None and (held.path.is_file() or held.is_shipped)
        if held is not None and not held.dirty and readable and held.changed_on_disk():
            logger.debug(f"{held.path} changed on disk and the editor held nothing unsaved of it — read again")
            del self._documents[name]
        if name not in self._documents:
            path = self.path_of(name)
            if path.is_file():
                self._documents[name] = Document.open(path, history_depth=self.history_depth)
            elif name in self._shipped:
                shipped = self._shipped[name]
                self._documents[name] = Document.open_shipped(path, shipped, history_depth=self.history_depth)
            else:
                raise KeyError(f"no graph named {name!r}: its file {path} is gone (the graphs are: {self.names()})")
        return self._documents[name]

    def kind(self, name: str) -> GraphKind:
        """What the document's root says: a ``contract`` node, a ``flow:`` key, else a plain graph."""
        document = self.document(name)
        if any(node.kind == "contract" for node in document.model.nodes):
            return "contract"
        try:
            if YamlText(document.text).path("flow") is not None:
                return "flow"
        except yaml.YAMLError:
            pass  # a text that does not parse is a plain graph whose problem the model already carries
        return "graph"

    def dirty(self, name: str) -> bool:
        """Whether the editor holds changes of ``name`` its file does not (a graph never opened holds none)."""
        document = self._documents.get(name)
        return document is not None and document.dirty

    def graphs(self) -> List[Dict[str, Any]]:
        """``[{name, path, kind, dirty, shipped}]`` of every graph of the list — ``shipped``: still the graph its host
        ships, the folder holding no file of it."""
        return [
            {
                "name": name,
                "path": str(self.path_of(name)),
                "kind": self.kind(name),
                "dirty": self.dirty(name),
                "shipped": self.shipped(name),
            }
            for name in self.names()
        ]

    def graph(self, name: str) -> Dict[str, Any]:
        """``{name, path, kind, text, model, positions, groups, history_depth, dirty, shipped}`` of one graph —
        ``path`` is where its Save writes (for a shipped graph: where its copy would go)."""
        document = self.document(name)
        return {
            "name": name,
            "shipped": document.is_shipped,
            "path": str(document.path),
            "kind": self.kind(name),
            "text": document.text,
            "model": _model_dict(document.model),
            "positions": _positions_dict(document.positions),
            "groups": document.groups,
            "history_depth": document.history_depth,
            "dirty": document.dirty,
        }

    # -- new, open, the file browser ---------------------------------------------------------

    def new_graph(self, name: str, kind: NewGraphKind, directory: Optional[str] = None) -> Dict[str, Any]:
        """File ▸ New…: WRITE the starter of ``kind`` (:func:`starter_text`) to ``<directory or graph
        folder>/<name>.yaml`` at once (:meth:`Document.create`) and answer the graph, holding nothing unsaved
        (``dirty: false``) — there is never a graph that exists only in the editor (the user, 2026-09-29: "New ... by
        mistake should not happen. It should ask to save"). The file joins the list like one opened with File ▸ Open:
        a stem another entry holds gets a free name (``demo~2``).

        Refused before anything is written, in words: ``kind`` outside :data:`NewGraphKind`; a name the file-stem
        rule refuses; a folder that does not exist (404) or is not a folder; a file already there
        (:data:`~streamstudio.document.ALREADY_EXISTS`); a file an entry of the list holds while it is gone from disk
        (its Save writes it again); a folder that cannot be written.
        """
        if kind not in NEW_GRAPH_KINDS:
            raise ValueError(f"kind must be one of {', '.join(NEW_GRAPH_KINDS)} — got {kind!r}")
        _refuse_stem(name, "a graph file")
        folder = self._absolute(directory) if directory else self.graph_dir
        if not folder.exists():
            raise KeyError(f"{folder} does not exist — pick another folder")
        if not folder.is_dir():
            raise ValueError(f"{folder} is not a folder — pick another folder")
        path = folder / f"{name}.yaml"
        if path.exists():
            raise ValueError(ALREADY_EXISTS.format(path=path))
        self._scan()
        held = self._entry_of(path)
        if held is not None:
            raise ValueError(
                f"{path} is open as {held!r} — its file is gone from disk; Save that graph, or pick another name"
            )
        document = Document.create(path, starter_text(kind, name), history_depth=self.history_depth)
        entry = self._free_name(path.stem)
        self._paths[entry] = path
        self._documents[entry] = document
        return self._tell_host([path], self.graph(entry))  # a document write like a Save: the host hears it

    def open_graph(self, path: str) -> Dict[str, Any]:
        """Add a ``.yaml`` file from anywhere on this machine to the list and answer it — the entry that
        already IS that file when there is one, else a new entry named by its stem (made unique)."""
        target = self._absolute(path)
        if not target.is_file():
            raise KeyError(f"no file {target}")
        if target.name.endswith(SIDECAR_SUFFIX):
            raise ValueError(f"{target} is a layout sidecar, not a graph")
        if is_settings_file(target):
            raise ValueError(f"{target} is a graph's settings file, not a graph — open the graph beside it")
        if target.suffix != ".yaml":
            raise ValueError(f"{target} is not a .yaml file")
        self._scan()
        existing = self._entry_of(target)
        if existing is not None:
            return self.graph(existing)
        # Read BEFORE the entry exists: a file that cannot be read is refused naming it, and the list stays as it
        # was (measured before: the entry was registered first, so the unreadable file made every later
        # `GET /api/graphs` a 500).
        try:
            document = Document.open(target, history_depth=self.history_depth)
        except (OSError, UnicodeDecodeError) as exc:
            reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
            raise ValueError(f"{target} cannot be read: {reason}") from None
        name = self._free_name(target.stem)
        self._paths[name] = target
        self._documents[name] = document
        return self.graph(name)

    def files(self, directory: Optional[str] = None, show: FileShow = "graphs") -> Dict[str, Any]:
        """``{dir, parent, entries: [{name, path, kind}]}`` of one directory of this machine — sub-directories
        first, then the files ``show`` asks for, each sorted by name, hidden entries left out: ``graphs`` (File ▸
        Open's browser, a folder picker) lists the ``*.yaml`` files but the layout sidecars, ``all`` (a file picker)
        every file, as ``file``. A ``$VAR`` in ``directory`` is expanded — a path parameter spelled
        ``$DATA_ROOT/…`` opens its picker where the variable points; one this machine does not have is a ``KeyError``
        naming it (``DATA_ROOT is not set on the editor's machine ($DATA_ROOT/captures)``)."""
        if show not in FILE_SHOWS:
            raise ValueError(f"show must be one of {', '.join(FILE_SHOWS)} — got {show!r}")
        expanded = os.path.expandvars(directory) if directory else ""
        folder = self._absolute(expanded) if directory else self.graph_dir
        if not folder.is_dir():
            # a `$VAR` this machine does not have is named — measured before: the path was read against the graph
            # folder, "no directory <folder>/$DATA_ROOT/captures"; a folder really named `$x` is listed as any other
            unset = _UNSET_VARIABLE.search(expanded)
            if unset is not None:
                raise KeyError(f"{unset.group(1) or unset.group(2)} is not set on the editor's machine ({directory})")
            raise KeyError(f"no directory {folder}")
        folder = folder.resolve()
        # Every look at the directory's children is inside the try: read permission WITHOUT search permission
        # lists the names but cannot tell a file from a directory (measured before: `is_dir()` raised, a 500).
        try:
            children = [child for child in folder.iterdir() if not child.name.startswith(".")]
            dirs = sorted((child for child in children if child.is_dir()), key=lambda child: child.name.lower())
            if show == "all":
                listed = [_file_entry(child, "file") for child in children if child.is_file()]
            else:
                listed = [
                    _file_entry(child, "yaml")
                    for child in children
                    if child.is_file() and child.suffix == ".yaml" and not child.name.endswith(SIDECAR_SUFFIX)
                ]
        except PermissionError as exc:
            raise ValueError(f"{folder} cannot be read: {exc.strerror}") from None
        return {
            "dir": str(folder),
            "parent": None if folder.parent == folder else str(folder.parent),
            "entries": [_file_entry(d, "dir") for d in dirs] + sorted(listed, key=lambda entry: entry["name"].lower()),
        }

    # -- save, save as, revert, check ----------------------------------------------------------

    def _spelled(self, path: Path) -> Path:
        """``path`` as the host names it: under the folder EXACTLY as :meth:`EditorHost.folder` spelled it when the file
        lies in that folder (compared resolved — a macOS temporary folder is ``/var/…`` for ``/private/var/…``), else
        resolved. ONE spelling for every path the host is handed (``record``'s and ``saved``'s): measured before,
        ``record`` got ``<folder>/view_x.yaml`` and ``saved`` the resolved ``/private/…/view_x.yaml``, which is not
        under ``folder()`` — a host matching the two missed every Save of an unsaved workspace."""
        folder = self.graph_dir
        resolved = Path(path).resolve()
        if resolved.is_relative_to(folder.resolve()):
            return folder / resolved.relative_to(folder.resolve())
        return resolved

    def save(self, name: str, only: str = "") -> Dict[str, Any]:
        """Write what the editor holds of ``name`` (refused, writing nothing, over a file changed on disk or a file
        the disk will not take — :meth:`Document.save`), and tell the host which document files it wrote.

        ``only="settings"`` writes the graph's settings file alone (:meth:`Document.save_settings`) — what the page
        shown with ``?only=settings`` saves after each change; a change of the graph itself stays held."""
        if only and only not in SAVE_ONLY:
            raise ValueError(f"only must be one of {', '.join(SAVE_ONLY)} — got {only!r}")
        document = self.document(name)
        written = document.save_settings() if only == "settings" else document.save()
        return self._tell_host(written, self.graph(name))

    def _tell_host(self, written: Sequence[Path], answer: Dict[str, Any]) -> Dict[str, Any]:
        """Tell the host which document files a Save wrote (:meth:`EditorHost.saved`) — nothing when it wrote none.

        The files ARE written when the host hears of it, so a host that fails on the news does not make the Save
        look refused: the answer stays the saved graph and carries ``host_error`` in words, for the page to show."""
        if self.host is None or not written:
            return answer
        try:
            self.host.saved([self._spelled(path) for path in written])
        except Exception as exc:  # noqa: BLE001 — whatever the host raised: reported, the Save stands
            logger.warning(f"the host could not take the news of a save ({', '.join(map(str, written))}): {exc}")
            return {**answer, "host_error": f"{type(exc).__name__}: {exc}"}
        return answer

    def save_as(self, name: str, path: str, overwrite: bool = False) -> Dict[str, Any]:
        """Write ``name`` to another ``.yaml`` file and point its entry there; the old file stays as it was.

        The entry keeps its name when the new stem IS that name, else takes the new stem (made unique); the
        folder's old file comes back on the next list as a graph of its own. A file another entry already
        is, is refused — saving over it would leave that entry holding a copy the disk no longer has.
        """
        document = self.document(name)
        target = self._absolute(path)
        if target.name.endswith(SIDECAR_SUFFIX):
            raise ValueError(f"{target.name} is a layout sidecar's name — choose another")
        if target.name.endswith(STEPS_SUFFIX):
            raise ValueError(f"{target.name} is a steps file's name — choose another")
        if target.suffix != ".yaml":
            raise ValueError(f"a graph is saved as a .yaml file, not {target.name}")
        if not target.parent.is_dir():
            raise KeyError(f"no directory {target.parent} — the graph cannot be saved there")
        other = self._entry_of(target)
        if other is not None and other != name:
            raise ValueError(f"{target} is open as {other!r} — save that one, or choose another file")
        written = document.save_as(target, overwrite=overwrite)
        del self._paths[name]
        del self._documents[name]
        new_name = name if target.stem == name else self._free_name(target.stem)
        self._paths[new_name] = target
        self._documents[new_name] = document
        return self._tell_host(written, self.graph(new_name))

    def revert(self, name: str) -> Dict[str, Any]:
        """Drop everything the editor holds of ``name`` — edits, history, positions, groups — and read the
        file again."""
        document = self.document(name)
        if document.is_shipped:
            assert document.shipped is not None  # is_shipped says so
            reopened = Document.open_shipped(document.path, document.shipped, history_depth=self.history_depth)
            self._documents[name] = reopened
            return self.graph(name)
        if not document.path.is_file():
            raise ValueError(f"{document.path} is not on disk any more — Save writes it again")
        self._documents[name] = Document.open(document.path, history_depth=self.history_depth)
        return self.graph(name)

    def check(self, name: str) -> Dict[str, List[Dict[str, str]]]:
        """``{problems: [{message, loc}]}`` of what the editor holds — the Check graph item."""
        return {"problems": [asdict(problem) for problem in self.document(name).check()]}

    # -- unsaved work of an earlier session: the recovery file ---------------------------------

    @property
    def recovery_path(self) -> Path:
        """``<graph folder>/.streamstudio-workspace.json`` (:data:`RECOVERY_FILE`)."""
        return self.graph_dir / RECOVERY_FILE

    def _read_recovery(self) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:  # the file's JSON, checked
        """``(content, None)`` — its paths rebased onto this folder when the folder moved since (:func:`_rebased`) — or
        ``(None, why it cannot be used)`` — the path and the reason, in words — or ``(None, None)`` when there is no
        file. Never raises: a broken file must not stop the editor starting."""
        path = self.recovery_path
        if not path.is_file():
            return None, None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:  # unreadable, not UTF-8 (a ValueError), not JSON (a ValueError)
            reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else " ".join(str(exc).split())
            return None, f"{path} cannot be read: {reason}"
        try:
            _check_recovery(data)
        except ValueError as exc:
            return None, f"{path} is not a recovery file the editor wrote: {exc}"
        return _rebased(data, self.graph_dir), None

    def _aside_path(self) -> Path:
        """Where an unreadable recovery file goes now: the first free :func:`_aside_name` of the folder."""
        number = 1
        while (self.graph_dir / _aside_name(number)).exists():
            number += 1
        return self.graph_dir / _aside_name(number)

    def workspace(self) -> Dict[str, Any]:
        """``{folder, session, recovery, host}`` — ``host`` is ``{name}`` for an editor mounted with a host
        (:class:`EditorHost`), ``None`` for a standalone one; ``session`` is this editor's (:data:`SESSION_HEADER`);
        ``recovery`` is ``None`` without a recovery file, else ``{saved_at, graphs, error, aside}``: per graph ``{name,
        path, disk, reason, changed_on_disk, saved_at}`` — every kept graph is a file with changes (New… writes its file
        at once, so no graph exists only in the editor, and a row needs no state); ``disk`` what the disk holds of the
        files a Save would write, a :data:`~streamstudio.document.DiskState` (``changed``: Save will refuse it;
        ``gone``: the file was deleted or moved after the stop, Save writes it again; ``unreadable``: ``reason`` names
        the file and why);
        ``changed_on_disk`` is ``disk == "changed"``; ``saved_at`` when THAT graph was kept (two versions of one file,
        kept by two stops, are two rows). For a file that cannot be used: no graphs, ``error`` saying why with its
        path, and ``aside`` — where *discard* moves it. Never raises: it is what a start prints from."""
        data, error = self._read_recovery()
        recovery: Optional[Dict[str, Any]] = None
        if error is not None:
            recovery = {"saved_at": None, "graphs": [], "error": error, "aside": str(self._aside_path())}
        elif data is not None:
            rows = []
            for entry in data["graphs"]:
                disk, reason = Document.from_held(entry, history_depth=self.history_depth).disk_state()
                rows.append(
                    {
                        "name": entry["name"],
                        "path": entry["path"],
                        "disk": disk,
                        "reason": reason,
                        "changed_on_disk": disk == "changed",
                        "saved_at": entry["saved_at"],
                    }
                )
            recovery = {"saved_at": data["saved_at"], "graphs": rows, "error": None, "aside": None}
        host = None if self.host is None else {"name": str(self.host.name)}
        return {"folder": str(self.graph_dir), "session": self.session, "recovery": recovery, "host": host}

    def restore_recovery(self) -> List[Dict[str, Any]]:
        """*Restore unsaved work*: every graph of the recovery file opens as it was when the editor stopped — its
        texts, positions and groups, unsaved (nothing is written until Save; a file changed on disk since gets
        Save's changed-on-disk refusal) and with no undo history — and the file is removed. A graph of another
        folder rejoins the list; a name taken meanwhile gets a free one (``away~2``).

        No version is ever dropped: a file this editor holds unsaved edits of, or a file kept twice (two stops of
        two editors), opens a SECOND entry beside the first (``demo~2``) — the Save of whichever is saved second
        gets the changed-on-disk refusal. Measured before: such a restore was refused ("Save or Revert them first")
        while the page's question offered only discarding. Answers the graph list, each entry with ``restored``."""
        with _folder_lock(self.graph_dir):
            data, error = self._read_recovery()
            if error is not None:
                raise ValueError(error)
            if data is None:
                raise KeyError(f"no unsaved work of an earlier session in {self.graph_dir}")
            self._scan()
            restored: List[str] = []
            for entry in data["graphs"]:
                path = Path(entry["path"])
                existing = self._entry_of(path)
                if existing is not None and existing not in restored and not self.dirty(existing):
                    name = existing  # the entry that IS the file, holding nothing unsaved: it takes the kept work
                elif entry["name"] not in self._paths:
                    name = entry["name"]
                else:
                    name = self._free_name(path.stem)
                self._paths[name] = path
                self._documents[name] = Document.from_held(entry, history_depth=self.history_depth)
                restored.append(name)
            self.recovery_path.unlink()
        return [{**graph, "restored": graph["name"] in restored} for graph in self.graphs()]

    def discard_recovery(self) -> List[Dict[str, Any]]:
        """*Open the saved files*: the recovery file is removed — or, when it cannot be used, moved aside to the first
        free :func:`_aside_name` (the ``aside`` the workspace named), never deleted. Answers the graph list."""
        with _folder_lock(self.graph_dir):
            path = self.recovery_path
            if not path.is_file():
                raise KeyError(f"no unsaved work of an earlier session in {self.graph_dir}")
            _data, error = self._read_recovery()
            if error is not None:
                path.replace(self._aside_path())
            else:
                path.unlink()
        return self.graphs()

    def stash(self) -> List[str]:
        """Keep the unsaved work for the next start — what the editor does as it STOPS (``streamstudio stop`` /
        ``restart``, Ctrl+C; ``streamstudio.cli``; a host that mounts the editor calls it on its own stop,
        ``studio_of(app).stash()``) — and answer the paths of the graphs THIS stop kept.

        Writes :data:`RECOVERY_FILE` only when a graph holds unsaved work (:meth:`Document.held` of each, the
        files of other folders included, each with the time it was kept); with nothing unsaved it writes nothing. A
        recovery file nobody answered yet is never lost: every graph of it stays in the new file — a file this
        session changed too is then kept TWICE, both versions offered (measured before: this session's replaced the
        earlier one, so a second editor's stop, or a tab left open across a restart, lost the first for good) — and
        one that cannot be read is moved aside first. The read-merge-write runs under the folder's lock
        (:func:`_folder_lock`), through a file of this stop's own (:func:`_writing_name`) renamed over the old one:
        two editors stopped at once keep both their work, and a stop cut short leaves no half-written file. A kill -9
        or a crash cannot write it — there is no periodic autosave. Waits for a request being served (:attr:`lock`): it
        keeps what that request leaves, never half of it."""
        with self.lock:
            return self._stash()

    def _stash(self) -> List[str]:
        """:meth:`stash`, run while :attr:`lock` is held."""
        self.follow()  # a host's folder that moved since the last request: keep the work where the host now is
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        entries = [
            {"name": name, "saved_at": now, **document.held()}
            for name, document in self._documents.items()
            if document.dirty
        ]
        if not entries:
            return []
        kept = [str(entry["path"]) for entry in entries]
        with _folder_lock(self.graph_dir):
            earlier, error = self._read_recovery()
            if error is not None:
                self.recovery_path.replace(self._aside_path())
            elif earlier is not None:
                entries += earlier["graphs"]
            data = {"folder": str(self.graph_dir), "saved_at": now, "graphs": entries}
            writing = self.graph_dir / _writing_name()
            try:
                writing.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
                writing.replace(self.recovery_path)
            finally:
                if writing.exists():
                    writing.unlink()
        return kept

    # -- editing -----------------------------------------------------------------------------

    def replace_text(self, name: str, text: str) -> Dict[str, Any]:
        """Make ``text`` the graph's document in the editor: checked as a whole, held only when it gains no
        problem over the current text; the undo history starts over. Save writes it."""
        self.document(name).replace_text(text)
        return self.graph(name)

    def set_param(self, name: str, node: str, param: str, value: Any) -> Dict[str, Any]:  # value: any YAML scalar
        self.document(name).set_param(node, param, value)
        return self.graph(name)

    def set_slot(self, name: str, node: str, slot: str, class_path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Put a new ``class_path`` object, built from ``params``, into the single slot ``slot`` of ``node`` — what
        it held is replaced (:meth:`Document.set_slot`; one edit). The canvas's *set source…*. Answers the graph plus
        ``set``: the name of the node now in the slot (``stream.source``)."""
        made = self.document(name).set_slot(node, slot, class_path, params)
        return {**self.graph(name), "set": made}

    def add_wire(self, name: str, source: str, source_socket: str, target: str, target_socket: str) -> Dict[str, Any]:
        """Save a wire drawn on the canvas in the document's own spelling (:meth:`Document.add_wire`; one edit, held
        until Save). Answers the graph plus ``wired``: the name of the card the wire comes from now — ``source`` for a
        ``stream.source`` that was moved out of its slot to a top-level key, since a ``!ref:`` names only those."""
        wired = self.document(name).add_wire(source, source_socket, target, target_socket)
        return {**self.graph(name), "wired": wired}

    def remove_wires(self, name: str, wires: Sequence[WireEnds]) -> Dict[str, Any]:
        """Take wires out as ONE edit — all or none, one undo entry (Delete on selected wires;
        :meth:`Document.remove_wires`)."""
        self.document(name).remove_wires(wires)
        return self.graph(name)

    def add_step(
        self,
        name: str,
        step: Optional[str],
        after: Optional[str],
        class_path: str,
        params: Dict[str, Any],
        into: Optional[str] = None,
        wire: Optional[WireEnds] = None,
    ) -> Dict[str, Any]:
        """Add ``class_path`` after the node ``after`` — a flow step named ``step``, or a member of an op list
        (``stream.ops[1]``) — or, with no ``after``, at the end of the op list ``into`` (``stream.ops``); one edit. A
        class that is not an op is a card of its own instead (:meth:`add_node`, with the dropped ``wire``). Answers the
        graph plus ``added``: the new node's name (a member's is its position, ``stream.ops[2]``)."""
        added = self.document(name).add_step(step or "", after, class_path, params, into=into, wire=wire)
        return {**self.graph(name), "added": added}

    def add_node(
        self, name: str, class_path: str, params: Dict[str, Any], wire: Optional[WireEnds] = None
    ) -> Dict[str, Any]:
        """Add ``class_path`` — a class that is not an op — as a CARD of its own: a new top-level key named after its
        class, above the graph's root (:meth:`Document.add_node`); with ``wire``, the wire dropped on empty canvas is
        saved to it in the same edit. Answers the graph plus ``added``: the card's name (``class_names_scan``)."""
        added = self.document(name).add_node(class_path, params, wire=wire)
        return {**self.graph(name), "added": added}

    def remove_step(self, name: str, step: str) -> Dict[str, Any]:
        self.document(name).remove_step(step)
        return self.graph(name)

    def remove_steps(self, name: str, steps: Sequence[str]) -> Dict[str, Any]:
        """Remove several steps as ONE edit — all or none, one undo entry (Cut, Delete of a selection)."""
        self.document(name).remove_steps(steps)
        return self.graph(name)

    def rename(self, name: str, node: str, new: str) -> Dict[str, Any]:
        self.document(name).rename_node(node, new)
        return self.graph(name)

    def undo(self, name: str) -> Dict[str, Any]:
        """Step back one edit; ``changed`` says whether there was one, and ``shifts`` how the names of op list members
        moved (:meth:`_stepped`)."""
        document = self.document(name)
        changed = document.undo()
        return {**self.graph(name), "changed": changed, "shifts": self._stepped(document)}

    def redo(self, name: str) -> Dict[str, Any]:
        """Step forward one undone edit — the answer of :meth:`undo`."""
        document = self.document(name)
        changed = document.redo()
        return {**self.graph(name), "changed": changed, "shifts": self._stepped(document)}

    @staticmethod
    def _stepped(document: Document) -> List[Dict[str, Any]]:
        """``[{list, first, by}]``: how the undo or redo just made renamed op list members, in order — ``list[i]`` with
        ``i >= first`` became ``list[i + by]`` (and every name under it). A member is named by its position, so a page
        showing the inside of ``stream.ops[0]`` follows it to ``stream.ops[1]`` when an undo puts a member back before
        it (measured before: the canvas stayed on the name and showed the OTHER subgraph under the same breadcrumb)."""
        return [{"list": owner, "first": first, "by": by} for owner, first, by in document.stepped_shifts]

    # -- subgraphs ---------------------------------------------------------------------------

    def convert_to_subgraph(self, name: str, nodes: Sequence[str], subgraph: str) -> Dict[str, Any]:
        """*Convert selection to subgraph* (one edit); the graph plus ``subgraph`` — the new node's name."""
        made = self.document(name).convert_to_subgraph(nodes, subgraph)
        return {**self.graph(name), "subgraph": made}

    def extract_steps(self, name: str, node: str, path: Optional[str] = None) -> Dict[str, Any]:
        """*Move to its own file*: the subgraph ``node``'s steps go to ``path`` (default: the graph's folder,
        ``<node>.steps.yaml`` from the node name's last part), held until Save; the graph answers."""
        document = self.document(name)
        if path:
            target = self._absolute(path)
        else:
            stem = _FILE_UNSAFE.sub("_", node.rpartition(SEPARATOR)[2])
            target = document.path.parent / f"{stem}{STEPS_SUFFIX}"
        document.extract_steps(node, target)
        return self.graph(name)

    def include_steps(self, name: str, path: str, after: str, step: Optional[str] = None) -> Dict[str, Any]:
        """*Insert steps file*: a new subgraph step after ``after`` whose ``steps:`` includes ``path``."""
        self.document(name).include_steps(after, self._absolute(path), step)
        return self.graph(name)

    def insert_template(self, name: str, template: str, after: str) -> Dict[str, Any]:
        """*Insert template*: a COPY of the template's steps as a new subgraph step after ``after``, named after
        the template (made unique) — nothing links back to the template."""
        path = self.templates_dir / f"{template}{STEPS_SUFFIX}"
        if not path.is_file():
            known = [entry["name"] for entry in self.templates()]
            raise KeyError(f"no template named {template!r} (the templates are: {known})")
        self.document(name).insert_steps(after, path.read_text(), template)
        return self.graph(name)

    # -- the clipboard -----------------------------------------------------------------------

    def copy(self, name: str, nodes: Sequence[str]) -> Dict[str, str]:
        """``{text, kind}``: the clipboard text of ``nodes`` (flow steps or list members)."""
        text, kind = self.document(name).copy_text(nodes)
        return {"text": text, "kind": kind}

    def paste(self, name: str, text: str, after: str) -> Dict[str, Any]:
        """Insert a clipboard text after ``after`` (one edit); the graph plus ``pasted`` — the new nodes' names."""
        pasted = self.document(name).paste_text(text, after)
        return {**self.graph(name), "pasted": pasted}

    # -- positions ---------------------------------------------------------------------------

    def set_positions(self, name: str, positions: Mapping[str, Any]) -> Dict[str, PositionList]:
        """Hold node positions (the sidecar is written by Save); answers every node's position.

        The answer stays the bare ``{node: [x, y]}`` map the page reads — a ``dirty`` key there could
        collide with a node named ``dirty``; the graph's ``dirty`` is in every graph answer and the list.
        """
        document = self.document(name)
        document.set_positions(_positions_from_json(positions, document.model))
        return _positions_dict(document.positions)

    def relayout(self, name: str) -> Dict[str, PositionList]:
        """Lay the graph out afresh (the deterministic first layout) in the editor — every level on its own,
        each subgraph's inside included; Save writes the sidecar."""
        document = self.document(name)
        laid: Dict[str, Tuple[float, float]] = {}
        for level in document.model.levels():
            laid.update(layout.layered_layout(level))
        document.set_positions(laid)
        return _positions_dict(document.positions)

    # -- groups ------------------------------------------------------------------------------

    def set_groups(self, name: str, groups: Any) -> Dict[str, Any]:  # groups: the request's JSON
        """Replace the graph's groups (the sidecar's ``groups:`` block, written by Save); ``{groups, dirty}``.

        Only the shape is checked — a list of JSON objects; what a group holds is the page's, stored as
        sent (``streamstudio.layout.Group``). The document is never touched: it has no group spelling.
        """
        document = self.document(name)
        if not isinstance(groups, list) or not all(isinstance(group, dict) for group in groups):
            raise ValueError(
                f"{name}.yaml: 'groups' must be a list of group objects ({{id, title, color, nodes}}), got {groups!r}"
            )
        document.set_groups(groups)
        return {"groups": document.groups, "dirty": document.dirty}

    # -- templates ---------------------------------------------------------------------------

    def _template_entry(self, path: Path) -> Dict[str, Any]:
        text = path.read_text()
        return {
            "name": path.name[: -len(STEPS_SUFFIX)],
            "path": str(path),
            "steps": _step_names(text),
            "doc": _first_comment(text),
        }

    def templates(self) -> List[Dict[str, Any]]:
        """``[{name, path, steps, doc}]`` of every ``*.steps.yaml`` in the templates folder, by name."""
        if not self.templates_dir.is_dir():
            return []
        found = sorted(
            path
            for path in self.templates_dir.glob(f"*{STEPS_SUFFIX}")
            if path.is_file() and not path.name.startswith(".")
        )
        return [self._template_entry(path) for path in found]

    def save_template(self, name: str, graph: str, nodes: Sequence[str], overwrite: bool = False) -> Dict[str, Any]:
        """Write ``nodes`` of ``graph`` as the template ``name`` AT ONCE — a template belongs to no graph, so
        no Save and the graph stays as it was. One subgraph step saves its inner steps; flow steps save
        themselves. The file's first line says where they came from (it is the template's ``doc``)."""
        _refuse_stem(name, "a template")
        document = self.document(graph)
        text = document.template_text(nodes)
        path = self.templates_dir / f"{name}{STEPS_SUFFIX}"
        if path.exists() and not overwrite:
            raise ValueError(f"a template named {name!r} exists — pass overwrite: true to replace it")
        if not self.templates_dir.is_dir():
            self.templates_dir.mkdir(parents=True)
        path.write_text(f"# {name}: {', '.join(_step_names(text))} — saved from {document.path}\n{text}")
        return self._template_entry(path)

    def delete_template(self, name: str) -> List[Dict[str, Any]]:
        """Remove the template ``name``; answers the templates left."""
        path = self.templates_dir / f"{name}{STEPS_SUFFIX}"
        if not path.is_file():
            known = [entry["name"] for entry in self.templates()]
            raise KeyError(f"no template named {name!r} (the templates are: {known})")
        path.unlink()
        return self.templates()

    # -- the catalogue -----------------------------------------------------------------------

    def catalogue(self, refresh: bool = False) -> List[Dict[str, Any]]:
        """Every class the editor can describe, without its settings — built by a helper process and waited for here
        (:meth:`streamstudio.catalogue.Catalogue.entries`); ``refresh`` runs a new helper."""
        return catalogue_module.CATALOGUE.entries(refresh)

    def class_spec(self, class_path: str) -> Dict[str, Any]:
        """One class with its settings, asked when someone selects it
        (:meth:`streamstudio.catalogue.Catalogue.spec`)."""
        return catalogue_module.CATALOGUE.spec(class_path)

    # -- the settings form -------------------------------------------------------------------

    def form(self, name: str) -> Dict[str, Dict[str, Any]]:
        """``{node: params_schema}`` for every node of every level (``prep/grey`` inside a subgraph) — the
        JSON-schema properties a form renders, a ``picker`` marked on a path / file / HuggingFace parameter
        (:func:`streamstudio.palette.picker_of`); a synthetic node (the host's side, a subgraph's boundary) and a
        class the palette cannot describe get ``{}`` (the model reports why). The gate a branch sets
        (``requires``) is never a field: it is drawn as the gate wire, not typed."""
        form: Dict[str, Dict[str, Any]] = {}
        for node in (node for level in self.document(name).model.levels() for node in level.nodes):
            try:
                schema = palette.spec_for(node.class_path).params_schema if node.class_path else {}
            except KeyError:
                schema = {}
            form[node.name] = {param: fragment for param, fragment in schema.items() if param != GATE_PARAM}
        return form

    def app_form(self, name: str) -> Dict[str, Any]:
        """The graph's SETTINGS: the fields its ``app:`` block lists — ``{declared, graph, fields, form, problems}``.

        ``app.inputs`` lists ``[Class, param]`` pairs; the class is
        the short or the dotted name of a node's class. ``fields`` is one ``{node, param, loc}`` per node of that
        class, in the block's order (``loc`` is the entry's ``file:line:col``); ``<param>__lo`` / ``<param>__hi`` —
        a range written as its two halves — name the ONE field ``<param>``. ``form`` holds those fields in
        :meth:`form`'s shape. An entry naming no node, no parameter of its class, or the gate (``requires``, never a
        field) is a located problem in words — the block is read from the text the editor holds — and so is one
        naming a class two nodes with no name share, which no setting can reach (``streamstudio.settings``): it gives
        no field. ``graph`` is what the graph lets a person change (``editable``, or ``settings-only`` from an
        ``app: {graph: settings-only}`` line; a word it does not know is a problem). A graph with no ``app:`` inputs
        answers ``declared: false`` and no field."""
        document = self.document(name)
        settings = document.declared()
        problems = [{"message": message, "loc": loc} for message, loc in settings.problems]
        if not settings.declared:
            return {"declared": False, "graph": settings.mode, "fields": [], "form": {}, "problems": problems}
        full = self.form(name)
        narrowed: Dict[str, Dict[str, Any]] = {}
        for field in settings.fields:
            narrowed.setdefault(field.node, {})[field.param] = full.get(field.node, {}).get(field.param, {})
        fields = [{"node": field.node, "param": field.param, "loc": field.loc} for field in settings.fields]
        return {"declared": True, "graph": settings.mode, "fields": fields, "form": narrowed, "problems": problems}

    # -- the HuggingFace pickers --------------------------------------------------------------

    @staticmethod
    def hf_datasets() -> Dict[str, Any]:
        """``{datasets, reason}``: the dataset ids this machine's HuggingFace cache holds, and — when there are none
        — why, in words (``streamstudio.cached_hf_datasets``; an empty list that explains nothing looks broken)."""
        datasets, reason = streamstudio.cached_hf_datasets()
        return {"datasets": datasets, "reason": reason}

    @staticmethod
    def hf_features(path: str, name: Optional[str] = None, split: Optional[str] = None) -> Dict[str, Any]:
        """``{features, all_columns}``: the columns of the dataset ``path`` (its subset ``name``, its ``split``) —
        ``[]`` without a dataset, or when it cannot be read — and the word a ``metadata_features`` list writes for
        "every column but the input and the target" (recordstream's own, ``METADATA_ALL_FEATURES``)."""
        from recordstream.sources.huggingface import METADATA_ALL_FEATURES  # ~110 ms on first use, never at import

        features = streamstudio.list_hf_dataset_features(path, name=name, split=split) if path else []
        return {"features": features, "all_columns": METADATA_ALL_FEATURES}

    @staticmethod
    def hf_splits(path: str, name: Optional[str] = None) -> Dict[str, List[str]]:
        """``{splits}``: the split names of the dataset ``path`` (its subset ``name``); ``[]`` without a dataset, or
        when they cannot be read."""
        return {"splits": streamstudio.list_hf_dataset_splits(path, name=name) if path else []}

    @staticmethod
    def lookup(tag: str, values: Mapping[str, str]) -> Dict[str, Any]:
        """``{names, reason}``: the list an installed plugin supplies for the picker ``tag``, asked with the node's
        current ``values`` (:func:`streamstudio.lookups.answer` — never raises; an empty list says why)."""
        return lookups.answer(tag, values)

    # -- runs --------------------------------------------------------------------------------

    def run(self, run_id: str) -> Run:
        with self.lock:
            if run_id not in self._runs:
                raise KeyError(f"no run {run_id!r} (the runs are: {sorted(self._runs)})")
            return self._runs[run_id]

    def trace(self, name: str, record: int = 0, until: Optional[str] = None) -> Dict[str, Any]:
        """Run record ``#record`` of the graph's own source through its stream, pausing before ``until`` — or, for a
        graph with no source of its own, the record its host supplies (:meth:`_host_trace`; ``record`` does not
        apply). Without a host such a graph is refused in words."""
        if record < 0:
            raise ValueError(f"{name}.yaml: record must be 0 or more, got {record}")
        # The document is read and built in the editor's turn; drawing the record and the run happen outside it (they
        # touch only the objects just built, and may take minutes — every other request would wait behind them).
        with self.lock:
            document = self.document(name)
            model = document.model
            if not model.nodes and model.problems:
                raise ValueError(model.problems[0].message)
            stream_node = _fed_stream(model, f"{name}.yaml")
            if stream_node is None:
                if self.host is None:
                    raise ValueError(_no_source(f"{name}.yaml"))
                graph, prefix = _host_graph(_built(document), document.model, f"{name}.yaml")
                where = str(document.path) if not prefix else f"{document.path}:{prefix[:-1]}"
                spelled = self._spelled(document.path)
            else:
                built = _built(document)
                stream = _object_at(built, stream_node.name)
                if not isinstance(stream, Stream) or stream.source is None:
                    raise ValueError(_no_source(f"{name}.yaml"))
                where = f"{document.path}:{stream_node.name}"
            run_id = self._new_run_id()
        if stream_node is None:
            return self._host_trace(name, run_id, graph, prefix, where, spelled, until)
        drawn = list(itertools.islice(iter(stream.source), record + 1))
        if not drawn:
            raise ValueError(self._empty_source(name, stream_node.name, stream.source, record))
        if len(drawn) <= record:
            raise ValueError(f"{name}.yaml: the source yields only {len(drawn)} records — there is no record #{record}")
        run = Run(run_id, name, record, Tracer(stream, where=where), f"{stream_node.name}.", where)
        return self._start(run, drawn[record], until)

    def _empty_source(self, name: str, stream: str, source: Any, record: int) -> str:  # source: any record source
        """The refusal of a Run whose own source yields NOTHING — in a host, typically a source the host fills when IT
        runs the graph (the files a workspace hands its files graph), which the editor is never handed. Measured before:
        ``the source yields only 0 records — there is no record #0``, a cause nobody can act on."""
        kind = type(source).__name__
        said = f"{name}.yaml: {stream}.source ({kind}) yields no records, so there is no record #{record} to run"
        remedy = "spell what the source reads (a folder, files) to run it here"
        if self.host is None:
            return f"{said} — {remedy}"
        host = self.host.name
        return (
            f"{said}. The editor runs a source as the document spells it — what {host} fills into it when {host} runs "
            f"this graph is not handed to the editor; {remedy}"
        )

    def _host_trace(
        self, name: str, run_id: str, graph: Any, prefix: str, where: str, spelled: Path, until: Optional[str]
    ) -> Dict[str, Any]:  # graph: the built flow or Stream of the document, as `_host_graph` answers it
        """Run the record the host supplies (:meth:`EditorHost.record`) through ``graph`` — built from the document
        as the editor holds it (:meth:`trace`): a ``flow:``'s steps, or the ops of its one ``Stream`` — with the host's
        fills set on the ops first (:class:`HostRecord`); every node is named as the document names it, so a pause, a
        step and a rerun work as for a graph's own record. Runs outside the editor's turn: the host may take long."""
        assert self.host is not None
        supplied = _host_record(self.host, name, spelled, where)
        filled = _fill(_top_ops(graph, prefix), supplied.fills, where)
        tracer = Tracer(graph, where=where)
        run = Run(run_id, name, None, tracer, prefix, where, {"label": supplied.label, "filled": filled})
        return self._start(run, dict(supplied.record), until)

    def _start(self, run: Run, record: Any, until: Optional[str]) -> Dict[str, Any]:  # record: what the source yields
        """Run ``record`` through the run's tracer, pausing before ``until``, keep the run and answer it."""
        tracer = run.tracer
        engine_until = None if until is None else run.engine_name(until)
        try:
            tracer.run(record, until=engine_until)
        except ChainContractError as refusal:  # nothing ran: the chain check refused
            raise ValueError(str(refusal)) from refusal
        except (
            Exception
        ) as exc:  # noqa: BLE001 — a node raised while running (recorded), or the engine refused the graph
            if "error" not in _statuses(tracer).values():
                raise ValueError(f"{run.where}: {type(exc).__name__}: {exc}") from exc
        with self.lock:
            self._runs[run.run_id] = run
        return run.to_dict()

    def rerun(self, run_id: str, node: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Run the node again with ``params`` — the run's own objects, outside the editor's turn (:meth:`run` looks
        the run up in it)."""
        run = self.run(run_id)
        run.rerun(node, params)
        return run.to_dict()

    def _new_run_id(self) -> str:
        while True:
            run_id = f"run_{secrets.token_hex(_RUN_ID_BYTES)}"
            if run_id not in self._runs:
                return run_id


def _host_record(host: EditorHost, name: str, path: Path, where: str) -> HostRecord:
    """What ``host.record(name, path)`` answers, checked — every way it can go wrong is the run's refusal in words
    naming the host, never an HTML 500 (measured before: a ``LookupError`` or a dict answered werkzeug's page, a
    ``KeyError`` a 404 saying ``'rec_0042'``, a ``None`` record ``'NoneType' object is not iterable``). A ``ValueError``
    is the host's own refusal and is answered as written — the contract's way to say "no record is selected"."""
    try:
        supplied = host.record(name, path)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 — whatever the host raised: the run's refusal, naming it
        raise ValueError(f"{where}: {host.name} could not give a record: {type(exc).__name__}: {exc!s}") from exc
    if not isinstance(supplied, HostRecord):
        raise ValueError(f"{where}: {host.name} answered {_kind_name(supplied)}, not a HostRecord")
    if not isinstance(supplied.record, Mapping):
        raise ValueError(
            f"{where}: the record {host.name} gave is {_kind_name(supplied.record)}, not a mapping of entries"
        )
    for group in supplied.fills:
        if not isinstance(group, Mapping):
            raise ValueError(
                f"{where}: the fills {host.name} gave hold {_kind_name(group)}, not a mapping of attribute → value"
            )
    return supplied


def _kind_name(value: Any) -> str:  # any value a host may answer
    """How a refusal names what a host answered: ``None``, ``a dict``, ``a tuple``, ``an IndexError`` …"""
    if value is None:
        return "None"
    kind = type(value).__name__
    return f"{'an' if kind[:1].lower() in 'aeiou' else 'a'} {kind}"


def _no_source(where: str) -> str:
    return (
        f"{where}: this graph has no source to draw a record from — "
        "a host supplies the record (mount the editor in one) or wire a source"
    )


def _fed_stream(model: Model, where: str) -> Optional[Node]:
    """The ONE engine node whose ``source`` slot is wired — the stream a trace draws its record from — or ``None``
    when no stream draws its own records (the graph runs the record a host supplies)."""
    fed = {wire.target for wire in model.wires if wire.target_socket == "source"}
    streams = [node for node in model.nodes if node.kind == "engine" and node.name in fed]
    if len(streams) > 1:
        names = [node.name for node in streams]
        raise ValueError(f"{where}: several streams draw records ({names}) — tracing one of several is not supported")
    return streams[0] if streams else None


def _built(document: Document) -> Any:  # the built document: a mapping of top-level objects, or the root object
    """The document BUILT from the texts the editor holds (:meth:`Document.load`) — an unsaved value is the value
    that runs; confluid's refusal to build is answered in its words."""
    try:
        return document.load()
    except Exception as exc:  # noqa: BLE001 — confluid's located refusal to build, answered in its words
        raise ValueError(f"{document.path}: does not build: {type(exc).__name__}: {exc}") from exc


def _host_graph(built: Any, model: Model, where: str) -> Tuple[Union[Stream, FlowGraph], str]:  # built: as _built
    """What a host's record runs through, and the prefix the document puts before the engine's node names: the
    ``flow:`` of a flow document (its steps keep their keys, ``""``), else the document's one ``Stream`` (its ops
    are ``<stream>.ops[i]``). Refused in words when there is neither, or several streams."""
    if isinstance(built, dict) and isinstance(built.get("flow"), dict):
        return FlowGraph(flow=built["flow"], outputs=str(built.get("outputs") or "")), ""
    engines = [node for node in model.nodes if node.kind == "engine"]
    if len(engines) > 1:
        names = [node.name for node in engines]
        raise ValueError(f"{where}: several streams ({names}) — a host's record runs through one")
    stream = _object_at(built, engines[0].name) if engines else None
    if not isinstance(stream, Stream):
        raise ValueError(
            f"{where}: nothing here runs a record — a host's record runs through a flow: or a Stream's ops"
        )
    return stream, f"{engines[0].name}."


def _top_ops(graph: Union[Stream, FlowGraph], prefix: str) -> List[Tuple[str, Any]]:  # (node name, op of any class)
    """The ops a host's fills reach, by their document names: a flow's steps, a Stream's ``ops`` members."""
    if isinstance(graph, FlowGraph):
        return [(step.name, step.op) for step in graph.steps if step.op is not None]
    return [(f"{prefix}ops[{index}]", op) for index, op in enumerate(graph.ops)]


def _fill(
    ops: Sequence[Tuple[str, Any]], fills: Sequence[Mapping[str, Any]], where: str
) -> Dict[str, Dict[str, Any]]:  # ops: (node name, op of any class); a fill value is whatever the op takes
    """Set each fill group on every op holding ALL its attributes (:class:`HostRecord`) and answer what was set, per
    node, as JSON; an op refusing a value (a read-only attribute, a validating setter) is a located refusal."""
    filled: Dict[str, Dict[str, Any]] = {}
    for name, op in ops:
        for group in fills:
            if not group or not all(hasattr(op, attribute) for attribute in group):
                continue
            for attribute, value in group.items():
                try:
                    setattr(op, attribute, value)
                except (AttributeError, TypeError, ValueError) as exc:
                    raise ValueError(f"{where}:{name}: the host's {attribute} = {value!r} is refused: {exc}") from exc
            filled.setdefault(name, {}).update({attribute: _plain(value) for attribute, value in group.items()})
    return filled


def _object_at(
    built: Any, name: str
) -> Any:  # the built document (a mapping or a root object) → the object a node names
    """Walk the built document by a node's name: a top-level key, then attributes, ``[i]`` indexing a list."""
    current = built
    segments = name.split(".")
    if not isinstance(current, dict):
        segments = segments[1:]  # a root marker: the object itself is the first segment
    for segment in segments:
        match = _SEGMENT.match(segment)
        if match is None:
            raise ValueError(f"cannot follow the node name {name!r} into the built document (at {segment!r})")
        key, index = match.group(1), match.group(2)
        current = current[key] if isinstance(current, dict) else getattr(current, key)
        if index is not None:
            current = current[int(index)]
    return current


def _statuses(tracer: Tracer) -> Dict[str, str]:
    try:
        return dict(tracer.statuses)
    except Exception:  # noqa: BLE001 — the plan itself failed: nothing has a status
        return {}


# ================================================================================================
# the blueprint
# ================================================================================================

#: The editor routes that take no turn (:attr:`Studio.lock`): each waits on something outside the editor — the class
#: catalogue's helper process (measured 2026-10-13: it hung on a stale lock of a package it imports, and every editor
#: request waited behind it), a plugin's list (up to ``lookups.TIMEOUT_S``), the HuggingFace Hub — and reads none of
#: its graphs or documents. A Run (``trace``, ``rerun``) takes the lock itself only while it reads the document and
#: keeps the run (:meth:`Studio.trace`): a run may take minutes. A route that reads the graph list or a document never
#: joins.
TURN_FREE = frozenset(
    {"palette_route", "class_route", "types", "lookup", "hf_datasets", "hf_features", "hf_splits", "trace", "rerun"}
)


def _studio() -> Studio:
    studio: Studio = current_app.extensions[EXTENSION]  # `extensions` is untyped; the blueprint put a Studio there
    return studio


def _body() -> Dict[str, Any]:  # the request's JSON object: values of any JSON type
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("the request body must be a JSON object")
    return data


def _field(body: Mapping[str, Any], key: str) -> Any:  # one required field of the JSON body, of any JSON type
    if key not in body:
        raise ValueError(f"the request needs {key!r} (it has: {sorted(body)})")
    return body[key]


def _mapping(body: Mapping[str, Any], key: str) -> Dict[str, Any]:  # one JSON-object field, defaulting to {}
    value = body.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"{key!r} must be a JSON object, got {value!r}")
    return value


def _error(message: str, status: int) -> Tuple[Response, int]:
    return jsonify({"error": message}), status


def _text_list(body: Mapping[str, Any], key: str) -> List[str]:  # one required JSON list of strings
    value = _field(body, key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key!r} must be a list of names, got {value!r}")
    return value


def _text(body: Mapping[str, Any], key: str) -> Optional[str]:  # one optional field, as text; None when absent
    value = body.get(key)
    return None if value is None else str(value)


def _flag(body: Mapping[str, Any], key: str) -> bool:  # one optional JSON boolean, false when absent
    value = body.get(key, False)
    if not isinstance(value, bool):
        raise ValueError(f"{key!r} must be true or false, got {value!r}")
    return value


#: The four names a wire is given by, in the order of :data:`streamstudio.document.WireEnds`.
_WIRE_FIELDS = ("source", "source_socket", "target", "target_socket")


def _wire(body: Mapping[str, Any]) -> WireEnds:  # one wire's four required text fields
    source, source_socket, target, target_socket = (str(_field(body, key)) for key in _WIRE_FIELDS)
    return source, source_socket, target, target_socket


def _wire_list(body: Mapping[str, Any]) -> List[WireEnds]:  # ``wires``: a list of {source, source_socket, …}
    value = _field(body, "wires")
    if not isinstance(value, list) or not all(
        isinstance(item, dict) and all(isinstance(item.get(key), str) for key in _WIRE_FIELDS) for item in value
    ):
        raise ValueError(f"'wires' must be a list of {{{', '.join(_WIRE_FIELDS)}}}, got {value!r}")
    return [_wire(item) for item in value]


def _dropped(body: Mapping[str, Any]) -> Optional[WireEnds]:  # ``wire``: the end a wire was dragged out of, or absent
    """An add's optional ``wire`` — a wire dragged out of a socket and dropped on empty canvas: ``{source,
    source_socket}`` or ``{target, target_socket}``, the end it was dragged out of. The other end is the card the add
    makes, left blank (``Document.add_node`` refuses anything but exactly one end)."""
    value = body.get("wire")
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) - set(_WIRE_FIELDS)
        or not all(isinstance(item, str) for item in value.values())
    ):
        raise ValueError(
            "'wire' must be the end a wire was dragged out of — {source, source_socket} or {target, target_socket} — "
            f"got {value!r}"
        )
    source, source_socket, target, target_socket = (str(value.get(key, "")) for key in _WIRE_FIELDS)
    return source, source_socket, target, target_socket


def editor_blueprint(
    graph_dir: Optional[Path] = None,
    *,
    history_depth: int = 50,
    templates_dir: Optional[Path] = None,
    host: Optional[EditorHost] = None,
) -> Blueprint:
    """The editor's routes under ``/api``, ready to be registered on any Flask app (under a prefix or not).

    The app it is registered on gets its :class:`Studio` at ``app.extensions["streamstudio"]`` (:func:`studio_of`);
    one editor per app. Routes whose URL carries a NODE use ``<path:node>``, so a qualified inner name
    (``prep/grey``) arrives whole.

    Args:
        graph_dir: The folder the editor serves — the standalone editor's. ``None`` with a ``host``.
        history_depth: How many edits each document's ``undo`` can walk back.
        templates_dir: The templates folder; ``None`` = :func:`default_templates_dir`.
        host: An app that mounts the editor (:class:`EditorHost`): it names the folder at each request, supplies
            the record a graph without a source runs, and is told which files every Save wrote. Exactly one of
            ``graph_dir`` and ``host`` is given — refused here, before anything is registered, otherwise.
    """
    _check_folder_or_host(graph_dir, host)
    blueprint = Blueprint(EXTENSION, __name__)
    blueprint.record_once(
        lambda state: state.app.extensions.__setitem__(
            EXTENSION,
            Studio(
                None if graph_dir is None else Path(graph_dir),
                history_depth=history_depth,
                templates_dir=None if templates_dir is None else Path(templates_dir),
                host=host,
            ),
        )
    )

    @blueprint.errorhandler(KeyError)
    def _missing(exc: KeyError) -> Tuple[Response, int]:
        return _error(str(exc.args[0]) if exc.args else str(exc), 404)

    @blueprint.errorhandler(ValueError)
    def _refused(exc: ValueError) -> Tuple[Response, int]:
        return _error(str(exc), 400)

    @blueprint.errorhandler(TypeError)
    def _wrong_type(exc: TypeError) -> Tuple[Response, int]:
        return _error(str(exc), 400)

    @blueprint.errorhandler(RuntimeError)
    def _wrong_state(exc: RuntimeError) -> Tuple[Response, int]:
        return _error(str(exc), 400)

    @blueprint.errorhandler(CatalogueUnavailable)
    def _no_catalogue(exc: CatalogueUnavailable) -> Tuple[Response, int]:
        return _error(str(exc), 503)  # the server's own helper failed — nothing the request did

    @blueprint.errorhandler(OSError)
    def _disk(exc: OSError) -> Tuple[Response, int]:
        """A file the editor could not read or write, answered in the route's JSON shape and naming the file — never
        werkzeug's HTML 500 (measured before: a Save onto a folder moved away answered ``<!doctype html> … 500``). The
        operations that touch the disk refuse in their own located words first; this catches what they do not."""
        where = f"{exc.filename}: " if exc.filename else ""
        return _error(f"{where}{exc.strerror or exc}", 400)

    @blueprint.before_request
    def _take_turn() -> None:
        """Hold the editor (:attr:`Studio.lock`) for the whole request — registered FIRST, so no other hook reads the
        editor before it; :func:`_end_turn` lets go. A threaded host serves its panel's requests and its own at once.
        A request of :data:`TURN_FREE` takes none: it waits on something outside the editor."""
        if request.endpoint in {f"{EXTENSION}.{endpoint}" for endpoint in TURN_FREE}:
            return
        _studio().lock.acquire()
        g.streamstudio_turn = True

    @blueprint.teardown_request
    def _end_turn(exc: Optional[BaseException]) -> None:
        """Let go of the editor after the answer — a refusal and a raise included (teardown runs for every request
        whose turn :func:`_take_turn` took, and only for those)."""
        if g.pop("streamstudio_turn", False):
            _studio().lock.release()

    @blueprint.before_request
    def _same_session() -> Optional[Tuple[Response, int]]:
        """Refuse (``409``, :data:`RESTARTED`) a request of another editor's session — a tab left open across a
        restart — before it changes anything. ``GET /api/workspace`` is always answered: it is how a page joins (again).
        A request that names no session (a tool, a script) is served as ever."""
        given = request.headers.get(SESSION_HEADER)
        if given is None or request.endpoint == f"{EXTENSION}.workspace" or given == _studio().session:
            return None
        return _error(RESTARTED, 409)

    @blueprint.before_request
    def _follow_the_host() -> None:
        """Ask a host where its folder is BEFORE the request reads any path: one that moved its folder since the last
        request is followed first (:meth:`Studio.graph_dir`). A standalone editor's folder never moves."""
        _studio().follow()

    @blueprint.get("/api/graphs")
    def graphs() -> Response:
        return jsonify(_studio().graphs())

    @blueprint.post("/api/graphs")
    def new_graph() -> Response:
        body = _body()
        directory = body.get("dir")
        kind = cast(NewGraphKind, str(_field(body, "kind")))  # checked against NEW_GRAPH_KINDS by new_graph
        return jsonify(
            _studio().new_graph(str(_field(body, "name")), kind, None if directory is None else str(directory))
        )

    @blueprint.post("/api/graphs/open")
    def open_graph() -> Response:
        return jsonify(_studio().open_graph(str(_field(_body(), "path"))))

    @blueprint.get("/api/files")
    def files() -> Response:
        show = cast(FileShow, request.args.get("show") or "graphs")  # checked against FILE_SHOWS by files
        return jsonify(_studio().files(request.args.get("dir") or None, show))

    @blueprint.get("/api/hf/datasets")
    def hf_datasets() -> Response:
        return jsonify(_studio().hf_datasets())

    @blueprint.get("/api/hf/features")
    def hf_features() -> Response:
        args = request.args
        return jsonify(
            _studio().hf_features(args.get("path", ""), name=args.get("name") or None, split=args.get("split") or None)
        )

    @blueprint.get("/api/hf/splits")
    def hf_splits() -> Response:
        return jsonify(_studio().hf_splits(request.args.get("path", ""), name=request.args.get("name") or None))

    @blueprint.get("/api/lookups/<tag>")
    def lookup(tag: str) -> Response:
        return jsonify(_studio().lookup(tag, {key: str(value) for key, value in request.args.items()}))

    @blueprint.get("/api/workspace")
    def workspace() -> Response:
        return jsonify(_studio().workspace())

    @blueprint.post("/api/workspace/recovery/restore")
    def restore_recovery() -> Response:
        return jsonify(_studio().restore_recovery())

    @blueprint.post("/api/workspace/recovery/discard")
    def discard_recovery() -> Response:
        return jsonify(_studio().discard_recovery())

    @blueprint.get("/api/graphs/<name>")
    def graph(name: str) -> Response:
        return jsonify(_studio().graph(name))

    @blueprint.post("/api/graphs/<name>/save")
    def save(name: str) -> Response:
        return jsonify(_studio().save(name, _text(_body(), "only") or ""))

    @blueprint.post("/api/graphs/<name>/save-as")
    def save_as(name: str) -> Response:
        body = _body()
        return jsonify(_studio().save_as(name, str(_field(body, "path")), _flag(body, "overwrite")))

    @blueprint.post("/api/graphs/<name>/revert")
    def revert(name: str) -> Response:
        return jsonify(_studio().revert(name))

    @blueprint.get("/api/graphs/<name>/check")
    def check(name: str) -> Response:
        return jsonify(_studio().check(name))

    @blueprint.post("/api/graphs/<name>/subgraph")
    def convert_to_subgraph(name: str) -> Response:
        body = _body()
        return jsonify(_studio().convert_to_subgraph(name, _text_list(body, "nodes"), str(_field(body, "name"))))

    @blueprint.post("/api/graphs/<name>/subgraph/<path:node>/extract")
    def extract_steps(name: str, node: str) -> Response:
        path = _body().get("path")
        return jsonify(_studio().extract_steps(name, node, None if path is None else str(path)))

    @blueprint.post("/api/graphs/<name>/include")
    def include_steps(name: str) -> Response:
        body = _body()
        step = body.get("name")
        return jsonify(
            _studio().include_steps(
                name, str(_field(body, "path")), str(_field(body, "after")), None if step is None else str(step)
            )
        )

    @blueprint.post("/api/graphs/<name>/templates/<template>/insert")
    def insert_template(name: str, template: str) -> Response:
        return jsonify(_studio().insert_template(name, template, str(_field(_body(), "after"))))

    @blueprint.post("/api/graphs/<name>/copy")
    def copy(name: str) -> Response:
        return jsonify(_studio().copy(name, _text_list(_body(), "nodes")))

    @blueprint.post("/api/graphs/<name>/paste")
    def paste(name: str) -> Response:
        body = _body()
        return jsonify(_studio().paste(name, str(_field(body, "text")), str(_field(body, "after"))))

    @blueprint.put("/api/graphs/<name>")
    def put_text(name: str) -> Response:
        return jsonify(_studio().replace_text(name, str(_field(_body(), "text"))))

    @blueprint.post("/api/graphs/<name>/param")
    def set_param(name: str) -> Response:
        body = _body()
        return jsonify(
            _studio().set_param(name, str(_field(body, "node")), str(_field(body, "param")), _field(body, "value"))
        )

    @blueprint.post("/api/graphs/<name>/slot")
    def set_slot(name: str) -> Response:
        body = _body()
        return jsonify(
            _studio().set_slot(
                name,
                str(_field(body, "node")),
                str(_field(body, "slot")),
                str(_field(body, "class_path")),
                _mapping(body, "params"),
            )
        )

    @blueprint.post("/api/graphs/<name>/wires")
    def add_wire(name: str) -> Response:
        return jsonify(_studio().add_wire(name, *_wire(_body())))

    @blueprint.post("/api/graphs/<name>/wires/remove")
    def remove_wires(name: str) -> Response:
        return jsonify(_studio().remove_wires(name, _wire_list(_body())))

    @blueprint.post("/api/graphs/<name>/steps")
    def add_step(name: str) -> Response:
        body = _body()
        return jsonify(
            _studio().add_step(
                name,
                _text(body, "name"),
                _text(body, "after"),
                str(_field(body, "class_path")),
                _mapping(body, "params"),
                into=_text(body, "list"),
                wire=_dropped(body),
            )
        )

    @blueprint.post("/api/graphs/<name>/nodes")
    def add_node(name: str) -> Response:
        body = _body()
        return jsonify(
            _studio().add_node(name, str(_field(body, "class_path")), _mapping(body, "params"), wire=_dropped(body))
        )

    @blueprint.delete("/api/graphs/<name>/steps/<path:node>")
    def remove_step(name: str, node: str) -> Response:
        return jsonify(_studio().remove_step(name, node))

    @blueprint.post("/api/graphs/<name>/steps/remove")
    def remove_steps(name: str) -> Response:
        return jsonify(_studio().remove_steps(name, _text_list(_body(), "nodes")))

    @blueprint.post("/api/graphs/<name>/rename")
    def rename(name: str) -> Response:
        body = _body()
        return jsonify(_studio().rename(name, str(_field(body, "node")), str(_field(body, "new"))))

    @blueprint.post("/api/graphs/<name>/undo")
    def undo(name: str) -> Response:
        return jsonify(_studio().undo(name))

    @blueprint.post("/api/graphs/<name>/redo")
    def redo(name: str) -> Response:
        return jsonify(_studio().redo(name))

    @blueprint.put("/api/graphs/<name>/positions")
    def set_positions(name: str) -> Response:
        return jsonify(_studio().set_positions(name, _mapping(_body(), "positions")))

    @blueprint.post("/api/graphs/<name>/layout")
    def relayout(name: str) -> Response:
        return jsonify(_studio().relayout(name))

    @blueprint.put("/api/graphs/<name>/groups")
    def set_groups(name: str) -> Response:
        return jsonify(_studio().set_groups(name, _field(_body(), "groups")))

    @blueprint.get("/api/templates")
    def templates() -> Response:
        return jsonify(_studio().templates())

    @blueprint.post("/api/templates")
    def save_template() -> Response:
        body = _body()
        return jsonify(
            _studio().save_template(
                str(_field(body, "name")),
                str(_field(body, "graph")),
                _text_list(body, "nodes"),
                _flag(body, "overwrite"),
            )
        )

    @blueprint.delete("/api/templates/<name>")
    def delete_template(name: str) -> Response:
        return jsonify(_studio().delete_template(name))

    @blueprint.get("/api/types")
    def types() -> Response:
        """``{socket type: [the types it may be wired into]}`` — the page's ``compatible(a, b)`` reads this so
        it applies the SAME meet rule the model does (:func:`streamstudio.document.supertypes`)."""
        return jsonify(document_module.supertypes())

    @blueprint.get("/api/palette")
    def palette_route() -> Response:
        refresh = request.args.get("refresh", "") not in ("", "0", "false")
        return jsonify(_studio().catalogue(refresh))

    @blueprint.get("/api/palette/<class_path>")
    def class_route(class_path: str) -> Response:
        return jsonify(_studio().class_spec(class_path))

    @blueprint.get("/api/graphs/<name>/form")
    def form(name: str) -> Response:
        return jsonify(_studio().form(name))

    @blueprint.get("/api/graphs/<name>/app")
    def app_form(name: str) -> Response:
        return jsonify(_studio().app_form(name))

    @blueprint.post("/api/graphs/<name>/trace")
    def trace(name: str) -> Response:
        body = _body()
        record = body.get("record", 0)
        if not isinstance(record, int) or isinstance(record, bool):
            raise ValueError(f"'record' must be an integer, got {record!r}")
        until = body.get("until")
        return jsonify(_studio().trace(name, record, None if until is None else str(until)))

    @blueprint.post("/api/runs/<run>/rerun")
    def rerun(run: str) -> Response:
        body = _body()
        return jsonify(_studio().rerun(run, str(_field(body, "node")), _mapping(body, "params")))

    @blueprint.get("/api/runs/<run>")
    def get_run(run: str) -> Response:
        return jsonify(_studio().run(run).to_dict())

    return blueprint


def web_dist() -> Optional[Path]:
    """``streamstudio/canvas`` inside the package when the canvas page is built there, else ``None``.

    The built page is COMMITTED inside the package (:func:`streamstudio.page.built_folder`), so every checkout and
    every install — one from GitHub or a wheel, with no ``frontend/`` source at all — has it; a server's start
    rebuilds it in a checkout whose page source changed (:func:`streamstudio.page.ensure_page_built`). Only a package
    that carries no build at all answers ``None``, and serves the API and a placeholder that says how to build.
    ``index.html`` is the test — a folder left over from an interrupted build must not be mistaken for a page.
    """
    built = page_module.built_folder()
    return built if (built / "index.html").is_file() else None


def page_blueprint(dist: Optional[Path] = None) -> Blueprint:
    """The canvas page: ``/`` is ``index.html`` and ``/<file>`` an asset of ``dist``, or — when ``dist`` holds
    no ``index.html`` — a placeholder naming the build command and listing the API routes.

    Separate from :func:`editor_blueprint` so a consuming app can mount the API alone, or both under one
    prefix (``url_prefix="/studio"``): the page is built with ``base: './'``, so its asset URLs resolve
    against whatever path serves it.

    Decided at EVERY request (a stat or two), never once when the blueprint is made: a server's start builds the
    page AFTER its app was made (:func:`streamstudio.page.ensure_page_built`), and that page is served. When that
    start could not make the page current — no Node, or the build failed (:func:`streamstudio.page.unbuildable`) —
    ``/`` says so instead of serving the old page. Without a start check (an app made in a test) ``/`` serves what
    ``dist`` holds.

    Args:
        dist: The built page; ``None`` = the one the package carries, :func:`web_dist` (measured before 2026-10-01:
            ``page_blueprint()`` served the placeholder even with the page built, so a host had to know to pass
            ``web_dist()``). A directory without ``index.html`` serves the placeholder — how a test asks for it.
    """
    blueprint = Blueprint(f"{EXTENSION}_page", __name__)
    given = None if dist is None else Path(dist)

    def built() -> Optional[Path]:  # the folder holding the page now, or None
        if given is None:
            return web_dist()
        return given if (given / "index.html").is_file() else None

    @blueprint.get("/")
    def index() -> Any:  # flask.Response from send_from_directory, or the placeholder str
        problem = page_module.unbuildable(given if given is not None else page_module.built_folder())
        if problem is not None:
            return _unbuildable_page(problem)
        served = built()
        if served is not None:
            # The editor is opening: its catalogue starts building now, in a helper process, so a list opened later is
            # there or nearly (a page that only looks at a graph never waits for it — it asks on first need).
            catalogue_module.CATALOGUE.start()
            return send_from_directory(served, "index.html")
        rules = sorted(
            (rule.rule, ", ".join(sorted((rule.methods or set()) - {"HEAD", "OPTIONS"})))
            for rule in current_app.url_map.iter_rules()
            if "/api/" in rule.rule
        )
        # Escaped: a route reads `…/templates/<template>`, and written raw that opened an HTML <template> element
        # that swallowed the rest of the page — the message to a host's panel included.
        items = "".join(f"<li><code>{html.escape(f'{methods} {path}')}</code></li>" for path, methods in rules)
        return (
            "<!doctype html><title>streamstudio editor</title>"
            "<h1>streamstudio editor</h1>"
            f"<p>The canvas page is not built. Build it once — <code>{html.escape(BUILD_COMMAND)}</code> — and reload; "
            f"the page lands in <code>streamstudio/canvas</code> inside the package. The API is up:</p>"
            f"<ul>{items}</ul>{_TELL_THE_HOST}"
        )

    @blueprint.get("/<path:filename>")
    def asset(filename: str) -> Response:
        served = built()
        if served is None:
            abort(404)
        return send_from_directory(served, filename)

    return blueprint


#: What a page served INSTEAD of the editor (the placeholder, no Node, a failed build) tells a host's panel — its
#: parent window, same origin only, and only with ``?embed`` (the editor's own rule, ``link.ts::embedFromSearch``):
#: ``{source: "streamstudio", type: "unavailable"}``, no editor will answer here. Measured before: a host covering its
#: panel until the editor's first ``state`` message covered these pages for good, its veil reading ``loading sink…``
#: over ``…Node is not installed — run aisland setup…``. A host that waits for no message ignores it.
_TELL_THE_HOST = (
    "<script>(() => {"
    " const asked = new URLSearchParams(location.search).get('embed');"
    " if (window.parent === window || asked === null"
    " || ['0', 'false', 'no'].includes(asked.trim().toLowerCase())) return;"
    " window.parent.postMessage({source: 'streamstudio', type: 'unavailable'}, location.origin);"
    " })();</script>"
)


def _unbuildable_page(problem: PageCheck) -> str:
    """What ``/`` says when this server's start could not make the page current — the start's own words."""
    if problem.state == "no-node":
        said = html.escape(
            f"The editor's page {problem.why} and Node is not installed — run aisland setup, "
            "then start this server again."
        )
    else:
        said = (
            html.escape(f"Building the editor's page failed when this server started — {problem.why}. To see why, run ")
            + f"<code>{html.escape(BUILD_COMMAND)}</code>, then start this server again."
        )
    tail = f"<pre>{html.escape(problem.output)}</pre>" if problem.output else ""
    return (
        f"<!doctype html><title>streamstudio editor</title><h1>streamstudio editor</h1><p>{said}</p>{tail}"
        f"{_TELL_THE_HOST}"
    )


def create_app(
    graph_dir: Path,
    *,
    history_depth: int = 50,
    web_dist: Optional[Path] = None,
    templates_dir: Optional[Path] = None,
) -> Flask:
    """The standalone editor: the API blueprint and the page blueprint at ``/``.

    Args:
        graph_dir: The directory whose ``*.yaml`` files are the graphs (more can be opened from anywhere).
        history_depth: How many edits each document's ``undo`` can walk back.
        web_dist: Where the built page is; ``None`` = the package's own (:func:`web_dist`). A directory
            without ``index.html`` serves the placeholder — which is how a test asks for it deterministically
            whatever the checkout holds.
        templates_dir: The templates folder; ``None`` = :func:`default_templates_dir`.
    """
    app = Flask(__name__)
    app.register_blueprint(editor_blueprint(Path(graph_dir), history_depth=history_depth, templates_dir=templates_dir))
    app.register_blueprint(page_blueprint(None if web_dist is None else Path(web_dist)))
    return app


def studio_of(app: Flask) -> Studio:
    """The editor :func:`editor_blueprint` put on ``app`` — a host's handle on it. A host keeps the unsaved work across
    its own stop by calling ``studio_of(app).stash()`` there (the standalone CLI does it on its way out): the next
    mount over the same folder offers it back, and the page asks the restore question. A ``KeyError`` naming the app
    when no editor is mounted on it."""
    studio = app.extensions.get(EXTENSION)
    if not isinstance(studio, Studio):
        raise KeyError(f"no editor is mounted on the app {app.name!r} — register editor_blueprint(…) on it first")
    return studio


__all__ = [
    "BROKEN_RECOVERY_FILE",
    "BUILD_COMMAND",
    "EXTENSION",
    "EditorHost",
    "FILE_SHOWS",
    "FileEntryKind",
    "FileShow",
    "GRAPH_KINDS",
    "GraphKind",
    "HostRecord",
    "NEW_GRAPH_KINDS",
    "NewGraphKind",
    "RECOVERY_FILE",
    "RESTARTED",
    "Run",
    "SESSION_HEADER",
    "SIDECAR_SUFFIX",
    "STEPS_SUFFIX",
    "Studio",
    "create_app",
    "default_templates_dir",
    "editor_blueprint",
    "ensure_page_built",
    "page_blueprint",
    "page_check_environment",
    "starter_text",
    "studio_of",
    "web_dist",
]
