"""The confluid document is the only truth: a canvas MODEL derived from it, written back by
patching the line a marker came from.

The editor never keeps a second document (``docs/rewrite.md`` §2.1). :class:`Document` reads
a YAML text, derives a :class:`Model` — nodes with typed sockets, wires, located problems —
from ``confluid.load(text, until="document")`` (markers kept, ``!ref`` kept, every marker
stamped ``file:line:col``), and every edit (``set_param``, ``add_step``, ``remove_step``,
``rename_node``) patches the TEXT through :mod:`streamstudio._yamltext`, re-derives, and is
refused before anything is written when the document no longer loads or :meth:`Document.check`
gains a problem. Measured against the alternative: a re-dump of the shipped Bluetooth pane
loses its 148 comment lines and bakes ``$DATA_ROOT`` in; a patch changes one line.

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
"""

import copy
import importlib
import inspect
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterable,
    List,
    Literal,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

import confluid
import yaml
from confluid import format_yaml_loc, input_specs, marks, output_specs, parse_param_docs, resolve_config_path
from confluid.fluid import Fluid, Reference, ScopeBlock, Target
from confluid.loader import ConfluidLoader
from confluid.registry import resolve_class
from recordstream.algorithm import Algorithm, algorithm_spec
from recordstream.flow.graph import FlowGraph
from recordstream.flow.steps import RESERVED_STEP_KEYS
from recordstream.flow.trace import Tracer
from recordstream.items import get_item_type, item_type_names, item_types
from recordstream.ops.contract import GRAPH_SLOT_KINDS, ChainContractError, ContractError, GraphContract

from streamstudio._yamltext import Edit, YamlText, spell_mapping

if TYPE_CHECKING:  # the annotation only: layout is imported on first use (`_layout`), a separate concern
    from streamstudio.layout import Group

# ================================================================================================
# the vocabulary
# ================================================================================================

#: A socket type: a registered item class name (``Image``, ``Spectrogram``), one of the graph slot
#: kinds (``Record``, ``Stream``, ``Source``, ``Op``, ``Sink``, ``List[str]``), a JSON-schema scalar
#: for a parameter (``number``, ``integer``, ``string``, ``boolean``, ``object``), or the ``*`` wildcard.
SocketType = str

NodeKind = Literal["source", "op", "algorithm", "engine", "sink", "runnable", "value", "contract", "host", "alias"]
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
        outputs: The flow's output step(s), when the document has a ``flow:``.
    """

    nodes: List[Node] = field(default_factory=list)
    wires: List[Wire] = field(default_factory=list)
    problems: List[Problem] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)

    def node(self, name: str) -> Node:
        """The node named ``name``; a miss names the nodes there are."""
        for node in self.nodes:
            if node.name == name:
                return node
        raise KeyError(f"no node {name!r} (the nodes are: {[node.name for node in self.nodes]})")

    def names(self) -> List[str]:
        return [node.name for node in self.nodes]


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
    """The node kind of a class: its confluid category where it has one, its shape otherwise."""
    if cls is GraphContract or issubclass(cls, GraphContract):
        return "contract"
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
    return {"engine": STREAM, "source": SOURCE, "op": OP, "algorithm": OP, "sink": SINK}.get(kind, OBJECT)


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
    instance, error = _build(marker, kwargs)
    declaration = _declaration_of(cls, instance) if kind in ("op", "algorithm") else _NO_DECLARATION
    inputs, socket_names = _slot_sockets(cls, kwargs)
    params = _params(cls, kwargs, socket_names)
    outputs: List[Socket] = []
    note = ""
    if kind == "algorithm":
        entry_inputs, entry_outputs = _algorithm_sockets(cls, kwargs)
        inputs = [Socket("record", RECORD), *inputs, *entry_inputs]
        outputs = [Socket("record", RECORD), Socket("op", OP), *entry_outputs]
    elif kind == "op":
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


class _Builder:
    """Derives a :class:`Model` from a loaded document — one pass, in document order."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.model = Model()
        self.instances: Dict[str, Any] = {}
        self.declarations: Dict[str, _Declaration] = {}
        self.seen: Dict[int, str] = {}
        self.pending: List[Tuple[str, str, Reference, WireKind]] = []
        self.refused: Set[Tuple[str, str]] = set()
        self.flow: Optional[_Flow] = None
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
            self.model, self.instances, self.flow, self.streams, self.contract, self.provided, [self.path.resolve()]
        )

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
        if described.error:
            self.problem(f"{name}: {described.error}", loc)
        if kind == "contract":
            if self.contract is None:
                self.contract = name
            else:
                self.problem(f"{name}: a second graph contract at {loc} — the root is {self.contract}", loc)
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
        self.model.wires.append(Wire(source.name, output.name, target.name, socket, kind, output.type))
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

    def derive_flow(self, flow: Dict[Any, Any], outputs: str) -> None:
        """A ``flow:`` mapping: one node per step; record wires from ``from:`` (or the previous step)
        and ``merge_from:``; bind wires from a step's output; gate wires from ``requires``; entry wires
        along the record lineage."""
        steps: List[_Step] = []
        sound = True
        previous: Optional[str] = None
        for raw_name, value in flow.items():
            name = str(raw_name)
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
                    name, "", "op", {}, [Socket("record", RECORD)], [Socket("record", RECORD)], "", str(self.path)
                )
                node.note = "a fan-in step: it merges records and has no op"
                self.model.nodes.append(node)
                self.instances[name] = None
                self.declarations[name] = _NO_DECLARATION
            known = [step.name for step in steps]
            from_ = reserved.get("from")
            source = str(from_) if from_ is not None else previous
            if from_ is not None and source not in known:
                self.problem(f"{name}: from: {source!r} does not name an earlier step at {node.loc}", node.loc)
                sound, source = False, None
            merge_raw = reserved.get("merge_from") or ()
            merge_from = (str(merge_raw),) if isinstance(merge_raw, str) else tuple(str(m) for m in merge_raw)
            for ref in merge_from:
                if ref not in known:
                    self.problem(f"{name}: merge_from: {ref!r} does not name an earlier step at {node.loc}", node.loc)
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
        self.flow = _Flow(steps, outputs or (steps[-1].name if steps else ""), sound)
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

    def bind_wire(self, node: Node, param: str, ref: str, known: Sequence[str]) -> bool:
        """``bind: {param: step.attr | step[key] | step}`` → a wire from that step's output into the parameter."""
        step, attr, key = _split_bind(ref)
        if step not in known:
            self.problem(f"{node.name}.{param}: bind {ref!r} names no earlier step at {node.loc}", node.loc)
            return False
        producer = self.model.node(step)
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
# loading: the text, with the real filename, includes anchored to the document's directory
# ================================================================================================


def _anchor_includes(data: Any, base_dir: Path) -> None:  # the raw parse: dicts, lists, markers
    """Rewrite every relative ``include:`` entry to the file found beside the document.

    The text is parsed in memory, so confluid would otherwise resolve a relative include against
    the process's working directory (``load()`` anchors text at ``cwd/string.yaml``); the document
    lives in a directory of its own, and an include is written relative to THAT.
    """
    if isinstance(data, list):
        for item in data:
            _anchor_includes(item, base_dir)
    elif isinstance(data, Fluid):
        _anchor_includes(data.kwargs, base_dir)
    elif isinstance(data, ScopeBlock):
        _anchor_includes(data.contents, base_dir)
    elif isinstance(data, dict):
        for value in data.values():
            _anchor_includes(value, base_dir)
        entries = data.get("include")
        if isinstance(entries, str):
            data["include"] = _anchor_entry(entries, base_dir)
        elif isinstance(entries, list):
            data["include"] = [_anchor_entry(entry, base_dir) for entry in entries]


def _anchor_entry(entry: Any, base_dir: Path) -> Any:  # one include entry: a path string, or malformed
    if not isinstance(entry, str) or not entry.strip():
        return entry
    found = resolve_config_path(entry, base_dir=base_dir)
    return str(found) if found.exists() else entry


def _load_document(path: Path, texts: Mapping[Path, str], *, build: bool = False) -> Tuple[Any, List[Path]]:
    """``confluid.load(until="document")`` of the IN-MEMORY texts: the main text parsed with the
    file's real name (so every marker's location names the file), an included file's pending text
    staged on disk for the duration of the load and restored right after — nothing this returns
    depends on what a ``save()`` has not yet written.

    ``build=True`` runs the load to the OBJECTS stage instead (without ``solidify``: constructors do
    no work, and the expensive finish is not what is being checked) — the stage every consumer loads
    to, and the only one where a top-level key is handed to a same-named constructor parameter."""
    main = path.resolve()
    stream = io.StringIO(texts[main])
    stream.name = str(path)
    raw = yaml.load(stream, Loader=ConfluidLoader)
    _anchor_includes(raw, path.parent)
    staged = {p: t for p, t in texts.items() if p != main and (not p.exists() or p.read_text() != t)}
    originals = {p: (p.read_text() if p.exists() else None) for p in staged}
    try:
        for p, text in staged.items():
            p.write_text(text)
        if build:
            doc, paths = confluid.load(raw, until="objects", solidify=False, return_paths=True)
        else:
            doc, paths = confluid.load(raw, until="document", return_paths=True)
    finally:
        for p, original in originals.items():
            if original is None:
                p.unlink(missing_ok=True)
            else:
                p.write_text(original)
    return doc, [Path(p).resolve() for p in paths]


def _load_problem(path: Path, exc: BaseException, verb: str = "does not load") -> Problem:
    """A document that does not load (or does not build) is ONE located problem, never an exception."""
    loc = str(path)
    mark = getattr(exc, "problem_mark", None)
    if mark is not None:
        loc = f"{path}:{mark.line + 1}:{mark.column + 1}"
    else:
        located = re.search(r" at (\S+:\d+:\d+)", str(exc))
        if located:
            loc = located.group(1)
    message = " ".join(str(exc).split())
    return Problem(f"{path}: {verb}: {type(exc).__name__}: {message}", loc)


def derive(path: Path, texts: Mapping[Path, str]) -> _Derivation:
    """The model of the document at ``path`` as the texts spell it — never raises."""
    try:
        doc, included = _load_document(path, texts)
    except Exception as exc:  # noqa: BLE001 — every failure to load is reported as a problem
        return _Derivation(Model(problems=[_load_problem(path, exc)]), {}, None, [], None, (), [path.resolve()])
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
            derivation.model.problems.append(_load_problem(path, exc, "does not build"))
    return derivation._replace(files=files)


# ================================================================================================
# the check
# ================================================================================================


def check(derivation: _Derivation, path: Path) -> List[Problem]:
    """Every problem of a derived document: what deriving found, the dangling required inputs, the
    contract's declaration, and recordstream's own chain check — never raises."""
    model = derivation.model
    problems = list(model.problems)
    problems.extend(_dangling(model))
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
        node_name = prefix + str(tracer.report[-1]["node"]) if tracer.report else ""
        loc = model.node(node_name).loc if node_name in model.names() else ""
        return [Problem(str(refusal), loc)]
    except Exception as exc:  # noqa: BLE001 — the engine refused the graph for another reason: still a problem
        return [Problem(f"{where}: {type(exc).__name__}: {exc}", "")]
    return []


# ================================================================================================
# the document
# ================================================================================================

_Texts = Dict[Path, str]


class _Entry(NamedTuple):
    """One step of the document's history: the texts before an edit, and the rename it made."""

    texts: _Texts
    rename: Optional[Tuple[str, str]]


class Document:
    """A confluid YAML document on disk, its derived model, and the edits that patch it in place.

    Args:
        path: The document's file.
        texts: The text of every file the document spans, by resolved path (the main file at least).
        history_depth: How many edits ``undo`` can walk back.
    """

    def __init__(self, path: Path, texts: _Texts, *, history_depth: int = 50) -> None:
        # Stores only: the model is derived on first use and cached per text.
        self.path = Path(path)
        self.history_depth = int(history_depth)
        self._texts: _Texts = dict(texts)
        self._saved: _Texts = dict(texts)
        # Each entry is the texts BEFORE an edit and the rename that edit made (``None`` for any other
        # edit): positions and groups are the page's state, not the document's, so undoing a rename
        # re-keys the CURRENT ones instead of restoring old ones — a node moved after the rename keeps
        # its place (measured before: undo restored the text only and left them under the new name).
        self._history: List[_Entry] = []
        self._redo: List[_Entry] = []
        self._cache: Optional[Tuple[Tuple[Tuple[Path, str], ...], _Derivation, Optional[List[Problem]]]] = None
        self._positions: Optional[Dict[str, Tuple[float, float]]] = None
        self._groups: Optional[List["Group"]] = None

    @classmethod
    def open(cls, path: Union[str, Path], *, history_depth: int = 50) -> "Document":
        """Read the file's text; parse nothing."""
        given = Path(path)
        return cls(given, {given.resolve(): given.read_text()}, history_depth=history_depth)

    # -- reading -----------------------------------------------------------------------------

    @property
    def text(self) -> str:
        """The main file's text as the document currently holds it (saved or not)."""
        return self._texts[self.path.resolve()]

    @property
    def files(self) -> List[Path]:
        """The main file and every file an ``include:`` brought, resolved."""
        return list(self._derivation().files)

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

    def add_step(self, name: str, after: str, class_path: str, params: Dict[str, Any]) -> None:
        """Insert a flow step after ``after``; the step that read ``after`` now reads the new one, and the
        flow's output moves to it when ``after`` was the tail."""
        derivation = self._derivation()
        model = derivation.model
        if name in model.names():
            raise ValueError(f"{name!r} is already a node (the nodes are: {model.names()})")
        anchor = model.node(after)
        self._flow_step_in_main(anchor, derivation)
        text = YamlText(self.text)
        flow_node = text.path("flow")
        assert isinstance(flow_node, yaml.MappingNode)
        found = text.pair(flow_node, after)
        assert found is not None
        key_node, value_node = found
        indent = " " * key_node.start_mark.column
        inner = indent + "  "
        block = [f"{indent}{name}:", f"{inner}op: !class:{class_path} {spell_mapping(params)}", f"{inner}from: {after}"]
        edits: List[Edit] = [text.insert_lines_edit(text.pair_stop_line(key_node, value_node), block)]
        steps = text.keys(flow_node)
        position = steps.index(after)
        if position + 1 < len(steps):
            next_value = text.pair(flow_node, steps[position + 1])
            assert next_value is not None
            reads = text.pair(next_value[1], "from") if isinstance(next_value[1], yaml.MappingNode) else None
            if reads is not None and isinstance(reads[1], yaml.ScalarNode) and reads[1].value == after:
                edits.append(text.replace_scalar_edit(reads[1], name))
        else:
            edits.extend(self._outputs_edit(text, after, name))
        self._commit({self.path.resolve(): text.apply(edits)}, f"add_step {name!r} after {after!r}", anchor.loc)

    def remove_step(self, name: str) -> None:
        """Remove a flow step; what read it now reads what it read from."""
        derivation = self._derivation()
        model = derivation.model
        node = model.node(name)
        step = self._flow_step_in_main(node, derivation)
        text = YamlText(self.text)
        flow_node = text.path("flow")
        assert isinstance(flow_node, yaml.MappingNode)
        found = text.pair(flow_node, name)
        assert found is not None
        edits: List[Edit] = [text.remove_pair_edit(*found)]
        upstream = step.source
        for other in text.keys(flow_node):
            if other == name:
                continue
            value = text.pair(flow_node, other)
            assert value is not None
            if not isinstance(value[1], yaml.MappingNode):
                continue
            reads = text.pair(value[1], "from")
            if reads is not None and isinstance(reads[1], yaml.ScalarNode) and reads[1].value == name:
                if upstream is not None:
                    edits.append(text.replace_scalar_edit(reads[1], upstream))
                else:
                    edits.append(text.remove_pair_edit(*reads))
        assert derivation.flow is not None
        if derivation.flow.outputs == name:
            if upstream is None:
                raise ValueError(f"{name!r} is the only step of the flow — nothing would be left to output")
            edits.extend(self._outputs_edit(text, name, upstream))
        self._commit({self.path.resolve(): text.apply(edits)}, f"remove_step {name!r}", node.loc)

    def rename_node(self, old: str, new: str) -> None:
        """Rename a node in ONE operation: its key, every ``from:`` / ``merge_from:`` / ``bind:`` /
        ``!ref:`` / ``outputs:`` that names it, its sidecar entry and every group that lists it."""
        model = self.model
        node = model.node(old)
        if new in model.names():
            raise ValueError(f"{new!r} is already a node (the nodes are: {model.names()})")
        if node.kind == "host":
            raise ValueError(f"{old} is the host's side of the graph, not a node of the document")
        if "[" in old:
            raise ValueError(f"{old} is a list member, named by its position — move it instead")
        if "." in old:
            raise ValueError(f"{old} is nested and named by the slot it fills — rename the slot's owner instead")
        if node.source_file and Path(node.source_file).resolve() != self.path.resolve():
            raise ValueError(f"{old} comes from {node.source_file}; rename it in that file")
        renamed = YamlText(self.text).rename(old, new)
        self._commit({self.path.resolve(): renamed}, f"rename {old!r} to {new!r}", node.loc, rename=(old, new))
        self._rekey(old, new)

    def undo(self) -> bool:
        """Step back one edit; ``False`` when there is none. Undoing a rename renames the node's
        position and its group memberships back."""
        if not self._history:
            return False
        entry = self._history.pop()
        self._redo.append(_Entry(dict(self._texts), entry.rename))
        self._texts = entry.texts
        if entry.rename is not None:
            old, new = entry.rename
            self._rekey(new, old)
        return True

    def redo(self) -> bool:
        """Step forward one undone edit; ``False`` when there is none."""
        if not self._redo:
            return False
        entry = self._redo.pop()
        self._history.append(_Entry(dict(self._texts), entry.rename))
        self._texts = entry.texts
        if entry.rename is not None:
            old, new = entry.rename
            self._rekey(old, new)
        return True

    def save(self) -> None:
        """Write every file whose text changed, and the sidecar when positions or groups were set.

        The sidecar is written WHOLE, so each block keeps what the other one did not set: positions set
        here are every node's (the laid-out ones frozen with the moved ones, as before groups existed),
        positions NOT set here are what the file already held — a grouping never freezes a layout nobody
        arranged — and the groups are this document's, or the file's when none were set.
        """
        for key, text in self._texts.items():
            if self._saved.get(key) != text:
                key.write_text(text)
                self._saved[key] = text
        if self._positions is not None or self._groups is not None:
            layout = _layout()
            kept = self.positions if self._positions is not None else layout.read_sidecar(self.sidecar_path)
            layout.write_sidecar(self.sidecar_path, kept, groups=self.groups)

    # -- positions ---------------------------------------------------------------------------

    @property
    def positions(self) -> Dict[str, Tuple[float, float]]:
        """Every node's position: a name in the sidecar keeps its place, an unknown one is laid out;
        positions set on this document (not yet saved) win."""
        laid = dict(_layout().positions(self.model, self.sidecar_path))
        if self._positions:
            for name, xy in self._positions.items():
                if name in laid:
                    laid[name] = (float(xy[0]), float(xy[1]))
        return laid

    def set_positions(self, mapping: Mapping[str, Tuple[float, float]]) -> None:
        self._positions = {str(name): (float(xy[0]), float(xy[1])) for name, xy in mapping.items()}

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

    # -- internals ---------------------------------------------------------------------------

    def _text_of(self, key: Path) -> str:
        if key not in self._texts:
            text = key.read_text()
            self._texts[key] = text
            self._saved[key] = text
        return self._texts[key]

    def _flow_step_in_main(self, node: Node, derivation: _Derivation) -> _Step:
        steps = [step.name for step in derivation.flow.steps] if derivation.flow is not None else []
        if node.name not in steps:
            raise ValueError(f"{node.name!r} is not a flow step (the steps are: {steps})")
        assert derivation.flow is not None
        if node.source_file and Path(node.source_file).resolve() != self.path.resolve():
            raise ValueError(f"{node.name!r} comes from {node.source_file}; edit the steps of that file there")
        return next(step for step in derivation.flow.steps if step.name == node.name)

    @staticmethod
    def _outputs_edit(text: YamlText, old: str, new: str) -> List[Edit]:
        root = text.root
        if isinstance(root, yaml.MappingNode):
            outputs = text.pair(root, "outputs")
            if outputs is not None and isinstance(outputs[1], yaml.ScalarNode) and outputs[1].value == old:
                return [text.replace_scalar_edit(outputs[1], new)]
        return []

    def _rekey(self, old: str, new: str) -> None:
        """Move a node's position and group memberships from ``old`` to ``new`` — in memory, written by
        :meth:`save`; the sidecar is read first when nothing was set here yet."""
        if self._positions is None and self.sidecar_path.exists():
            self._positions = dict(_layout().read_sidecar(self.sidecar_path))
        if self._positions is not None and old in self._positions:
            self._positions[new] = self._positions.pop(old)
        if self._groups is None and self.sidecar_path.exists():
            self._groups = list(_layout().read_groups(self.sidecar_path))
        if self._groups:
            self._groups = _layout().rename_member(self._groups, old, new)

    def _commit(self, changes: _Texts, what: str, loc: str, *, rename: Optional[Tuple[str, str]] = None) -> None:
        """Validate a candidate text, then make it the document's — or refuse it, unwritten."""
        before = {problem.message for problem in self.check()}
        candidate: _Texts = {**self._texts, **changes}
        derivation = derive(self.path, candidate)
        after = check(derivation, self.path)
        gained = [problem for problem in after if problem.message not in before]
        if gained:
            where = f" at {loc}" if loc else ""
            raise ValueError(f"{what} refused{where}: " + "; ".join(problem.message for problem in gained))
        self._history.append(_Entry(dict(self._texts), rename))
        del self._history[: -self.history_depth]
        self._redo.clear()
        self._texts = candidate
        self._cache = (self._key(), derivation, after)


def _layout() -> ModuleType:
    """The layout module, imported on first use (positions are a separate concern with its own tests)."""
    return importlib.import_module("streamstudio.layout")


__all__ = [
    "ANY",
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
    "SINK",
    "SOURCE",
    "STREAM",
    "Socket",
    "SocketType",
    "Wire",
    "WireKind",
    "check",
    "derive",
]
