"""The editor's HTTP surface: a Flask blueprint whose every route is ONE call on the document, the
palette, the layout or the engine — JSON in, JSON out, a refusal as ``400 {error}`` carrying the
located message, an unknown graph / node / run as ``404 {error}`` naming what there is.

Why a blueprint and a factory, not a module-level ``app``: the editor is served two ways
(``docs/rewrite.md`` §0) — standalone by ``streamstudio editor``, and MOUNTED by a consuming Flask
app under a prefix of its own. :func:`editor_blueprint` is the mountable half; :func:`create_app`
wraps it for the standalone case. All state — the open documents with their undo history, the
runs — lives on ``app.extensions["streamstudio"]`` (:class:`Studio`), never on this module: two
apps in one process are two editors, and a test never leaks a run into the next test.

Why the routes hold no logic: the same operations become LLM tools in phase 5 (``run_graph``,
``rerun_node``, ``set_param``, ``add_step``, …) and a canvas action in phase 3. A route parses its
request and calls one :class:`Studio` method, so a tool calls the same method and cannot drift
from what the page does.

Two rules of the document carry into the routes. **Every edit lands on disk**: the YAML file is the
only truth (§2.1), so ``set_param`` / ``add_step`` / ``remove_step`` / ``rename`` / ``undo`` /
``redo`` save right after the document accepted them — an agent's ``set_param`` answers *written
line 179* (§2.4), not *pending*. **A refusal writes nothing**: :class:`~streamstudio.document.Document`
refuses an edit that makes the document stop loading or gain a problem; the same test guards
``PUT`` of a whole text (a text that does not load, or gains a wire whose types do not meet, is
answered with the located message and the file is untouched). A ``PUT`` REPLACES the open
document — its undo history starts over — because the text arrived whole, not as a patch the
history could hold.

Why the trace maps names: the engine (:class:`recordstream.flow.Tracer`) names a ``Stream``'s ops
``ops[0]``, ``ops[1]``, … relative to the stream; the document names the same nodes by their path,
``stream.ops[1]`` (§3: one naming scheme for the engine, the sidecar, the tools and the canvas).
A :class:`Run` keeps the stream node's name as the prefix and translates both ways, so a page or a
tool never sees ``ops[1]``. The record a trace runs is drawn from the document's OWN source (its
record ``#record``); a graph without a source — a view chain, a ``flow:`` alone — is refused in
words: a host supplies the record (mount the editor in one) or wire a source.
"""

import itertools
import re
import secrets
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple, get_args

import confluid
import yaml
from flask import Blueprint, Flask, Response, abort, current_app, jsonify, request, send_from_directory
from recordstream.core.stream import Stream
from recordstream.flow import Tracer
from recordstream.ops.contract import ChainContractError

from streamstudio import document as document_module
from streamstudio import layout, palette
from streamstudio._yamltext import YamlText
from streamstudio.document import Document, Model, Node

#: The key under ``app.extensions`` where the :class:`Studio` of an app lives.
EXTENSION = "streamstudio"

#: The command that builds the canvas page — printed by the placeholder, so the fix is on the page that needs it.
BUILD_COMMAND = "cd streamstudio/frontend && npm install && npm run build"

#: What a document's root says it is: a ``flow:`` graph, a contract-rooted graph (a template twin),
#: or any other configuration (a ``Stream``, a processor …).
GraphKind = Literal["flow", "graph", "contract"]
GRAPH_KINDS: Tuple[str, ...] = get_args(GraphKind)

#: A node position as the JSON carries it: ``[x, y]``.
PositionList = List[float]

_RUN_ID_BYTES = 4
_SEGMENT = re.compile(r"^(\w+)(?:\[(\d+)\])?$")


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
    """The model as plain dicts — nodes (with their params and sockets), wires, problems, outputs."""
    return {
        "nodes": [_plain(asdict(node)) for node in model.nodes],
        "wires": [asdict(wire) for wire in model.wires],
        "problems": [asdict(problem) for problem in model.problems],
        "outputs": list(model.outputs),
    }


def _positions_dict(positions: Mapping[str, Tuple[float, float]]) -> Dict[str, PositionList]:
    return {name: [float(x), float(y)] for name, (x, y) in positions.items()}


def _positions_from_json(body: Mapping[str, Any], model: Model) -> Dict[str, Tuple[float, float]]:
    """``{name: [x, y]}`` → positions; an unknown node is a ``KeyError`` (404), a malformed pair a ``ValueError``."""
    known = model.names()
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
        record: Which record of the source was drawn.
        tracer: The engine, already holding the run.
        prefix: What the document puts before the engine's node names (``stream.`` for a stream named ``stream``).
        where: What refusals name as the location.
    """

    def __init__(self, run_id: str, graph: str, record: int, tracer: Tracer, prefix: str, where: str) -> None:
        # Stores only: the run happened (or is paused) inside the tracer already.
        self.run_id = run_id
        self.graph = graph
        self.record = record
        self.tracer = tracer
        self.prefix = prefix
        self.where = where

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

    def to_dict(self) -> Dict[str, Any]:
        """``{run, graph, record, trace, statuses, generations, paused_at}`` — every node by its document name."""
        trace = self.tracer.to_dict()
        trace["nodes"] = [{**node, "node": self.document_name(str(node["node"]))} for node in trace["nodes"]]
        trace["check"] = [{**row, "node": self.document_name(str(row["node"]))} for row in trace["check"]]
        paused = self.tracer.paused_at
        trace["paused_at"] = None if paused is None else self.document_name(paused)
        return {
            "run": self.run_id,
            "graph": self.graph,
            "record": self.record,
            "trace": trace,
            "statuses": {self.document_name(name): status for name, status in self.tracer.statuses.items()},
            "generations": {self.document_name(name): gen for name, gen in self.tracer.generations.items()},
            "paused_at": trace["paused_at"],
        }


# ================================================================================================
# the studio: the documents of one directory, and the runs
# ================================================================================================


class Studio:
    """The editor's state for one graph directory: its documents (opened on first use, kept for their
    undo history) and its runs.

    Args:
        graph_dir: The directory whose ``*.yaml`` files are the graphs.
        history_depth: How many edits each document's ``undo`` can walk back.
    """

    def __init__(self, graph_dir: Path, *, history_depth: int = 50) -> None:
        # Stores only: a document is opened when a route first names it.
        self.graph_dir = Path(graph_dir)
        self.history_depth = int(history_depth)
        self._documents: Dict[str, Document] = {}
        self._runs: Dict[str, Run] = {}

    # -- graphs ------------------------------------------------------------------------------

    def names(self) -> List[str]:
        """Every graph, by name: the ``*.yaml`` files of the directory, a ``.layout.yaml`` sidecar left out."""
        return sorted(
            path.stem
            for path in self.graph_dir.glob("*.yaml")
            if path.is_file() and not path.name.endswith(".layout.yaml")
        )

    def path_of(self, name: str) -> Path:
        """The file of the graph ``name``; a miss is a ``KeyError`` naming the graphs there are."""
        path = self.graph_dir / f"{name}.yaml"
        if "/" in name or name.startswith(".") or not path.is_file():
            raise KeyError(f"no graph named {name!r} in {self.graph_dir} (the graphs are: {self.names()})")
        return path

    def document(self, name: str) -> Document:
        if name not in self._documents:
            self._documents[name] = Document.open(self.path_of(name), history_depth=self.history_depth)
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

    def graphs(self) -> List[Dict[str, str]]:
        return [{"name": name, "path": str(self.path_of(name)), "kind": self.kind(name)} for name in self.names()]

    def graph(self, name: str) -> Dict[str, Any]:
        """``{name, path, kind, text, model, positions, groups, history_depth}`` of one graph."""
        document = self.document(name)
        return {
            "name": name,
            "path": str(document.path),
            "kind": self.kind(name),
            "text": document.text,
            "model": _model_dict(document.model),
            "positions": _positions_dict(document.positions),
            "groups": document.groups,
            "history_depth": document.history_depth,
        }

    # -- editing -----------------------------------------------------------------------------

    def replace_text(self, name: str, text: str) -> Dict[str, Any]:
        """Make ``text`` the graph's document: checked as a whole, written only when it gains no problem
        over the current text, and then opened afresh (the undo history starts over)."""
        current = self.document(name)
        candidate = Document(current.path, {current.path.resolve(): text}, history_depth=self.history_depth)
        before = {problem.message for problem in current.check()}
        gained = [problem for problem in candidate.check() if problem.message not in before]
        if gained:
            raise ValueError("; ".join(problem.message for problem in gained))
        current.path.write_text(text)
        self._documents[name] = Document.open(current.path, history_depth=self.history_depth)
        return self.graph(name)

    def set_param(self, name: str, node: str, param: str, value: Any) -> Dict[str, Any]:  # value: any YAML scalar
        document = self.document(name)
        document.set_param(node, param, value)
        document.save()
        return self.graph(name)

    def add_step(self, name: str, step: str, after: str, class_path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        document = self.document(name)
        document.add_step(step, after, class_path, params)
        document.save()
        return self.graph(name)

    def remove_step(self, name: str, step: str) -> Dict[str, Any]:
        document = self.document(name)
        document.remove_step(step)
        document.save()
        return self.graph(name)

    def rename(self, name: str, node: str, new: str) -> Dict[str, Any]:
        document = self.document(name)
        document.rename_node(node, new)
        document.save()
        return self.graph(name)

    def undo(self, name: str) -> Dict[str, Any]:
        """Step back one edit and save; ``changed`` says whether there was one."""
        document = self.document(name)
        changed = document.undo()
        if changed:
            document.save()
        return {**self.graph(name), "changed": changed}

    def redo(self, name: str) -> Dict[str, Any]:
        document = self.document(name)
        changed = document.redo()
        if changed:
            document.save()
        return {**self.graph(name), "changed": changed}

    # -- positions ---------------------------------------------------------------------------

    def set_positions(self, name: str, positions: Mapping[str, Any]) -> Dict[str, PositionList]:
        document = self.document(name)
        document.set_positions(_positions_from_json(positions, document.model))
        document.save()
        return _positions_dict(document.positions)

    def relayout(self, name: str) -> Dict[str, PositionList]:
        """Lay the graph out afresh (the deterministic first layout) and save it as the sidecar."""
        document = self.document(name)
        document.set_positions(layout.layered_layout(document.model))
        document.save()
        return _positions_dict(document.positions)

    # -- groups ------------------------------------------------------------------------------

    def set_groups(self, name: str, groups: Any) -> Dict[str, List[layout.Group]]:  # groups: the request's JSON
        """Replace the graph's groups (the sidecar's ``groups:`` block) and answer ``{groups}``.

        Only the shape is checked — a list of JSON objects; what a group holds is the page's, stored as
        sent (``streamstudio.layout.Group``). The document is never touched: it has no group spelling.
        """
        document = self.document(name)
        if not isinstance(groups, list) or not all(isinstance(group, dict) for group in groups):
            raise ValueError(
                f"{name}.yaml: 'groups' must be a list of group objects ({{id, title, color, nodes}}), got {groups!r}"
            )
        document.set_groups(groups)
        document.save()
        return {"groups": document.groups}

    # -- the settings form -------------------------------------------------------------------

    def form(self, name: str) -> Dict[str, Dict[str, Any]]:
        """``{node: params_schema}`` for every node — the JSON-schema properties a form renders; a synthetic
        node (the host's side) and a class the palette cannot describe get ``{}`` (the model reports why)."""
        form: Dict[str, Dict[str, Any]] = {}
        for node in self.document(name).model.nodes:
            try:
                form[node.name] = dict(palette.spec_for(node.class_path).params_schema) if node.class_path else {}
            except KeyError:
                form[node.name] = {}
        return form

    # -- runs --------------------------------------------------------------------------------

    def run(self, run_id: str) -> Run:
        if run_id not in self._runs:
            raise KeyError(f"no run {run_id!r} (the runs are: {sorted(self._runs)})")
        return self._runs[run_id]

    def trace(self, name: str, record: int = 0, until: Optional[str] = None) -> Dict[str, Any]:
        """Run record ``#record`` of the graph's own source through its stream, pausing before ``until``."""
        if record < 0:
            raise ValueError(f"{name}.yaml: record must be 0 or more, got {record}")
        document = self.document(name)
        model = document.model
        if not model.nodes and model.problems:
            raise ValueError(model.problems[0].message)
        stream_node = _stream_node(model, f"{name}.yaml")
        stream = _object_at(confluid.load(document.path), stream_node.name)
        if not isinstance(stream, Stream) or stream.source is None:
            raise ValueError(_no_source(f"{name}.yaml"))
        drawn = list(itertools.islice(iter(stream.source), record + 1))
        if len(drawn) <= record:
            raise ValueError(f"{name}.yaml: the source yields only {len(drawn)} records — there is no record #{record}")
        where = f"{document.path}:{stream_node.name}"
        tracer = Tracer(stream, where=where)
        run = Run(self._new_run_id(), name, record, tracer, f"{stream_node.name}.", where)
        engine_until = None if until is None else run.engine_name(until)
        try:
            tracer.run(drawn[record], until=engine_until)
        except ChainContractError as refusal:  # nothing ran: the chain check refused
            raise ValueError(str(refusal)) from refusal
        except (
            Exception
        ) as exc:  # noqa: BLE001 — a node raised while running (recorded), or the engine refused the graph
            if "error" not in _statuses(tracer).values():
                raise ValueError(f"{where}: {type(exc).__name__}: {exc}") from exc
        self._runs[run.run_id] = run
        return run.to_dict()

    def rerun(self, run_id: str, node: str, params: Dict[str, Any]) -> Dict[str, Any]:
        run = self.run(run_id)
        run.rerun(node, params)
        return run.to_dict()

    def _new_run_id(self) -> str:
        while True:
            run_id = f"run_{secrets.token_hex(_RUN_ID_BYTES)}"
            if run_id not in self._runs:
                return run_id


def _no_source(where: str) -> str:
    return (
        f"{where}: this graph has no source to draw a record from — "
        "a host supplies the record (mount the editor in one) or wire a source"
    )


def _stream_node(model: Model, where: str) -> Node:
    """The ONE engine node whose ``source`` slot is wired — the stream a trace draws its record from."""
    fed = {wire.target for wire in model.wires if wire.target_socket == "source"}
    streams = [node for node in model.nodes if node.kind == "engine" and node.name in fed]
    if not streams:
        raise ValueError(_no_source(where))
    if len(streams) > 1:
        names = [node.name for node in streams]
        raise ValueError(f"{where}: several streams draw records ({names}) — tracing one of several is not supported")
    return streams[0]


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


def editor_blueprint(graph_dir: Path, *, history_depth: int = 50) -> Blueprint:
    """The editor's routes under ``/api``, ready to be registered on any Flask app (under a prefix or not).

    The app it is registered on gets its :class:`Studio` at ``app.extensions["streamstudio"]``; one
    editor per app.
    """
    blueprint = Blueprint(EXTENSION, __name__)
    blueprint.record_once(
        lambda state: state.app.extensions.__setitem__(EXTENSION, Studio(Path(graph_dir), history_depth=history_depth))
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

    @blueprint.get("/api/graphs")
    def graphs() -> Response:
        return jsonify(_studio().graphs())

    @blueprint.get("/api/graphs/<name>")
    def graph(name: str) -> Response:
        return jsonify(_studio().graph(name))

    @blueprint.put("/api/graphs/<name>")
    def put_text(name: str) -> Response:
        return jsonify(_studio().replace_text(name, str(_field(_body(), "text"))))

    @blueprint.post("/api/graphs/<name>/param")
    def set_param(name: str) -> Response:
        body = _body()
        return jsonify(
            _studio().set_param(name, str(_field(body, "node")), str(_field(body, "param")), _field(body, "value"))
        )

    @blueprint.post("/api/graphs/<name>/steps")
    def add_step(name: str) -> Response:
        body = _body()
        return jsonify(
            _studio().add_step(
                name,
                str(_field(body, "name")),
                str(_field(body, "after")),
                str(_field(body, "class_path")),
                _mapping(body, "params"),
            )
        )

    @blueprint.delete("/api/graphs/<name>/steps/<node>")
    def remove_step(name: str, node: str) -> Response:
        return jsonify(_studio().remove_step(name, node))

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

    @blueprint.get("/api/types")
    def types() -> Response:
        """``{socket type: [the types it may be wired into]}`` — the page's ``compatible(a, b)`` reads this so
        it applies the SAME meet rule the model does (:func:`streamstudio.document.supertypes`)."""
        return jsonify(document_module.supertypes())

    @blueprint.get("/api/palette")
    def palette_route() -> Response:
        refresh = request.args.get("refresh", "") not in ("", "0", "false")
        return jsonify([asdict(spec) for spec in palette.palette(refresh=refresh)])

    @blueprint.get("/api/graphs/<name>/form")
    def form(name: str) -> Response:
        return jsonify(_studio().form(name))

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
    """``<repo>/frontend/dist`` when the canvas page is built there, else ``None``.

    The page is a build artifact beside the package, never in it (navigaitor's shape): ``frontend/`` is
    the committed source, ``frontend/dist`` is gitignored and produced by :data:`BUILD_COMMAND`, and an
    install without it still serves the API and a placeholder that says how to build. ``index.html`` is
    the test — a ``dist/`` left over from an interrupted build must not be mistaken for a page.
    """
    dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    return dist if (dist / "index.html").is_file() else None


_default_web_dist = web_dist  # create_app's keyword of the same name shadows the function inside it


def page_blueprint(dist: Optional[Path] = None) -> Blueprint:
    """The canvas page: ``/`` is ``index.html`` and ``/<file>`` an asset of ``dist``, or — when ``dist`` holds
    no ``index.html`` — a placeholder naming the build command and listing the API routes.

    Separate from :func:`editor_blueprint` so a consuming app can mount the API alone, or both under one
    prefix (``url_prefix="/editor"``): the page is built with ``base: './'``, so its asset URLs resolve
    against whatever path serves it.
    """
    blueprint = Blueprint(f"{EXTENSION}_page", __name__)
    built = dist if dist is not None and (dist / "index.html").is_file() else None

    @blueprint.get("/")
    def index() -> Any:  # flask.Response from send_from_directory, or the placeholder str
        if built is not None:
            return send_from_directory(built, "index.html")
        rules = sorted(
            (rule.rule, ", ".join(sorted((rule.methods or set()) - {"HEAD", "OPTIONS"})))
            for rule in current_app.url_map.iter_rules()
            if "/api/" in rule.rule
        )
        items = "".join(f"<li><code>{methods} {path}</code></li>" for path, methods in rules)
        return (
            "<!doctype html><title>streamstudio editor</title>"
            "<h1>streamstudio editor</h1>"
            f"<p>The canvas page is not built. Build it once — <code>{BUILD_COMMAND}</code> — and reload; "
            f"the page lands in <code>frontend/dist</code> beside the package. The API is up:</p>"
            f"<ul>{items}</ul>"
        )

    @blueprint.get("/<path:filename>")
    def asset(filename: str) -> Response:
        if built is None:
            abort(404)
        return send_from_directory(built, filename)

    return blueprint


def create_app(graph_dir: Path, *, history_depth: int = 50, web_dist: Optional[Path] = None) -> Flask:
    """The standalone editor: the API blueprint and the page blueprint at ``/``.

    Args:
        graph_dir: The directory whose ``*.yaml`` files are the graphs.
        history_depth: How many edits each document's ``undo`` can walk back.
        web_dist: Where the built page is; ``None`` looks beside the package (:func:`web_dist`). A directory
            without ``index.html`` serves the placeholder — which is how a test asks for it deterministically
            whatever the checkout holds.
    """
    app = Flask(__name__)
    app.register_blueprint(editor_blueprint(Path(graph_dir), history_depth=history_depth))
    app.register_blueprint(page_blueprint(_default_web_dist() if web_dist is None else Path(web_dist)))
    return app


__all__ = [
    "BUILD_COMMAND",
    "EXTENSION",
    "GRAPH_KINDS",
    "GraphKind",
    "Run",
    "Studio",
    "create_app",
    "editor_blueprint",
    "page_blueprint",
    "web_dist",
]
