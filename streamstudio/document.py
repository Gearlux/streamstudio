"""The confluid document is the only truth: a canvas MODEL derived from it, written back by
patching the line a marker came from.

The editor never keeps a second document (``docs/rewrite.md`` §2.1). :class:`Document` reads
a YAML text, derives a :class:`Model` — nodes with typed sockets, wires, located problems —
from ``confluid.load(path, until="document", texts=…)`` — the file, as the texts it holds spell
it (markers kept, ``!ref`` kept, every marker stamped ``file:line:col``) — and every edit
(``set_param``, ``add_step``, ``remove_step``, ``rename_node``) patches the TEXT through
:mod:`streamstudio._yamltext`, re-derives, and is refused before anything is written when the
document no longer loads or :meth:`Document.check` gains a problem. Measured against the
alternative: a re-dump of the shipped Bluetooth pane loses its 148 comment lines and bakes
``$DATA_ROOT`` in; a patch changes one line.

Three things the model is built from, and nothing else:

* **the declarations** — ``recordstream.algorithm_spec`` for an ``Algorithm`` (its ``Input`` /
  ``Output`` slots are the node's entry sockets, typed by the item class), an op's own
  ``consumes`` / ``produces`` / ``flags`` / ``reports`` for anything else, ``confluid.input_specs``
  / ``output_specs`` / ``to_pydantic`` for constructor parameters and ``@output`` properties,
  ``GraphContract.inputs`` / ``outputs`` for a graph's boundary;
* **the item registry** — ``recordstream.items`` names every socket type an entry can have, and
  ``issubclass`` between two registered items is the one subtyping rule a wire may lean on;
* **the document's own shape** — a nested marker is a node wired into its parent's slot, a list
  member is ``slot[i]``, a ``!ref:`` is a wire, a ``flow:`` step's ``from:`` / ``merge_from:`` /
  ``bind:`` / ``requires`` are record, bind and gate wires.

Node names follow ``recordstream.flow.Tracer``: a flow step by its key, a list member by
``path[i]`` (``stream.ops[1]``), a nested marker by its document path (``processor.stream``),
the contract root by its key, and — for a graph WITHOUT a contract — whatever the host must
provide as ONE synthetic node ``<host>``. A graph with a contract needs no such node: the
contract's ``inputs`` ARE the host's side, and an entry nothing produces is refused by
recordstream's own chain check (``Tracer.check`` → ``check_chain``) in its own words.

A types-only declaration (a ``Transform``'s tuple of classes) gives no entry sockets: nothing
can be checked by name there, the node carries a note saying so, and the run shows the truth
(``docs/rewrite.md`` §5).

A SUBGRAPH (``recordstream.flow.subgraph.Subgraph``, a ``flow:`` used as one op, written inline or
``steps: {include: <file>.steps.yaml}``) is ONE node of kind ``subgraph`` on its level — ``record`` in,
``record`` and ``op`` out, the entry sockets the engine DERIVES from its inner ops, ``result`` its only
parameter (``steps`` is never a form field: a form edit of it flattened the whole block, measured) —
and a nested :class:`Model` in ``model.inside[<node>]``, derived by the same builder, so each inner node
carries its own ``file:line:col``. Inner nodes are named the way the engine's tracer descends:
``prep/grey``, ``stream.ops[1]/grey``, ``prep/inner/x``; each inside also holds two ``boundary`` nodes,
``prep/<in>`` (the record the subgraph receives, and every entry its inside reads from it) and
``prep/<out>`` (the record of its result step). The subgraph marker is BUILT while deriving — the chain
check needs the op, and without it one subgraph step switched the whole flow's check off (measured) —
and every edit takes a qualified name and changes the steps mapping of the node's own LEVEL (the top
``flow:``, or a subgraph's ``steps:``, inline or in its included file); a step name is scoped to that
mapping, so renaming an outer ``grey`` never touches an inner ``from: grey``.
"""

import copy
import errno
import importlib
import inspect
import os
import re
import threading
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import (
    TYPE_CHECKING,
    Any,
    Collection,
    Dict,
    Iterable,
    Iterator,
    List,
    Literal,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

import confluid
import yaml
from confluid import (
    ConfigFileNotFoundError,
    format_yaml_loc,
    input_specs,
    marks,
    output_specs,
    parse_param_docs,
    resolve_config_path,
)
from confluid.fluid import Fluid, Reference, Target
from confluid.registry import resolve_class
from recordstream.algorithm import Algorithm, algorithm_spec
from recordstream.flow.graph import FlowGraph
from recordstream.flow.steps import RESERVED_STEP_KEYS, SUBGRAPH_SEPARATOR
from recordstream.flow.trace import Tracer
from recordstream.items import get_item_type, item_type_names, item_types
from recordstream.ops.contract import GRAPH_SLOT_KINDS, ChainContractError, ContractError, GraphContract

from streamstudio._yamltext import Edit, YamlText, dedent, indent_lines, is_subgraph_marker, spell, spell_mapping

if TYPE_CHECKING:  # the annotation only: layout is imported on first use (`_layout`), a separate concern
    from recordstream.flow.subgraph import Steps, Subgraph

    from streamstudio.layout import Group

# ================================================================================================
# the vocabulary
# ================================================================================================

#: A socket type: a registered item class name (``Image``, ``Spectrogram``), one of the graph slot
#: kinds (``Record``, ``Stream``, ``Source``, ``Op``, ``Sink``, ``List[str]``), a JSON-schema scalar
#: for a parameter (``number``, ``integer``, ``string``, ``boolean``, ``object``), or the ``*`` wildcard.
SocketType = str

#: ``subgraph`` is a ``recordstream.flow.subgraph.Subgraph`` drawn as one node (its inside is
#: ``Model.inside[name]``); ``boundary`` is the ``<in>`` / ``<out>`` node of an inside.
NodeKind = Literal[
    "source",
    "op",
    "algorithm",
    "engine",
    "sink",
    "runnable",
    "value",
    "contract",
    "host",
    "alias",
    "subgraph",
    "boundary",
]
WireKind = Literal["record", "entry", "gate", "bind", "ref", "delivered", "list"]

RECORD: SocketType = "Record"
STREAM: SocketType = "Stream"
SOURCE: SocketType = "Source"
OP: SocketType = "Op"
SINK: SocketType = "Sink"
LIST_STR: SocketType = "List[str]"
OBJECT: SocketType = "object"
ANY: SocketType = "*"

#: The synthetic node standing for what a contract-less graph's host provides.
HOST = "<host>"
#: What joins a subgraph's name and an inner node's (``prep/grey``) — the engine's own separator, so the
#: canvas, the sidecar and the tracer's descent spell a node inside a subgraph the same way.
SEPARATOR = SUBGRAPH_SEPARATOR
#: The two boundary nodes of a subgraph's inside: what it receives and what it returns (``prep/<in>``).
BOUNDARY_IN = "<in>"
BOUNDARY_OUT = "<out>"
#: The class every subgraph step the editor WRITES names (Convert to subgraph, Insert template / steps file).
SUBGRAPH_CLASS = "recordstream.flow.subgraph.Subgraph"
#: The suffix of a steps file — a bare steps mapping, what ``steps: {include: …}`` reads and a template is.
STEPS_SUFFIX = ".steps.yaml"
#: The constructor parameter that gates a step on a flag (``recordstream.ops.contract.check_chain`` reads it).
GATE_PARAM = "requires"

#: Python scalars → the JSON-schema words a parameter socket speaks.
_SCALARS: Dict[type, SocketType] = {float: "number", int: "integer", str: "string", bool: "boolean"}
#: A graph slot kind → socket type: the scalar kinds are respelled, every other kind spells itself.
_SLOT_KINDS: Dict[str, SocketType] = {
    kind: _SCALARS.get({"str": str, "int": int, "float": float, "bool": bool}.get(kind, type), kind)
    for kind in GRAPH_SLOT_KINDS
}
#: The subtypes outside the item registry: a Stream IS a Source (it yields records), an integer IS a number.
_SUBTYPES: Dict[SocketType, Tuple[SocketType, ...]] = {STREAM: (SOURCE,), "integer": ("number",)}
#: Constructor slots that are WIRED rather than typed, by NAME — recordstream's engine grammar
#: (``Stream.source`` / ``.ops``, ``JointStream.streams``, ``ConcatSource.sources``,
#: ``DatasetProcessor.stream`` / ``.sink``, ``Pipeline.transforms``, ``RandomApply.op``). Their
#: annotations are ``Any``-shaped on purpose (a source is anything iterable), so the name is the
#: only honest signal. ``(socket type, is a list)``.
_NAMED_SLOTS: Dict[str, Tuple[SocketType, bool]] = {
    "source": (SOURCE, False),
    "sources": (SOURCE, True),
    "stream": (STREAM, False),
    "streams": (STREAM, True),
    "sink": (SINK, False),
    "op": (OP, False),
    "ops": (OP, True),
    "transforms": (OP, True),
}
#: The OP LISTS: the named list slots whose members are ops (a Stream's ``ops``, a Pipeline's ``transforms``). Adding a
#: class to a graph whose level is such a list inserts a MEMBER, and a member can be removed (the user's answer "yes",
#: 2026-09-29); a member is named by its position, ``stream.ops[1]``.
OP_LISTS: Tuple[str, ...] = tuple(sorted(name for name, (kind, many) in _NAMED_SLOTS.items() if kind == OP and many))
#: A confluid category that names a node kind outright; anything else is classified by shape.
_CATEGORY_KINDS: Dict[str, NodeKind] = {
    "op": "op",
    "source": "source",
    "engine": "engine",
    "sink": "sink",
    "value": "value",
    "contract": "contract",
}
#: The output a bare reference to a node means, per kind: its object.
_PRIMARY_OUTPUT: Dict[str, str] = {
    "source": "stream",
    "engine": "stream",
    "op": "op",
    "algorithm": "op",
    "subgraph": "op",
    "sink": "sink",
    "value": "object",
    "runnable": "runnable",
}
#: Top-level keys that are not nodes: the flow grammar, the loader's directives, a settings form.
_TOP_LEVEL_NOT_NODES = frozenset({"flow", "outputs", "app", "include", "import", "default_scopes", "scope_aliases"})


# ================================================================================================
# the model
# ================================================================================================


@dataclass(frozen=True)
class Socket:
    """One input or output of a node.

    Args:
        name: The socket's name — a slot, a declared entry, an outcome, ``record``.
        type: Its :data:`SocketType`.
        required: An input nothing may leave unwired.
        doc: One line from the declaration.
        entry: For an entry socket, the record key it reads or writes (after ``keys``); blank otherwise.
    """

    name: str
    type: SocketType
    required: bool = False
    doc: str = ""
    entry: str = ""


@dataclass(frozen=True)
class Param:
    """One constructor parameter of a node, as the document sets it.

    Args:
        name: The parameter.
        type: The JSON-schema scalar word (``number`` …) or ``List[str]`` / ``object``.
        value: What the document spells, or the constructor's default when it spells nothing.
        default: The constructor's default (``None`` for a required parameter).
        doc: The ``Args:`` line.
        schema: The JSON-schema fragment ``confluid.to_pydantic`` gives this parameter (enum, bounds, …).
        spelled: Whether the document spells the parameter (an unspelled one is INSERTED by ``set_param``).
        required: A parameter with no default, or declared ``Mandatory``.
    """

    name: str
    type: SocketType
    value: Any  # the document's value: any YAML scalar or container
    default: Any  # the constructor's default: any Python value
    doc: str = ""
    schema: Dict[str, Any] = field(default_factory=dict)
    spelled: bool = False
    required: bool = False


@dataclass
class Node:
    """One node of the drawing.

    Args:
        name: Its name (see the module docstring for the scheme).
        class_path: The dotted class path (``recordstream.ops.numpy.Threshold``); blank for a synthetic node.
        kind: Its :data:`NodeKind`.
        params: The constructor parameters, by name.
        inputs: Input sockets, in drawing order (``record`` first for an op).
        outputs: Output sockets.
        loc: ``file:line:col`` of the marker; blank for a synthetic node.
        source_file: The file the marker came from — an ``include:`` brings another file's nodes.
        note: A remark the drawing shows (a types-only declaration, an alias).
        alias_of: For an ``alias`` node, the node it is the same object as.
    """

    name: str
    class_path: str
    kind: NodeKind
    params: Dict[str, Param] = field(default_factory=dict)
    inputs: List[Socket] = field(default_factory=list)
    outputs: List[Socket] = field(default_factory=list)
    loc: str = ""
    source_file: str = ""
    note: str = ""
    alias_of: str = ""

    def input(self, name: str) -> Optional[Socket]:
        return next((socket for socket in self.inputs if socket.name == name), None)

    def output(self, name: str) -> Optional[Socket]:
        return next((socket for socket in self.outputs if socket.name == name), None)


@dataclass(frozen=True)
class Wire:
    """One wire: ``source.source_socket`` → ``target.target_socket``, of ``kind``, carrying ``type``."""

    source: str
    source_socket: str
    target: str
    target_socket: str
    kind: WireKind
    type: SocketType


@dataclass(frozen=True)
class Problem:
    """Something the document gets wrong; ``message`` already carries the ``loc`` text."""

    message: str
    loc: str


@dataclass
class Model:
    """The drawing: nodes, wires, and what the document gets wrong.

    Args:
        nodes: In document order (the synthetic ``<host>`` last).
        wires: In derivation order.
        problems: Located problems found while deriving (a wire whose types do not meet, a class
            that cannot be resolved, a constructor that refuses the document's values, …).
        outputs: The flow's output step(s), when the document has a ``flow:`` — for an inside, its result step.
        inside: Per ``subgraph`` node of THIS level, the model of its inside (recursive); every node there
            carries its qualified name (``prep/grey``). A problem found inside is listed there AND here, so
            the top model's problems are every problem of the document.
    """

    nodes: List[Node] = field(default_factory=list)
    wires: List[Wire] = field(default_factory=list)
    problems: List[Problem] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    inside: Dict[str, "Model"] = field(default_factory=dict)

    def node(self, name: str) -> Node:
        """The node named ``name`` on this level or inside any subgraph (a qualified name); a miss names the
        nodes there are, on every level."""
        holder = self.holder(name)
        if holder is None:
            raise KeyError(f"no node {name!r} (the nodes are: {self.all_names()})")
        return next(node for node in holder.nodes if node.name == name)

    def names(self) -> List[str]:
        """The names of THIS level's nodes."""
        return [node.name for node in self.nodes]

    def all_names(self) -> List[str]:
        """Every node's name on every level: each node, then — for a subgraph — its inside, depth first."""
        out: List[str] = []
        for node in self.nodes:
            out.append(node.name)
            if node.name in self.inside:
                out.extend(self.inside[node.name].all_names())
        return out

    def levels(self) -> List["Model"]:
        """This model and every inside, depth first — each one a level the canvas draws on its own."""
        out: List[Model] = [self]
        for inner in self.inside.values():
            out.extend(inner.levels())
        return out

    def holder(self, name: str) -> Optional["Model"]:
        """The level whose nodes include ``name`` (``None`` when no level does)."""
        return next((level for level in self.levels() if any(node.name == name for node in level.nodes)), None)

    def inside_of(self, name: str) -> "Model":
        """The inside of the subgraph node ``name`` (on any level); a ``KeyError`` when it is not one."""
        for level in self.levels():
            if name in level.inside:
                return level.inside[name]
        raise KeyError(f"no subgraph {name!r} (the subgraphs are: {[n for m in self.levels() for n in m.inside]})")


# ================================================================================================
# types: annotations, declarations and the meet rule
# ================================================================================================


def _unwrap(annotation: Any) -> Any:  # any annotation form
    """``Annotated[X, …]`` → ``X``; ``Optional[X]`` → ``X``; anything else unchanged."""
    while hasattr(annotation, "__metadata__"):
        annotation = annotation.__origin__
    if get_origin(annotation) is Union:
        members = [member for member in get_args(annotation) if member is not type(None)]
        if len(members) == 1:
            return _unwrap(members[0])
    return annotation


def _item_name(cls: type) -> Optional[str]:
    """The registered item name of a class — its own, or the nearest registered base."""
    registered = item_types()
    if not (isinstance(cls, type) and issubclass(cls, registered)):
        return None
    return next(klass.__name__ for klass in cls.__mro__ if klass in registered)


def _is_configurable(cls: Any) -> bool:  # any annotation leaf: a class, or something that is not one
    try:
        return bool(marks(cls).configurable) or marks(cls).category is not None
    except Exception:
        return False


def _kind_of(cls: type) -> NodeKind:
    """The node kind of a class: its confluid category where it has one, its shape otherwise.

    A subgraph is told by the class attribute the engine's own duck-typed checks read
    (``Subgraph.FLOW_SUBGRAPH``), before its category (``op``) would claim it."""
    if cls is GraphContract or issubclass(cls, GraphContract):
        return "contract"
    if getattr(cls, "FLOW_SUBGRAPH", False) is True:
        return "subgraph"
    if issubclass(cls, Algorithm):
        return "algorithm"
    try:
        category = marks(cls).category
        role = marks(cls).role
    except Exception:
        category, role = None, None
    if category in _CATEGORY_KINDS:
        return _CATEGORY_KINDS[category]
    if role == "dataset":
        return "source"
    run = getattr(cls, "run", None)
    if callable(run) and _takes_no_arguments(run):
        return "runnable"
    call = getattr(cls, "__call__", None)
    if call is not None and call is not type.__call__ and _takes_one_argument(call):
        return "op"
    if hasattr(cls, "__iter__"):
        return "source"
    return "value"


def _positional(func: Any) -> List[inspect.Parameter]:  # any callable attribute: a function, a slot wrapper, a method
    try:
        parameters = list(inspect.signature(func).parameters.values())
    except (TypeError, ValueError):
        return []
    return [p for p in parameters if p.name != "self" and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]


def _takes_no_arguments(func: Any) -> bool:  # see _positional
    return all(p.default is not inspect.Parameter.empty for p in _positional(func))


def _takes_one_argument(func: Any) -> bool:  # see _positional
    return len(_positional(func)) >= 1


def _socket_of_class(cls: type) -> SocketType:
    """The socket a whole object of ``cls`` travels on: what its primary output carries."""
    kind = _kind_of(cls)
    return {"engine": STREAM, "source": SOURCE, "op": OP, "algorithm": OP, "subgraph": OP, "sink": SINK}.get(
        kind, OBJECT
    )


def _socket_of_annotation(annotation: Any) -> SocketType:  # any annotation form
    """An annotation → socket type: a registered item by name, a scalar by its JSON word,
    ``List[str]`` by itself, a class by what it produces, anything else ``object``."""
    plain = _unwrap(annotation)
    if isinstance(plain, type):
        item = _item_name(plain)
        if item is not None:
            return item
        if plain in _SCALARS:
            return _SCALARS[plain]
        return _socket_of_class(plain)
    if get_origin(plain) in (list, List) and get_args(plain) == (str,):
        return LIST_STR
    return OBJECT


def _is_socket_annotation(annotation: Any) -> bool:  # any annotation form
    """Does the annotation alone make a constructor parameter a WIRED slot — an item or a configurable class?"""
    plain = _unwrap(annotation)
    return isinstance(plain, type) and (_item_name(plain) is not None or _is_configurable(plain))


def _entry_type(declared: str) -> SocketType:
    """A ``consumes`` / ``produces`` value → socket type: an item name, ``*``, or a scalar word."""
    if declared in item_type_names() or declared == ANY:
        return declared
    return _SLOT_KINDS.get(declared, declared)


def _schema_types(schema: Mapping[str, Any]) -> Tuple[SocketType, ...]:
    """The socket types a parameter's JSON-schema fragment accepts (``*`` for an open one)."""
    found: List[SocketType] = []

    def visit(fragment: Mapping[str, Any]) -> None:
        if "anyOf" in fragment:
            for member in fragment["anyOf"]:
                visit(member)
            return
        if "$ref" in fragment:
            found.append(OBJECT)
            return
        kind = fragment.get("type")
        if kind is None:
            found.append(ANY)
        elif kind == "array":
            found.append(LIST_STR if fragment.get("items", {}).get("type") == "string" else OBJECT)
        elif kind != "null":
            found.append(str(kind))

    visit(schema)
    return tuple(dict.fromkeys(found)) or (ANY,)


def _param_type(schema: Mapping[str, Any]) -> SocketType:
    """The one word a parameter socket shows: its first accepted type."""
    return _schema_types(schema)[0]


def _meets(source: SocketType, target: SocketType) -> bool:
    """Do two socket types MEET — identical, a wildcard, a registered item into its base, a declared subtype."""
    if source == target or ANY in (source, target):
        return True
    registered = item_type_names()
    if source in registered and target in registered:
        return issubclass(get_item_type(source), get_item_type(target))
    return target in _SUBTYPES.get(source, ())


def _meets_any(source: SocketType, targets: Iterable[SocketType]) -> bool:
    return any(_meets(source, target) for target in targets)


def supertypes() -> Dict[SocketType, List[SocketType]]:
    """Every socket type that has supertypes, with the types it may be wired INTO — the meet rule of
    :func:`_meets` as a table, for a page that cannot call ``issubclass``.

    A registered item lists its registered bases (``issubclass`` between registered items is the one
    subtyping rule; an unregistered class between them, like ``NDArrayItem``, is never a socket type
    and so never appears); ``Stream`` lists ``Source`` and ``integer`` lists ``number`` (:data:`_SUBTYPES`).
    Every registered item appears, with ``[]`` when it has no registered base, so a consumer can tell
    "no ancestors" from "not a type". Identity and the ``*`` wildcard are not listed: they hold for
    every type and the consumer applies them itself.
    """
    table: Dict[SocketType, List[SocketType]] = {
        name: [
            other
            for other in item_type_names()
            if other != name and issubclass(get_item_type(name), get_item_type(other))
        ]
        for name in item_type_names()
    }
    for source, targets in _SUBTYPES.items():
        table.setdefault(source, []).extend(targets)
    return table


def _split_loc(loc: str) -> Tuple[str, int, int]:
    """``file:line:col`` → ``(file, line, col)``; the file may itself contain colons."""
    file, line, col = loc.rsplit(":", 2)
    return file, int(line), int(col)


# ================================================================================================
# a class, described
# ================================================================================================


class _Declaration(NamedTuple):
    """What an op declares about the record: by NAME, by TYPE, or not at all."""

    consumes: Dict[str, SocketType]
    produces: Dict[str, SocketType]
    types_only: str  # the note when the declaration is a tuple of classes, else blank
    flags: Tuple[str, ...]
    reports: str
    requires: str


_NO_DECLARATION = _Declaration({}, {}, "", (), "", "")

_loaded_entry_points = False


def _resolve(target: Any) -> Optional[type]:  # a class object, a registry key or a dotted path
    """The class a marker's target names, or ``None``.

    A registry key (``Stream``) resolves only once the module defining it has been imported; on a
    miss the entry points are loaded once per process (``confluid.load_configurables``), which is
    what a running app does at start.
    """
    global _loaded_entry_points
    if isinstance(target, type):
        return target
    if not isinstance(target, str):
        return None
    try:
        found = resolve_class(target)
    except Exception:
        found = None
    if found is None and "." not in target and not _loaded_entry_points:
        _loaded_entry_points = True
        try:
            confluid.load_configurables()
            found = resolve_class(target)
        except Exception:
            found = None
    return found if isinstance(found, type) else None


def _dotted(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def _holds_fluid(value: Any) -> bool:  # any document value
    if isinstance(value, Fluid):
        return True
    if isinstance(value, (list, tuple)):
        return any(_holds_fluid(item) for item in value)
    if isinstance(value, dict):
        return any(_holds_fluid(item) for item in value.values())
    return False


def _build(marker: Target, kwargs: Dict[str, Any]) -> Tuple[Any, Optional[str]]:
    """The marker's object built from ITS kwargs (nested markers or references: nothing built).

    Constructors do no work (workspace rule), so building is what makes the declarations readable
    — an Algorithm derives ``consumes`` from ``keys``, a domain op from ``field`` — and it is where
    confluid's validation lives: a value outside a ``Literal`` is refused HERE, located.
    """
    if any(_holds_fluid(value) for value in kwargs.values()):
        return None, None
    standalone = copy.copy(marker)
    standalone.kwargs = dict(kwargs)
    try:
        return confluid.flow(standalone), None
    except Exception as exc:  # noqa: BLE001 — every constructor failure is a located problem
        return None, str(exc)


def _holds_reference(value: Any) -> bool:  # any document value
    """Does ``value`` hold a ``!ref:`` anywhere (inside markers too) — something only the whole document resolves."""
    if isinstance(value, Reference):
        return True
    if isinstance(value, Fluid):
        return _holds_reference(value.kwargs)
    if isinstance(value, (list, tuple)):
        return any(_holds_reference(item) for item in value)
    if isinstance(value, dict):
        return any(_holds_reference(item) for item in value.values())
    return False


def _open_subgraph(op: Any) -> None:  # op: a built recordstream.flow.subgraph.Subgraph
    """Parse a built subgraph's inside — where the engine refuses what a subgraph cannot hold (a ``result:``
    naming no inner step, an expanding inner op, an inner step reading a step outside), before any record."""
    op.flow_steps


def _build_subgraph(marker: Target) -> Tuple[Optional["Subgraph"], Optional[str]]:
    """A subgraph marker BUILT, and its inside opened — or ``(None, the refusal)``.

    :func:`_build` builds nothing whose settings hold markers, and a subgraph's ``steps`` always do: without
    this the chain check had no op for the step and skipped the WHOLE flow (measured on the prototype: a
    real defect two steps later went unreported, ``check() == []``). The marker is deep-copied so the
    derivation's own markers stay as they were loaded; a ``!ref:`` inside is left to the whole document
    (``None, None``: nothing is refused, the step is simply not checked).
    """
    kwargs = {str(k): v for k, v in marker.kwargs.items() if str(k) not in RESERVED_STEP_KEYS}
    if _holds_reference(kwargs):
        return None, None
    standalone = copy.deepcopy(marker)
    standalone.kwargs = {k: v for k, v in standalone.kwargs.items() if str(k) not in RESERVED_STEP_KEYS}
    try:
        built = cast("Subgraph", confluid.flow(standalone))  # kind "subgraph": the marker names the engine's Subgraph
        _open_subgraph(built)
    except Exception as exc:  # noqa: BLE001 — the engine's refusal (or a constructor's) is a located problem
        return None, str(exc)
    return built, None


def _declared(instance: Any, cls: type, name: str) -> Any:  # a declaration attribute of any shape
    """One declaration, read off the instance when there is one, else off the class (a property there is unreadable)."""
    if instance is not None:
        try:
            return getattr(instance, name, None)
        except Exception:
            return None
    value = getattr(cls, name, None)
    return None if isinstance(value, property) else value


def _declaration_of(cls: type, instance: Any) -> _Declaration:  # instance: the built op of any class, or None
    consumes = _declared(instance, cls, "consumes")
    produces = _declared(instance, cls, "produces")
    flags = tuple(str(flag) for flag in (_declared(instance, cls, "flags") or ()))
    reports = str(_declared(instance, cls, "reports") or "")
    requires = str(_declared(instance, cls, GATE_PARAM) or "")
    tuples = [value for value in (consumes, produces) if isinstance(value, (tuple, list)) and value]
    if tuples:
        names = " → ".join(
            ", ".join(getattr(kind, "__name__", str(kind)) for kind in value) or "nothing"
            for value in (consumes or (), produces or ())
        )
        note = (
            f"{cls.__name__} declares the TYPES it consumes and produces ({names}), not the record "
            "entries — nothing can be checked by name here; the run shows what it wrote"
        )
        return _Declaration({}, {}, note, flags, reports, requires)
    return _Declaration(
        {str(k): _entry_type(str(v)) for k, v in consumes.items()} if isinstance(consumes, dict) else {},
        {str(k): _entry_type(str(v)) for k, v in produces.items()} if isinstance(produces, dict) else {},
        "",
        flags,
        reports,
        requires,
    )


def _output_sockets(cls: type) -> List[Socket]:
    """A node's ``@output`` properties as sockets, typed from each getter's return annotation."""
    sockets: List[Socket] = []
    try:
        specs = output_specs(cls)
    except Exception:
        return sockets
    for spec in specs:
        member = getattr(cls, spec["name"], None)
        annotation: Any = Any
        if isinstance(member, property) and member.fget is not None:
            try:
                annotation = get_type_hints(member.fget).get("return", Any)
            except Exception:
                annotation = Any
        sockets.append(Socket(spec["name"], _socket_of_annotation(annotation), doc=spec["description"]))
    return sockets


class _Description(NamedTuple):
    params: Dict[str, Param]
    inputs: List[Socket]
    outputs: List[Socket]
    note: str
    instance: Any  # the built object, or None
    declaration: _Declaration
    error: Optional[str]  # the constructor's refusal, located


def _describe(cls: type, kind: NodeKind, kwargs: Dict[str, Any], marker: Target) -> _Description:
    """Params and sockets of a class as the document configures it."""
    if kind == "contract":
        return _describe_contract(kwargs)
    if kind == "subgraph":
        # Its steps are its inside (Model.inside), never a slot and never a form field: a form edit of
        # `steps` replaced the whole block with one flow-style line (measured: 18 -> 11 lines, 4 -> 3 comments).
        instance, error = _build_subgraph(marker)
        inputs: List[Socket] = []
        socket_names = {"steps"}
    else:
        instance, error = _build(marker, kwargs)
        inputs, socket_names = _slot_sockets(cls, kwargs)
    declaration = _declaration_of(cls, instance) if kind in ("op", "algorithm", "subgraph") else _NO_DECLARATION
    params = _params(cls, kwargs, socket_names)
    outputs: List[Socket] = []
    note = ""
    if kind == "algorithm":
        entry_inputs, entry_outputs = _algorithm_sockets(cls, kwargs)
        inputs = [Socket("record", RECORD), *inputs, *entry_inputs]
        outputs = [Socket("record", RECORD), Socket("op", OP), *entry_outputs]
    elif kind in ("op", "subgraph"):
        inputs = [
            Socket("record", RECORD),
            *inputs,
            *(Socket(k, t, required=True, entry=k) for k, t in declaration.consumes.items()),
        ]
        outputs = [
            Socket("record", RECORD),
            Socket("op", OP),
            *(Socket(k, t, entry=k) for k, t in declaration.produces.items()),
            *_output_sockets(cls),
            *(Socket(flag, RECORD, doc="the record, when this outcome fires") for flag in declaration.flags),
        ]
        if declaration.reports:
            outputs.append(Socket(f"report:{declaration.reports}", "string", doc="this node's finding"))
        note = declaration.types_only
    elif kind == "source":
        outputs = [Socket("stream", SOURCE), *_output_sockets(cls)]
    elif kind == "engine":
        outputs = [Socket("stream", STREAM), *_output_sockets(cls)]
    elif kind == "sink":
        outputs = [Socket("sink", SINK), *_output_sockets(cls)]
    elif kind == "runnable":
        outputs = [Socket("runnable", OBJECT), *_output_sockets(cls)]
    else:  # value
        outputs = [*_output_sockets(cls), Socket("object", OBJECT)]
    return _Description(params, inputs, outputs, note, instance, declaration, error)


def _describe_contract(kwargs: Dict[str, Any]) -> _Description:
    """A ``GraphContract``: one INPUT per declared output slot, one OUTPUT per declared input.

    A slot delivered by a LITERAL (``classes: [cat, dog]``) is recorded as a spelled parameter of
    the slot's name, so the dangling check sees it filled; a slot delivered by a marker or a
    reference becomes a wire.
    """
    raw_outputs, raw_inputs, raw_delivered = kwargs.get("outputs"), kwargs.get("inputs"), kwargs.get("delivered")
    declared_outputs: Dict[str, Any] = raw_outputs if isinstance(raw_outputs, dict) else {}
    declared_inputs: Dict[str, Any] = raw_inputs if isinstance(raw_inputs, dict) else {}
    inputs = [Socket(str(slot), _slot_kind(kind), required=True) for slot, kind in declared_outputs.items()]
    outputs = [Socket(str(slot), _slot_kind(kind)) for slot, kind in declared_inputs.items()]
    params = _params(GraphContract, kwargs, {"delivered"})
    delivered: Dict[str, Any] = raw_delivered if isinstance(raw_delivered, dict) else {}
    for slot, value in delivered.items():
        if value is not None and not _holds_fluid(value):
            params[str(slot)] = Param(
                str(slot), _slot_kind(declared_outputs.get(slot, ANY)), value, None, "delivered literally", {}, True
            )
    return _Description(params, inputs, outputs, "", None, _NO_DECLARATION, None)


def _slot_kind(kind: Any) -> SocketType:  # a declared kind: a string in the document
    return _SLOT_KINDS.get(str(kind), str(kind))


def _signature(cls: type) -> List[inspect.Parameter]:
    try:
        parameters = list(inspect.signature(cls).parameters.values())
    except (TypeError, ValueError):
        return []
    return [p for p in parameters if p.name not in ("self", "cls") and p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)]


def _hints(cls: type) -> Dict[str, Any]:
    try:
        return get_type_hints(getattr(cls, "__init__"), include_extras=True)
    except Exception:
        return {}


def _slot_sockets(cls: type, kwargs: Dict[str, Any]) -> Tuple[List[Socket], Set[str]]:
    """Constructor parameters that are WIRED: by name (the engine grammar), by annotation (an item or
    a configurable class), or because the document puts a marker or reference there."""
    sockets: List[Socket] = []
    names: Set[str] = set()
    try:
        required = {spec["name"]: bool(spec["required"]) for spec in input_specs(cls)}
    except Exception:
        required = {}
    hints = _hints(cls)
    for parameter in _signature(cls):
        name = parameter.name
        annotation = hints.get(name, parameter.annotation)
        value = kwargs.get(name)
        many = False
        socket_type: Optional[SocketType] = None
        if name in _NAMED_SLOTS:
            socket_type, many = _NAMED_SLOTS[name]
        elif _is_socket_annotation(annotation):
            socket_type = _socket_of_annotation(annotation)
        elif isinstance(value, Fluid):
            socket_type = ANY
        elif isinstance(value, (list, tuple)) and any(isinstance(item, Fluid) for item in value):
            socket_type, many = ANY, True
        if socket_type is None:
            continue
        names.add(name)
        if many:
            count = len(value) if isinstance(value, (list, tuple)) and value else 1
            sockets.extend(
                Socket(f"{name}[{i}]", socket_type, required=required.get(name, False)) for i in range(count)
            )
        else:
            sockets.append(Socket(name, socket_type, required=required.get(name, False)))
    return sockets, names


def _params(cls: type, kwargs: Dict[str, Any], sockets: Set[str]) -> Dict[str, Param]:
    """Every constructor parameter that is not a socket, with the document's value or the default."""
    try:
        required = {spec["name"]: bool(spec["required"]) for spec in input_specs(cls)}
    except Exception:
        required = {}
    try:
        from confluid import to_pydantic

        schema = to_pydantic(cls).model_json_schema().get("properties", {})
    except Exception:
        schema = {}
    try:
        docs = parse_param_docs(cls)
    except Exception:
        docs = {}
    params: Dict[str, Param] = {}
    for parameter in _signature(cls):
        name = parameter.name
        if name in sockets or name in RESERVED_STEP_KEYS:
            continue
        default = None if parameter.default is inspect.Parameter.empty else parameter.default
        spelled = name in kwargs
        fragment = dict(schema.get(name, {}))
        params[name] = Param(
            name,
            _param_type(fragment),
            kwargs[name] if spelled else default,
            default,
            docs.get(name, ""),
            fragment,
            spelled,
            required.get(name, parameter.default is inspect.Parameter.empty),
        )
    return params


def _algorithm_sockets(cls: type, kwargs: Dict[str, Any]) -> Tuple[List[Socket], List[Socket]]:
    """An Algorithm's ``Input`` / ``Output`` slots as entry sockets, the entries following ``keys`` and ``replaces``."""
    spec = algorithm_spec(cls)
    raw_keys = kwargs.get("keys")
    keys: Dict[str, Any] = raw_keys if isinstance(raw_keys, dict) else {}
    entries = {slot.name: str(keys.get(slot.name, slot.name)) for slot in spec.inputs}
    for slot in spec.outputs:
        fallback = entries[slot.replaces] if slot.replaces else slot.name
        entries[slot.name] = str(keys.get(slot.name, fallback))
    inputs = [
        Socket(slot.name, _socket_of_annotation(slot.annotation), slot.required, slot.doc, entries[slot.name])
        for slot in spec.inputs
    ]
    outputs = [
        Socket(slot.name, _socket_of_annotation(slot.annotation), False, slot.doc, entries[slot.name])
        for slot in spec.outputs
    ]
    return inputs, outputs


# ================================================================================================
# deriving the model from a document
# ================================================================================================


class _Step(NamedTuple):
    """One flow step as the derivation saw it."""

    name: str
    source: Optional[str]  # the step whose record it reads (``from:`` or the previous step)
    merge_from: Tuple[str, ...]
    bind: Dict[str, str]
    reserved: Dict[str, Any]  # the raw reserved keys, handed to the engine's parser for the check


class _Flow(NamedTuple):
    steps: List[_Step]
    outputs: str
    sound: bool  # no structural problem (an unknown ``from:`` / ``bind:``) — the chain check may run


class _Derivation(NamedTuple):
    """A model plus what the check needs that the model does not show."""

    model: Model
    instances: Dict[str, Any]  # node name -> the built object (None when nothing could be built)
    flow: Optional[_Flow]
    streams: List[Tuple[str, List[str]]]  # (stream node, its member nodes)
    contract: Optional[str]  # the contract node's name
    provided: Tuple[str, ...]  # the record entries the host provides
    files: List[Path]  # the main file and every included one
    subflows: Dict[str, _Flow]  # each subgraph node's inner steps, by its (qualified) name


#: A ``!ref:`` still to be wired: (target node, target socket, the reference, the wire kind).
_Pending = Tuple[str, str, Reference, WireKind]


class _Builder:
    """Derives a :class:`Model` from a loaded document — one pass, in document order.

    Args:
        path: The document's file.
        prefix: What every node name of this level starts with — ``""`` at the top, ``"prep/"`` for the
            inside of the subgraph ``prep`` (a child builder derives each inside).
        pending: The root's list of ``!ref:`` still to wire. A reference names a TOP-LEVEL key, which an
            inside may use too, so every level hands its references to the root, which resolves them once
            every level's nodes exist.
    """

    def __init__(self, path: Path, prefix: str = "", pending: Optional[List[_Pending]] = None) -> None:
        self.path = path
        self.prefix = prefix
        self.model = Model()
        self.instances: Dict[str, Any] = {}
        self.declarations: Dict[str, _Declaration] = {}
        self.seen: Dict[int, str] = {}
        self.pending: List[_Pending] = [] if pending is None else pending
        self.refused: Set[Tuple[str, str]] = set()
        self.flow: Optional[_Flow] = None
        self.subflows: Dict[str, _Flow] = {}
        self.streams: List[Tuple[str, List[str]]] = []
        self.contract: Optional[str] = None
        self.host: Optional[Node] = None
        self.provided: Tuple[str, ...] = ()

    # -- entry -------------------------------------------------------------------------------

    def derive(self, doc: Any) -> _Derivation:  # the loaded document: a mapping, or a root marker
        if isinstance(doc, Target):
            self.add_marker(self.path.stem, doc)
        elif isinstance(doc, dict):
            for key, value in doc.items():
                key = str(key)
                if key == "flow" and isinstance(value, dict):
                    continue  # after the top-level nodes: a step may bind to them
                if key == "outputs" and isinstance(value, str):
                    self.model.outputs = [value]
                elif key in _TOP_LEVEL_NOT_NODES:
                    continue
                elif isinstance(value, Target):
                    self.add_marker(key, value)
                elif isinstance(value, dict) and any(isinstance(item, Fluid) for item in value.values()):
                    for sub, item in value.items():
                        if isinstance(item, Target):
                            self.add_marker(f"{key}.{sub}", item)
                elif isinstance(value, list) and any(isinstance(item, Fluid) for item in value):
                    for index, item in enumerate(value):
                        if isinstance(item, Target):
                            self.add_marker(f"{key}[{index}]", item)
            if isinstance(doc.get("flow"), dict):
                self.derive_flow(doc["flow"], str(doc.get("outputs", "") or ""))
        self.resolve_references()
        if self.host is not None:
            self.model.nodes.append(self.host)
        return _Derivation(
            self.model,
            self.instances,
            self.flow,
            self.streams,
            self.contract,
            self.provided,
            [self.path.resolve()],
            self.subflows,
        )

    def derive_inside(self, steps: "Steps", result: str, file: str) -> None:
        """A subgraph's inside, as a child builder: its steps (the flow grammar, names qualified by the
        prefix), the boundary ``<in>`` — the record, and every entry nothing inside produces, wired to the
        steps that read it — and ``<out>``, fed by the result step (blank = the last step)."""
        receives = Node(self.prefix + BOUNDARY_IN, "", "boundary", {}, [], [Socket("record", RECORD)], "", file)
        receives.note = "what the subgraph receives: the record, and every entry its inside reads from it"
        self.host = receives
        self.derive_flow(steps, result, file)
        assert self.flow is not None
        self.model.nodes.insert(0, receives)
        head = self.flow.steps[0] if self.flow.steps else None
        if head is not None and head.source is None and "from" not in head.reserved:
            self.model.wires.insert(0, Wire(receives.name, "record", head.name, "record", "record", RECORD))
        returns = Node(self.prefix + BOUNDARY_OUT, "", "boundary", {}, [Socket("record", RECORD)], [], "", file)
        result_step = self.flow.outputs
        returns.note = f"what the subgraph returns: the record of {result_step[len(self.prefix):]!r}"
        self.model.nodes.append(returns)
        if result_step in [step.name for step in self.flow.steps]:
            self.model.wires.append(Wire(result_step, "record", returns.name, "record", "record", RECORD))
            self.model.outputs = [result_step]

    def add_inside(self, node: Node, kwargs: Dict[str, Any], refusal: Optional[str]) -> None:
        """Derive the inside of the subgraph ``node`` into ``model.inside[node.name]``; its problems are this
        level's problems too (so the top model lists every problem of the document).

        ``refusal`` is the engine's (or a constructor's) refusal to build and open the subgraph. It is this
        node's problem only when the inside derived clean — a defect the inside already places at its own
        line (a refused ``Literal`` of an inner op, an inner ``from:`` naming a step outside) would otherwise
        be reported twice, once at the inner node and once at the subgraph (measured: both messages)."""
        steps, result = kwargs.get("steps"), kwargs.get("result")
        child = _Builder(self.path, prefix=node.name + SEPARATOR, pending=self.pending)
        child.derive_inside(
            steps if isinstance(steps, dict) else {}, result if isinstance(result, str) else "", node.source_file
        )
        self.model.inside[node.name] = child.model
        assert child.flow is not None
        self.subflows[node.name] = child.flow
        self.subflows.update(child.subflows)
        self.model.problems.extend(child.model.problems)
        if refusal and not child.model.problems:
            self.problem(f"{node.name}: {refusal}", node.loc)

    # -- nodes -------------------------------------------------------------------------------

    def problem(self, message: str, loc: str) -> None:
        self.model.problems.append(Problem(message, loc))

    def file_of(self, loc: str) -> str:
        return _split_loc(loc)[0] if loc else str(self.path)

    def add_marker(self, name: str, marker: Target, in_flow: bool = False) -> Node:
        """A node for a marker: its class described, its nested values wired into its slots."""
        loc = format_yaml_loc(marker)
        if id(marker) in self.seen:
            original = self.model.node(self.seen[id(marker)])
            alias = Node(
                name, original.class_path, "alias", original.params, [], original.outputs, loc, self.file_of(loc)
            )
            alias.alias_of = original.name
            alias.note = f"the same object as {original.name}"
            self.model.nodes.append(alias)
            return alias
        self.seen[id(marker)] = name
        kwargs = {str(k): v for k, v in marker.kwargs.items() if str(k) not in RESERVED_STEP_KEYS}
        cls = _resolve(marker.target)
        if cls is None:
            node = Node(name, str(marker.target), "op" if in_flow else "value", {}, [], [], loc, self.file_of(loc))
            if in_flow:
                node.inputs, node.outputs = [Socket("record", RECORD)], [Socket("record", RECORD), Socket("op", OP)]
            self.model.nodes.append(node)
            self.problem(f"{name}: cannot resolve the class {str(marker.target)!r} at {loc}", loc)
            return node
        kind = _kind_of(cls)
        described = _describe(cls, kind, kwargs, marker)
        node = Node(
            name, _dotted(cls), kind, described.params, described.inputs, described.outputs, loc, self.file_of(loc)
        )
        node.note = described.note
        self.model.nodes.append(node)
        self.instances[name] = described.instance
        self.declarations[name] = described.declaration
        if described.error and kind != "subgraph":  # a subgraph's refusal waits for its inside (add_inside)
            self.problem(f"{name}: {described.error}", loc)
        if kind == "contract":
            if self.contract is None:
                self.contract = name
            else:
                self.problem(f"{name}: a second graph contract at {loc} — the root is {self.contract}", loc)
        if kind == "subgraph":
            self.add_inside(node, kwargs, described.error)  # its steps are its inside, never nodes of this level
            return node
        members: List[str] = []
        for key, value in kwargs.items():
            self.wire_value(node, key, value, kind, members)
        if kind == "engine" and members:
            self.streams.append((name, members))
            self.chain(members)
        return node

    def wire_value(self, node: Node, key: str, value: Any, kind: NodeKind, members: List[str]) -> None:
        """A nested marker, a reference, or a container of them under ``key`` → child nodes and wires."""
        if isinstance(value, Reference):
            self.pending.append((node.name, key, value, "delivered" if kind == "contract" else "ref"))
        elif isinstance(value, Target):
            child = self.add_marker(f"{node.name}.{key}", value)
            self.wire_into(child, node, key, "ref")
        elif isinstance(value, (list, tuple)) and any(isinstance(item, Fluid) for item in value):
            for index, item in enumerate(value):
                socket = f"{key}[{index}]"
                if isinstance(item, Reference):
                    self.pending.append((node.name, socket, item, "list"))
                elif isinstance(item, Target):
                    child = self.add_marker(f"{node.name}.{socket}", item, in_flow=key in _NAMED_SLOTS)
                    self.wire_into(child, node, socket, "list")
                    if key in ("ops", "transforms"):
                        members.append(child.name)
        elif isinstance(value, dict) and any(isinstance(item, Fluid) for item in value.values()):
            delivered = kind == "contract" and key == "delivered"
            for sub, item in value.items():
                socket = str(sub) if delivered else f"{key}.{sub}"
                if isinstance(item, Reference):
                    self.pending.append((node.name, socket, item, "delivered" if delivered else "ref"))
                elif isinstance(item, Target):
                    child = self.add_marker(f"{node.name}.{key}.{sub}", item)
                    self.wire_into(child, node, socket, "delivered" if delivered else "ref")

    def wire_into(self, source: Node, target: Node, socket: str, kind: WireKind) -> None:
        """Wire a child's chosen output into ``socket`` of its parent, creating the socket when the class
        did not declare the slot (a parameter the document filled with a marker)."""
        accepted = self.accepted_types(target, socket)
        output = self.choose_output(source, accepted)
        if output is None:
            self.problem(f"{target.name}.{socket}: {source.name} has no output socket at {target.loc}", target.loc)
            return
        if target.input(socket) is None and socket not in target.params:
            target.inputs.append(Socket(socket, output.type))
            accepted = (output.type,)
        self.wire(source, output, target, socket, kind, accepted)

    def accepted_types(self, node: Node, socket: str) -> Tuple[SocketType, ...]:
        """What a target socket takes: the input's type, a parameter's schema types, or anything."""
        declared = node.input(socket)
        if declared is not None:
            return (declared.type,)
        if socket in node.params:
            return _schema_types(node.params[socket].schema)
        return (ANY,)

    @staticmethod
    def choose_output(node: Node, accepted: Iterable[SocketType]) -> Optional[Socket]:
        """The output a bare reference to ``node`` means: the first that meets the target, else its object."""
        accepted = tuple(accepted)
        for output in node.outputs:
            if _meets_any(output.type, accepted):
                return output
        primary = node.output(_PRIMARY_OUTPUT.get(node.kind, ""))
        return primary or (node.outputs[0] if node.outputs else None)

    def wire(
        self, source: Node, output: Socket, target: Node, socket: str, kind: WireKind, accepted: Iterable[SocketType]
    ) -> bool:
        """Add a wire when the types meet, else a located problem."""
        accepted = tuple(accepted)
        if not _meets_any(output.type, accepted):
            shown = accepted[0] if len(accepted) == 1 else " | ".join(accepted)
            self.problem(
                f"cannot wire {source.name}.{output.name} ({output.type}) into {target.name}.{socket} ({shown}) "
                f"at {target.loc}: the types do not meet",
                target.loc,
            )
            self.refused.add((target.name, socket))
            return False
        # A !ref: the root resolves may point into a subgraph: the wire belongs to the level that draws its target.
        (self.model.holder(target.name) or self.model).wires.append(
            Wire(source.name, output.name, target.name, socket, kind, output.type)
        )
        return True

    def resolve_references(self) -> None:
        names = self.model.names()
        for target_name, socket, ref, kind in self.pending:
            target = self.model.node(target_name)
            wanted = str(ref.target)
            if wanted not in names:
                self.problem(
                    f"{target_name}.{socket}: !ref:{wanted} names no node at {target.loc} (the nodes are: {names})",
                    target.loc,
                )
                continue
            self.wire_into(self.model.node(wanted), target, socket, kind)

    # -- chains ------------------------------------------------------------------------------

    def chain(self, members: Sequence[str]) -> None:
        """A Stream's ops: a record chain in list order, plus the entry wires along it."""
        for previous, current in zip(members, members[1:]):
            source, target = self.model.node(previous), self.model.node(current)
            self.wire(source, Socket("record", RECORD), target, "record", "record", (RECORD,))
        lineage: Dict[str, List[str]] = {}
        for index, name in enumerate(members):
            lineage[name] = list(reversed(members[:index]))
        for name in members:
            self.entry_wires(name, lineage[name], host=False)

    def entry_wires(self, name: str, producers: Sequence[str], *, host: bool) -> None:
        """Who produced what ``name`` reads: the nearest node in its record lineage with that entry."""
        node = self.model.node(name)
        for socket in list(node.inputs):
            if not socket.entry:
                continue
            producer = next(
                (
                    (candidate, out)
                    for candidate in (self.model.node(p) for p in producers)
                    for out in candidate.outputs
                    if out.entry == socket.entry
                ),
                None,
            )
            if producer is not None:
                self.wire(producer[0], producer[1], node, socket.name, "entry", (socket.type,))
                continue
            if self.contract is not None:
                root = self.model.node(self.contract)
                declared = root.output(socket.entry)
                if declared is not None:
                    self.wire(root, declared, node, socket.name, "entry", (socket.type,))
                continue
            if host:
                if self.host is None:
                    self.host = Node(HOST, "", "host", {}, [], [], "", str(self.path))
                    self.host.note = "what the host provides — this graph declares no contract"
                if self.host.output(socket.entry) is None:
                    self.host.outputs.append(Socket(socket.entry, socket.type, entry=socket.entry))
                out = self.host.output(socket.entry)
                assert out is not None
                self.wire(self.host, out, node, socket.name, "entry", (socket.type,))

    def derive_flow(self, flow: Dict[Any, Any], outputs: str, file: str = "") -> None:
        """A ``flow:`` mapping: one node per step; record wires from ``from:`` (or the previous step)
        and ``merge_from:``; bind wires from a step's output; gate wires from ``requires``; entry wires
        along the record lineage.

        Inside a subgraph every name is qualified by the builder's prefix — the step ``grey`` is the node
        ``prep/grey``, and ``from: grey`` there names ``prep/grey``, never an outer ``grey`` — while a problem
        quotes the reference as the document spells it."""
        steps: List[_Step] = []
        sound = True
        previous: Optional[str] = None
        for raw_name, value in flow.items():
            name = self.prefix + str(raw_name)
            marker: Optional[Target] = None
            reserved: Dict[str, Any] = {}
            if isinstance(value, Target):
                marker = value
                reserved = {k: value.kwargs[k] for k in RESERVED_STEP_KEYS if k in value.kwargs}
            elif isinstance(value, dict):
                op = value.get("op")
                marker = op if isinstance(op, Target) else None
                reserved = {k: v for k, v in value.items() if k in RESERVED_STEP_KEYS}
            if marker is not None:
                node = self.add_marker(name, marker, in_flow=True)
            else:
                node = Node(
                    name,
                    "",
                    "op",
                    {},
                    [Socket("record", RECORD)],
                    [Socket("record", RECORD)],
                    "",
                    file or str(self.path),
                )
                node.note = "a fan-in step: it merges records and has no op"
                self.model.nodes.append(node)
                self.instances[name] = None
                self.declarations[name] = _NO_DECLARATION
            known = [step.name for step in steps]
            from_ = reserved.get("from")
            source = self.prefix + str(from_) if from_ is not None else previous
            if from_ is not None and source not in known:
                self.problem(
                    f"{name}: from: {str(from_)!r} does not name an earlier step{self.inside()} at {node.loc}", node.loc
                )
                sound, source = False, None
            merge_raw = reserved.get("merge_from") or ()
            written = (str(merge_raw),) if isinstance(merge_raw, str) else tuple(str(m) for m in merge_raw)
            merge_from = tuple(self.prefix + ref for ref in written)
            for ref in written:
                if self.prefix + ref not in known:
                    self.problem(
                        f"{name}: merge_from: {ref!r} does not name an earlier step{self.inside()} at {node.loc}",
                        node.loc,
                    )
                    sound = False
            bind = (
                {str(k): str(v) for k, v in (reserved.get("bind") or {}).items()}
                if isinstance(reserved.get("bind"), dict)
                else {}
            )
            if source is not None:
                self.wire(self.model.node(source), Socket("record", RECORD), node, "record", "record", (RECORD,))
            for ref in merge_from:
                if ref in known:
                    self.wire(self.model.node(ref), Socket("record", RECORD), node, "record", "record", (RECORD,))
            for param, ref in bind.items():
                if not self.bind_wire(node, param, ref, known):
                    sound = False
            steps.append(_Step(name, source, merge_from, bind, reserved))
            previous = name
        self.flow = _Flow(steps, self.prefix + outputs if outputs else (steps[-1].name if steps else ""), sound)
        self.gate_wires(steps)
        lineage: Dict[str, List[str]] = {}
        for step in steps:
            reaching: List[str] = []
            for head in ([step.source] if step.source else []) + list(step.merge_from):
                for candidate in [head, *lineage.get(head, [])]:
                    if candidate not in reaching:
                        reaching.append(candidate)
            lineage[step.name] = reaching
        for step in steps:
            self.entry_wires(step.name, lineage[step.name], host=self.contract is None)
        if self.contract is not None:
            self.provided = tuple(socket.name for socket in self.model.node(self.contract).outputs)
        elif self.host is not None:
            self.provided = tuple(socket.name for socket in self.host.outputs)

    def inside(self) -> str:
        """What a refused step reference adds inside a subgraph — where it must point and why, since a step
        outside is right there in the file and still out of reach; nothing at the top."""
        if not self.prefix:
            return ""
        return (
            f" inside the subgraph {self.prefix[:-1]!r} — a step inside a subgraph reads only the record the "
            "subgraph receives and the steps before it inside"
        )

    def bind_wire(self, node: Node, param: str, ref: str, known: Sequence[str]) -> bool:
        """``bind: {param: step.attr | step[key] | step}`` → a wire from that step's output into the parameter."""
        step, attr, key = _split_bind(ref)
        if self.prefix + step not in known:
            self.problem(
                f"{node.name}.{param}: bind {ref!r} names no earlier step{self.inside()} at {node.loc}", node.loc
            )
            return False
        producer = self.model.node(self.prefix + step)
        if attr:
            output = producer.output(attr)
            if output is None:
                self.problem(
                    f"{node.name}.{param}: bind {ref!r} — {step} has no output {attr!r} at {node.loc} "
                    f"(its outputs are: {[s.name for s in producer.outputs]})",
                    node.loc,
                )
                return False
        elif key:
            output = next((s for s in producer.outputs if s.entry == key), None) or Socket(key, ANY, entry=key)
        else:
            output = Socket("record", RECORD)
        self.wire(producer, output, node, param, "bind", self.accepted_types(node, param))
        return True

    def gate_wires(self, steps: Sequence[_Step]) -> None:
        """``requires: X`` hangs a step off the outcome socket of the EARLIER step raising X; a flag no
        step raises is left to the chain check, which says so in recordstream's words."""
        raised: Dict[str, str] = {}
        for step in steps:
            declaration = self.declarations.get(step.name, _NO_DECLARATION)
            node = self.model.node(step.name)
            gate = declaration.requires or str(node.params.get(GATE_PARAM, Param("", "", "", "")).value or "")
            if gate and gate in raised:
                producer = self.model.node(raised[gate])
                out = producer.output(gate)
                if out is not None:
                    self.wire(producer, out, node, "record", "gate", (RECORD,))
            for flag in declaration.flags:
                raised.setdefault(flag, step.name)


def _split_bind(ref: str) -> Tuple[str, str, str]:
    """A bind reference → ``(step, attr, key)`` — the three spellings of ``recordstream.flow.steps``."""
    if ref.endswith("]") and "[" in ref:
        head, _, inner = ref[:-1].partition("[")
        if head and inner and "." not in head:
            return head, "", inner
    head, dot, attr = ref.partition(".")
    return head, attr if dot else "", ""


# ================================================================================================
# loading: the graph's path and every text the editor holds, handed to confluid
# ================================================================================================


#: Held around every read and every write of a file on disk. A write (:meth:`Document.save`, :meth:`Document.save_as`,
#: :meth:`Document.create`) empties the file before it writes the new text, and the files are shared by every document
#: and every request of the process (the editor serves requests on threads — a page and an agent's tool; two graphs
#: may include one steps file), so a read overlapping a write would see the file half-written. Measured 2026-09-30,
#: loads writing nothing: graph A saved a steps file 300 times while graph B, which includes it without holding it,
#: was checked 352 times — without the lock 4 of B's loads read the file half-written (``Subgraph: steps is empty``,
#: a ``ParserError``), with it none. The reads it covers: Open, :meth:`Document.disk_state`, the changed-on-disk check
#: of a save, a file the editor does not hold (:meth:`Document._text_of`) — and every load (:func:`_load_document`),
#: because confluid reads each included file the editor does NOT hold from disk. A load writes nothing: a held text
#: reaches confluid as ``texts=``.
_DISK_LOCK = threading.RLock()


def _load_document(
    path: Path, texts: Mapping[Path, str], *, build: bool = False, solidify: bool = False
) -> Tuple[Any, List[Path]]:
    """``confluid.load(until="document")`` of the graph as the FILE it is — its path — with every text the editor
    holds, the graph's own among them, handed over as ``texts=``. confluid parses a held text in place of the file
    (it never reads the disk for one, and nothing is written: a steps file Move to its own file… made stays in the
    editor until Save, even in a folder that cannot be written), resolves every ``include:`` from the folder of the
    file that names it through its own search, and names the real file in every ``file:line:col`` and every message.
    Needs confluid 0.4.0, the release carrying ``load(..., texts=)``.

    Measured before 2026-09-30, when the graph's text was parsed here and handed over as DATA: confluid names data
    ``<cwd>/string.yaml``, so a missing steps file was reported as ``/Users/…/streamstudio/string.yaml includes
    prep.steps.yaml: Not found: prep.steps.yaml (searched: /Users/…/streamstudio/prep.steps.yaml, …)`` — included by
    a file that does not exist, searched for in the editor's working directory and not in the graph's folder. Now:
    ``…/graphs/demo.yaml includes prep.steps.yaml: Not found: prep.steps.yaml (searched:
    …/graphs/prep.steps.yaml, …)``, whatever the working directory.

    ``build=True`` runs the load to the OBJECTS stage instead (without ``solidify`` by default:
    constructors do no work, and the expensive finish is not what is being checked) — the stage every
    consumer loads to, and the only one where a top-level key is handed to a same-named constructor
    parameter. ``solidify=True`` adds the finish, which is what a RUN needs (:meth:`Document.load`).

    The load runs under :data:`_DISK_LOCK`: an included file the editor does not hold is read from disk, and a save
    (of another graph including the same file) is never read half-written."""
    until: Literal["objects", "document"] = "objects" if build else "document"  # ``solidify`` acts at "objects" only
    with _DISK_LOCK:
        doc, paths = confluid.load(path.resolve(), until=until, solidify=solidify, return_paths=True, texts=texts)
    return doc, [Path(p).resolve() for p in paths]


def _unwritable(path: Path) -> Optional[int]:
    """The ``errno`` a write of ``path`` would fail with, as far as the disk can tell BEFORE writing — else None.

    Checked for every file of a save before the first one is written (:meth:`Document._refuse_unwritable`): an
    existing file must be writable itself (overwriting needs no write permission on its folder), a new one needs a
    folder that exists and can be written. What no check can foresee (a full disk) still fails at the write."""
    if path.exists():
        if path.is_dir():
            return errno.EISDIR
        return None if os.access(path, os.W_OK) else errno.EACCES
    folder = path.parent
    if not folder.exists():
        return errno.ENOENT
    if not folder.is_dir():
        return errno.ENOTDIR
    return None if os.access(folder, os.W_OK | os.X_OK) else errno.EACCES


def _load_problem(
    path: Path,
    exc: BaseException,
    verb: str = "does not load",
    *,
    texts: Optional[Mapping[Path, str]] = None,
    included: Collection[Path] = (),
) -> Problem:
    """A document that does not load (or does not build) is ONE located problem, never an exception.

    The message always starts with the GRAPH (``<graph>: does not load: …``); the location is where the error is:
    for a YAML error, the file PyYAML's mark names (:func:`_mark_file`), which for a syntax error in an included
    steps file is that steps file, not the graph; for a missing included file, the ``include:`` entry naming it
    (:func:`_include_loc`); else the ``file:line:col`` the message names (:func:`_message_loc`); else the graph.
    ``texts`` are the files the editor holds (resolved path → text, the graph's own among them), ``included`` the
    files the load read — known only when it got that far (a build error)."""
    held: Mapping[Path, str] = texts or {}
    mark = getattr(exc, "problem_mark", None)
    if mark is not None:
        loc = f"{_mark_file(path, mark, held)}:{mark.line + 1}:{mark.column + 1}"
    else:
        found = _include_loc(str(exc), held) if isinstance(exc, ConfigFileNotFoundError) else None
        loc = found or _message_loc(str(exc), [path, *held, *included]) or str(path)
    message = " ".join(str(exc).split())
    return Problem(f"{path}: {verb}: {type(exc).__name__}: {message}", loc)


def _include_loc(message: str, texts: Mapping[Path, str]) -> Optional[str]:
    """The ``file:line:col`` of the ``include:`` entry a missing-file message names — confluid's ``<file> includes
    <entry>: Not found: …`` — when the including file is one the editor holds (the graph always is), else ``None``.

    Measured before 2026-09-30: the problem pointed at the bare graph file (``…/demo.yaml``); now at the entry,
    ``…/demo.yaml:5:18``. An entry is matched by every name confluid may give it (:func:`_include_names`); the first
    one in the text wins (confluid stops at the first file it cannot find)."""
    for file in sorted(texts, key=lambda key: len(str(key)), reverse=True):
        named = re.match(re.escape(str(file)) + r" includes (.+?): ", message)
        if named is None:
            continue
        text = YamlText(texts[file])
        try:
            nodes = text.nodes()
        except yaml.YAMLError:
            return None
        for node in nodes:
            pair = text.pair(node, _INCLUDE) if isinstance(node, yaml.MappingNode) else None
            if pair is None:
                continue
            entries = pair[1].value if isinstance(pair[1], yaml.SequenceNode) else [pair[1]]
            for entry in entries:
                if isinstance(entry, yaml.ScalarNode) and named.group(1) in _include_names(entry.value, file.parent):
                    return f"{file}:{entry.start_mark.line + 1}:{entry.start_mark.column + 1}"
        return None
    return None


def _include_names(entry: str, folder: Path) -> Set[str]:
    """The names confluid may give the ``include:`` entry of a file in ``folder`` after ``… includes``: the entry as
    written (``str(Path(entry))``), and the path confluid's own resolver made of it — :func:`resolve_config_path`,
    the function confluid resolved it with — which is ``~`` expanded (``~/nope.steps.yaml`` →
    ``/home/me/nope.steps.yaml``) and, when its search found something there, the path found (a folder
    ``adir.steps.yaml`` beside the file → ``<folder>/adir.steps.yaml``, one in the working directory →
    ``<cwd>/adir.steps.yaml``). Measured before 2026-09-30: only the first was compared, so a missing
    ``~/nope.steps.yaml`` and an include naming a folder pointed at the bare graph file."""
    names = {str(Path(entry))}
    try:
        names.add(str(resolve_config_path(entry, base_dir=folder)))
    except RuntimeError:  # `~someone/x` for a user this machine does not know: no home to expand into
        pass
    return names


def _message_loc(message: str, known: Iterable[Path]) -> Optional[str]:
    """The ``file:line:col`` a message names after `` at `` — how confluid locates an error it has no YAML mark for
    (``Failed to construct GraphContract at <file>:1:11: …``, ``duplicate key 'flow' at <file>:5:1 …``) — or ``None``.

    A path may hold spaces (the workspace's data folders do), so no pattern can tell where one begins. The location
    is read as one of the files the document knows (``known``: the graph, the texts it holds, the files a load read;
    the longest spelling first, the one the message names first wins), else as a file on disk (an included file the
    editor does not hold — a load that fails never says which files it read), else as before: up to the first space.
    Measured before 2026-09-30, with the last reading alone: in a folder ``with space/`` a build error pointed at the
    bare graph file while its message named ``…/with space/demo.yaml:1:11``."""
    names = sorted({str(file) for file in known}, key=len, reverse=True)
    if names:
        found = re.search(" at (" + "|".join(re.escape(name) for name in names) + r"):(\d+):(\d+)", message)
        if found is not None:
            return f"{found.group(1)}:{found.group(2)}:{found.group(3)}"
    for found in re.finditer(r" at (?=(.+?):(\d+):(\d+))", message):
        if os.path.isfile(found.group(1)):
            return f"{found.group(1)}:{found.group(2)}:{found.group(3)}"
    located = re.search(r" at (\S+:\d+:\d+)", message)
    return located.group(1) if located else None


def _mark_file(path: Path, mark: yaml.Mark, held: Collection[Path]) -> str:
    """The file a PyYAML ``mark`` is in, as the mark spells it: the graph or an included file (confluid parses each
    under its real path, :func:`_load_document`), on disk or held by the editor and not saved — else the graph, for a
    stream that is no file (``<unicode string>``).

    Measured before 2026-09-30: a syntax error on line 3 of an included ``prep.steps.yaml`` was located at
    ``demo.yaml:3:7`` — the graph with the steps file's line — while the message named ``prep.steps.yaml``."""
    named = Path(mark.name)
    if named.resolve() in held or named.is_file():
        return str(mark.name)
    return str(path)


def derive(path: Path, texts: Mapping[Path, str]) -> _Derivation:
    """The model of the document at ``path`` as the texts spell it — never raises."""
    try:
        doc, included = _load_document(path, texts)
    except Exception as exc:  # noqa: BLE001 — every failure to load is reported as a problem
        problem = _load_problem(path, exc, texts=texts)
        return _Derivation(Model(problems=[problem]), {}, None, [], None, (), [path.resolve()], {})
    derivation = _Builder(path).derive(doc)
    files = [path.resolve()] + [p for p in included if p != path.resolve()]
    if not derivation.model.problems:
        # The document stage keeps the markers, so it can derive a document that does not BUILD — a
        # top-level key handed to a same-named constructor parameter, measured 2026-09-28 on a contract
        # placed before a flow whose ``outputs:`` line the contract then received. Only when deriving
        # found nothing, so one defect is one message.
        try:
            _load_document(path, texts, build=True)
        except Exception as exc:  # noqa: BLE001 — every failure to build is reported as a problem
            problem = _load_problem(path, exc, "does not build", texts=texts, included=files)
            derivation.model.problems.append(problem)
    return derivation._replace(files=files)


# ================================================================================================
# the check
# ================================================================================================


def check(derivation: _Derivation, path: Path) -> List[Problem]:
    """Every problem of a derived document: what deriving found, the dangling required inputs, the
    contract's declaration, and recordstream's own chain check — never raises."""
    model = derivation.model
    problems = list(model.problems)  # every level's: an inside's problems are its parent's too
    for level in model.levels():
        problems.extend(_dangling(level))
    if derivation.contract is not None:
        problems.extend(_contract_problems(derivation))
    problems.extend(_chain_problems(derivation, path))
    return problems


def _dangling(model: Model) -> List[Problem]:
    wired = {(wire.target, wire.target_socket) for wire in model.wires}
    refused = [problem.message for problem in model.problems if problem.message.startswith("cannot wire ")]
    out: List[Problem] = []
    for node in model.nodes:
        for socket in node.inputs:
            if not socket.required or (node.name, socket.name) in wired:
                continue
            if socket.entry:
                continue  # an entry nothing produces is the chain check's finding, in recordstream's words
            if socket.name in node.params and node.params[socket.name].spelled:
                continue
            if any(f" into {node.name}.{socket.name} (" in message for message in refused):
                continue  # a wire WAS drawn to it and refused: that problem is already reported
            out.append(
                Problem(
                    f"{node.name}.{socket.name} ({socket.type}) is required and nothing is wired into it at {node.loc}",
                    node.loc,
                )
            )
    return out


def _contract_problems(derivation: _Derivation) -> List[Problem]:
    """``GraphContract.check_declaration`` over the declaration, with a zero-arg instance of each
    delivered node's class standing in for the object (the name collision it refuses is a property
    of the CLASS)."""
    assert derivation.contract is not None
    model = derivation.model
    root = model.node(derivation.contract)
    delivered: Dict[str, Any] = {}
    for wire in model.wires:
        if wire.target == root.name and wire.kind == "delivered":
            cls = _resolve(model.node(wire.source).class_path)
            if cls is not None:
                try:
                    delivered[wire.target_socket] = cls()
                except Exception:
                    continue
    for name, param in root.params.items():
        if param.doc == "delivered literally":
            delivered[name] = param.value
    contract = GraphContract(
        name=str(root.params["name"].value or ""),
        inputs={s.name: s.type for s in root.outputs},
        outputs={str(k): str(v) for k, v in (root.params["outputs"].value or {}).items()},
        records={str(k): str(v) for k, v in (root.params["records"].value or {}).items()},
        delivered=delivered,
    )
    try:
        contract.check_declaration()
    except ContractError as exc:
        return [Problem(f"{exc} at {root.loc}", root.loc)]
    return []


def _chain_problems(derivation: _Derivation, path: Path) -> List[Problem]:
    """recordstream's own check, over the engine's own objects: ``Tracer.check`` on a ``FlowGraph`` of
    the flow's built ops, and on a ``Stream`` of each ops list — a refusal is its located
    ``ChainContractError`` text, placed at the node it names."""
    from recordstream.core.stream import Stream

    model = derivation.model
    problems: List[Problem] = []
    flow = derivation.flow
    if flow is not None and flow.sound and flow.steps:
        built = {step.name: derivation.instances.get(step.name) for step in flow.steps}
        if all(built[step.name] is not None or model.node(step.name).class_path == "" for step in flow.steps):
            flow_doc: Dict[str, Any] = {}
            for step in flow.steps:
                spec: Dict[str, Any] = dict(step.reserved)
                if built[step.name] is not None:
                    spec["op"] = built[step.name]
                flow_doc[step.name] = spec
            graph = FlowGraph(flow=flow_doc, outputs=flow.outputs)
            problems.extend(_trace_problems(graph, str(path), derivation.provided, model, prefix=""))
    for stream_name, members in derivation.streams:
        ops = [derivation.instances.get(member) for member in members]
        if any(op is None for op in ops):
            continue
        head = derivation.model.node(members[0])
        provided = tuple(socket.entry for socket in head.inputs if socket.entry)
        stream = Stream(ops=ops)
        problems.extend(_trace_problems(stream, f"{path}:{stream_name}", provided, model, prefix=f"{stream_name}."))
    return problems


def _trace_problems(
    graph: Any, where: str, provided: Sequence[str], model: Model, *, prefix: str
) -> List[Problem]:  # graph: a Stream or a FlowGraph — the two facades the Tracer takes, with no common base
    tracer = Tracer(graph, where=where)
    seed: Dict[str, Any] = {key: None for key in provided}
    try:
        tracer.check(seed)
    except ChainContractError as refusal:
        # The tracer descends into a subgraph and names the inner node it refused (``prep/grey``), the
        # document's own qualified name — so the problem lands on that node's line.
        node_name = prefix + str(tracer.report[-1]["node"]) if tracer.report else ""
        holder = model.holder(node_name) if node_name else None
        loc = model.node(node_name).loc if holder is not None else ""
        return [Problem(str(refusal), loc)]
    except Exception as exc:  # noqa: BLE001 — the engine refused the graph for another reason: still a problem
        return [Problem(f"{where}: {type(exc).__name__}: {exc}", "")]
    return []


# ================================================================================================
# the document
# ================================================================================================

_Texts = Dict[Path, str]

#: What a clipboard text holds: flow steps (a steps mapping) or list members (a YAML sequence).
ClipKind = Literal["steps", "ops"]

#: A save refused because someone else wrote the file after the editor read (or wrote) it. It names the
#: two ways on, in the words of the menu: Revert takes theirs, Save As keeps both.
CHANGED_ON_DISK = "{path} changed on disk since the editor read it — Revert to load it, or Save As to keep both"
#: The same refusal for a file the graph INCLUDES (a steps file): Save As writes a graph that includes the SAME
#: file, so it cannot keep both, and the message does not offer it.
CHANGED_ON_DISK_INCLUDED = (
    "{path} changed on disk since the editor read it — Revert to load it (Save As cannot keep both: the graph "
    "includes this file wherever it is saved)"
)
#: A save refused because a file it would write cannot be READ: the editor cannot tell whether someone else wrote it
#: since, and never writes over what it cannot compare. Measured before: an unreadable file made Save an HTML 500.
CANNOT_READ = (
    "{path} cannot be read: {reason} — the editor never writes over a file it cannot compare with what it read; make "
    "it readable, or Save As to write the graph elsewhere"
)

#: New… refused because its file is there already: the editor never writes over a file it did not read. It names the
#: two ways on, in the words of the dialog and the menu.
ALREADY_EXISTS = "{path} already exists — pick another name, or open it with File ▸ Open"

#: What the disk holds of the files a Save would write, against what the editor last read or wrote of them: the same
#: text (``unchanged``, or no file yet for a steps file the editor holds but has not saved), another text (``changed``
#: — Save refuses it), no file where there was one (``gone`` — deleted or moved away; Save writes it again), or a file
#: that cannot be read.
DiskState = Literal["unchanged", "changed", "gone", "unreadable"]


def unreadable_reason(exc: Union[OSError, UnicodeDecodeError]) -> str:
    """Why a file could not be read, in words: the system's (``Permission denied``), or — for a file that is not UTF-8
    text — which byte stopped it (``it is not UTF-8 text (byte 0xe9 at position 5)``)."""
    if isinstance(exc, UnicodeDecodeError):
        return f"it is not UTF-8 text (byte 0x{exc.object[exc.start]:02x} at position {exc.start})"
    return exc.strerror or str(exc)


#: A list member's node name, ``<owner>.<slot>[<i>]`` or ``<top-level key>[<i>]`` (the ``Tracer`` scheme).
_LIST_MEMBER = re.compile(r"^(?P<owner>.+)\[(?P<index>\d+)\]$")
#: The steps-mapping key that is a loader directive, never a step (``flow: {include: …}``).
_INCLUDE = "include"


def _free_step_name(name: str, used: Set[str]) -> str:
    """``grey`` → ``grey_2`` (then ``_3`` …): the first suffix no name in ``used`` has."""
    index = 2
    while f"{name}_{index}" in used:
        index += 1
    return f"{name}_{index}"


#: What a step the editor NAMES may be called (Convert to subgraph, Insert steps file / template): a letter
#: or ``_``, then letters, digits, ``_`` or ``-`` — never ``/`` (a qualified name's separator) or ``.``
#: (``step.attr`` in ``bind:``), the two the engine refuses, and nothing YAML would read as more than a key.
_STEP_NAME = re.compile(r"[A-Za-z_][\w-]*")
#: Where a word boundary of a class name falls: ``ConvertMode`` → ``convert_mode``, ``HTTPServer`` → ``http_server``.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _check_step_name(name: str) -> str:
    """``name`` when a new step may be called that, else the refusal naming the rule."""
    if not _STEP_NAME.fullmatch(name) or name == _INCLUDE:
        raise ValueError(
            f"{name!r} cannot name a step — use letters, digits, '_' or '-', starting with a letter or '_' "
            f"(and not {_INCLUDE!r})"
        )
    return name


def _step_name_of(text: str) -> str:
    """A step name made from any text — a file stem, a template name, a class name: every other character
    becomes ``_``, and one that could not start a name gets a ``_`` in front."""
    name = re.sub(r"[^\w-]", "_", text) or "_"
    return name if _STEP_NAME.fullmatch(name) and name != _INCLUDE else f"_{name}"


def _snake(class_path: str) -> str:
    """``recordstream.ops.image.ConvertMode`` → ``convert_mode``: a generated step name for a list member."""
    return _step_name_of(_CAMEL_BOUNDARY.sub("_", class_path.rsplit(".", 1)[-1]).lower())


def _spelled(node: yaml.Node) -> str:
    """A ``from:`` / ``merge_from:`` value re-spelled on one line: ``a``, or ``[a, b]``."""
    if isinstance(node, yaml.SequenceNode):
        return spell([str(item.value) for item in node.value if isinstance(item, yaml.ScalarNode)])
    return spell(str(node.value)) if isinstance(node, yaml.ScalarNode) else ""


def _own_stop_line(text: YamlText, stop: int, column: int) -> int:
    """``stop`` — the first line after a block, by the ``pair_stop_line`` rule — moved up past the comment lines
    at the block's end written LEFT of ``column`` (the block's keys): such a comment sits directly above the line
    that follows — ``outputs:``, a step's own ``from:`` — and is about THAT line, so it stays with it when the
    block moves (measured before: Convert took a column-0 ``# before outputs`` into the subgraph, and Move to
    its own file wrote ``# prep reads the probe explicitly`` into the steps file). A comment at the keys' own
    column still travels with the block above it, the ``pair_stop_line`` rule."""
    lines = text.text.splitlines()
    own = stop
    for line in range(min(stop, len(lines)) - 1, -1, -1):
        stripped = lines[line].strip()
        if not stripped:
            continue
        if not stripped.startswith("#") or len(lines[line]) - len(lines[line].lstrip(" ")) >= column:
            break
        own = line
    return own


def _at(key: Path, node: yaml.Node) -> str:
    """``file:line:col`` of a composed node of the file ``key`` — what a refusal about that line starts with."""
    return f"{key}:{node.start_mark.line + 1}:{node.start_mark.column + 1}"


class _Level(NamedTuple):
    """One steps mapping an edit changes: the top ``flow:``, or the ``steps:`` of a subgraph — written inline,
    or in the steps file its ``steps: {include: …}`` names (then the steps and the marker's ``result:``
    live in two files, each edited against its own text).

    Args (fields):
        name: ``""`` for the top flow, else the subgraph node's (qualified) name.
        prefix: What the level's node names start with (``""``, ``"prep/"``).
        flow: The level's steps as the derivation saw them (names qualified).
        key / text / steps: The file holding the steps mapping, its held text, the mapping node.
        result_key / result_text / result: The file holding the marker (the main file for the top), its
            text, and the ``result:`` (``outputs:`` at the top) scalar when spelled.
        marker: The subgraph marker's mapping node (``None`` for the top).
        label: How a message names the level.
    """

    name: str
    prefix: str
    flow: _Flow
    key: Path
    text: YamlText
    steps: yaml.MappingNode
    result_key: Path
    result_text: YamlText
    result: Optional[yaml.ScalarNode]
    marker: Optional[yaml.MappingNode]
    label: str

    def local(self, name: str) -> str:
        """A node name of this level without the level's prefix (``prep/grey`` → ``grey``)."""
        return name[len(self.prefix) :]

    def step_names(self) -> List[str]:
        return [step.name for step in self.flow.steps]

    def step(self, name: str) -> _Step:
        return next(step for step in self.flow.steps if step.name == name)

    @property
    def included(self) -> bool:
        """Whether the steps live in a file of their own (``steps: {include: …}``)."""
        return self.key != self.result_key


class _Edits:
    """Edits to several held files as ONE change — each file's edits against the text their offsets were read
    from (a subgraph whose steps come from a steps file edits two files: the steps there, ``result:`` here)."""

    def __init__(self) -> None:
        self._by_file: Dict[Path, Tuple[YamlText, List[Edit]]] = {}

    def add(self, key: Path, text: YamlText, edits: Iterable[Edit]) -> None:
        held = self._by_file.setdefault(key, (text, []))
        if held[0].text != text.text:
            raise AssertionError(f"two different texts of {key} in one change")
        held[1].extend(edits)

    def texts(self) -> _Texts:
        return {key: text.apply(edits) for key, (text, edits) in self._by_file.items()}


def rebase_includes(text: str, old_dir: Path, new_dir: Path, held: Collection[Path] = ()) -> str:
    """``text`` with every RELATIVE ``include:`` entry rewritten so that, read from ``new_dir``, it names
    the file it named from ``old_dir`` — what Save As needs, since confluid reads an include beside the
    document (measured 2026-09-29: the unchanged text one folder over does not load,
    ``ConfigFileNotFoundError``). An absolute, ``~`` or ``$VAR`` entry reads the same from anywhere, and an
    entry that names no file beside the old place was not resolving through it: both are left as written —
    a file the editor HOLDS but has not saved yet (``held``: a subgraph's steps just moved to their own file)
    counts as there. A text that is not YAML is returned unchanged (the document's own problem says why).
    """
    if old_dir == new_dir:
        return text
    try:
        patched = YamlText(text)
        nodes = patched.nodes()
    except yaml.YAMLError:
        return text
    edits: List[Edit] = []
    for node in nodes:
        if not isinstance(node, yaml.MappingNode):
            continue
        found = patched.pair(node, _INCLUDE)
        if found is None:
            continue
        value = found[1]
        entries = [value] if isinstance(value, yaml.ScalarNode) else []
        if isinstance(value, yaml.SequenceNode):
            entries = [item for item in value.value if isinstance(item, yaml.ScalarNode)]
        for scalar in entries:
            entry = str(scalar.value)
            if not entry or entry.startswith("~") or "$" in entry or Path(entry).is_absolute():
                continue
            named = old_dir / entry
            if not named.exists() and named.resolve() not in held:
                continue
            rebased = os.path.relpath(named, new_dir)
            if rebased != entry:
                edits.append(patched.replace_scalar_edit(scalar, spell(rebased)))
    return patched.apply(edits)


#: A shift of the positional names of one list's members: ``(owner, first, shift)`` — every member
#: ``owner[i]`` with ``i >= first`` becomes ``owner[i + shift]`` (and every name under it); with a negative
#: shift the members ``first + shift … first - 1`` are gone first (:meth:`Document._shift_members`).
_MemberShift = Tuple[str, int, int]


class _Dropped(NamedTuple):
    """What ONE shift of a list's member names dropped: the places (``positions``) and group memberships (``groups``:
    per group id, ``(index in its nodes, name)``) of the names that went — the members a removal took out, or the one
    an undo of an add took out. The step that puts those members back puts them back too."""

    positions: Dict[str, Tuple[float, float]]
    groups: Dict[str, List[Tuple[int, str]]]


#: A location inside a problem's message: confluid's ``file:line:col`` (relative or absolute) and PyYAML's
#: ``line N, column M``.
_MESSAGE_LOCATION = re.compile(r"\S+:\d+:\d+|line \d+, column \d+")


def _where_free(message: str) -> str:
    """A problem's message with every location taken out — what stays the same when an edit only moves lines."""
    return _MESSAGE_LOCATION.sub("<loc>", message)


def _gained(before: Sequence[Problem], after: Sequence[Problem]) -> List[Problem]:
    """The problems ``after`` holds beyond ``before``: compared by message with the locations taken out, and COUNTED.

    Measured before 2026-09-30: problems were compared by their whole message, location included, so a step
    inserted above an existing problem moved it from ``flow.yaml:6:9`` to ``flow.yaml:9:9`` and the edit was
    refused for "gaining" the problem that was already there. Counting keeps the other half: the same problem
    moved is not gained, a second copy of it is.
    """
    left = Counter(_where_free(problem.message) for problem in before)
    gained: List[Problem] = []
    for problem in after:
        key = _where_free(problem.message)
        if left[key] > 0:
            left[key] -= 1
        else:
            gained.append(problem)
    return gained


class _Entry(NamedTuple):
    """One step of the document's history: the texts before an edit, the rename it made, the shifts of a list's
    positional member names it made, in the order they were made (a Convert, a Paste, an add or a removal of list
    members — a removal of several members as ONE edit makes one shift per member), and — per shift — what to put
    back after the step that walks it again: on the undo stack what the edit's shifts dropped (a removed member's
    place and groups, measured before: an undo brought the member back out of its group), on the redo stack what the
    undo's shifts dropped (an added member's place and groups, which a redo then restores)."""

    texts: _Texts
    rename: Optional[Tuple[str, str]]
    members: Tuple[_MemberShift, ...] = ()
    dropped: Tuple[_Dropped, ...] = ()


class Document:
    """A confluid YAML document on disk, its derived model, and the edits that patch it in place.

    Every edit changes the texts this object HOLDS; only :meth:`save` / :meth:`save_as` write a file —
    the editor's explicit Save — besides :meth:`create`, which writes a NEW file's first text (File ▸ New…).
    :attr:`dirty` says whether the held state differs from what was last read or written.

    Args:
        path: The document's file.
        texts: The text of every file the document spans, by resolved path (the main file at least).
        history_depth: How many edits ``undo`` can walk back.
        saved: What each file held when the editor last read or wrote it — the baseline :attr:`dirty`
            compares against and :meth:`save` checks the disk against. ``None`` = the texts themselves
            (read or written just now); a file it leaves out has no text on disk yet (a steps file Move to its
            own file… made, held until Save).
    """

    def __init__(
        self, path: Path, texts: _Texts, *, history_depth: int = 50, saved: Optional[Mapping[Path, str]] = None
    ) -> None:
        # Stores only: the model is derived on first use and cached per text.
        self.path = Path(path)
        self.history_depth = int(history_depth)
        self._texts: _Texts = dict(texts)
        self._saved: _Texts = dict(texts) if saved is None else dict(saved)
        # Positions or groups set since the last save: page state the file does not hold yet.
        self._layout_dirty = False
        # Each entry is the texts BEFORE an edit and the rename that edit made (``None`` for any other
        # edit): positions and groups are the page's state, not the document's, so undoing a rename
        # re-keys the CURRENT ones instead of restoring old ones — a node moved after the rename keeps
        # its place (measured before: undo restored the text only and left them under the new name).
        self._history: List[_Entry] = []
        self._redo: List[_Entry] = []
        self._cache: Optional[Tuple[Tuple[Tuple[Path, str], ...], _Derivation, Optional[List[Problem]]]] = None
        self._positions: Optional[Dict[str, Tuple[float, float]]] = None
        # Whether the held positions are WHOLE — the sidecar's taken in and re-keyed (a list's members shifted, see
        # :meth:`_shift_members`), so a name they lack is a node to lay out, never one the sidecar still places: after
        # a member is inserted, the sidecar's entry under its name is the place of the member that moved down.
        self._positions_whole = False
        self._groups: Optional[List["Group"]] = None
        # What each file the editor does NOT hold read as, the last time an operation read it
        # (:meth:`_text_of`): the baseline an edit of that file starts from, kept when the edit holds it.
        self._read: _Texts = {}
        # While set, commits join ONE edit (:meth:`_one_edit`): no history entry each — and the member shifts the
        # joined commits made, with what each dropped, are collected here, for the one entry.
        self._joining = False
        self._joined_shifts: List[_MemberShift] = []
        self._joined_dropped: List[_Dropped] = []
        # The member shifts the last undo or redo applied, in order (:attr:`stepped_shifts`).
        self._stepped: Tuple[_MemberShift, ...] = ()

    @classmethod
    def open(cls, path: Union[str, Path], *, history_depth: int = 50) -> "Document":
        """Read the file's text; parse nothing."""
        given = Path(path)
        with _DISK_LOCK:
            text = given.read_text()
        return cls(given, {given.resolve(): text}, history_depth=history_depth)

    @classmethod
    def create(cls, path: Union[str, Path], text: str, *, history_depth: int = 50) -> "Document":
        """WRITE ``text`` to the new file ``path`` and answer its document, holding nothing unsaved — File ▸ New…: a
        graph is a file from its first moment, never one that exists only in the editor (the user, 2026-09-29: "New
        ... by mistake should not happen. It should ask to save").

        Refused, writing NOTHING: a file already at ``path`` — the write itself creates the file or fails
        (:data:`ALREADY_EXISTS`), so a file someone made after a caller looked is never written over — and a file the
        disk will not take (``<path> cannot be written: Permission denied — pick another folder``). A write cut short
        (a full disk) removes the file it began: a refusal leaves no half a graph behind.
        """
        given = Path(path)
        created = False
        with _DISK_LOCK:
            try:
                with given.open("x") as handle:  # "x": create the file, never open one that is there
                    created = True
                    handle.write(text)
            except FileExistsError:
                raise ValueError(ALREADY_EXISTS.format(path=given)) from None
            except OSError as exc:
                if created:
                    given.unlink(missing_ok=True)
                raise ValueError(f"{given} cannot be written: {exc.strerror or exc} — pick another folder") from None
        return cls(given, {given.resolve(): text}, history_depth=history_depth)

    @classmethod
    def from_held(cls, held: Mapping[str, Any], *, history_depth: int = 50) -> "Document":  # held: :meth:`held`'s JSON
        """The document :meth:`held` described, as the editor held it — its texts, the baseline each file is compared
        to (a file someone changed on disk since is still refused by :meth:`save`), its positions and groups — with
        an empty undo history. Reads nothing; the caller checked the shape (``streamstudio.app`` reads the file)."""
        document = cls(
            Path(str(held["path"])),
            {Path(key): str(text) for key, text in held["texts"].items()},
            history_depth=history_depth,
            saved={Path(key): str(text) for key, text in held["saved"].items()},
        )
        positions = held.get("positions")
        if positions is not None:
            document._positions = {str(name): (float(xy[0]), float(xy[1])) for name, xy in positions.items()}
            document._positions_whole = bool(held.get("positions_whole", False))
        groups = held.get("groups")
        if groups is not None:
            document._groups = list(groups)
        document._layout_dirty = bool(held.get("layout_dirty", False))
        return document

    # -- reading -----------------------------------------------------------------------------

    @property
    def dirty(self) -> bool:
        """Whether the editor holds something the files do not: a text that differs from what was last
        read or written (undoing back to it is clean again), or positions / groups set since the last save."""
        return self._layout_dirty or any(self._saved.get(key) != text for key, text in self._texts.items())

    def held(self) -> Dict[str, Any]:  # JSON: strings, lists, numbers, booleans
        """What the editor holds of this document, as JSON — what a stop keeps of a graph with unsaved work (the
        recovery file, ``streamstudio.app``) and :meth:`from_held` opens again: ``path``; ``texts``, the text of every
        file it spans by resolved path (the graph, a steps file it includes or holds); ``saved``, what each of those
        held when the editor last read or wrote it (none for a file never on disk); ``positions`` / ``groups`` held
        here (``null`` when none were set) with ``positions_whole`` and ``layout_dirty``. The undo history is NOT
        kept — an undo after a restore has nothing to walk back."""
        return {
            "path": str(self.path),
            "texts": {str(key): text for key, text in self._texts.items()},
            "saved": {str(key): text for key, text in self._saved.items() if key in self._texts},
            "positions": (
                None if self._positions is None else {name: [x, y] for name, (x, y) in self._positions.items()}
            ),
            "positions_whole": self._positions_whole,
            "groups": None if self._groups is None else [dict(group) for group in self._groups],
            "layout_dirty": self._layout_dirty,
        }

    def disk_state(self) -> Tuple[DiskState, Optional[str]]:
        """What the disk holds of the files :meth:`save` would write (:data:`DiskState`), and — for ``unreadable`` —
        the file and why in words (``<path> cannot be read: Permission denied``). Never raises: the recovery question
        is asked from this, and a file that cannot be read must be reported there, never stop the editor starting
        (measured before: a ``chmod 000`` file of another folder made every start die with ``PermissionError``)."""
        pending = [key for key, text in self._texts.items() if self._saved.get(key) != text]
        changed = gone = False
        with _DISK_LOCK:
            for key in pending:
                if not key.is_file():
                    gone = gone or key in self._saved  # one the editor read or wrote, not there any more
                    continue
                try:
                    text = key.read_text()
                except (OSError, UnicodeDecodeError) as exc:
                    return "unreadable", f"{key} cannot be read: {unreadable_reason(exc)}"
                changed = changed or text != self._saved.get(key)
        if changed:
            return "changed", None
        return ("gone", None) if gone else ("unchanged", None)

    def load(self) -> Any:  # the built document: a mapping of top-level objects, or the root object itself
        """The document BUILT from the texts the editor holds — saved or not, what the canvas shows — to
        the objects stage, solidified: what a run executes. Every call builds fresh objects (a run's ops
        keep state), and a document that does not build raises confluid's own located error."""
        built, _paths = _load_document(self.path, self._texts, build=True, solidify=True)
        return built

    @property
    def text(self) -> str:
        """The main file's text as the document currently holds it (saved or not)."""
        return self._texts[self.path.resolve()]

    @property
    def files(self) -> List[Path]:
        """The main file and every file an ``include:`` brought, resolved."""
        return list(self._derivation().files)

    @property
    def _folder(self) -> Path:
        """The folder the graph's own ``include:`` entries are read from: its REAL file's, the folder confluid reads
        them from (the graph reaches confluid resolved, :func:`_load_document`). For a graph that is a symbolic link
        to a FILE the link's folder is another folder — measured before 2026-09-30: Copy and Save as template read a
        ``prep.steps.yaml`` beside the link (``mode: RGB``) while the canvas, a check and a run read the one beside
        the real file (``mode: L``), and with none beside the link they were refused for a graph that loaded clean."""
        return self.path.resolve().parent

    @property
    def sidecar_path(self) -> Path:
        return self.path.with_suffix(".layout.yaml")

    @property
    def model(self) -> Model:
        """The drawing derived from the current text (cached until the text changes)."""
        return self._derivation().model

    def check(self) -> List[Problem]:
        """Every problem of the current document; never raises."""
        key = self._key()
        if self._cache is None or self._cache[0] != key:
            self._derivation()
        assert self._cache is not None
        problems = self._cache[2]
        if problems is None:
            problems = check(self._cache[1], self.path)
            self._cache = (key, self._cache[1], problems)
        return list(problems)

    def _key(self) -> Tuple[Tuple[Path, str], ...]:
        return tuple(sorted(self._texts.items()))

    def _derivation(self) -> _Derivation:
        key = self._key()
        if self._cache is None or self._cache[0] != key:
            self._cache = (key, derive(self.path, self._texts), None)
        return self._cache[1]

    # -- editing -----------------------------------------------------------------------------

    def set_param(self, node: str, param: str, value: Any) -> None:  # value: any YAML scalar or container
        """Set one parameter: the located line is patched, or the line is inserted under the node when
        the file left it at its default. Comments and ``$VAR`` spellings elsewhere are untouched."""
        found = self.model.node(node)
        if param not in found.params:
            raise ValueError(f"{node} has no parameter {param!r} (its parameters are: {list(found.params)})")
        if not found.loc:
            raise ValueError(f"{node} has no location in the document — nothing to patch")
        file, line, col = _split_loc(found.loc)
        key = Path(file).resolve()
        patched = YamlText(self._text_of(key)).set_kwarg(line, col, param, value)
        self._commit({key: patched}, f"{node}.{param} = {value!r}", found.loc)

    def add_step(
        self,
        name: str,
        after: Optional[str],
        class_path: str,
        params: Dict[str, Any],
        *,
        into: Optional[str] = None,
    ) -> str:
        """Add ``class_path`` after the node ``after`` and answer the new node's name.

        After a flow step: a new STEP on its level (inside ``prep``'s ``steps:`` for ``after='prep/grey'``; ``name``
        bare or qualified by that level); the step that read ``after`` now reads the new one, and the level's output
        (``outputs:`` / ``result:``) moves to it when ``after`` was the tail.

        After a member of an op list (``stream.ops[1]``, :data:`OP_LISTS`), or with no ``after`` and ``into`` naming
        the list (``stream.ops``, for "at the end"): a new MEMBER, written in the list's own style — its own
        ``- !class:…`` line in a block list, ``, !class:…`` inside a one-line ``[…]``; an empty ``ops: []`` under a
        block marker becomes a block list, and a list the marker does not spell is added. A member is named by its
        position (``name`` does not apply), and the later members' positions and groups follow them.
        """
        if after is None or self._op_member(after) is not None:
            return self._add_member(after, into, class_path, params)
        if into is not None:
            raise ValueError(
                f"{after!r} is not a member of {into} — name one of its members, or none to add at its end"
            )
        if not name:
            raise ValueError(f"a new flow step needs a name — after {after!r}, it has none")
        derivation = self._derivation()
        model = derivation.model
        anchor = model.node(after)
        level, after_local = self._level_of_step(anchor, derivation)
        local = self._local_name(name, level, after)
        qualified = level.prefix + local
        if qualified in model.all_names():
            raise ValueError(f"{qualified!r} is already a node (the nodes are: {self._level_model(level).names()})")
        edits = _Edits()
        body = [f"op: !class:{class_path} {spell_mapping(params)}", f"from: {after_local}"]
        self._insert_step(edits, level, after_local, local, body)
        self._commit(edits.texts(), f"add_step {qualified!r} after {after!r}", anchor.loc)
        return qualified

    def remove_step(self, name: str) -> None:
        """Remove a step of any level; what read it now reads what it read from, and the level's output moves
        to that step when the removed one was it. A member of an op list (``stream.ops[1]``) is taken out of its
        list, the later members' positions and groups following them; the last one leaves ``ops: []``."""
        if self._op_member(name) is not None:
            self._remove_member(name)
            return
        derivation = self._derivation()
        node = derivation.model.node(name)
        level, local = self._level_of_step(node, derivation)
        step = level.step(name)
        text = level.text
        self._refuse_one_line(level, "remove a step")
        found = text.pair(level.steps, local)
        assert found is not None
        changes: List[Edit] = [text.remove_pair_edit(*found)]
        upstream = level.local(step.source) if step.source is not None else None
        for other in text.keys(level.steps):
            if other == local:
                continue
            value = text.pair(level.steps, other)
            assert value is not None
            if not isinstance(value[1], yaml.MappingNode):
                continue
            reads = text.pair(value[1], "from")
            if reads is not None and isinstance(reads[1], yaml.ScalarNode) and reads[1].value == local:
                if upstream is not None:
                    changes.append(text.replace_scalar_edit(reads[1], upstream))
                else:
                    changes.append(text.drop_pair_edit(value[1], "from"))
        edits = _Edits()
        edits.add(level.key, text, changes)
        if level.flow.outputs == name:
            if upstream is None:
                if level.name:
                    raise ValueError(
                        f"{name!r} is the only step of the subgraph {level.name!r} — nothing would be left for it to "
                        f"return; remove {level.name!r} instead"
                    )
                raise ValueError(f"{name!r} is the only step of the flow — nothing would be left to output")
            self._result_edit(edits, level, local, upstream)
        self._commit(edits.texts(), f"remove_step {name!r}", node.loc)

    def remove_steps(self, names: Sequence[str]) -> None:
        """Remove several steps as ONE edit — all of them or none, and one undo entry (Cut, Delete of a
        selection). A step inside a subgraph that is removed too goes with it; the rest are removed last
        first, in document order, each by the :meth:`remove_step` rule, and a refusal of any restores
        everything, unwritten (measured before, one request per step: a Select-all + Delete inside a
        subgraph removed ``scaled``, then refused ``grey`` — half applied)."""
        if not names:
            raise ValueError("remove needs at least one node")
        model = self._derivation().model
        for name in names:
            model.node(name)  # an unknown one is a KeyError naming the nodes, before anything changes
        chosen = list(dict.fromkeys(names))
        removed = [name for name in chosen if not any(name.startswith(other + SEPARATOR) for other in chosen)]
        order = model.all_names()
        with self._one_edit():
            for name in sorted(removed, key=order.index, reverse=True):
                self.remove_step(name)

    def rename_node(self, old: str, new: str) -> None:
        """Rename a node in ONE operation: its key, every ``from:`` / ``merge_from:`` / ``bind:`` / ``!ref:`` /
        ``outputs:`` that names it, its sidecar entry and every group that lists it.

        A node inside a subgraph is renamed on its own level only (``prep/grey`` → ``prep/mono``, or the bare
        ``mono``): its subgraph's ``steps:`` and ``result:`` change and nothing else, and a new name on
        another level is refused. A step name is scoped to its steps mapping, so renaming an outer ``grey``
        leaves a subgraph's inner ``from: grey`` alone. Renaming a subgraph renames every ``prep/…`` position
        and group member with it."""
        derivation = self._derivation()
        model = derivation.model
        node = model.node(old)
        level_name, _sep, local = old.rpartition(SEPARATOR)
        head, sep, new_local = new.rpartition(SEPARATOR)
        if sep and head != level_name:
            where = f"'{level_name}{SEPARATOR}<name>' or a bare name" if level_name else "a name without '/'"
            at = f"{node.loc}: " if node.loc else ""
            raise ValueError(f"{at}{old} can only be renamed on its own level — {where}, not {new!r}")
        qualified = f"{level_name}{SEPARATOR}{new_local}" if level_name else new_local
        if qualified in model.all_names():
            shown = model.names() if not level_name else model.inside_of(level_name).names()
            raise ValueError(f"{qualified!r} is already a node (the nodes are: {shown})")
        if node.kind == "host":
            raise ValueError(f"{old} is the host's side of the graph, not a node of the document")
        if node.kind == "boundary":
            raise ValueError(f"{old} is the boundary of the subgraph {level_name!r}, not a node of the document")
        if "[" in local:
            raise ValueError(f"{old} is a list member, named by its position — move it instead")
        if "." in local:
            raise ValueError(f"{old} is nested and named by the slot it fills — rename the slot's owner instead")
        if not level_name:
            if node.source_file and Path(node.source_file).resolve() != self.path.resolve():
                raise ValueError(f"{old} comes from {node.source_file}; rename it in that file")
            changes = {self.path.resolve(): YamlText(self.text).rename(old, new_local)}
        else:
            level = self._level(level_name, derivation)
            edits = _Edits()
            edits.add(level.key, level.text, level.text.step_rename_edits(level.steps, local, new_local))
            self._result_edit(edits, level, local, new_local)
            changes = edits.texts()
        self._commit(changes, f"rename {old!r} to {qualified!r}", node.loc, rename=(old, qualified))
        self._rekey(old, qualified)

    # -- subgraphs ---------------------------------------------------------------------------

    def convert_to_subgraph(self, nodes: Sequence[str], name: str) -> str:
        """Make CONSECUTIVE steps of one level (or consecutive members of one ``ops`` list) one subgraph step,
        as ONE edit; answer its node name.

        The moved steps keep their own lines and comments (re-indented); ``result:`` is the last of them. The
        first one's ``from:`` / ``merge_from:`` move to the subgraph step (inside, it reads what the subgraph
        receives), and every outer reference to the last one names the subgraph. Refused, naming the step:
        an outer step reading a selected step other than the last (it would move out of its reach), and a
        selected step reading a step left outside (a step inside reads only the inside). List members are
        named by position, so ``name`` does not apply to them; their steps are named after their classes.
        """
        if not nodes:
            raise ValueError("convert to subgraph needs at least one node")
        derivation = self._derivation()
        model = derivation.model
        for each in nodes:
            model.node(each)
        levels = {each.rpartition(SEPARATOR)[0] for each in nodes}
        if len(levels) > 1:
            first = nodes[0]
            other = next(each for each in nodes if each.rpartition(SEPARATOR)[0] != first.rpartition(SEPARATOR)[0])
            raise ValueError(
                f"the nodes to convert must be steps of ONE level — {first!r} and {other!r} are on different levels"
            )
        level_name = levels.pop()
        members = [each for each in nodes if _LIST_MEMBER.match(each.rpartition(SEPARATOR)[2])]
        if members and not level_name:
            if len(members) != len(nodes):
                raise ValueError("convert to subgraph takes flow steps or list members, not both")
            return self._convert_members(nodes, derivation)
        return self._convert_steps(nodes, name, self._level(level_name, derivation), derivation)

    def extract_steps(self, node: str, target: Path) -> Path:
        """*Move to its own file*: the inline steps of the subgraph ``node`` become the steps file ``target``
        (held in the editor like every other change — Save writes it) and its ``steps:`` becomes
        ``{include: <target, relative to the file holding the subgraph>}``. One edit; the inner nodes keep
        their names, now located in the new file."""
        derivation = self._derivation()
        level = self._level(node, derivation)
        if level.included:
            raise ValueError(f"{node}'s steps are already in their own file ({level.key}) — nothing to move")
        if not target.name.endswith(STEPS_SUFFIX):
            raise ValueError(f"a steps file is named *{STEPS_SUFFIX} — got {target.name}")
        if not target.parent.is_dir():
            raise KeyError(f"no directory {target.parent} — the steps file cannot be written there")
        key = target.resolve()
        if key in self._texts or target.exists():
            raise ValueError(f"{target} already exists — choose another file")
        assert level.marker is not None
        steps_pair = level.result_text.pair(level.marker, "steps")
        assert steps_pair is not None
        steps_key, steps_value = steps_pair
        assert isinstance(steps_value, yaml.MappingNode)  # _level found a steps mapping there
        replacement = spell_mapping({_INCLUDE: os.path.relpath(key, level.result_key.parent)})
        text = level.result_text
        if steps_value.flow_style:
            edit: Edit = (steps_value.start_mark.index, steps_value.end_mark.index, replacement)
        else:
            column = (
                int(steps_value.value[0][0].start_mark.column) if steps_value.value else steps_key.start_mark.column
            )
            stop = text.line_start(_own_stop_line(text, text.pair_stop_line(steps_key, steps_value), column))
            edit = (steps_key.end_mark.index, stop, f": {replacement}\n")
        changes = {level.result_key: text.apply([edit]), key: self._steps_text(level)}
        self._commit(changes, f"move the steps of {node!r} to {target}", derivation.model.node(node).loc)
        return target

    def include_steps(self, after: str, target: Path, name: Optional[str] = None) -> str:
        """*Insert steps file*: a new subgraph step after ``after`` (on its level) whose ``steps:`` is
        ``{include: <target, relative to the file the step is written in>}``; named ``name``, else after the
        file (made unique with ``_2`` …). Answers its node name."""
        derivation = self._derivation()
        model = derivation.model
        anchor = model.node(after)
        level, after_local = self._level_of_step(anchor, derivation)
        key = target.resolve()
        if key not in self._texts and not target.is_file():
            raise KeyError(f"no file {target}")
        stem = target.name[: -len(STEPS_SUFFIX)] if target.name.endswith(STEPS_SUFFIX) else target.stem
        local = self._fresh_step_name(level, name, stem)
        body = [
            f"op: !class:{SUBGRAPH_CLASS}",
            f"  steps: {spell_mapping({_INCLUDE: os.path.relpath(key, level.key.parent)})}",
            f"from: {after_local}",
        ]
        edits = _Edits()
        self._insert_step(edits, level, after_local, local, body)
        self._commit(edits.texts(), f"insert the steps file {target} after {after!r}", anchor.loc)
        return level.prefix + local

    def insert_steps(self, after: str, steps_text: str, base: str) -> str:
        """A COPY of ``steps_text`` (a steps file's text: a template) as a new subgraph step after ``after``,
        named after ``base`` (made unique with ``_2`` …), ``result:`` its last step. Nothing links back.

        The paste rule applies to the copy: a step reading a step the text does not hold reads the one before
        it, and the first one reads what the subgraph receives (its ``from:`` is dropped) — a template saved
        from flow steps still names a step of the graph it came from."""
        try:
            parsed = YamlText(steps_text)
            root = parsed.root
        except yaml.YAMLError as exc:
            raise ValueError(f"the steps are not YAML: {' '.join(str(exc).split())}") from None
        keys = parsed.keys(root) if isinstance(root, yaml.MappingNode) else []
        if not keys:
            raise ValueError(f"{base!r} holds no steps")
        if _INCLUDE in keys:
            raise ValueError(f"{base!r} includes another file — insert that file with Insert steps file… instead")
        rewired: List[Edit] = []
        assert isinstance(root, yaml.MappingNode)
        for position, key in enumerate(keys):
            found = parsed.pair(root, key)
            assert found is not None
            value = found[1]
            reads = parsed.pair(value, "from") if isinstance(value, yaml.MappingNode) else None
            if reads is None or not isinstance(reads[1], yaml.ScalarNode) or reads[1].value in keys:
                continue
            assert isinstance(value, yaml.MappingNode)
            if position:
                rewired.append(parsed.replace_scalar_edit(reads[1], keys[position - 1]))
            else:
                rewired.append(parsed.drop_pair_edit(value, "from"))
        copied = dedent(parsed.apply(rewired), int(root.start_mark.column))
        derivation = self._derivation()
        anchor = derivation.model.node(after)
        level, after_local = self._level_of_step(anchor, derivation)
        local = self._fresh_step_name(level, None, base)
        body = [
            f"op: !class:{SUBGRAPH_CLASS}",
            "  steps:",
            *indent_lines(copied, "    "),
            f"  result: {keys[-1]}",
            f"from: {after_local}",
        ]
        edits = _Edits()
        self._insert_step(edits, level, after_local, local, body)
        self._commit(edits.texts(), f"insert {base!r} after {after!r}", anchor.loc)
        return level.prefix + local

    def undo(self) -> bool:
        """Step back one edit; ``False`` when there is none. Undoing a rename renames the node's
        position and its group memberships back; undoing a removal of list members puts each member back at its
        place and into the groups it was in (:class:`_Dropped`)."""
        self._stepped = ()
        if not self._history:
            return False
        entry = self._history.pop()
        texts = dict(self._texts)
        self._texts = dict(entry.texts)
        if entry.rename is not None:
            old, new = entry.rename
            self._rekey(new, old)
        applied: List[_MemberShift] = []
        undone: List[_Dropped] = [_Dropped({}, {})] * len(entry.members)
        for index in reversed(range(len(entry.members))):
            owner, first, shift = entry.members[index]
            inverse: _MemberShift = (owner, first + shift, -shift)
            undone[index] = self._shift_members(*inverse)
            if index < len(entry.dropped):
                self._restore_dropped(entry.dropped[index])
            applied.append(inverse)
        self._redo.append(_Entry(texts, entry.rename, entry.members, tuple(undone)))
        self._stepped = tuple(applied)
        return True

    def redo(self) -> bool:
        """Step forward one undone edit; ``False`` when there is none. A member the undo took out (an add undone)
        comes back at its place and in its groups."""
        self._stepped = ()
        if not self._redo:
            return False
        entry = self._redo.pop()
        texts = dict(self._texts)
        self._texts = dict(entry.texts)
        if entry.rename is not None:
            old, new = entry.rename
            self._rekey(old, new)
        redone: List[_Dropped] = []
        for index, shift in enumerate(entry.members):
            redone.append(self._shift_members(*shift))
            if index < len(entry.dropped):
                self._restore_dropped(entry.dropped[index])
        self._history.append(_Entry(texts, entry.rename, entry.members, tuple(redone)))
        self._stepped = tuple(entry.members)
        return True

    @property
    def stepped_shifts(self) -> Tuple[_MemberShift, ...]:
        """The shifts of op list member names the last :meth:`undo` / :meth:`redo` applied, in order —
        ``(list, first, by)``: ``list[i]`` with ``i >= first`` became ``list[i + by]``. Empty after any other step."""
        return self._stepped

    def replace_text(self, text: str) -> None:
        """Make ``text`` the main file's text: checked as a whole, refused (nothing changes) when it gains a
        problem over the current text, and the undo history starts over — the text arrived whole, not as
        a patch the history could hold. Nothing is written; positions and groups held here are kept."""
        candidate: _Texts = {**self._texts, self.path.resolve(): text}
        before = self.check()
        derivation = derive(self.path, candidate)
        after = check(derivation, self.path)
        gained = _gained(before, after)
        if gained:
            raise ValueError("; ".join(problem.message for problem in gained))
        self._texts = candidate
        self._history.clear()
        self._redo.clear()
        self._cache = (self._key(), derivation, after)

    def save(self) -> None:
        """Write every file whose text changed, and the sidecar when positions or groups were set.

        Refused — nothing written, not even the sidecar — when a file about to be written changed on disk
        since the editor read or wrote it (:data:`CHANGED_ON_DISK`, :data:`CHANGED_ON_DISK_INCLUDED`): the
        editor never overwrites what someone else wrote. A file gone from disk meanwhile is written again —
        there is nothing to overwrite, and it is what Revert's refusal promises. Refused the same way when the
        disk will not take one of the files (:meth:`_refuse_unwritable`), and the graph's own file is written
        LAST — so the graph on disk never names a steps file this save did not write.

        The sidecar is written WHOLE, so each block keeps what the other one did not set: positions set
        here are every node's (the laid-out ones frozen with the moved ones, as before groups existed),
        positions NOT set here are what the file already held — a grouping never freezes a layout nobody
        arranged — and the groups are this document's, or the file's when none were set.
        """
        main = self.path.resolve()
        pending = {key: text for key, text in self._texts.items() if self._saved.get(key) != text}
        with _DISK_LOCK:
            self._refuse_changed_on_disk(pending)
            self._refuse_unwritable(pending)
            for key in sorted(pending, key=lambda key: key == main):  # the graph's own file last (False sorts first)
                self._write(key, pending[key])
                self._saved[key] = pending[key]
        if self._positions is not None or self._groups is not None:
            layout = _layout()
            kept = self.positions if self._positions is not None else layout.read_sidecar(self.sidecar_path)
            layout.write_sidecar(self.sidecar_path, kept, groups=self.groups)
        self._layout_dirty = False

    def save_as(self, target: Path, *, overwrite: bool = False) -> None:
        """Write the held document to ``target`` and make ``target`` this document's file from now on.

        The old file is left as it was. A relative ``include:`` is rewritten so it still names the same
        file from the new place (:func:`rebase_includes`) — in the held text and in every history entry,
        so undo keeps walking the edits made before. The new sidecar holds the positions and groups the
        editor shows when any were set, else a byte copy of the old sidecar. A pending edit of an INCLUDED
        file is written to that file, under the same changed-on-disk and cannot-be-written refusals as
        :meth:`save`, and before ``target`` — a new graph never names a steps file that was not written. Refused
        when ``target`` exists and ``overwrite`` is not asked.
        """
        target = Path(target)
        old_main, new_main = self.path.resolve(), target.resolve()
        if new_main == old_main:
            self.save()
            return
        if target.exists() and not overwrite:
            raise ValueError(f"{target} already exists — pass overwrite: true to replace it")
        others = {key: text for key, text in self._texts.items() if key != old_main and self._saved.get(key) != text}
        with _DISK_LOCK:
            self._refuse_changed_on_disk(others)
            self._refuse_unwritable([*others, target])
            self._save_as_write(target, old_main, new_main, others)

    def _save_as_write(self, target: Path, old_main: Path, new_main: Path, others: _Texts) -> None:
        """The writes of :meth:`save_as`, once nothing is refused (under :data:`_DISK_LOCK`)."""
        editor_files = frozenset(self._texts)  # a steps file held but not saved yet still counts as beside it

        def moved(texts: _Texts) -> _Texts:
            out = {key: text for key, text in texts.items() if key != old_main}
            if old_main in texts:
                out[new_main] = rebase_includes(texts[old_main], old_main.parent, new_main.parent, editor_files)
            return out

        held = moved(self._texts)
        old_sidecar, new_sidecar = self.sidecar_path, target.with_suffix(".layout.yaml")
        blocks = (self.positions, self.groups) if self._positions is not None or self._groups is not None else None
        for key, text in others.items():  # the included files first: a write cut short never leaves a graph naming them
            self._write(key, text)
        self._write(target, held[new_main])
        if blocks is not None:
            _layout().write_sidecar(new_sidecar, blocks[0], groups=blocks[1])
        elif old_sidecar.is_file():
            new_sidecar.write_bytes(old_sidecar.read_bytes())
        self._texts = held
        self._saved = {**{k: t for k, t in self._saved.items() if k != old_main}, **others, new_main: held[new_main]}
        self._history = [_Entry(moved(entry.texts), entry.rename, entry.members) for entry in self._history]
        self._redo = [_Entry(moved(entry.texts), entry.rename, entry.members) for entry in self._redo]
        self.path = target
        self._cache = None
        self._layout_dirty = False

    # -- the clipboard -----------------------------------------------------------------------

    def copy_text(self, nodes: Sequence[str]) -> Tuple[str, ClipKind]:
        """The YAML text of ``nodes``, as the clipboard holds it, in document order.

        Flow steps give a steps mapping — each step's own lines (the comments below it travel with it,
        the ``pair_stop_line`` rule) dedented to the left edge; list members (``stream.ops[1]``) give a
        block sequence the same way. Nothing else is copied, and the two never mix. The steps of a
        subgraph's inside copy the same way (``prep/grey`` gives ``grey:`` …); the nodes are of ONE level.
        A copied subgraph whose steps are ``{include: <file>}`` carries that file's steps INLINE
        (:meth:`_inline_includes`): a copy is a full, independent copy of the block.
        """
        if not nodes:
            raise ValueError("copy needs at least one node")
        derivation = self._derivation()
        model = derivation.model
        for name in nodes:
            model.node(name)
        levels = [name.rpartition(SEPARATOR)[0] for name in nodes]
        if len(set(levels)) > 1:
            other = next(name for name, level in zip(nodes, levels) if level != levels[0])
            raise ValueError(f"copy takes the nodes of ONE level — {nodes[0]!r} and {other!r} are on different levels")
        if levels[0]:
            level = self._level(levels[0], derivation)
            others = [name for name in nodes if name not in level.step_names()]
            if others:
                raise ValueError(f"{others[0]!r} is not a flow step or a list member — only those can be copied")
            block = self._steps_block(level.text, level.steps, [level.local(name) for name in nodes])
            return self._inline_includes(block, level.key.parent), "steps"
        steps = [step.name for step in derivation.flow.steps] if derivation.flow is not None else []
        step_names = [name for name in nodes if name in steps]
        members = [name for name in nodes if name not in steps and _LIST_MEMBER.match(name)]
        others = [name for name in nodes if name not in step_names and name not in members]
        if others:
            raise ValueError(f"{others[0]!r} is not a flow step or a list member — only those can be copied")
        if step_names and members:
            raise ValueError("copy takes flow steps or list members, not both")
        text = YamlText(self.text)
        if step_names:
            for name in step_names:
                self._flow_step_in_main(model.node(name), derivation)
            flow_node = text.path("flow")
            assert isinstance(flow_node, yaml.MappingNode)
            return self._inline_includes(self._steps_block(text, flow_node, step_names), self._folder), "steps"
        pieces: List[Tuple[int, str]] = []
        for name in members:
            sequence, index = self._list_of(text, name, derivation)
            item = sequence.value[index]
            # the member's own lines — the comments written for it above, never the next member's (what Cut removes)
            block = text.lines(self._member_start(text, sequence, index), self._member_stop(text, sequence, index))
            column = int(sequence.start_mark.column)
            pieces.append((int(item.start_mark.index), dedent(block, column)))
        return self._inline_includes("".join(piece for _at, piece in sorted(pieces)), self._folder), "ops"

    def paste_text(self, clip: str, after: str) -> List[str]:
        """Insert the clipboard ``clip`` after the node ``after`` as ONE edit; answer the new nodes' names.

        Flow steps land as a chain after ``after``: a taken name gets the next free ``_2`` suffix and every
        reference AMONG the pasted steps follows it; a pasted step reading a step that was NOT pasted reads
        the one before it in the paste instead (the first one reads ``after``); the step that read
        ``after`` now reads the last pasted one, and the output moves to it when ``after`` was the tail —
        the :meth:`add_step` rule. List members are inserted after the member ``after`` in its list.
        """
        try:
            pasted = YamlText(clip)
            root = pasted.root
        except yaml.YAMLError as exc:
            raise ValueError(f"the pasted text is not YAML: {' '.join(str(exc).split())}") from None
        derivation = self._derivation()
        anchor = derivation.model.node(after)
        if isinstance(root, yaml.MappingNode):
            return self._paste_steps(pasted, root, anchor, derivation)
        if isinstance(root, yaml.SequenceNode):
            return self._paste_members(pasted, root, anchor, derivation)
        shown = "nothing" if root is None else f"a single value ({' '.join(clip.split())[:60]!r})"
        raise ValueError(f"the pasted text is not flow steps or a list — it is {shown}")

    def template_text(self, nodes: Sequence[str]) -> str:
        """The steps a template saves from ``nodes``: ONE subgraph step gives its INNER steps (its own lines,
        or the text of the file its ``steps:`` includes); flow steps give themselves (:meth:`copy_text`)."""
        if len(nodes) == 1:
            inner = self._subgraph_steps(nodes[0])
            if inner is not None:
                return inner
        text, kind = self.copy_text(nodes)
        if kind != "steps":
            raise ValueError(f"a template holds flow steps — {nodes[0]!r} is a list member")
        return text

    # -- positions ---------------------------------------------------------------------------

    @property
    def positions(self) -> Dict[str, Tuple[float, float]]:
        """Every node's position, on every level: a name in the sidecar keeps its place, an unknown one is
        laid out — each subgraph's inside on its own, by the same layered rule, so a first open of a subgraph
        is arranged; positions set on this document (not yet saved) win. Once the held positions are WHOLE (a
        list's members shifted, :meth:`_shift_members`) they are the only places kept: a name they lack is laid out."""
        laid: Dict[str, Tuple[float, float]] = {}
        kept = self._positions if self._positions_whole else None
        for level in self.model.levels():
            laid.update(_layout().positions(level, self.sidecar_path, kept=kept))
        if self._positions and not self._positions_whole:
            for name, xy in self._positions.items():
                if name in laid:
                    laid[name] = (float(xy[0]), float(xy[1]))
        return laid

    def set_positions(self, mapping: Mapping[str, Tuple[float, float]]) -> None:
        """Hold these positions (written to the sidecar by :meth:`save`); the document is dirty until then.

        MERGED over the positions already held, never replacing them: a caller sending only the nodes it moved
        must not drop an earlier move that is not saved yet (before Explicit Save each write was saved, so a
        partial map accumulated in the sidecar — the held copy keeps that behaviour)."""
        held = dict(self._positions or {})
        held.update({str(name): (float(xy[0]), float(xy[1])) for name, xy in mapping.items()})
        self._positions = held
        self._layout_dirty = True

    # -- groups ------------------------------------------------------------------------------

    @property
    def groups(self) -> List["Group"]:
        """The boxes the canvas draws around nodes: the ones set on this document, else the sidecar's.

        Opaque (``streamstudio.layout.Group``): stored as the page spelled them, a member the model no
        longer has included — the document has no group spelling, so nothing here can refuse one.
        """
        if self._groups is not None:
            return list(self._groups)
        return list(_layout().read_groups(self.sidecar_path))

    def set_groups(self, groups: Sequence["Group"]) -> None:
        """Replace every group; written to the sidecar by :meth:`save`, never to the document."""
        self._groups = [group for group in groups]
        self._layout_dirty = True

    # -- internals ---------------------------------------------------------------------------

    def _refuse_changed_on_disk(self, keys: Iterable[Path]) -> None:
        """Refuse a write over a file whose disk text is no longer what the editor last read or wrote — naming
        the remedies that work for it: Revert, and Save As only for the graph's own file. A file gone from
        disk is not refused (writing it overwrites nothing)."""
        changed = self._changed_on_disk(keys)
        if changed:
            message = CHANGED_ON_DISK if changed[0] == self.path.resolve() else CHANGED_ON_DISK_INCLUDED
            raise ValueError(message.format(path=changed[0]))

    def _changed_on_disk(self, keys: Iterable[Path]) -> List[Path]:
        """Those of ``keys`` on disk whose text is not what the editor last read or wrote (under :data:`_DISK_LOCK`);
        one that cannot be read is refused (:data:`CANNOT_READ`)."""
        changed = []
        for key in keys:
            if not key.is_file():
                continue
            try:
                text = key.read_text()
            except (OSError, UnicodeDecodeError) as exc:
                raise ValueError(CANNOT_READ.format(path=key, reason=unreadable_reason(exc))) from None
            if text != self._saved.get(key):
                changed.append(key)
        return changed

    @staticmethod
    def _refuse_unwritable(keys: Iterable[Path]) -> None:
        """Refuse a save BEFORE it writes anything when the disk will not take one of ``keys``, in the words a failed
        write gives (``<path> cannot be written: Permission denied``, ``… No such file or directory``). A save writes
        several files, and a refusal changes nothing: measured before (2026-09-30), a Save whose new steps file sat in
        a read-only folder wrote the graph first, then refused — leaving a graph on disk that includes a file that is
        not there (a fresh ``confluid.load`` of it: ``ConfigFileNotFoundError``)."""
        for key in keys:
            reason = _unwritable(key)
            if reason is not None:
                raise ValueError(f"{key} cannot be written: {os.strerror(reason)}")

    @staticmethod
    def _write(key: Path, text: str) -> None:
        """Write one file, refused in words naming it when the disk says no (``<path> cannot be written: No such file
        or directory``) — never a bare ``OSError`` (measured before: a Save into a folder moved away was an HTML 500).
        """
        try:
            key.write_text(text)
        except OSError as exc:
            raise ValueError(f"{key} cannot be written: {exc.strerror or exc}") from None

    @staticmethod
    def _steps_block(text: YamlText, steps_node: yaml.MappingNode, names: Sequence[str]) -> str:
        """The lines of the steps ``names`` of one steps mapping, in document order, dedented to column 0."""
        pieces: List[str] = []
        for key in text.keys(steps_node):
            if key not in names:
                continue
            found = text.pair(steps_node, key)
            assert found is not None
            key_node, value_node = found
            block = text.lines(int(key_node.start_mark.line), text.pair_stop_line(key_node, value_node))
            pieces.append(dedent(block, int(key_node.start_mark.column)))
        return "".join(pieces)

    def _inline_includes(self, text: str, base_dir: Path) -> str:
        """``text`` with every subgraph whose ``steps:`` is ``{include: <file>}`` given that file's steps INLINE —
        its lines and comments, re-indented under ``steps:`` — read from ``base_dir`` (the folder of the file
        the text came from); an include inside the inlined steps is read from THAT file's folder, recursively.

        Why: DESIGN §0.3.1 — Copy pastes a full, independent copy of a block; sharing one steps file is what
        *Insert steps file…* is for. Measured before: a copied ``steps: {include: prep.steps.yaml}`` pasted
        into a graph in another folder bound silently to THAT folder's ``prep.steps.yaml``, and in the same
        graph editing the copy edited the original's file. A comment after the braces stays on the
        ``steps:`` line; the file's own comments come along."""
        try:
            parsed = YamlText(text)
            nodes = parsed.nodes()
        except yaml.YAMLError:
            return text
        edits: List[Edit] = []
        for node in nodes:
            if not isinstance(node, yaml.MappingNode) or not is_subgraph_marker(node):
                continue
            found = parsed.pair(node, "steps")
            if found is None or not isinstance(found[1], yaml.MappingNode) or parsed.keys(found[1]) != [_INCLUDE]:
                continue
            steps_key, steps_node = found
            assert isinstance(steps_node, yaml.MappingNode)  # checked just above, on found[1]
            entry = parsed.pair(steps_node, _INCLUDE)
            assert entry is not None
            entries = entry[1].value if isinstance(entry[1], yaml.SequenceNode) else [entry[1]]
            if node.flow_style:
                raise ValueError(
                    f"a copied subgraph written on one line ({{…}}) includes {spell(str(entries[0].value))} — spell "
                    "it as a block mapping to copy it"
                )
            inner = ""
            for item in entries:
                target = self._include_target(str(item.value), base_dir)
                if target.resolve() not in self._texts and not target.is_file():
                    raise ValueError(f"the steps file {item.value!r} does not exist (looked for {target})")
                inner += self._inline_includes(self._text_of(target.resolve()), target.parent)
            indent = " " * (int(steps_key.start_mark.column) + 2)
            body = "".join(line + "\n" for line in indent_lines(inner, indent))
            next_line = parsed.line_start(int(steps_key.start_mark.line) + 1)
            if steps_node.flow_style:  # `steps: {include: x}   # a comment` — the comment stays on the steps: line
                tail = parsed.text[steps_node.end_mark.index : next_line].rstrip("\n")
                edits.append((int(steps_key.end_mark.index), next_line, f":{tail}\n{body}"))
            else:  # `steps:` then `include: x` on a line of its own
                head = parsed.text[steps_key.end_mark.index : next_line].rstrip("\n")
                stop = parsed.line_start(int(entries[-1].end_mark.line) + 1)
                edits.append((int(steps_key.end_mark.index), stop, f"{head}\n{body}"))
        return parsed.apply(edits)

    def _list_of(self, text: YamlText, name: str, derivation: _Derivation) -> Tuple[yaml.SequenceNode, int]:
        """The BLOCK sequence of the main text holding the list member ``name``, and its index there."""
        node = derivation.model.node(name)
        match = _LIST_MEMBER.match(name)
        if match is None:
            raise ValueError(f"{name!r} is not a list member")
        if node.source_file and Path(node.source_file).resolve() != self.path.resolve():
            raise ValueError(f"{name!r} comes from {node.source_file}; edit that list there")
        owner_path, index = match.group("owner"), int(match.group("index"))
        owner, dot, slot = owner_path.rpartition(".")
        container: Optional[yaml.Node] = text.root
        if dot:
            owner_loc = derivation.model.node(owner).loc
            if not owner_loc:
                raise ValueError(f"{owner!r} has no location in the document — its list cannot be edited")
            container = text.node_at(*_split_loc(owner_loc)[1:])
        else:
            slot = owner_path
        found = text.pair(container, slot) if isinstance(container, yaml.MappingNode) else None
        if found is None or not isinstance(found[1], yaml.SequenceNode):
            raise ValueError(f"{owner_path} is not a list the document spells in block form")
        sequence = found[1]
        if sequence.flow_style:
            raise ValueError(
                f"{owner_path} is written on one line ([…]) — spell it as a block list (one '- ' item per line) "
                "to copy from it or paste into it"
            )
        return sequence, index

    def _paste_steps(
        self, pasted: YamlText, root: yaml.MappingNode, anchor: Node, derivation: _Derivation
    ) -> List[str]:
        """Flow steps after ``anchor`` on ITS level — the top ``flow:`` or a subgraph's ``steps:`` (the paste
        rules in :meth:`paste_text`); answers the pasted nodes' (qualified) names."""
        try:
            level, after = self._level_of_step(anchor, derivation)
        except ValueError as exc:
            raise ValueError(f"the clipboard holds flow steps; {exc}") from None
        originals = pasted.keys(root)
        if not originals:
            raise ValueError("the pasted text holds no steps")
        if _INCLUDE in originals:
            raise ValueError("the pasted text holds an include: — paste the steps it includes instead")
        taken = {level.local(name) for name in self._level_model(level).names()}
        used = taken | set(originals)
        renames: Dict[str, str] = {}
        for name in originals:
            if name in taken:
                renames[name] = _free_step_name(name, used)
                used.add(renames[name])
        new_names = [renames.get(name, name) for name in originals]
        edits: List[Edit] = []
        for old, new in renames.items():
            edits.extend(pasted.rename_edits(old, new, flow_key=None))
        for position, name in enumerate(originals):
            found = pasted.pair(root, name)
            assert found is not None
            reads = pasted.pair(found[1], "from") if isinstance(found[1], yaml.MappingNode) else None
            if reads is not None and isinstance(reads[1], yaml.ScalarNode) and reads[1].value not in originals:
                chained = new_names[position - 1] if position else after
                edits.append(pasted.replace_scalar_edit(reads[1], chained))
        respelled = dedent(pasted.apply(edits), int(root.start_mark.column))
        self._refuse_one_line(level, "paste into it")
        text = level.text
        found_anchor = text.pair(level.steps, after)
        assert found_anchor is not None
        key_node, value_node = found_anchor
        lines = indent_lines(respelled, " " * int(key_node.start_mark.column))
        out = _Edits()
        out.add(level.key, text, [text.insert_lines_edit(text.pair_stop_line(key_node, value_node), lines)])
        self._reroute(out, level, after, new_names[-1])
        self._commit(out.texts(), f"paste after {anchor.name!r}", anchor.loc)
        return [level.prefix + name for name in new_names]

    def _paste_members(
        self, pasted: YamlText, root: yaml.SequenceNode, anchor: Node, derivation: _Derivation
    ) -> List[str]:
        if _LIST_MEMBER.match(anchor.name) is None:
            raise ValueError(
                f"the clipboard holds list members; {anchor.name!r} is not a list member — paste them after one"
            )
        if root.flow_style:
            raise ValueError("the pasted list is written on one line ([…]) — paste a block list, one '- ' per item")
        if not root.value:
            raise ValueError("the pasted list holds no members")
        text = YamlText(self.text)
        sequence, index = self._list_of(text, anchor.name, derivation)
        # after the member's own lines, above the comments written for the member after it (:meth:`_member_start`)
        stop = self._member_stop(text, sequence, index)
        lines = indent_lines(dedent(pasted.text, int(root.start_mark.column)), " " * int(sequence.start_mark.column))
        owner = anchor.name[: anchor.name.rindex("[")]
        new_names = [f"{owner}[{index + 1 + offset}]" for offset in range(len(root.value))]
        shift: _MemberShift = (owner, index + 1, len(root.value))
        self._commit(
            {self.path.resolve(): text.apply([text.insert_lines_edit(stop, lines)])},
            f"paste after {anchor.name!r}",
            anchor.loc,
            members=shift,
        )
        return new_names

    # -- op lists: members added and removed -----------------------------------------------------

    @staticmethod
    def _op_member(name: str) -> Optional[Tuple[str, int]]:
        """``(the list, the index)`` when ``name`` is a member of an op list — ``stream.ops[1]`` → ``("stream.ops",
        1)``, also inside a subgraph (``prep/pipe.transforms[0]``) — else ``None``. The list must be a SLOT of a node
        (``<node>.ops``): a top-level ``ops:`` key is a list of the document, not of a Stream."""
        match = _LIST_MEMBER.match(name)
        if match is None:
            return None
        owner_path = match.group("owner")
        owner, dot, slot = owner_path.rpartition(".")
        if not dot or not owner or slot not in OP_LISTS or SEPARATOR in slot:
            return None
        return owner_path, int(match.group("index"))

    def _op_list(
        self, owner_path: str, derivation: _Derivation, *, create: bool
    ) -> Tuple[YamlText, yaml.MappingNode, yaml.ScalarNode, yaml.SequenceNode]:
        """The main text (with the list added when ``create`` and the marker spells none), the marker owning the op
        list ``owner_path`` (``stream.ops``), its key and the list — refused, naming why, when that is not an op list
        of a node the main file spells, or not a list."""
        owner, dot, slot = owner_path.rpartition(".")
        if not dot or not owner or slot not in OP_LISTS:
            lists = ", ".join(f"'<node>.{each}'" for each in OP_LISTS)
            raise ValueError(
                f"{owner_path!r} is not an op list — a class is added to the ops of a Stream or the transforms of a "
                f"Pipeline ({lists})"
            )
        node = derivation.model.node(owner)
        if not any(socket.name.startswith(f"{slot}[") for socket in node.inputs):
            raise ValueError(f"{owner} ({node.class_path or node.kind}) takes no {slot} list")
        if node.source_file and Path(node.source_file).resolve() != self.path.resolve():
            raise ValueError(f"{owner_path} comes from {node.source_file}; edit that list there")
        if not node.loc:
            raise ValueError(f"{owner!r} has no location in the document — its list cannot be edited")
        _file, line, col = _split_loc(node.loc)
        text = YamlText(self.text)
        marker = text.node_at(line, col)
        found = text.pair(marker, slot) if isinstance(marker, yaml.MappingNode) else None
        if found is None and create:
            text = YamlText(text.set_kwarg(line, col, slot, []))
            marker = text.node_at(line, col)
            found = text.pair(marker, slot) if isinstance(marker, yaml.MappingNode) else None
        if found is None:
            raise ValueError(f"{node.loc}: {owner} spells no {slot} list")
        key, value = found
        if not isinstance(value, yaml.SequenceNode):
            raise ValueError(
                f"{_at(self.path.resolve(), value)}: {owner_path} is not a list the document spells — write it as a "
                f"list ({slot}: []) to add to it"
            )
        assert isinstance(marker, yaml.MappingNode)
        if value.flow_style and value.start_mark.line != value.end_mark.line:
            raise ValueError(
                f"{_at(self.path.resolve(), value)}: {owner_path} is written in brackets over several lines — spell "
                "it as a block list (one '- ' item per line) to add to it or remove from it"
            )
        return text, marker, key, value

    @staticmethod
    def _dash_line(text: YamlText, item: yaml.Node) -> int:
        """The line of a block list item's ``-`` — the item's own line, or the line above when the ``-`` stands
        alone there (``-`` then the value on the next line)."""
        line = int(item.start_mark.line)
        lines = text.text.splitlines()
        if line > 0 and not lines[line].lstrip().startswith("-") and lines[line - 1].strip() == "-":
            return line - 1
        return line

    def _member_start(self, text: YamlText, sequence: yaml.SequenceNode, index: int) -> int:
        """The first line of the member ``index`` of a block list: its ``-`` line with the comment lines written
        directly above it, at the ``-``'s column — a comment on its own line above a member is about THAT member, so
        it goes when the member goes, moves when it is cut and pasted, and a member inserted before it goes above it.
        Measured before: removing ``ConvertMode`` also took the ``# then the mask`` line of the member below it, and a
        member added after ``ConvertMode`` landed between that comment and its member. The comments above the FIRST
        member are the list's (written under its key) and stay with the list."""
        dash = self._dash_line(text, sequence.value[index])
        if index == 0:
            return dash
        lines = text.text.splitlines()
        column = int(sequence.start_mark.column)
        start = dash
        while start > 0:
            above = lines[start - 1]
            if not above.lstrip().startswith("#") or len(above) - len(above.lstrip()) != column:
                break
            start -= 1
        return start

    def _member_stop(self, text: YamlText, sequence: yaml.SequenceNode, index: int) -> int:
        """The first line after the member ``index`` of a block list: where the next member starts
        (:meth:`_member_start` — above the comments written for it); for the last one,
        :meth:`YamlText.item_stop_line` moved up past the blank lines and the comments written LEFT of the
        list's ``-`` that end it (such a comment is about the line after the list, and stays with it)."""
        if index + 1 < len(sequence.value):
            return self._member_start(text, sequence, index + 1)
        stop = text.item_stop_line(sequence, index)
        column = int(sequence.start_mark.column)
        lines = text.text.splitlines()
        first = int(sequence.value[index].start_mark.line)
        own = stop
        for line in range(min(stop, len(lines)) - 1, first, -1):
            stripped = lines[line].strip()
            if stripped and not (stripped.startswith("#") and len(lines[line]) - len(lines[line].lstrip()) < column):
                break
            own = line
        return own

    def _add_member(self, after: Optional[str], into: Optional[str], class_path: str, params: Dict[str, Any]) -> str:
        """:meth:`add_step` for an op list: the new member after the member ``after``, or at the end of ``into``."""
        derivation = self._derivation()
        if after is None:
            if into is None:
                raise ValueError(
                    "adding a class needs 'after' (the node to add it after) or 'list' (the op list to add it to the "
                    "end of, e.g. stream.ops)"
                )
            owner_path, position, loc = into, -1, ""
        else:
            anchor = derivation.model.node(after)
            member = self._op_member(after)
            assert member is not None  # add_step routes only members here
            owner_path, position, loc = member[0], member[1] + 1, anchor.loc
            if into is not None and into != owner_path:
                raise ValueError(f"{after!r} is a member of {owner_path}, not of {into}")
        text, marker, key, sequence = self._op_list(owner_path, derivation, create=after is None)
        items = sequence.value
        position = len(items) if position < 0 else position
        spelled = f"!class:{class_path} {spell_mapping(params)}"
        edits: List[Edit]
        if not sequence.flow_style:
            at = position if position < len(items) else None
            line = (
                self._member_start(text, sequence, at)
                if at is not None
                else self._member_stop(text, sequence, len(items) - 1)
            )
            edits = [text.insert_lines_edit(line, [" " * int(sequence.start_mark.column) + "- " + spelled])]
        elif not items and not marker.flow_style:
            # `ops: []` under a block marker becomes a block list: the key keeps its line (and its comment)
            colon = text.text.index(":", key.end_mark.index)
            column = int(key.start_mark.column) + 2
            edits = [
                (colon + 1, sequence.end_mark.index, ""),
                text.insert_lines_edit(int(key.start_mark.line) + 1, [" " * column + "- " + spelled]),
            ]
        elif not items:
            edits = [(sequence.start_mark.index + 1, sequence.start_mark.index + 1, spelled)]
        else:
            end = items[position - 1].end_mark.index
            edits = [(end, end, f", {spelled}")]
        shift: _MemberShift = (owner_path, position, 1)
        self._commit(
            {self.path.resolve(): text.apply(edits)},
            f"add {class_path} to {owner_path} at [{position}]",
            loc or derivation.model.node(owner_path.rpartition(".")[0]).loc,
            members=shift,
        )
        return f"{owner_path}[{position}]"

    def _remove_member(self, name: str) -> None:
        """:meth:`remove_step` for a member of an op list: its lines (or its text and one comma in a one-line
        ``[…]``) go; the last member of a block list leaves ``ops: []``."""
        derivation = self._derivation()
        node = derivation.model.node(name)
        member = self._op_member(name)
        assert member is not None
        owner_path, index = member
        text, _marker, key, sequence = self._op_list(owner_path, derivation, create=False)
        items = sequence.value
        if index >= len(items):  # pragma: no cover - the model names only the members the list has
            raise KeyError(f"no member {name!r}")
        edits: List[Edit]
        if sequence.flow_style:
            if index > 0:
                edits = [(items[index - 1].end_mark.index, items[index].end_mark.index, "")]
            elif len(items) > 1:
                edits = [(items[0].start_mark.index, items[1].start_mark.index, "")]
            else:
                edits = [(items[0].start_mark.index, items[0].end_mark.index, "")]
        else:
            start = text.line_start(self._member_start(text, sequence, index))
            edits = [(start, text.line_start(self._member_stop(text, sequence, index)), "")]
            if len(items) == 1:
                colon = text.text.index(":", key.end_mark.index)
                edits.append((colon + 1, colon + 1, " []"))
        shift: _MemberShift = (owner_path, index + 1, -1)
        self._commit({self.path.resolve(): text.apply(edits)}, f"remove {name!r}", node.loc, members=shift)

    def _subgraph_steps(self, name: str) -> Optional[str]:
        """The inner steps text of the subgraph node ``name`` — its ``steps:`` lines dedented, or the text of
        the file(s) ``steps: {include: …}`` names — else ``None`` (not a subgraph).

        A top-level flow step is read off the TEXT, so a document whose included steps file is missing (it
        does not load, so it has no model) still names that file in the refusal; a node inside a subgraph, a
        list member or a subgraph in another op's slot (``maybe.op``) is found through the model's levels. An
        include inside the steps is written inline (:meth:`_inline_includes`): a template lives in another
        folder, where the include would name nothing."""
        if SEPARATOR in name or "." in name or _LIST_MEMBER.match(name):
            derivation = self._derivation()
            if derivation.model.node(name).kind != "subgraph":
                return None
            level = self._level(name, derivation)
            return self._inline_includes(self._steps_text(level), level.key.parent)
        text = YamlText(self.text)
        flow_node = text.path("flow")
        found = text.pair(flow_node, name) if isinstance(flow_node, yaml.MappingNode) else None
        if found is None:
            return None
        value = found[1]
        op = text.pair(value, "op") if isinstance(value, yaml.MappingNode) and not is_subgraph_marker(value) else None
        marker = op[1] if op is not None else value
        if not (isinstance(marker, yaml.MappingNode) and is_subgraph_marker(marker)):
            return None
        steps = text.pair(marker, "steps")
        if steps is None:
            return None
        steps_key, steps_node = steps
        if not isinstance(steps_node, yaml.MappingNode):
            return None
        if text.keys(steps_node) == [_INCLUDE]:
            include = text.pair(steps_node, _INCLUDE)
            assert include is not None
            entries = include[1].value if isinstance(include[1], yaml.SequenceNode) else [include[1]]
            return "".join(
                self._inline_includes(
                    self._included_text(str(entry.value)),
                    self._include_target(str(entry.value), self._folder).parent,
                )
                for entry in entries
            )
        if steps_node.flow_style:
            return self.text[steps_node.start_mark.index : steps_node.end_mark.index] + "\n"
        block = text.lines(int(steps_key.start_mark.line) + 1, text.pair_stop_line(steps_key, steps_node))
        first_key = steps_node.value[0][0] if steps_node.value else steps_key
        return self._inline_includes(dedent(block, int(first_key.start_mark.column)), self._folder)

    def _included_text(self, entry: str) -> str:
        """The text of an included file as the editor holds it (a pending edit wins over the disk, and a steps
        file moved there and not saved yet is found)."""
        found = self._include_target(entry, self._folder)
        key = found.resolve()
        if key in self._texts:
            return self._texts[key]
        if not found.is_file():
            raise ValueError(f"{self.path}: the steps file {entry!r} does not exist (looked for {found})")
        return self._text_of(key)

    def _text_of(self, key: Path) -> str:
        """The text of ``key`` as the editor holds it — its held text, else the disk's, read NOW.

        Reading never makes the editor HOLD a file: a read-only operation (Copy, Save as template, the level
        an edit looks at) would otherwise pin a copy the canvas then keeps showing and a trace keeps running,
        whatever a teammate wrote since (measured before: after a Copy, the canvas said ``mode L`` while the
        file said RGB). Only an edit that changes the file makes the editor hold it (:meth:`_commit`), with the
        text read here as its baseline."""
        if key in self._texts:
            return self._texts[key]
        with _DISK_LOCK:
            text = key.read_text()
        self._read[key] = text
        return text

    def _flow_step_in_main(self, node: Node, derivation: _Derivation) -> _Step:
        steps = [step.name for step in derivation.flow.steps] if derivation.flow is not None else []
        if node.name not in steps:
            raise ValueError(f"{node.name!r} is not a flow step (the steps are: {steps})")
        assert derivation.flow is not None
        if node.source_file and Path(node.source_file).resolve() != self.path.resolve():
            raise ValueError(f"{node.name!r} comes from {node.source_file}; edit the steps of that file there")
        return next(step for step in derivation.flow.steps if step.name == node.name)

    # -- levels: the steps mapping a node lives in ----------------------------------------------

    def _level(self, name: str, derivation: _Derivation) -> _Level:
        """The level ``name`` names: ``""`` the top ``flow:``, else the subgraph node ``name`` — its ``steps:``
        mapping, found at the marker's own ``file:line:col``, or the root mapping of the steps file its
        ``steps: {include: …}`` names (that file's text is then held here like any edited file)."""
        if not name:
            if derivation.flow is None:
                raise ValueError(f"{self.path} has no flow: steps")
            main = self.path.resolve()
            text = YamlText(self.text)
            steps = text.path("flow")
            assert isinstance(steps, yaml.MappingNode)  # a derived flow came from a flow: mapping
            outputs = text.pair(text.root, "outputs") if isinstance(text.root, yaml.MappingNode) else None
            result = outputs[1] if outputs is not None and isinstance(outputs[1], yaml.ScalarNode) else None
            return _Level("", "", derivation.flow, main, text, steps, main, text, result, None, "the flow")
        node = derivation.model.node(name)
        if node.kind != "subgraph":
            raise ValueError(f"{name!r} is not a subgraph")
        file, line, col = _split_loc(node.loc)  # a subgraph node always comes from a located marker
        key = Path(file).resolve()
        text = YamlText(self._text_of(key))
        marker = text.node_at(line, col)
        found = text.pair(marker, "steps") if isinstance(marker, yaml.MappingNode) else None
        if found is None or not isinstance(found[1], yaml.MappingNode):
            raise ValueError(f"{name} spells no steps: mapping at {node.loc}")
        assert isinstance(marker, yaml.MappingNode)
        steps, steps_key, steps_text = found[1], key, text
        if text.keys(steps) == [_INCLUDE]:
            entry = text.pair(steps, _INCLUDE)
            assert entry is not None
            if not isinstance(entry[1], yaml.ScalarNode):
                raise ValueError(f"{name}'s steps come from several files ({node.loc}) — edit each file on its own")
            target = self._include_target(str(entry[1].value), key.parent)
            steps_key = target.resolve()
            if steps_key not in self._texts and not target.is_file():
                raise ValueError(f"{self.path}: the steps file {entry[1].value!r} does not exist (looked for {target})")
            steps_text = YamlText(self._text_of(steps_key))
            root = steps_text.root
            if not isinstance(root, yaml.MappingNode) or _INCLUDE in steps_text.keys(root):
                raise ValueError(f"{name}'s steps file {target} is not a plain steps mapping — edit it there")
            steps = root
        result_pair = text.pair(marker, "result")
        result = result_pair[1] if result_pair is not None and isinstance(result_pair[1], yaml.ScalarNode) else None
        flow = derivation.subflows[name]
        return _Level(
            name,
            name + SEPARATOR,
            flow,
            steps_key,
            steps_text,
            steps,
            key,
            text,
            result,
            marker,
            f"the subgraph {name!r}",
        )

    def _include_target(self, entry: str, base_dir: Path) -> Path:
        """The file an ``include:`` entry names, read from ``base_dir``: the file beside it when the disk or the
        editor has it (a steps file moved there and not saved yet counts), else confluid's own search."""
        beside = base_dir / Path(entry).expanduser()
        if beside.resolve() in self._texts or beside.is_file():
            return beside
        return resolve_config_path(entry, base_dir=base_dir)

    def _level_of_step(self, node: Node, derivation: _Derivation) -> Tuple[_Level, str]:
        """The level the STEP ``node`` is a step of, and its name there (``prep/grey`` → prep's, ``grey``)."""
        level_name, _sep, local = node.name.rpartition(SEPARATOR)
        if not level_name:
            self._flow_step_in_main(node, derivation)
        else:
            flow = derivation.subflows.get(level_name)
            names = [step.name for step in flow.steps] if flow is not None else []
            if node.name not in names:
                loc = node.loc or derivation.model.node(level_name).loc  # a boundary has no line: its subgraph's
                at = f"{loc}: " if loc else ""
                raise ValueError(
                    f"{at}{node.name!r} is not a step of the subgraph {level_name!r} (the steps are: {names})"
                )
        return self._level(level_name, derivation), local

    def _level_model(self, level: _Level) -> Model:
        model = self.model
        return model if not level.name else model.inside_of(level.name)

    @staticmethod
    def _local_name(given: str, level: _Level, anchor: str) -> str:
        """A new step's name on ``level``, given bare (``mid``) or qualified by that level (``prep/mid``)."""
        head, sep, tail = given.rpartition(SEPARATOR)
        if not sep:
            return given
        if head == level.name:
            return tail
        spelled = f"{tail!r} or {level.prefix + tail!r}" if level.name else repr(tail)
        raise ValueError(f"{given!r} is not a name on the level of {anchor!r} — name it {spelled}")

    def _fresh_step_name(self, level: _Level, given: Optional[str], base: str) -> str:
        """A new step's name on ``level``: ``given`` when it is free (refused when taken), else ``base`` made a
        step name (:func:`_step_name_of`) and unique with ``_2`` …"""
        taken = {level.local(name) for name in self._level_model(level).names()}
        if given:
            local = _check_step_name(self._local_name(given, level, level.name or "the flow"))
            if local in taken:
                raise ValueError(f"a step named {local!r} is already there — choose another name")
            return local
        name = _step_name_of(base)
        return name if name not in taken else _free_step_name(name, taken)

    @staticmethod
    def _refuse_one_line(level: _Level, what: str) -> None:
        if level.steps.flow_style:
            raise ValueError(
                f"{_at(level.key, level.steps)}: the steps of {level.label} are written on one line ({{…}}) — spell "
                f"them as a block mapping, one step per line, to {what}"
            )

    def _insert_step(self, edits: _Edits, level: _Level, after: str, local: str, body: Sequence[str]) -> None:
        """Insert the step ``local`` (its lines under the key: ``body``, relative) after ``after`` on ``level``,
        and re-route what read ``after`` (:meth:`_reroute`)."""
        self._refuse_one_line(level, "add a step")
        text = level.text
        found = text.pair(level.steps, after)
        assert found is not None
        key_node, value_node = found
        indent = " " * key_node.start_mark.column
        lines = [f"{indent}{local}:", *(f"{indent}  {line}" if line else "" for line in body)]
        edits.add(level.key, text, [text.insert_lines_edit(text.pair_stop_line(key_node, value_node), lines)])
        self._reroute(edits, level, after, local)

    def _reroute(self, edits: _Edits, level: _Level, after: str, new: str) -> None:
        """The ``add_step`` rule for a step inserted after ``after``: the step that read ``after`` (its
        ``from:``) now reads ``new``, and the level's output moves to ``new`` when ``after`` was the tail."""
        text = level.text
        steps = text.keys(level.steps)
        position = steps.index(after)
        if position + 1 < len(steps):
            following = text.pair(level.steps, steps[position + 1])
            assert following is not None
            reads = text.pair(following[1], "from") if isinstance(following[1], yaml.MappingNode) else None
            if reads is not None and isinstance(reads[1], yaml.ScalarNode) and reads[1].value == after:
                edits.add(level.key, text, [text.replace_scalar_edit(reads[1], new)])
        else:
            self._result_edit(edits, level, after, new)

    @staticmethod
    def _result_edit(edits: _Edits, level: _Level, old: str, new: str) -> None:
        """Re-point the level's spelled output (``outputs:`` / ``result:``) from ``old`` to ``new``."""
        if level.result is not None and level.result.value == old:
            edits.add(level.result_key, level.result_text, [level.result_text.replace_scalar_edit(level.result, new)])

    def _steps_text(self, level: _Level) -> str:
        """A subgraph level's steps as a steps file holds them: the included file's text, else its inline
        ``steps:`` lines dedented (or the one-line mapping as written)."""
        if level.included:
            return level.text.text
        assert level.marker is not None
        text = level.result_text
        found = text.pair(level.marker, "steps")
        assert found is not None
        steps_key, steps_node = found
        assert isinstance(steps_node, yaml.MappingNode)  # _level found a steps mapping there
        if steps_node.flow_style:
            return text.text[steps_node.start_mark.index : steps_node.end_mark.index] + "\n"
        first_key = steps_node.value[0][0] if steps_node.value else steps_key
        column = int(first_key.start_mark.column)
        stop = _own_stop_line(text, text.pair_stop_line(steps_key, steps_node), column)
        return dedent(text.lines(int(steps_key.start_mark.line) + 1, stop), column)

    # -- convert to subgraph -----------------------------------------------------------------

    @staticmethod
    def _reads(step: _Step) -> List[Tuple[str, str, str, str]]:
        """What a step reads, as the document spells it: ``(key, step read, how it is written, attr)``."""
        reads: List[Tuple[str, str, str, str]] = []
        from_ = step.reserved.get("from")
        if from_ is not None:
            reads.append(("from", str(from_), f"from: {from_}", ""))
        merge = step.reserved.get("merge_from") or ()
        for ref in [merge] if isinstance(merge, str) else list(merge):
            reads.append(("merge_from", str(ref), f"merge_from: {ref}", ""))
        bind = step.reserved.get("bind")
        for param, ref in bind.items() if isinstance(bind, dict) else []:
            read, attr, _key = _split_bind(str(ref))
            reads.append(("bind", read, f"bind: {param}={ref}", attr))
        return reads

    @staticmethod
    def _read_at(level: _Level, reader: str, key: str) -> str:
        """``file:line:col: `` of the line where the step ``reader`` of ``level`` spells ``key`` (``from:``,
        ``merge_from:``, ``bind:``) — its key line when that is not found; ``""`` when the step is not there."""
        found = level.text.pair(level.steps, reader)
        if found is None:
            return ""
        node: yaml.Node = found[0]
        if isinstance(found[1], yaml.MappingNode):
            spelled = level.text.pair(found[1], key)
            if spelled is not None:
                node = spelled[0]
        return f"{_at(level.key, node)}: "

    def _convert_steps(self, nodes: Sequence[str], name: str, level: _Level, derivation: _Derivation) -> str:
        order = level.step_names()
        strangers = [each for each in nodes if each not in order]
        if strangers:
            raise ValueError(f"{strangers[0]!r} is not a step of {level.label} (the steps are: {order})")
        for each in nodes:
            if level.text.pair(level.steps, level.local(each)) is None:
                raise ValueError(f"{each!r} comes from another file; edit the steps of that file there")
        indices = sorted({order.index(each) for each in nodes})
        span = order[indices[0] : indices[-1] + 1]
        if len(span) != len(indices):
            between = ", ".join(level.local(each) for each in span if each not in nodes)
            raise ValueError(
                f"{level.local(span[0])} and {level.local(span[-1])} are not consecutive steps — select the steps "
                f"between them too ({between})"
            )
        selected = [level.local(each) for each in span]
        first, last = selected[0], selected[-1]
        taken = {level.local(each) for each in self._level_model(level).names()} - set(selected)
        local = _check_step_name(self._local_name(name, level, span[0]))
        if local in taken:
            raise ValueError(f"a step named {local!r} is already there — choose another name")
        inside = set(selected)
        for step in level.flow.steps:
            reader = level.local(step.name)
            for key, read, shown, attr in self._reads(step):
                if reader not in inside and read in inside and read != last:
                    raise ValueError(
                        f"{self._read_at(level, reader, key)}{reader} reads {read} ({shown}), which would move inside "
                        f"the subgraph — select {reader} too, or leave {read} out"
                    )
                if reader not in inside and read == last and attr:
                    raise ValueError(
                        f"{self._read_at(level, reader, key)}{reader} reads {read}.{attr} ({shown}), a value of "
                        f"{read}, which would move inside the subgraph — a step outside a subgraph cannot read a "
                        f"value of a step inside it; leave {read} out"
                    )
                if reader in inside and read not in inside and not (reader == first and key != "bind"):
                    raise ValueError(
                        f"{self._read_at(level, reader, key)}{reader} reads {read} ({shown}), which would stay "
                        "outside the subgraph — a step inside a subgraph reads only the record the subgraph "
                        f"receives and the steps before it inside; select {read} too, or leave {reader} out"
                    )
        output = level.local(level.flow.outputs)
        if output in inside and output != last:
            key = "result" if level.name else "outputs"
            at = f"{_at(level.result_key, level.result)}: " if level.result is not None else ""
            raise ValueError(
                f"{at}{level.label} returns {output} ({key}: {output}), which would move inside the subgraph — make "
                f"{output} the last selected step, or leave it out"
            )
        self._refuse_one_line(level, "convert them")
        text = level.text
        first_pair, last_pair = text.pair(level.steps, first), text.pair(level.steps, last)
        assert first_pair is not None and last_pair is not None
        column = int(first_pair[0].start_mark.column)
        start = text.line_start(first_pair[0].start_mark.line)
        stop_line = _own_stop_line(text, text.pair_stop_line(*last_pair), column)
        stop = text.line_start(stop_line)
        moved: List[Edit] = []
        outer: List[str] = []
        head = first_pair[1]
        if isinstance(head, yaml.MappingNode):
            for key in ("from", "merge_from"):
                found = text.pair(head, key)
                if found is None:
                    continue
                if head.flow_style:  # `{op: x, from: a}`: the pair has no line of its own to move
                    moved.append(text.drop_pair_edit(head, key))
                    outer.append(f"{key}: {_spelled(found[1])}")
                    continue
                # its own lines, as written — a trailing comment, a block list and the comments in it
                key_node, value_node = found
                pair_start = text.line_start(key_node.start_mark.line)
                own = _own_stop_line(text, text.pair_stop_line(key_node, value_node), int(key_node.start_mark.column))
                pair_stop = text.line_start(min(own, stop_line))
                moved.append((pair_start, pair_stop, ""))
                outer.extend(dedent(text.text[pair_start:pair_stop], int(key_node.start_mark.column)).splitlines())
        block = text.text[start:stop]
        for edit_start, edit_stop, replacement in sorted(moved, reverse=True):
            block = block[: edit_start - start] + replacement + block[edit_stop - start :]
        body = block.splitlines()
        trailing = 0
        while body and not body[-1].strip():
            body.pop()
            trailing += 1
        indent = " " * column
        lines = [
            f"{indent}{local}:",
            f"{indent}  op: !class:{SUBGRAPH_CLASS}",
            f"{indent}    steps:",
            *indent_lines(dedent("".join(line + "\n" for line in body), column), indent + "      "),
            f"{indent}    result: {last}",
            *(f"{indent}  {line}" if line.strip() else "" for line in outer),
            *([""] * trailing),
        ]
        edits = _Edits()
        edits.add(level.key, text, [(start, stop, "".join(line + "\n" for line in lines))])
        if local != last:
            others = [key for key in text.keys(level.steps) if key not in inside]
            edits.add(level.key, text, text.reference_edits(level.steps, last, local, only=others))
            self._result_edit(edits, level, last, local)
        qualified = level.prefix + local
        loc = derivation.model.node(span[0]).loc
        self._commit(edits.texts(), f"convert {selected} to the subgraph {qualified!r}", loc)
        return qualified

    def _convert_members(self, nodes: Sequence[str], derivation: _Derivation) -> str:
        """Consecutive members of ONE ``ops`` list become one member ``!class:…Subgraph``, whose steps are named
        after their classes (``convert_mode``, ``scale``, …) and hold each member's own lines as its ``op:``."""
        owners = {each[: each.rindex("[")] for each in nodes}
        if len(owners) > 1:
            raise ValueError(f"the members to convert must be of ONE list — {sorted(owners)} are several")
        owner = owners.pop()
        text = YamlText(self.text)
        found = [self._list_of(text, each, derivation) for each in nodes]
        sequence = found[0][0]
        indices = sorted({index for _sequence, index in found})
        missing = [f"{owner}[{index}]" for index in range(indices[0], indices[-1] + 1) if index not in indices]
        if missing:
            raise ValueError(
                f"{owner}[{indices[0]}] and {owner}[{indices[-1]}] are not consecutive members — select the members "
                f"between them too ({', '.join(missing)})"
            )
        first_item = sequence.value[indices[0]]
        column = int(first_item.start_mark.column)
        names: List[str] = []
        body: List[str] = []
        for index in indices:
            item = sequence.value[index]
            base = _snake(derivation.model.node(f"{owner}[{index}]").class_path)
            step = base if base not in names else _free_step_name(base, set(names))
            names.append(step)
            head = text.text[item.start_mark.index : text.line_start(item.start_mark.line + 1)].rstrip("\n")
            rest = dedent(text.lines(item.start_mark.line + 1, text.item_stop_line(sequence, index)), column)
            if str(item.tag or "").startswith("!"):
                body += [f"{step}:", f"  op: {head}", *indent_lines(rest, "    ")]
            else:  # a plain `_target_:` mapping: the whole item goes under `op:`
                body += [f"{step}:", "  op:", f"    {head}", *indent_lines(rest, "    ")]
        trailing = 0
        while body and not body[-1].strip():
            body.pop()
            trailing += 1
        start = text.line_start(first_item.start_mark.line)
        stop = text.line_start(text.item_stop_line(sequence, indices[-1]))
        dash = text.text[start : first_item.start_mark.index]
        indent = " " * column
        lines = [
            f"{dash}!class:{SUBGRAPH_CLASS}",
            f"{indent}steps:",
            *(f"{indent}  {line}" if line else "" for line in body),
            f"{indent}result: {names[-1]}",
            *([""] * trailing),
        ]
        new = f"{owner}[{indices[0]}]"
        loc = derivation.model.node(nodes[0]).loc
        shift: Optional[_MemberShift] = (owner, indices[-1] + 1, 1 - len(indices)) if len(indices) > 1 else None
        self._commit(
            {self.path.resolve(): text.apply([(start, stop, "".join(line + "\n" for line in lines))])},
            f"convert {[f'{owner}[{index}]' for index in indices]} to one subgraph member",
            loc,
            members=shift,
        )
        return new

    def _rekey(self, old: str, new: str) -> None:
        """Move a node's position and group memberships from ``old`` to ``new`` — and, for a subgraph, those of
        every node inside it (``prep/…`` → ``pre/…``, ``layout.rename_key``) — in memory, written by
        :meth:`save`; the sidecar is read first when nothing was set here yet."""
        layout = _layout()
        if self._positions is None and self.sidecar_path.exists():
            self._positions = dict(layout.read_sidecar(self.sidecar_path))
        if self._positions is not None:
            self._positions = {layout.rename_key(name, old, new): xy for name, xy in self._positions.items()}
        if self._groups is None and self.sidecar_path.exists():
            self._groups = list(_layout().read_groups(self.sidecar_path))
        if self._groups:
            self._groups = _layout().rename_member(self._groups, old, new)

    def _shift_members(self, owner: str, first: int, shift: int) -> _Dropped:
        """Re-key the positions and group memberships of the members of the list ``owner`` after its length
        changed: ``owner[i]`` with ``i >= first`` — and every name under it (``owner[i]/x``, ``owner[i].slot``)
        — becomes ``owner[i + shift]``; with a negative shift the members ``first + shift … first - 1`` are
        gone, and their names are dropped first — answered, so the step that brings them back restores them
        (:meth:`_restore_dropped`). A member's name IS its position, so without this a Convert
        or a Paste of members left every later member's place and group under a name another member now has.
        In memory, written by :meth:`save`.

        The sidecar's positions are taken in UNDER the held ones first, and the held positions are then WHOLE
        (:attr:`positions` lays out a name they lack): measured before, with a sidecar, a member pasted after
        ``stream.ops[0]`` was drawn at the sidecar's place of the old ``stream.ops[1]`` — on top of that member,
        now ``stream.ops[2]`` — and a later member only the sidecar placed kept its old name when a move was held."""
        member = re.compile(rf"^{re.escape(owner)}\[(\d+)\](?=$|[./\[])")

        def moved(name: str) -> Optional[str]:
            found = member.match(name)
            if found is None:
                return name
            index = int(found.group(1))
            if index < first:
                return None if first + shift <= index else name
            return f"{owner}[{index + shift}]{name[found.end():]}"

        layout = _layout()
        dropped = _Dropped({}, {})
        if not self._positions_whole and self.sidecar_path.exists():
            self._positions = {**layout.read_sidecar(self.sidecar_path), **(self._positions or {})}
        if self._positions is not None:
            dropped.positions.update({name: xy for name, xy in self._positions.items() if moved(name) is None})
            self._positions = {new: xy for name, xy in self._positions.items() if (new := moved(name)) is not None}
            self._positions_whole = True
        if self._groups is None and self.sidecar_path.exists():
            self._groups = list(layout.read_groups(self.sidecar_path))
        if self._groups:
            for group in self._groups:
                members = group.get("nodes")
                gone = [
                    (index, member)
                    for index, member in enumerate(members if isinstance(members, list) else [])
                    if isinstance(member, str) and moved(member) is None
                ]
                if gone:
                    dropped.groups[str(group.get("id"))] = gone
            self._groups = layout.map_members(self._groups, moved)
        return dropped

    def _restore_dropped(self, dropped: _Dropped) -> None:
        """Put back what a shift dropped: each name's place, and each name into its group at the index it had."""
        if dropped.positions:
            self._positions = {**(self._positions or {}), **dropped.positions}
        if dropped.groups and self._groups:
            restored: List["Group"] = []
            for group in self._groups:
                back = dropped.groups.get(str(group.get("id")))
                copy = dict(group)
                members = copy.get("nodes")
                if back and isinstance(members, list):
                    nodes = list(members)
                    for index, name in sorted(back):
                        if name not in nodes:
                            nodes.insert(min(index, len(nodes)), name)
                    copy["nodes"] = nodes
                restored.append(cast("Group", copy))
            self._groups = restored

    def _commit(
        self,
        changes: _Texts,
        what: str,
        loc: str,
        *,
        rename: Optional[Tuple[str, str]] = None,
        members: Optional[_MemberShift] = None,
    ) -> None:
        """Validate a candidate text, then make it the document's — or refuse it, unwritten.

        A file this edit changes for the FIRST time (an included steps file) becomes held here, with the text
        it was read as (:meth:`_text_of`) as its saved baseline — and that text is what it held before every
        earlier edit too, so each older history entry gets it: undoing past this edit restores the file,
        rather than falling back to the disk, which a Save may have changed since (measured before: undo
        after Save showed the saved ``RGB``, and the next Save wrote it back)."""
        before = self.check()
        candidate: _Texts = {**self._texts, **changes}
        derivation = derive(self.path, candidate)
        after = check(derivation, self.path)
        gained = _gained(before, after)
        if gained:
            where = f" at {loc}" if loc else ""
            raise ValueError(f"{what} refused{where}: " + "; ".join(problem.message for problem in gained))
        first_held = {key: self._read[key] for key in changes if key not in self._texts and key in self._read}
        self._hold(first_held)
        shifts: Tuple[_MemberShift, ...] = () if members is None else (members,)
        # the later members' names follow the edit (their places and groups) — and what the shift drops (a removed
        # member's) is kept with the edit, for the undo that brings the member back
        dropped = tuple(self._shift_members(*shift) for shift in shifts)
        if self._joining:
            self._joined_shifts.extend(shifts)
            self._joined_dropped.extend(dropped)
        else:
            self._history.append(_Entry({**self._texts, **first_held}, rename, shifts, dropped))
            del self._history[: -self.history_depth]
            self._redo.clear()
        self._texts = candidate
        self._cache = (self._key(), derivation, after)

    def _hold(self, first_held: _Texts) -> None:
        """Start holding files at the texts they were read as: their saved baseline, and what every earlier
        history entry held of them (they were read from disk before any of those edits touched them)."""
        for key, text in first_held.items():
            self._saved.setdefault(key, text)
            for entry in (*self._history, *self._redo):
                entry.texts.setdefault(key, text)

    @contextmanager
    def _one_edit(self) -> Iterator[None]:
        """Several commits as ONE edit: all of them, or none (a refusal of any restores everything, unwritten),
        and ONE history entry — the texts before the first — so one undo walks the whole of it back. The member
        shifts the joined commits made (a removal of several list members) are that entry's, in order; a refusal
        puts the positions and groups those shifts moved back too."""
        texts, saved, cache = dict(self._texts), dict(self._saved), self._cache
        history, redo = list(self._history), list(self._redo)
        layout_held = (self._positions, self._positions_whole, self._groups)
        self._joining = True
        self._joined_shifts = []
        self._joined_dropped = []
        try:
            yield
        except BaseException:
            self._texts, self._saved, self._history, self._redo, self._cache = texts, saved, history, redo, cache
            self._positions, self._positions_whole, self._groups = layout_held
            raise
        finally:
            self._joining = False
        # a file first held during the join: what it read as is what it held before it, as in `_commit`
        first_held = {
            key: self._saved[key] for key in self._texts if key not in texts and key not in saved and key in self._saved
        }
        self._history.append(
            _Entry({**texts, **first_held}, None, tuple(self._joined_shifts), tuple(self._joined_dropped))
        )
        del self._history[: -self.history_depth]
        self._redo.clear()


def _layout() -> ModuleType:
    """The layout module, imported on first use (positions are a separate concern with its own tests)."""
    return importlib.import_module("streamstudio.layout")


__all__ = [
    "ALREADY_EXISTS",
    "ANY",
    "BOUNDARY_IN",
    "BOUNDARY_OUT",
    "CANNOT_READ",
    "CHANGED_ON_DISK",
    "ClipKind",
    "DiskState",
    "Document",
    "HOST",
    "LIST_STR",
    "Model",
    "Node",
    "NodeKind",
    "OBJECT",
    "OP",
    "Param",
    "Problem",
    "RECORD",
    "SEPARATOR",
    "SINK",
    "SOURCE",
    "STEPS_SUFFIX",
    "STREAM",
    "SUBGRAPH_CLASS",
    "Socket",
    "SocketType",
    "Wire",
    "WireKind",
    "check",
    "derive",
    "rebase_includes",
    "unreadable_reason",
]
