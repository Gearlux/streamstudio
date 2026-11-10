"""The confluid document is the only truth: a canvas MODEL derived from it, written back by
patching the line a marker came from.

The editor never keeps a second document (``docs/rewrite.md`` §2.1). :class:`Document` reads
a YAML text, derives a :class:`Model` — nodes with typed sockets, wires, located problems —
from ``confluid.load(path, until="document", texts=…)`` — the file, as the texts it holds spell
it (markers kept, ``!ref`` kept, every marker stamped ``file:line:col``) — and every edit
(``set_param``, ``add_step``, ``add_node``, ``remove_step``, ``rename_node``, ``add_wire``, ``remove_wires``)
patches the TEXT through :mod:`streamstudio._yamltext`, re-derives, and is refused before anything is written when the
document no longer loads or :meth:`Document.check` gains a problem. Measured against the
alternative: a re-dump of the shipped Bluetooth pane loses its 148 comment lines and bakes
``$DATA_ROOT`` in; a patch changes one line.

Three things the model is built from, and nothing else:

* **the declarations** — ``recordstream.algorithm_spec`` for an ``Algorithm`` (its ``Input`` /
  ``Output`` slots are the node's entry sockets, typed by the item class), an op's own
  ``consumes`` / ``produces`` / ``flags`` / ``reports`` for anything else, a source's ``produces`` for
  its record-entry outputs, ``confluid.input_specs`` / ``output_specs`` / ``to_pydantic`` for
  constructor parameters and ``@output`` properties, ``GraphContract.inputs`` / ``outputs`` /
  ``records`` for a graph's boundary;
* **the item registry** — ``recordstream.items`` names every socket type an entry can have, and
  ``issubclass`` between two registered items is the one subtyping rule a wire may lean on;
* **the document's own shape** — a nested marker is a node wired into its parent's slot, a list
  member is ``slot[i]``, a ``!ref:`` is a wire, a root's ``delivered: {input: image}`` is a wire from
  the card writing ``image`` into the stream it delivers, a ``flow:`` step's ``from:`` /
  ``merge_from:`` / ``bind:`` / ``requires`` are record, bind and gate wires.

Node names follow ``recordstream.flow.Tracer``: a flow step by its key, a list member by
``path[i]`` (``stream.ops[1]``), a nested marker by its document path (``processor.stream``),
the contract root by its key, and the host's side as ONE synthetic node ``<host>`` — for a graph
WITHOUT a contract, whatever its steps read that nothing produces; for a ROOTED graph, what its
contract's ``inputs`` declare (``files · List[str]``; an item-typed input is an entry of the record
the host hands in), and nothing more: an entry nothing produces is then refused by recordstream's own
chain check (``Tracer.check`` → ``check_chain``) in its own words. Where the host PUTS an input when it
runs the graph — the parameter of that name on exactly one card of the delivered stream's source chain
(a ``FilesSource``'s ``files``) — is a derived ``host`` wire: drawn, never written, never removed.

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
import functools
import importlib
import importlib.metadata
import inspect
import os
import re
import sys
import threading
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import ModuleType
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
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
    TypeVar,
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
from loggair import get_logger
from recordstream.algorithm import Algorithm, algorithm_spec
from recordstream.flow.graph import FlowGraph
from recordstream.flow.steps import RESERVED_STEP_KEYS, SUBGRAPH_SEPARATOR
from recordstream.flow.trace import Tracer
from recordstream.items import get_item_type, item_type_names, item_types
from recordstream.ops.contract import GRAPH_SLOT_KINDS, ChainContractError, ContractError, GraphContract

from streamstudio._yamltext import Edit, YamlText, dedent, indent_lines, is_subgraph_marker, spell, spell_mapping
from streamstudio.settings import (
    LOCKED,
    SETTINGS_ONLY,
    Declared,
    GraphMode,
    SettingNode,
    composed,
    declared_settings,
    graph_mode,
    graph_name,
    read_settings,
    set_setting,
)
from streamstudio.settings import settings_path as settings_path_of

if TYPE_CHECKING:  # the annotation only: layout is imported on first use (`_layout`), a separate concern
    from recordstream.flow.subgraph import Steps, Subgraph

    from streamstudio.layout import Group

logger = get_logger(__name__)

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
#: ``host`` is the one wire no text spells: where the host puts what it hands a rooted graph
#: (:meth:`_Builder.host_wires`).
WireKind = Literal["record", "entry", "gate", "bind", "ref", "delivered", "list", "host"]

RECORD: SocketType = "Record"
STREAM: SocketType = "Stream"
SOURCE: SocketType = "Source"
OP: SocketType = "Op"
SINK: SocketType = "Sink"
LIST_STR: SocketType = "List[str]"
OBJECT: SocketType = "object"
ANY: SocketType = "*"

#: The synthetic node standing for what the host provides: a contract-less graph's unproduced reads, a rooted graph's
#: declared ``inputs``. Never a node of the document — no parameters, never renamed, removed or written.
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


#: The scalar a value node's class may BE, in the order they are tried — ``bool`` before ``int``: a bool IS an int.
_BUILT_SCALARS: Tuple[Tuple[type, SocketType], ...] = (
    (bool, "boolean"),
    (int, "integer"),
    (float, "number"),
    (str, "string"),
)


def _socket_of_built(cls: type) -> SocketType:
    """What a value node's ``object`` output carries: the scalar its class IS — a ``float`` subclass (a number written
    with its unit, ``recordstream.quantity.Quantity``) is a ``number``, and meets a number setting — else ``object``
    (user pick 2026-10-09: any node whose result is a number feeds a number setting). The palette's
    ``_built_socket`` is the same rule."""
    return next((word for scalar, word in _BUILT_SCALARS if issubclass(cls, scalar)), OBJECT)


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

#: The entry-point group ``confluid.load_configurables`` reads (its default): every package's
#: ``@configurable``-bearing modules.
_CONFIGURABLES = "confluid.configurables"

#: The ``_CONFIGURABLES`` entry points no short-name lookup has imported yet, in their listed order;
#: ``None`` until the first miss reads the list (once per process). A lookup takes each one off the
#: list as it imports it, so no entry point is imported twice and a later miss starts where the
#: last one stopped.
_pending_entry_points: Optional[List[importlib.metadata.EntryPoint]] = None
_entry_points_lock = threading.Lock()


def _resolve(target: Any) -> Optional[type]:  # a class object, a registry key or a dotted path
    """The class a marker's target names, or ``None``.

    A registry key (``Stream``) resolves only once the module defining it has been imported; on a
    miss :func:`_import_until` imports the installed entry points one at a time until it does. A
    dotted path is imported by ``resolve_class`` itself and walks no entry point.
    """
    if isinstance(target, type):
        return target
    if not isinstance(target, str):
        return None
    found = _registered(target)
    if found is None and "." not in target:
        found = _import_until(target)
    return found if isinstance(found, type) else None


def _import_until(name: str) -> Any:
    """Import the ``confluid.configurables`` entry points ONE AT A TIME until ``name`` resolves.

    Measured 2026-10-02: importing all of them on the first miss (``confluid.load_configurables``)
    made the first graph list of an editor mounted in a fresh host app take 13.9 s (6.0 s on a warm
    disk cache) — for one key, ``AnnotationUpdateSink``, whose module (a domain package's ``sinks``) was not
    imported yet; the other entry points brought torch, transformers and the detection backends with
    them. This walk finds it after 15 of the 127 and the list takes 1.1 s. So:

    * the entry points of a package that is imported already go first (its dependencies are loaded,
      so they are cheap — and the key usually lives there), then the rest; within each, the entry points whose
      module's own name shares a word with the name go first, the most words first (``ExportMask`` →
      ``….export_mask``, words compared by their stem), then the listed order (:func:`_shared_words`).
      Measured 2026-11-10: the 48 graphs of a host's workspace type named ``ExportMask``,
      ``MeasureRecordLevels`` and ``GeneratedSignals``, and in listed order the walk imported eight other
      packages of the same imported top-level package first — 15.2 s and 8 600 modules to derive them all,
      1.5 s and 140 modules with the ranking;
    * each is loaded with ``EntryPoint.load()``, also when its module is in ``sys.modules`` already:
      that costs nothing for a module that is imported, and it WAITS for one another thread is still
      importing — Python lists a module in ``sys.modules`` before its body runs, and the catalogue
      (``palette``) imports every entry point in its own request thread. Skipping such a module
      answered "cannot resolve" for a class half a second from being registered;
    * the walk stops at the first entry point after which ``name`` resolves;
    * a failing entry point is skipped with the warning line ``confluid.load_configurables`` writes
      for it, and is not tried again.

    A name no entry point defines ends with every entry point imported, once per process; a later
    miss then costs nothing. The walk holds a lock, and a request that waited for it looks the name
    up again before walking on: the walk it waited for may have registered it (and taken the defining
    entry point off the list), so walking on would answer "cannot resolve", or import an entry point
    nobody needs.

    Known limit: a name two entry points define (``FourierOp``: a package's ``fourier`` and
    ``fourier_torch`` modules) resolves to the first one the walk imports, because the second is
    not imported yet; once the catalogue has imported everything the name is ambiguous and resolves
    to nothing, which is what a run (``strict``) refuses.

    Returns:
        Whatever ``resolve_class`` answers for ``name`` once found (a class, or a function a key
        names), else ``None``.
    """
    global _pending_entry_points
    with _entry_points_lock:
        found = _registered(name)
        if found is not None:
            return found
        if _pending_entry_points is None:
            _pending_entry_points = list(importlib.metadata.entry_points(group=_CONFIGURABLES))
        pending = _pending_entry_points
        imported = [ep for ep in pending if ep.module.split(".")[0] in sys.modules]
        others = [ep for ep in pending if ep.module.split(".")[0] not in sys.modules]
        order = [ep for tier in (imported, others) for ep in sorted(tier, key=lambda ep: -_shared_words(name, ep))]
        for ep in order:
            pending.remove(ep)
            try:
                ep.load()
            except Exception as exc:  # broad on purpose, as confluid.load_configurables: one broken package
                logger.warning(f"load_configurables: entry point '{ep.name}' ({ep.value}) failed to import: {exc}")
                continue
            found = _registered(name)
            if found is not None:
                return found
    return None


def _shared_words(name: str, entry_point: importlib.metadata.EntryPoint) -> int:
    """How many words of the class name ``name`` (``MeasureRecordLevels`` → measure, record, levels) the entry point's
    module's own name holds (``package.record_levels`` → record, levels: 2), each compared by its stem
    (``GeneratedSignals`` shares ``generat`` with ``package.generate``) — the walk's hint where a name lives, never a
    rule: a module named otherwise is still reached in its turn."""
    words = {_stem(word) for word in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+", name)}
    return len(words & {_stem(word) for word in entry_point.module.rpartition(".")[2].split("_")})


def _stem(word: str) -> str:
    """``word`` in lower case without one ending ``-ing``, ``-ed``, ``-es``, ``-s`` or ``-e`` (files, file → fil)."""
    word = word.lower()
    for ending in ("ing", "ed", "es", "s", "e"):
        if word.endswith(ending) and len(word) > len(ending) + 2:
            return word[: -len(ending)]
    return word


def _registered(name: str) -> Any:
    """``resolve_class(name)``, with a lookup that raises (a malformed selector, an ambiguous name) as ``None``."""
    try:
        return resolve_class(name)
    except Exception:
        return None


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
        outputs = [Socket("stream", SOURCE), *_entry_outputs(cls, instance), *_output_sockets(cls)]
    elif kind == "engine":
        outputs = [Socket("stream", STREAM), *_output_sockets(cls)]
    elif kind == "sink":
        outputs = [Socket("sink", SINK), *_output_sockets(cls)]
    elif kind == "runnable":
        outputs = [Socket("runnable", OBJECT), *_output_sockets(cls)]
    else:  # value
        outputs = [*_output_sockets(cls), Socket("object", _socket_of_built(cls))]
    return _Description(params, inputs, outputs, note, instance, declaration, error)


def _entry_outputs(cls: type, instance: Any) -> List[Socket]:  # instance: the built source of any class, or None
    """A source's record entries as OUTPUT sockets — its ``produces`` (``{"image": "Image", "class": "Label"}``), the
    declaration an op makes, read off the BUILT source when there is one: a source may answer it from its settings
    (a HuggingFace source lists the metadata columns it keeps), so the outputs follow a setting's edit. Each carries
    its record key as ``entry``, which is what a wire from it into a root's record entry is saved as."""
    produces = _declared(instance, cls, "produces")
    if not isinstance(produces, dict):
        return []
    return [Socket(str(key), _entry_type(str(kind)), entry=str(key)) for key, kind in produces.items()]


#: The doc of a parameter that stands for a value written straight under a root's ``delivered:`` — a typed list in an
#: output slot (``classes: [cat, dog]``), the key a record entry is read from (``input: image``).
DELIVERED_LITERALLY = "delivered literally"


def _describe_contract(kwargs: Dict[str, Any]) -> _Description:
    """A ``GraphContract``: one INPUT per declared output slot (required), then one per ``records`` entry (an ENTRY
    socket, never required: an entry nothing is wired into is read under its own name), then one per ``optional`` slot
    (never required: the host has its own answer for a slot left unwired — wired, it is saved and checked like an
    output). NO output: what the contract's ``inputs`` declare is the host card's (:meth:`_Builder.root_host`), so the
    root draws only what is wired INTO it.

    A slot delivered by a LITERAL (``classes: [cat, dog]``, ``input: image``) is recorded as a spelled parameter of
    the slot's name, so the dangling check sees it filled — a record entry's key is drawn as a wire as well, from the
    card that writes it (:meth:`_Builder.record_entry_wires`); a slot delivered by a marker or a reference becomes a
    wire.
    """
    raw_outputs, raw_optional, raw_delivered = kwargs.get("outputs"), kwargs.get("optional"), kwargs.get("delivered")
    raw_records = kwargs.get("records")
    declared_outputs: Dict[str, Any] = raw_outputs if isinstance(raw_outputs, dict) else {}
    declared_optional: Dict[str, Any] = raw_optional if isinstance(raw_optional, dict) else {}
    declared_records: Dict[str, Any] = raw_records if isinstance(raw_records, dict) else {}
    inputs = [Socket(str(slot), _slot_kind(kind), required=True) for slot, kind in declared_outputs.items()]
    inputs += [Socket(str(entry), _entry_type(str(kind)), entry=str(entry)) for entry, kind in declared_records.items()]
    inputs += [Socket(str(slot), _slot_kind(kind)) for slot, kind in declared_optional.items()]
    params = _params(GraphContract, kwargs, {"delivered"})
    delivered: Dict[str, Any] = raw_delivered if isinstance(raw_delivered, dict) else {}
    for slot, value in delivered.items():
        if value is not None and not _holds_fluid(value):
            kind = declared_outputs.get(slot, declared_records.get(slot, declared_optional.get(slot, ANY)))
            params[str(slot)] = Param(str(slot), _slot_kind(kind), value, None, DELIVERED_LITERALLY, {}, True)
    return _Description(params, inputs, [], "", None, _NO_DECLARATION, None)


def _contract_slots(root: Node, declaration: str) -> Dict[str, str]:
    """``{slot: kind}`` a root's ``declaration`` (``inputs``, ``outputs``, ``optional``, ``records``) spells —
    ``{}`` when it spells none, or something that is not a mapping (the constructor's refusal says so)."""
    found = root.params.get(declaration)
    value = found.value if found is not None else None
    return {str(slot): str(kind) for slot, kind in value.items()} if isinstance(value, dict) else {}


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
        self.record_entry_wires()
        self.host_wires()
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
                self.root_host(node)
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
        """The output a bare reference to ``node`` means: the first that meets the target, else its object. A record
        ENTRY output (``image`` of a source) is never one: a ``!ref:`` hands over the object, not an entry of each of
        its records."""
        accepted = tuple(accepted)
        for output in node.outputs:
            if not output.entry and _meets_any(output.type, accepted):
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

    def record_entry_wires(self) -> None:
        """A root's record entry read from a KEY (``delivered: {input: image}``) is drawn as a wire from the card that
        writes that key into the stream the root delivers (:func:`_record_writer`) — after every reference is wired,
        since the delivered stream and its source are references. A key no card declares draws no wire; the entry's
        socket is filled by the spelled parameter (:func:`_describe_contract`)."""
        if self.contract is None:
            return
        root = self.model.node(self.contract)
        for socket in root.inputs:
            param = root.params.get(socket.name)
            if (
                not socket.entry
                or param is None
                or param.doc != DELIVERED_LITERALLY
                or not isinstance(param.value, str)
            ):
                continue
            found = _record_writer(self.model, root.name, param.value)
            if found is not None:
                self.wire(found[0], found[1], root, socket.name, "delivered", (socket.type,))

    def root_host(self, root: Node) -> None:
        """The HOST card of a rooted graph: one output per slot the contract's ``inputs`` declare — what the host hands
        the graph when it runs it (``files · List[str]``). An item-typed slot (a pane's ``input · Signal``) is an ENTRY
        of the record the host hands in, which a step reads by name (:meth:`entry_wires`). No card when the contract
        declares no input, or inside a subgraph (whose ``<in>`` is its host side).

        It replaces the root's own outputs (before 2026-10-05 the root drew the inputs as outputs, and the wires a step
        read from them left the root): the root now draws only what is wired INTO it, and the host's side is one card,
        as for a graph without a root."""
        if self.prefix or self.host is not None:
            return
        declared = _contract_slots(root, "inputs")
        if not declared:
            return
        names = item_type_names()
        outputs = [
            Socket(slot, kind, entry=slot) if kind in names else Socket(slot, _slot_kind(kind))
            for slot, kind in declared.items()
        ]
        self.host = Node(HOST, "", "host", {}, [], outputs, "", str(self.path))
        self.host.note = f"the inputs {root.name} declares — what the host hands the graph when it runs it"

    def host_wires(self) -> None:
        """Where the host PUTS what it hands in: for each output of the host card that names a constructor PARAMETER of
        exactly one card of the delivered stream's source chain (:func:`_source_chain`), a ``host`` wire into that
        parameter — the rule a host fills a graph's ``files`` by: an object in that chain with a ``files`` it can set.
        None or several such cards draw no wire, and neither does a parameter whose type does not take the slot's.

        Derived from the document's shape and never written: no text spells it, so it can be neither saved nor
        removed (:data:`WIRE_FROM_THE_HOST`). A parameter the card does not show as a socket or a setting gets an input
        socket of its own, as :meth:`wire_into` gives an undeclared slot."""
        if self.contract is None or self.host is None:
            return
        _slot, stream = _delivered_stream(self.model, self.contract)
        if stream is None:
            return
        chain = _source_chain(self.model, stream)
        for output in self.host.outputs:
            takers = [card for card in chain if output.name in _parameter_names(card)]
            if len(takers) != 1:
                continue
            card = takers[0]
            if card.input(output.name) is None and output.name not in card.params:
                card.inputs.append(Socket(output.name, output.type))
            if _meets_any(output.type, self.accepted_types(card, output.name)):
                self.model.wires.append(Wire(HOST, output.name, card.name, output.name, "host", output.type))

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
                # a rooted graph: the host card hands in what the contract declares (root_host), and nothing more
                declared = self.host.output(socket.entry) if self.host is not None else None
                if self.host is not None and declared is not None:
                    self.wire(self.host, declared, node, socket.name, "entry", (socket.type,))
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
            self.provided = tuple(_contract_slots(self.model.node(self.contract), "inputs"))
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


def _delivered_stream(model: Model, root: str) -> Tuple[Optional[str], Optional[str]]:
    """``(the root's Stream output slot, the card wired into it)`` — ``(None, None)`` when the root declares no
    ``Stream`` output, ``(slot, None)`` when nothing is wired into it yet."""
    node = model.node(root)
    slot = next((s.name for s in node.inputs if not s.entry and s.type == STREAM), None)
    if slot is None:
        return None, None
    wired = next((w.source for w in model.wires if w.target == root and w.target_socket == slot), None)
    return slot, wired


def _source_chain(model: Model, card: str) -> List[Node]:
    """The card ``card`` (the stream a root delivers) and the cards its records come from: the card wired into its
    ``source`` slot, that card's own ``source``, … — where a host looks for the object to fill (``files``)."""
    chain: List[Node] = []
    name: Optional[str] = card
    while name is not None and name not in [node.name for node in chain]:
        chain.append(model.node(name))
        name = next((w.source for w in model.wires if w.target == name and w.target_socket == "source"), None)
    return chain


def _parameter_names(node: Node) -> Set[str]:
    """The constructor parameter names of a card's class — ``set()`` for a card without one (a synthetic card, a class
    that does not resolve)."""
    cls = _resolve(node.class_path) if node.class_path else None
    return {parameter.name for parameter in _signature(cls)} if cls is not None else set()


def _stream_cards(model: Model, stream: str) -> List[str]:
    """The cards of a Stream in the order they write into its records: its source, then its ``ops`` members."""
    source = [w.source for w in model.wires if w.target == stream and w.target_socket == "source"]
    members: List[Tuple[int, str]] = []
    for wire in model.wires:
        found = re.fullmatch(r"ops\[(\d+)\]", wire.target_socket) if wire.target == stream else None
        if found is not None and wire.kind == "list":
            members.append((int(found.group(1)), wire.source))
    return source[:1] + [name for _index, name in sorted(members)]


def _record_writer(model: Model, root: str, key: str) -> Optional[Tuple[Node, Socket]]:
    """The card — and its output — that writes the record key ``key`` into the stream ``root`` delivers: the LAST
    member of its ``ops`` that produces ``key``, else its source when that produces it — what a record entry read
    from ``key`` (``delivered: {input: key}``) is drawn from. ``None`` when no card declares it."""
    _slot, stream = _delivered_stream(model, root)
    if stream is None:
        return None
    for name in reversed(_stream_cards(model, stream)):
        node = model.node(name)
        output = next((s for s in node.outputs if s.entry == key), None)
        if output is not None:
            return node, output
    return None


def _head(name: str) -> str:
    """The top-level key a node name starts with — ``stream`` for ``stream.source`` and ``stream.ops[1]``."""
    return re.split(r"[.\[/]", name, maxsplit=1)[0]


def _wire_ends(model: Model) -> Set[Tuple[str, str, str, str]]:
    """Every wire of every level as its four ends."""
    return {(w.source, w.source_socket, w.target, w.target_socket) for level in model.levels() for w in level.wires}


def _is_entry_input(model: Model, node: str, socket: str) -> bool:
    """Whether ``node.socket`` is an entry input — a root's record entry, a step's read of the record."""
    holder = model.holder(node)
    found = model.node(node).input(socket) if holder is not None else None
    return found is not None and bool(found.entry)


def _drawn(wanted: Tuple[str, str, str, str]) -> Callable[["_Derivation"], None]:
    """A check of a wire edit's candidate: refused unless the wire it wrote is drawn exactly as it was drawn."""

    def check(derivation: _Derivation) -> None:
        if wanted not in _wire_ends(derivation.model):
            raise ValueError(WIRE_NOT_YET)

    return check


def _taking(node: Node, kind: SocketType) -> Optional[str]:
    """The first input of ``node`` a wire carrying ``kind`` can be saved into — a slot, then a setting, in the order the
    card draws them — never the record, a record entry or a list member, which no saved wire goes into; ``None`` when no
    input takes it. Where a wire dragged OUT of an output lands on the card added for it (``Document.add_node``)."""
    for socket in node.inputs:
        if (
            socket.type != RECORD
            and not socket.entry
            and not _LIST_SOCKET.match(socket.name)
            and _meets(kind, socket.type)
        ):
            return socket.name
    for name, param in node.params.items():
        if node.input(name) is None and _meets_any(kind, _schema_types(param.schema)):
            return name
    return None


def _a(kind: str) -> str:
    """The article a node kind takes in a sentence: ``an engine``, ``a value``."""
    return "an" if kind[:1] in "aeiou" else "a"


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


def _entry(path: Path, texts: Mapping[Path, str], settings: Optional[Path]) -> Tuple[Path, Mapping[Path, str]]:
    """What confluid loads for the graph ``path``: the graph itself, or — with its ``settings`` file among ``texts`` —
    the HELD document that is the graph with the settings over it (:func:`streamstudio.settings.composed`), its text
    added to the texts. Nothing is written either way."""
    if settings is None:
        return path, texts
    wrapper, text = composed(path, settings)
    return wrapper, {**texts, wrapper: text}


def _settings_texts(graph: Path) -> Dict[Path, str]:
    """The settings file beside ``graph`` as a held text — ``{}`` when there is none (under :data:`_DISK_LOCK`)."""
    beside = settings_path_of(graph)
    return {beside.resolve(): beside.read_text()} if beside.is_file() else {}


def _setting_nodes(model: Model) -> List[SettingNode]:
    """Every node of every level, as the settings read it (the gate a branch sets is never a setting)."""
    return [
        SettingNode(node.name, node.class_path, tuple(name for name in node.params if name != GATE_PARAM))
        for level in model.levels()
        for node in level.nodes
        if node.class_path
    ]


def settings_declared(text: str, path: Path, model: Model) -> Declared:
    """The settings the graph ``text`` declares (its ``app:`` block), resolved against its drawing ``model``."""
    return declared_settings(text, path, _setting_nodes(model), gate=GATE_PARAM)


def _apply_settings(model: Model, path: Path, texts: Mapping[Path, str], settings: Path) -> None:
    """Show every setting of the ``settings`` file on its node, and add a located problem for each line that is none
    of the graph's settings (:func:`streamstudio.settings.read_settings`).

    The model applies them ITSELF: confluid delivers a class line (``SetFloor.floor_db: -60.0``, a node with no name)
    only at its ``settled`` stage, after the ``document`` stage the model is derived at — measured 2026-10-05, the
    canvas showed ``-90.0`` while a run used ``-60.0``. A node's line (``flow.first.op.level``) is already in the
    document; setting it again changes nothing."""
    key = path.resolve()
    graph_text = texts[key] if key in texts else path.read_text()
    found = settings_declared(graph_text, path, model)
    entries, problems = read_settings(texts[settings], settings, found, graph_name(settings))
    for field_, value in entries:
        node = model.node(field_.node)
        param = node.params.get(field_.param)
        if param is not None:
            node.params[field_.param] = replace(param, value=value, spelled=True)
    model.problems.extend(Problem(message, loc) for message, loc in problems)


def derive(path: Path, texts: Mapping[Path, str], settings: Optional[Path] = None) -> _Derivation:
    """The model of the document at ``path`` as the texts spell it — never raises.

    ``settings`` is the graph's settings file when its text is among ``texts`` (resolved, as every key): the graph is
    then loaded with the settings over it (:func:`_entry`), each setting shows on its node, and a line that is none of
    the graph's settings is a located problem (:func:`_apply_settings`). Every message and location names the graph or
    the settings file — never the held document that joins them."""
    entry, loaded = _entry(path, texts, settings)
    try:
        doc, included = _load_document(entry, loaded)
    except Exception as exc:  # noqa: BLE001 — every failure to load is reported as a problem
        problem = _load_problem(path, exc, texts=loaded)
        return _Derivation(Model(problems=[problem]), {}, None, [], None, (), [path.resolve()], {})
    derivation = _Builder(path).derive(doc)
    aside = {path.resolve(), entry.resolve()} | ({settings} if settings is not None else set())
    files = [path.resolve()] + [p for p in included if p not in aside]
    if settings is not None:
        _apply_settings(derivation.model, path, texts, settings)
    if not derivation.model.problems:
        # The document stage keeps the markers, so it can derive a document that does not BUILD — a
        # top-level key handed to a same-named constructor parameter, measured 2026-09-28 on a contract
        # placed before a flow whose ``outputs:`` line the contract then received. Only when deriving
        # found nothing, so one defect is one message.
        try:
            _load_document(entry, loaded, build=True)
        except Exception as exc:  # noqa: BLE001 — every failure to build is reported as a problem
            known = [*files, *([settings] if settings is not None else [])]
            problem = _load_problem(path, exc, "does not build", texts=loaded, included=known)
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
            out.append(Problem(_unwired(node, socket), node.loc))
    return out


def _unwired(node: Node, socket: Socket) -> str:
    """The problem of a required input nothing is wired into — what removing the wire into it leaves, and may."""
    return f"{node.name}.{socket.name} ({socket.type}) is required and nothing is wired into it at {node.loc}"


def _contract_problems(derivation: _Derivation) -> List[Problem]:
    """``GraphContract.check_declaration`` over the declaration, with a zero-arg instance of each
    delivered node's class standing in for the object (the name collision it refuses is a property
    of the CLASS)."""
    assert derivation.contract is not None
    model = derivation.model
    root = model.node(derivation.contract)
    delivered: Dict[str, Any] = {}
    for wire in model.wires:
        socket = root.input(wire.target_socket)
        if wire.target == root.name and wire.kind == "delivered" and socket is not None and not socket.entry:
            cls = _resolve(model.node(wire.source).class_path)
            if cls is not None:
                try:
                    delivered[wire.target_socket] = cls()
                except Exception:
                    continue
    for name, param in root.params.items():
        if param.doc == DELIVERED_LITERALLY:
            delivered[name] = param.value
    contract = GraphContract(
        name=str(root.params["name"].value or ""),
        inputs=_contract_slots(root, "inputs"),
        outputs=_contract_slots(root, "outputs"),
        records=_contract_slots(root, "records"),
        delivered=delivered,
        optional=_contract_slots(root, "optional"),
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

#: The one plain line each wire the editor cannot save yet is refused with (``Document.add_wire`` /
#: ``remove_wires``); the page shows it in a toast as it is, and nothing is drawn or written. A Stream's record
#: wires ARE its ``ops:`` order, and a member of an op list is placed by its position in it.
WIRE_CHANGES_ORDER = "This wire would change the order of the steps — the editor can't save that yet."
#: A flow's wires are its steps' ``from:`` / ``bind:`` / gates, which no route writes yet.
WIRE_IN_A_FLOW = "The editor can't save wires in this kind of graph yet."
#: Any other wire there is no spelling for yet.
WIRE_NOT_YET = "The editor can't save this wire yet."
WIRE_INSIDE_A_SUBGRAPH = "The editor can't save wires inside a subgraph yet."
#: A document whose root is ONE marker has no top-level key a ``!ref:`` could name.
WIRE_ONE_OBJECT = (
    "The editor can't save wires in this graph yet: the whole file is one object, with no top-level name a wire can "
    "point at."
)
#: Only an entry output (a source's ``image``) can feed a root's record entry — what it is saved as is its key.
WIRE_ENTRY_ONLY = "Only a record entry — like a source's image — can be wired into {entry}."
#: A ``host`` wire is where the host puts what it hands in (``_Builder.host_wires``): no text spells it, so no edit
#: takes it out.
WIRE_FROM_THE_HOST = "The host fills this when it runs the graph — it isn't a wire you can remove."
#: The host card (``<host>``) is drawn, never written: it cannot be renamed or removed.
HOST_NOT_A_NODE = "{name} is the host's side of the graph, not a node of the document"

#: The node kinds that are added AFTER a step or INTO an op list (``Document.add_step``); a class of any other kind is a
#: CARD of its own (``Document.add_node``) — the kind the document derives (``_kind_of``), never the catalogue's word.
_STEP_KINDS: Tuple[NodeKind, ...] = ("op", "algorithm", "subgraph")
#: A card asked of a flow graph, whose top level is steps — the user's words (2026-10-05); ``{article} {kind}`` is the
#: class's node kind (``a value``, ``an engine``).
CARD_IN_A_FLOW = "The editor can't add {article} {kind} card to a flow graph yet — pick an op."
#: The same inside a subgraph: its inside is a flow of its own.
CARD_INSIDE_A_SUBGRAPH = "The editor can't add {article} {kind} card inside a subgraph yet — pick an op."
#: An op asked as a card: it goes after a step or into an op list (``Document.add_step``).
CARD_IS_AN_OP = "{name} is an op — the editor adds it after a step or to an op list, never as a card of its own."
#: A document whose root is ONE marker has no top-level key a card could take.
CARD_ONE_OBJECT = (
    "The editor can't add a card to this graph yet: the whole file is one object, with no top-level name a card can "
    "take."
)
#: A wire dropped on empty canvas names the end it was dragged out of; the card added for it is the other end.
CARD_WIRE_ENDS = (
    "a wire dropped on empty canvas names the end it was dragged out of — {source, source_socket} or "
    "{target, target_socket}; the new card is its other end"
)

#: The ends of one wire, as the routes and the page name them: ``(source, source_socket, target, target_socket)``.
WireEnds = Tuple[str, str, str, str]
#: A socket that is a member of a list slot — ``ops[1]`` — and the slot.
_LIST_SOCKET = re.compile(r"^(?P<slot>.+)\[\d+\]$")


def _one_end(wire: WireEnds) -> bool:
    """Whether a wire dropped on empty canvas names exactly the end it was dragged out of — ``(source, source_socket)``
    or ``(target, target_socket)``, the other two blank (they are the card added for it)."""
    source, source_socket, target, target_socket = wire
    return (bool(source) and bool(source_socket) and not target and not target_socket) or (
        bool(target) and bool(target_socket) and not source and not source_socket
    )


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
    """``recordstream.ops.image.ConvertMode`` → ``convert_mode``: a generated step name for a list member, and the
    top-level key a card moved out of a slot takes (:meth:`Document._moved_key`)."""
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


def rebase_path(path: Path, old: Path, new: Path) -> Path:
    """``path`` with the folder ``old`` it sits under replaced by ``new`` — compared as given and resolved (a
    folder named through a symbolic link, ``/var/…`` for ``/private/var/…``, matches a resolved path under it and
    gives the resolved ``new``); a path under neither comes back unchanged."""
    for before, after in ((old, new), (old.resolve(), new.resolve())):
        if path.is_relative_to(before):
            return after / path.relative_to(before)
    return path


def _rewrite_includes(text: str, new_dir: Path, locate: Callable[[str], Optional[Path]]) -> str:
    """``text`` with every RELATIVE ``include:`` entry that ``locate`` maps to a file rewritten so that, read from
    ``new_dir``, it names that file. An absolute, ``~`` or ``$VAR`` entry reads the same from anywhere and is left as
    written, as is an entry ``locate`` answers ``None`` for (one that was not resolving beside the file — confluid's
    search tiers found it, or nothing did). A text that is not YAML is returned unchanged (the document's own problem
    says why)."""
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
            named = locate(entry)
            if named is None:
                continue
            rebased = os.path.relpath(named, new_dir)
            if rebased != entry:
                edits.append(patched.replace_scalar_edit(scalar, spell(rebased)))
    return patched.apply(edits)


def rebase_includes(text: str, old_dir: Path, new_dir: Path, held: Collection[Path] = ()) -> str:
    """``text`` with every RELATIVE ``include:`` entry rewritten so that, read from ``new_dir``, it names
    the file it named from ``old_dir`` — what Save As needs, since confluid reads an include beside the
    document (measured 2026-09-29: the unchanged text one folder over does not load,
    ``ConfigFileNotFoundError``). An entry that names no file beside the old place was not resolving through it
    and is left as written — a file the editor HOLDS but has not saved yet (``held``: a subgraph's steps just moved
    to their own file) counts as there (:func:`_rewrite_includes` has the rest of the rules).
    """
    if old_dir == new_dir:
        return text

    def beside_the_old_place(entry: str) -> Optional[Path]:
        named = old_dir / entry
        return named if named.exists() or named.resolve() in held else None

    return _rewrite_includes(text, new_dir, beside_the_old_place)


def follow_includes(
    text: str, file_before: Path, file_after: Path, old: Path, new: Path, held: Collection[Path] = ()
) -> str:
    """``text`` — a file that was ``file_before`` until the folder ``old`` was moved to ``new`` and is ``file_after``
    now — with every RELATIVE ``include:`` entry rewritten to name, from ``file_after``'s folder, the file it named
    before: one that sat inside ``old`` moved with it (to the same place under ``new``), one outside it stayed where it
    was. So an include between two files of the moved folder reads the same, and one out of it is rewritten — what a
    host's move of its folder needs (``Document.moved``). Measured before 2026-10-01: Insert steps file… wrote
    ``../../home/gert/steps/prep.steps.yaml`` from a temporary folder; after the host moved that folder the entry
    named nothing, check said ``does not load: ConfigFileNotFoundError`` and Save wrote a graph that did not load.
    The old folder is gone when this runs, so the entry is read LEXICALLY from ``file_before``'s folder (a ``..``
    is never resolved through a folder that is no longer there); ``held`` (the files the editor holds, by their NEW
    paths) counts as there, as for :func:`rebase_includes`."""
    before_dir = file_before.parent

    def where_it_is_now(entry: str) -> Optional[Path]:
        named = rebase_path(Path(os.path.normpath(before_dir / entry)), old, new)
        return named if named.exists() or named.resolve() in held else None

    return _rewrite_includes(text, file_after.parent, where_it_is_now)


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


_Method = TypeVar("_Method", bound=Callable[..., Any])


def _structural(method: _Method) -> _Method:
    """Mark a :class:`Document` method that edits the graph ITSELF (a node, a wire, a step, the text): on a graph that
    offers its settings only (``app: {graph: settings-only}``) it is refused FIRST, before any check of its own —
    :data:`streamstudio.settings.LOCKED`, whatever else would have been wrong with it. ``_commit`` refuses the same
    for any other change of such a graph (a parameter that is not one of its settings)."""

    @functools.wraps(method)
    def guarded(self: "Document", *args: Any, **kwargs: Any) -> Any:  # answers what the method answers
        if self.mode == SETTINGS_ONLY:
            raise ValueError(LOCKED.format(name=self.path.stem))
        return method(self, *args, **kwargs)

    return cast(_Method, guarded)


class _Entry(NamedTuple):
    """One step of the document's history: the texts before an edit, the renames it made (a rename; a wire whose
    producer moved out of a slot to a top-level key — several, for several wires removed as one edit), the shifts of a
    list's
    positional member names it made, in the order they were made (a Convert, a Paste, an add or a removal of list
    members — a removal of several members as ONE edit makes one shift per member), and — per shift — what to put
    back after the step that walks it again: on the undo stack what the edit's shifts dropped (a removed member's
    place and groups, measured before: an undo brought the member back out of its group), on the redo stack what the
    undo's shifts dropped (an added member's place and groups, which a redo then restores)."""

    texts: _Texts
    renames: Tuple[Tuple[str, str], ...]
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
        shipped: The file a host SHIPS this graph in, while ``path`` is not written yet (:meth:`open_shipped`) —
            ``None`` for a graph that is its own file.

    The graph's SETTINGS file (:attr:`settings_path`, ``streamstudio.settings``) is one more text it holds — read
    with the graph when it is there, held from the first setting changed when it is not.
    """

    def __init__(
        self,
        path: Path,
        texts: _Texts,
        *,
        history_depth: int = 50,
        saved: Optional[Mapping[Path, str]] = None,
        shipped: Optional[Path] = None,
    ) -> None:
        # Stores only: the model is derived on first use and cached per text.
        self.path = Path(path)
        self.shipped = None if shipped is None else Path(shipped)
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
        self._joined_renames: List[Tuple[str, str]] = []
        # The member shifts the last undo or redo applied, in order (:attr:`stepped_shifts`).
        self._stepped: Tuple[_MemberShift, ...] = ()

    @classmethod
    def open(cls, path: Union[str, Path], *, history_depth: int = 50) -> "Document":
        """Read the file's text, and its settings file's when it has one; parse nothing."""
        given = Path(path)
        with _DISK_LOCK:
            texts = {given.resolve(): given.read_text()}
            texts.update(_settings_texts(given))
        return cls(given, texts, history_depth=history_depth)

    @classmethod
    def open_shipped(cls, path: Union[str, Path], shipped: Union[str, Path], *, history_depth: int = 50) -> "Document":
        """The graph ``path`` — a file NOT written yet — as a host ships it, in ``shipped``: it holds the shipped text
        as the graph's and nothing unsaved, so a Save writes the graph only once it was changed (the COPY), and the
        settings file beside ``path`` from the first setting changed. ``shipped`` itself is never written. Its relative
        ``include:`` entries are rebased to name, from ``path``'s folder, the files they name beside ``shipped``
        (:func:`rebase_includes`): confluid reads an include beside the document, and the folder holds no copy of a
        file shipped with it (measured before 2026-10-27: a shipped graph including a pane shipped beside it loaded
        no node — ``includes pane_view.yaml: Not found``)."""
        given = Path(path)
        with _DISK_LOCK:
            text = rebase_includes(Path(shipped).read_text(), Path(shipped).parent, given.parent)
            texts = {given.resolve(): text}
            texts.update(_settings_texts(given))
        return cls(given, texts, history_depth=history_depth, shipped=Path(shipped))

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
            shipped=None if held.get("shipped") is None else Path(str(held["shipped"])),
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
        here (``null`` when none were set) with ``positions_whole`` and ``layout_dirty``; ``shipped``, the file a host
        ships the graph in while its own is not written (``null`` otherwise). The undo history is NOT kept — an undo
        after a restore has nothing to walk back."""
        return {
            "path": str(self.path),
            "shipped": None if self.shipped is None else str(self.shipped),
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

    def changed_on_disk(self) -> bool:
        """Whether a file this document holds a text of says something else on disk than the editor last read or wrote
        — what makes ``streamstudio.app.Studio.document`` read a document holding nothing unsaved again. A file gone
        from disk, one never written yet, and one that cannot be read do not count: never raises."""
        with _DISK_LOCK:
            beside = self.settings_path.resolve()
            if beside not in self._texts and beside.is_file():
                return True  # a settings file written since the graph was read: what the graph runs changed
            for key in self._texts:
                if key not in self._saved or not key.is_file():
                    continue
                try:
                    text = key.read_text()
                except (OSError, UnicodeDecodeError):
                    continue
                if text != self._saved[key]:
                    return True
        return False

    def load(self) -> Any:  # the built document: a mapping of top-level objects, or the root object itself
        """The document BUILT from the texts the editor holds — saved or not, what the canvas shows — to
        the objects stage, solidified: what a run executes. Every call builds fresh objects (a run's ops
        keep state), and a document that does not build raises confluid's own located error."""
        entry, texts = _entry(self.path, self._texts, self._settings_in(self._texts))
        built, _paths = _load_document(entry, texts, build=True, solidify=True)
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
    def settings_path(self) -> Path:
        """The graph's settings file — beside :attr:`path`, ``<graph>.settings.yaml`` (``streamstudio.settings``)."""
        return settings_path_of(self.path)

    @property
    def mode(self) -> GraphMode:
        """What the graph lets a person change, as its ``app: {graph: …}`` says (``editable`` without the line)."""
        return graph_mode(self.text, self.path)[0]

    @property
    def is_shipped(self) -> bool:
        """Whether the graph is still the one its host ships — its own file not written yet."""
        return self.shipped is not None and not self.path.is_file()

    def declared(self) -> Declared:
        """The graph's settings, as its ``app:`` block declares them over the drawing the editor shows."""
        return settings_declared(self.text, self.path, self.model)

    def _settings_in(self, texts: Mapping[Path, str]) -> Optional[Path]:
        """The settings file's key when ``texts`` hold its text, else ``None`` (the graph alone is loaded)."""
        key = self.settings_path.resolve()
        return key if key in texts else None

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
            self._cache = (key, derive(self.path, self._texts, self._settings_in(self._texts)), None)
        return self._cache[1]

    # -- editing -----------------------------------------------------------------------------

    def set_param(self, node: str, param: str, value: Any) -> None:  # value: any YAML scalar or container
        """Set one parameter: the located line is patched, or the line is inserted under the node when
        the file left it at its default. Comments and ``$VAR`` spellings elsewhere are untouched.

        A SETTING (a field the graph's ``app:`` block names) goes to the settings file, never the graph: its line
        there is replaced, or one is appended (:func:`streamstudio.settings.set_setting`). One the block names but no
        setting can reach — two nodes with no name share its class — is refused, saying so."""
        found = self.model.node(node)
        if param not in found.params:
            raise ValueError(f"{node} has no parameter {param!r} (its parameters are: {list(found.params)})")
        settings = self.declared()
        setting = settings.field(node, param)
        if setting is not None:
            key = self.settings_path.resolve()
            current = self._text_of(key) if key in self._texts or key.is_file() else ""
            self._commit({key: set_setting(current, setting, value)}, f"{node}.{param} = {value!r}", found.loc)
            return
        unreachable = settings.unaddressable.get((node, param))
        if unreachable is not None:
            raise ValueError(f"{node}.{param} cannot be set: {unreachable}")
        if not found.loc:
            raise ValueError(f"{node} has no location in the document — nothing to patch")
        file, line, col = _split_loc(found.loc)
        key = Path(file).resolve()
        patched = YamlText(self._text_of(key)).set_kwarg(line, col, param, value)
        self._commit({key: patched}, f"{node}.{param} = {value!r}", found.loc)

    @_structural
    def set_slot(self, node: str, slot: str, class_path: str, params: Dict[str, Any]) -> str:
        """Put a new object into the single slot ``slot`` of ``node`` — ``slot: !class:<class_path> {<params>}`` —
        replacing what it held (a marker, a ``!ref:``, nothing), and answer the name of the node now wired into it
        (``stream.source``). ONE edit, held until Save; every comment outside the replaced value stays
        (:meth:`YamlText.set_marker`). The canvas's *set source…*: a source into ``stream.source``.

        Refused, changing nothing, in words: a LIST slot (``stream.ops`` — a member is added or removed, the list is
        never set whole), a typed parameter (its value is set, not an object), a name that is neither; and — the
        edit's own test — a class whose output does not meet the slot's type, or that does not resolve.
        """
        found = self.model.node(node)
        slots = [socket.name for socket in found.inputs if not socket.entry and socket.name != "record"]
        singles = [name for name in slots if "[" not in name]
        listed = ", ".join(singles) or "none"
        if any(name.startswith(f"{slot}[") for name in slots):
            raise ValueError(f"{node}.{slot} is a list — add a member to it, or remove one; it is never set whole")
        if slot not in singles:
            if slot in found.params:
                raise ValueError(
                    f"{slot} is a parameter of {node}, not a slot — set its value instead (the slots of {node} are: "
                    f"{listed})"
                )
            raise ValueError(f"{node} has no slot {slot!r} (its slots are: {listed})")
        if not found.loc:
            raise ValueError(f"{node} has no location in the document — nothing to patch")
        file, line, col = _split_loc(found.loc)
        key = Path(file).resolve()
        marker = f"!class:{class_path} {spell_mapping(params)}"
        patched = YamlText(self._text_of(key)).set_marker(line, col, slot, marker)
        self._commit({key: patched}, f"{node}.{slot} = {marker}", found.loc)
        wired = (wire.source for wire in self.model.wires if wire.target == node and wire.target_socket == slot)
        return next(wired, f"{node}.{slot}")

    # -- wires -------------------------------------------------------------------------------

    @_structural
    def add_wire(self, source: str, source_socket: str, target: str, target_socket: str) -> str:
        """Save the wire ``source.source_socket → target.target_socket`` drawn on the canvas, in the document's own
        spelling, as ONE edit held until Save; answer the name of the card the wire now comes from.

        Three spellings, each a text patch where it belongs: (a) into an OUTPUT slot of a ``GraphContract`` root,
        ``delivered: {<slot>: !ref:<P>}`` — ``delivered:`` is made when the root has none; (b) into a single slot or
        parameter of any other card, ``<slot>: !ref:<P>`` on that card's marker; (c) from a record ENTRY output (a
        source's ``image``) into a record entry of the root, ``delivered: {<entry>: <key>}`` — the plain key the entry
        is read from (:meth:`_wire_record_entry`). Whatever the input held is replaced.

        ``P`` is the producer's TOP-LEVEL key: a ``!ref:`` names nothing else (confluid refuses ``!ref:stream.source``).
        A producer written inside another card's slot (``stream.source``) is first MOVED OUT to a top-level key named
        after its CLASS (:meth:`_moved_key`: ``hugging_face_source``, or ``hugging_face_source_2`` …), inserted above
        the top-level key that held it and dedented, and the slot reads ``!ref:<P>``; its sidecar position and groups
        follow the new name, those of the cards under it too, and it keeps the place it was drawn at. An ``alias`` card
        wires as the card it is.

        A bare ``!ref:P`` is DRAWN from P's first output that fits the input (:meth:`_Builder.choose_output`), so only
        that output can be wired; and every edit must draw exactly the wire asked for, or it is refused unwritten.
        Refused before anything is written, in one plain line: a record wire or a wire into an op list's member
        (:data:`WIRE_CHANGES_ORDER`), a wire of a flow (:data:`WIRE_IN_A_FLOW`), one inside a subgraph, in a document
        that is one object, from a card inside a list, and any other wire there is no spelling for yet.
        """
        derivation = self._derivation()
        model = derivation.model
        producer, taker = model.node(source), model.node(target)
        output = producer.output(source_socket)
        if output is None:
            shown = ", ".join(s.name for s in producer.outputs)
            raise ValueError(f"{source} has no output {source_socket!r} (its outputs are: {shown})")
        socket = taker.input(target_socket)
        if socket is None and target_socket not in taker.params:
            shown = ", ".join([s.name for s in taker.inputs] + [n for n in taker.params if taker.input(n) is None])
            raise ValueError(f"{target} has no input {target_socket!r} (its inputs are: {shown})")
        self._refuse_wire_place(derivation, source, target)
        if (source, source_socket, target, target_socket) in _wire_ends(model):
            raise ValueError(f"{source}.{source_socket} is already wired into {target}.{target_socket}.")
        if output.type == RECORD or (socket is not None and socket.type == RECORD):
            raise ValueError(WIRE_CHANGES_ORDER)
        listed = _LIST_SOCKET.match(target_socket)
        if listed is not None:
            if listed.group("slot") in OP_LISTS:
                raise ValueError(WIRE_CHANGES_ORDER)
            raise ValueError(f"The editor can't save this wire yet: {target}.{target_socket} is a member of a list.")
        accepted = _Builder(self.path).accepted_types(taker, target_socket)
        if not _meets_any(output.type, accepted):
            shown = accepted[0] if len(accepted) == 1 else " | ".join(accepted)
            raise ValueError(
                f"{source}.{source_socket} ({output.type}) cannot be wired into {target}.{target_socket} ({shown}): "
                "the types do not meet."
            )
        if taker.kind == "contract" and socket is not None and socket.entry:
            return self._wire_record_entry(derivation, producer, output, taker, socket)
        if output.entry:
            raise ValueError(
                f"The editor can't save this wire yet: {source}.{source_socket} is an entry of every record, and only "
                "a record entry of the graph's root can take one."
            )
        if socket is not None and socket.entry:
            raise ValueError(
                f"The editor can't save this wire yet: {target}.{target_socket} reads the record entry "
                f"'{socket.entry}' from the record it is handed."
            )
        origin = model.node(producer.alias_of) if producer.kind == "alias" else producer
        under = (origin.name, f"{origin.name}.", f"{origin.name}/", f"{origin.name}[")
        if (
            origin.kind == "host"  # the host card is never written: where it puts an input is the host's own rule
            or taker.kind in ("alias", "host", "boundary", "subgraph")
            or not taker.loc
            or target.startswith(under)
        ):
            raise ValueError(WIRE_NOT_YET)
        if taker.kind == "contract" and socket is None:
            raise ValueError(WIRE_NOT_YET)  # the root's own declaration (name, outputs …) is typed, never wired
        moving = origin.name not in self._top_keys()
        owner, slot = self._movable(origin, model) if moving else ("", "")
        first = _Builder.choose_output(origin, accepted)
        if first is None or first.name != source_socket:
            only = first.name if first is not None else "none"
            raise ValueError(
                f"{source} has more than one output that fits {target}.{target_socket}; only {only} can be wired into "
                "it."
            )
        main = self.path.resolve()
        main_text = YamlText(self.text)
        edits = _Edits()
        name, rename, hoist = origin.name, None, []
        if moving:
            name = self._moved_key(origin, model)
            _file, line, col = _split_loc(model.node(owner).loc)
            mapping = main_text.node_at(line, col)
            assert isinstance(mapping, yaml.MappingNode)
            above = main_text.top_key_line(_head(origin.name))
            hoist = main_text.hoist_edits(mapping, slot, name, above, keep_slot=True)
            rename = (origin.name, name)
        file, line, col = _split_loc(taker.loc)
        key = Path(file).resolve()
        text = main_text if key == main else YamlText(self._text_of(key))
        reference = f"!ref:{name}"
        if taker.kind == "contract":
            taking = text.set_entry_edits(line, col, "delivered", target_socket, reference)
        else:
            taking = [text.set_marker_edit(line, col, target_socket, reference)]
        if hoist:
            edits.add(main, main_text, hoist)  # first: an insertion at the same place as the target's goes below it
        edits.add(key, text, taking)
        before = self.positions if rename is not None else {}
        self._commit(
            edits.texts(),
            f"the wire {source}.{source_socket} → {target}.{target_socket}",
            taker.loc,
            rename=rename,
            verify=_drawn((name, source_socket, target, target_socket)),
        )
        if rename is not None:
            self._rekey(*rename)
            self._pin(before, *rename)
        return name

    @_structural
    def remove_wires(self, wires: Sequence[WireEnds]) -> None:
        """Take one or several wires out as ONE edit — all of them or none, one undo entry (Delete on selected wires).

        A wire into a root's slot or record entry drops its ``delivered:`` pair (and ``delivered:`` with its last one);
        a ``!ref:`` drops its pair from the card it fills, whose parameter returns to its default; a card written INSIDE
        the slot the wire fills is moved out to a top-level key named after its class, as :meth:`add_wire` moves one
        (:meth:`_moved_key` — never a name the slot's owner takes as a parameter, or confluid would hand the card
        straight back into the slot), and the slot is dropped: both cards stay, only the wire goes. The one problem a
        removal may gain is the input it leaves open, when that input is required (``contract.stream (Stream) is
        required and nothing is wired into it``); any other refuses it. Refused like :meth:`add_wire`, in one plain
        line: a Stream's record wires, a flow's, a wire inside a subgraph, a wire drawn from what steps read and write
        — and a ``host`` wire, which no text spells (:data:`WIRE_FROM_THE_HOST`)."""
        if not wires:
            raise ValueError("remove needs at least one wire")
        model = self._derivation().model
        drawn = _wire_ends(model)
        for wire in wires:
            if tuple(wire) not in drawn:
                raise ValueError(f"{wire[0]}.{wire[1]} → {wire[2]}.{wire[3]} is not a wire of this graph")
        # a root's record entry is drawn from the stream the root delivers: it goes before that stream's own wire would
        ordered = sorted(wires, key=lambda wire: 0 if _is_entry_input(model, wire[2], wire[3]) else 1)
        renames: List[Tuple[str, str]] = []
        layout = _layout()
        with self._one_edit():
            for wire in ordered:
                ends = list(wire)
                for old, new in renames:  # a card an earlier wire moved out is named by its new name now
                    ends[0], ends[2] = layout.rename_key(ends[0], old, new), layout.rename_key(ends[2], old, new)
                moved = self._remove_wire(ends[0], ends[1], ends[2], ends[3])
                if moved is not None:
                    renames.append(moved)

    def _remove_wire(
        self, source: str, source_socket: str, target: str, target_socket: str
    ) -> Optional[Tuple[str, str]]:
        """One wire of :meth:`remove_wires`, committed; the ``(old, new)`` name of a card it moved out, else
        ``None``."""
        derivation = self._derivation()
        model = derivation.model
        wire = next(
            (
                w
                for level in model.levels()
                for w in level.wires
                if (w.source, w.source_socket, w.target, w.target_socket)
                == (source, source_socket, target, target_socket)
            ),
            None,
        )
        if wire is None:
            raise ValueError(f"{source}.{source_socket} → {target}.{target_socket} is not a wire of this graph")
        if wire.kind == "host":
            raise ValueError(WIRE_FROM_THE_HOST)
        self._refuse_wire_place(derivation, source, target)
        if wire.kind in ("record", "list"):
            raise ValueError(WIRE_CHANGES_ORDER)
        if wire.kind in ("bind", "gate"):
            raise ValueError(WIRE_IN_A_FLOW)
        if wire.kind == "entry":
            raise ValueError(
                f"The editor can't save this wire yet: {target}.{target_socket} is fed by what the steps before it "
                "write into the record, which no setting spells."
            )
        taker = model.node(target)
        socket = taker.input(target_socket)
        allow = [_unwired(taker, socket)] if socket is not None and socket.required else []
        what = f"removing the wire {source}.{source_socket} → {target}.{target_socket}"
        file, line, col = _split_loc(taker.loc)
        key = Path(file).resolve()
        text = YamlText(self._text_of(key))
        marker = text.node_at(line, col)
        delivered = wire.kind == "delivered"
        found = text.pair(marker, "delivered") if delivered and isinstance(marker, yaml.MappingNode) else None
        mapping = found[1] if found is not None else marker
        held = text.pair(mapping, target_socket) if isinstance(mapping, yaml.MappingNode) else None
        if held is None or not isinstance(mapping, yaml.MappingNode):
            raise ValueError(WIRE_NOT_YET)
        if source != (f"{target}.delivered.{target_socket}" if delivered else f"{target}.{target_socket}"):
            if delivered:
                edits = [text.cut_entry_edit(line, col, "delivered", target_socket)]
            elif str(held[1].tag).startswith("!ref:"):
                edits = [text.cut_pair_edit(mapping, target_socket)]
            else:
                raise ValueError(WIRE_NOT_YET)
            self._commit({key: text.apply(edits)}, what, taker.loc, allow=allow)
            return None
        # the card is written INSIDE the slot: it moves out to a top-level key, and the slot goes
        if key != self.path.resolve():
            raise ValueError(f"The editor can't save this yet: {source} comes from {file}.")
        assert isinstance(marker, yaml.MappingNode) and isinstance(mapping, yaml.MappingNode)
        name = self._moved_key(model.node(source), model)
        above = text.top_key_line(_head(source))
        hoist = text.hoist_edits(mapping, target_socket, name, above, keep_slot=False)
        if delivered and len(mapping.value) == 1:
            hoist = [hoist[0], text.cut_pair_edit(marker, "delivered")]
        before = self.positions
        self._commit({key: text.apply(hoist)}, what, taker.loc, rename=(source, name), allow=allow)
        self._rekey(source, name)
        self._pin(before, source, name)
        return source, name

    def _refuse_wire_place(self, derivation: _Derivation, *names: str) -> None:
        """Refuse, in one plain line, a wire between ``names`` the editor has no spelling for by WHERE they are:
        inside a subgraph, in a document that is one object, in a flow (its wires are ``from:`` / ``bind:`` /
        gates)."""
        if any(SEPARATOR in name for name in names):
            raise ValueError(WIRE_INSIDE_A_SUBGRAPH)
        try:
            root = YamlText(self.text).root
        except yaml.YAMLError:
            raise ValueError(WIRE_NOT_YET) from None
        if not isinstance(root, yaml.MappingNode) or str(root.tag).startswith("!") or "_target_" in YamlText.keys(root):
            raise ValueError(WIRE_ONE_OBJECT)
        steps = {step.name for step in derivation.flow.steps} if derivation.flow is not None else set()
        if any(_head(name) in steps for name in names):
            raise ValueError(WIRE_IN_A_FLOW)

    def _top_keys(self) -> List[str]:
        """The main file's top-level keys — the names a ``!ref:`` can point at."""
        root = YamlText(self.text).root
        return YamlText.keys(root) if isinstance(root, yaml.MappingNode) else []

    def _movable(self, node: Node, model: Model) -> Tuple[str, str]:
        """``(owner, slot)`` of a card written in a single slot of another card of the main file — the card a wire from
        it moves out — or the refusal saying why it cannot move: it sits inside a list, in a plain mapping, or in an
        included file."""
        name = node.name
        if "[" in name:
            raise ValueError(
                f"The editor can't save a wire from {name} yet: it sits inside a list ({name[: name.index(']') + 1]})."
            )
        owner, dot, slot = name.rpartition(".")
        holder = model.holder(owner) if dot else None
        if holder is None or not model.node(owner).loc:
            raise ValueError(
                f"The editor can't save a wire from {name} yet: it sits in a plain mapping ({owner}), not in a card's "
                "slot."
            )
        for each in (node, model.node(owner)):
            if each.source_file and Path(each.source_file).resolve() != self.path.resolve():
                raise ValueError(f"The editor can't save a wire from {name} yet: it comes from {each.source_file}.")
        return owner, slot

    def _moved_key(self, node: Node, model: Model) -> str:
        """The top-level key the card ``node``, moved out of a slot, takes: its CLASS name in snake case (:func:`_snake`
        — ``HuggingFaceSource`` → ``hugging_face_source``, ``HTTPSource`` → ``http_source``), else the first free
        ``_2``, ``_3`` …. A key says what the card IS; the slot's name said only where it sat (every source moved out of
        a Stream was ``source``, ``source_2`` …).

        TAKEN is a top-level key, a loader word (``flow``, ``include`` …), a node — and a constructor parameter of ANY
        card, the slot's owner included: confluid hands a top-level key to every constructor taking a parameter of that
        name. Measured 2026-10-04: a top-level ``source`` reached a second Stream's unspelled ``source``, and overrode
        the ``source`` a card ABOVE it spelled (last spec wins) — a wire nobody drew, and one silently lost; 2026-10-05:
        ``second (no source written) got: FilesSource ['a.png']``. The owner too, since its slot may lose the reference
        later (a removed wire), and a key named like it would then flow straight back in."""
        return self._free_key(node.class_path or node.name, model)

    def _free_key(self, class_path: str, model: Model) -> str:
        """The top-level key a card of ``class_path`` takes by :meth:`_moved_key`'s rule — a card moved out of a slot,
        and a card ADDED as one of its own (:meth:`add_node`) alike, so the two never name a card differently."""
        wanted = _snake(class_path)
        taken = set(self._top_keys()) | set(_TOP_LEVEL_NOT_NODES) | set(model.all_names())
        for level in model.levels():
            for each in level.nodes:
                taken.update(_parameter_names(each))
        return wanted if wanted not in taken else _free_step_name(wanted, taken)

    def _pin(self, before: Mapping[str, Tuple[float, float]], old: str, new: str) -> None:
        """Keep a card moved from ``old`` to ``new`` — and every card under it — where it was DRAWN: a place the sidecar
        or this document held followed the name already (:meth:`_rekey`); a laid-out place is held now, since the
        layout of the changed document could put the renamed card elsewhere."""
        layout = _layout()
        held = dict(self._positions or {})
        for name, xy in before.items():
            moved = layout.rename_key(name, old, new)
            if moved != name:
                held.setdefault(moved, xy)
        self._positions = held

    def _wire_record_entry(
        self, derivation: _Derivation, producer: Node, output: Socket, root: Node, socket: Socket
    ) -> str:
        """:meth:`add_wire` into a record entry of the root: ``delivered: {<entry>: <key>}``, the key ``output`` writes
        (a plain word — never a ``!ref:``, so the producer never moves). Only an entry output can feed it, and only the
        card that writes that key LAST into the stream the root delivers — that is the card the entry is read from."""
        model = derivation.model
        entry = socket.name
        if not output.entry:
            raise ValueError(WIRE_ENTRY_ONLY.format(entry=entry))
        slot, stream = _delivered_stream(model, root.name)
        if slot is None:
            raise ValueError(
                f"{root.name} delivers no stream, and {root.name}.{entry} is read from a stream's records."
            )
        if stream is None:
            raise ValueError(
                f"Wire a stream into {root.name}.{slot} first — {root.name}.{entry} is read from the records it "
                "delivers."
            )
        if producer.name not in _stream_cards(model, stream):
            raise ValueError(
                f"{producer.name} does not feed the stream {root.name} delivers ({stream}) — {root.name}.{entry} is "
                "read from that stream's records."
            )
        writer = _record_writer(model, root.name, output.entry)
        if writer is not None and writer[0].name != producer.name:
            raise ValueError(
                f"{root.name}.{entry} would be read from {writer[0].name}.{writer[1].name}, which writes "
                f"'{output.entry}' after {producer.name} does."
            )
        file, line, col = _split_loc(root.loc)
        key = Path(file).resolve()
        text = YamlText(self._text_of(key))
        edits = _Edits()
        edits.add(key, text, text.set_entry_edits(line, col, "delivered", entry, spell(output.entry)))
        self._commit(
            edits.texts(),
            f"the wire {producer.name}.{output.name} → {root.name}.{entry}",
            root.loc,
            verify=_drawn((producer.name, output.name, root.name, entry)),
        )
        return producer.name

    # -- cards: a class that is not an op ------------------------------------------------------

    @_structural
    def add_node(self, class_path: str, params: Dict[str, Any], *, wire: Optional[WireEnds] = None) -> str:
        """Add ``class_path`` — a class that is NOT an op — as a CARD of its own and answer its name: a new top-level
        key ``<key>: !class:<class_path> {<params>}``, ONE edit held until Save (the user's design, 2026-10-05).

        The key is :meth:`_moved_key`'s (:meth:`_free_key` — ``ClassNamesScan`` → ``class_names_scan``, ``_2`` … when
        that name is a top-level key, a node or a constructor parameter of any card: confluid hands a top-level key to
        every same-named parameter). The line goes directly ABOVE the graph's root and the comment lines written over
        it (:meth:`YamlText.top_key_line`), so the root stays last, or at the end of the file when there is no root.

        ``wire`` is a wire dragged out of a socket and dropped on empty canvas, its other end blank — the new card:
        ``("", "", target, target_socket)`` (the card's first output that fits feeds that input) or ``(source,
        source_socket, "", "")`` (that output goes into the card's first input that takes it, :func:`_taking`). It is
        saved in the document's own spelling (:meth:`add_wire`) in the SAME edit — one undo takes both — or nothing is
        added and the wire's refusal is the answer.

        Refused before anything is written, in one plain line: an op (:data:`CARD_IS_AN_OP` — it goes after a step or
        into a list), a flow graph (:data:`CARD_IN_A_FLOW`), a document that is one object or does not parse, a class
        that does not resolve, a wire that does not name exactly one end (:data:`CARD_WIRE_ENDS`)."""
        if wire is not None and not _one_end(wire):
            raise ValueError(CARD_WIRE_ENDS)
        derivation = self._derivation()
        cls = _resolve(class_path)
        if cls is None:
            raise ValueError(f"cannot resolve the class {class_path!r}")
        kind = _kind_of(cls)
        if kind in _STEP_KINDS:
            raise ValueError(CARD_IS_AN_OP.format(name=class_path.rsplit(".", 1)[-1]))
        text = YamlText(self.text)
        root = self._card_root(text)
        if root is not None and "flow" in YamlText.keys(root):
            raise ValueError(CARD_IN_A_FLOW.format(article=_a(kind), kind=kind))
        name = self._free_key(class_path, derivation.model)
        line = f"{name}: !class:{class_path} {spell_mapping(params)}"
        head = _head(derivation.contract) if derivation.contract is not None else None
        if root is not None and head is not None and head in YamlText.keys(root):
            edit = text.insert_lines_edit(text.top_key_line(head), [line])
        else:
            end = len(text.text)
            edit = (end, end, ("\n" if text.text and not text.text.endswith("\n") else "") + line + "\n")
        changes = {self.path.resolve(): text.apply([edit])}
        if wire is None:
            self._commit(changes, f"add {class_path} as {name}", "")
            return name
        with self._one_edit():
            self._commit(changes, f"add {class_path} as {name}", "")
            self._wire_card(name, wire)
        return name

    def _card_root(self, text: YamlText) -> Optional[yaml.MappingNode]:
        """The main file's top-level mapping a card is added to — ``None`` while the text holds nothing — or the refusal
        saying why a card has no place: the text does not parse (located where it breaks), or the whole file is one
        object (:data:`CARD_ONE_OBJECT`)."""
        try:
            root = text.root
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)  # a ReaderError (a control character) carries none
            where = f"{self.path.resolve()}" + (f":{mark.line + 1}:{mark.column + 1}" if mark is not None else "")
            raise ValueError(f"{where}: the text does not parse — fix it, then add the card") from None
        if root is None:
            return None
        if not isinstance(root, yaml.MappingNode) or str(root.tag).startswith("!") or "_target_" in YamlText.keys(root):
            raise ValueError(CARD_ONE_OBJECT)
        return root

    def _wire_card(self, name: str, wire: WireEnds) -> None:
        """Save the dropped ``wire`` to the card ``name`` :meth:`add_node` has just written — inside its one edit: into
        the input the wire was dragged out of, from the card's first output that fits it; out of the output it was
        dragged out of, into the card's first input that takes it (:func:`_taking`)."""
        source, source_socket, target, target_socket = wire
        model = self._derivation().model
        card = model.node(name)
        if target:
            output = _Builder.choose_output(card, _Builder(self.path).accepted_types(model.node(target), target_socket))
            if output is None:
                raise ValueError(f"{name} has no output a wire into {target}.{target_socket} could come from.")
            self.add_wire(name, output.name, target, target_socket)
            return
        producer = model.node(source)
        given = producer.output(source_socket)
        if given is None:
            shown = ", ".join(socket.name for socket in producer.outputs)
            raise ValueError(f"{source} has no output {source_socket!r} (its outputs are: {shown})")
        taking = _taking(card, given.type)
        if taking is None:
            raise ValueError(f"{name} has no input that takes {source}.{source_socket} ({given.type}).")
        self.add_wire(source, source_socket, name, taking)

    @_structural
    def add_step(
        self,
        name: str,
        after: Optional[str],
        class_path: str,
        params: Dict[str, Any],
        *,
        into: Optional[str] = None,
        wire: Optional[WireEnds] = None,
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

        The host card and the root are never an anchor (the user's rule, 2026-10-05): ``after`` naming one counts as
        none. A class whose node kind is not an op's (:data:`_STEP_KINDS`, as the DOCUMENT derives it — the catalogue
        lists 35 classes as values the document derives as ops, ``FilterOp`` among them) has no place after a step or in
        a list: it is a card of its own, :meth:`add_node` with ``wire`` — ``after``, ``into`` and ``name`` placing ops
        only — and inside a subgraph it is refused (:data:`CARD_INSIDE_A_SUBGRAPH`). An op's ``wire`` is its place.
        """
        derivation = self._derivation()
        if after is not None and (derivation.model.node(after).kind == "host" or after == derivation.contract):
            after = None
        cls = _resolve(class_path)
        kind = _kind_of(cls) if cls is not None else None
        if kind is not None and kind not in _STEP_KINDS:
            if after is not None and SEPARATOR in after:
                raise ValueError(CARD_INSIDE_A_SUBGRAPH.format(article=_a(kind), kind=kind))
            return self.add_node(class_path, params, wire=wire)
        if after is None or self._op_member(after) is not None:
            return self._add_member(after, into, class_path, params)
        if into is not None:
            raise ValueError(
                f"{after!r} is not a member of {into} — name one of its members, or none to add at its end"
            )
        if not name:
            raise ValueError(f"a new flow step needs a name — after {after!r}, it has none")
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

    @_structural
    def remove_step(self, name: str) -> None:
        """Remove a step of any level; what read it now reads what it read from, and the level's output moves
        to that step when the removed one was it. A member of an op list (``stream.ops[1]``) is taken out of its
        list, the later members' positions and groups following them; the last one leaves ``ops: []``."""
        if self._op_member(name) is not None:
            self._remove_member(name)
            return
        derivation = self._derivation()
        node = derivation.model.node(name)
        if node.kind == "host":
            raise ValueError(HOST_NOT_A_NODE.format(name=name))
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

    @_structural
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

    @_structural
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
            raise ValueError(HOST_NOT_A_NODE.format(name=old))
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

    @_structural
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

    @_structural
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

    @_structural
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

    @_structural
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
        for old, new in reversed(entry.renames):
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
        self._redo.append(_Entry(texts, entry.renames, entry.members, tuple(undone)))
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
        for old, new in entry.renames:
            self._rekey(old, new)
        redone: List[_Dropped] = []
        for index, shift in enumerate(entry.members):
            redone.append(self._shift_members(*shift))
            if index < len(entry.dropped):
                self._restore_dropped(entry.dropped[index])
        self._history.append(_Entry(texts, entry.renames, entry.members, tuple(redone)))
        self._stepped = tuple(entry.members)
        return True

    @property
    def stepped_shifts(self) -> Tuple[_MemberShift, ...]:
        """The shifts of op list member names the last :meth:`undo` / :meth:`redo` applied, in order —
        ``(list, first, by)``: ``list[i]`` with ``i >= first`` became ``list[i + by]``. Empty after any other step."""
        return self._stepped

    @_structural
    def replace_text(self, text: str) -> None:
        """Make ``text`` the main file's text: checked as a whole, refused (nothing changes) when it gains a
        problem over the current text, and the undo history starts over — the text arrived whole, not as
        a patch the history could hold. Nothing is written; positions and groups held here are kept."""
        self._refuse_locked({self.path.resolve(): text})
        candidate: _Texts = {**self._texts, self.path.resolve(): text}
        before = self.check()
        derivation = derive(self.path, candidate, self._settings_in(candidate))
        after = check(derivation, self.path)
        gained = _gained(before, after)
        if gained:
            raise ValueError("; ".join(problem.message for problem in gained))
        self._texts = candidate
        self._history.clear()
        self._redo.clear()
        self._cache = (self._key(), derivation, after)

    def save_settings(self) -> List[Path]:
        """Write the graph's SETTINGS file alone — what a settings sheet saves after each change (``?only=settings``):
        a change of the graph itself the editor holds stays held, unsaved (user pick 2026-10-05; measured before, the
        sheet's Save wrote the editor tab's unsaved change too). Refused like :meth:`save`, writing nothing, over a
        settings file changed on disk since the editor read it, or one the disk will not take. Answers the file written
        (resolved), ``[]`` when the settings hold nothing unsaved."""
        key = self.settings_path.resolve()
        text = self._texts.get(key)
        if text is None or self._saved.get(key) == text:
            return []
        with _DISK_LOCK:
            self._refuse_changed_on_disk([key])
            self._refuse_unwritable([key])
            self._write(key, text)
            self._saved[key] = text
        return [key]

    def save(self) -> List[Path]:
        """Write every file whose text changed, and the sidecar when positions or groups were set; answer the DOCUMENT
        files written (resolved, in the order written — the graph's own last; never the sidecar), ``[]`` when only
        the sidecar was — what a host that mounts the editor is told (``streamstudio.app.EditorHost.saved``).

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
        written: List[Path] = []
        with _DISK_LOCK:
            self._refuse_changed_on_disk(pending)
            self._refuse_unwritable(pending)
            for key in sorted(pending, key=lambda key: key == main):  # the graph's own file last (False sorts first)
                self._write(key, pending[key])
                self._saved[key] = pending[key]
                written.append(key)
        if self._positions is not None or self._groups is not None:
            layout = _layout()
            kept = self.positions if self._positions is not None else layout.read_sidecar(self.sidecar_path)
            layout.write_sidecar(self.sidecar_path, kept, groups=self.groups)
        self._layout_dirty = False
        return written

    def save_as(self, target: Path, *, overwrite: bool = False) -> List[Path]:
        """Write the held document to ``target`` and make ``target`` this document's file from now on; answer the
        document files written, resolved and in the order written (the changed included files, then ``target``).

        The old file is left as it was. A relative ``include:`` is rewritten so it still names the same
        file from the new place (:func:`rebase_includes`) — in the held text and in every history entry,
        so undo keeps walking the edits made before. The new sidecar holds the positions and groups the
        editor shows when any were set, else a byte copy of the old sidecar. A pending edit of an INCLUDED
        file is written to that file, under the same changed-on-disk and cannot-be-written refusals as
        :meth:`save`, and before ``target`` — a new graph never names a steps file that was not written. Refused
        when ``target`` exists and ``overwrite`` is not asked.

        The graph's SETTINGS go with it: a settings text the editor holds is written beside ``target``
        (``<target stem>.settings.yaml``, before ``target``) and is that graph's from now on — refused, like
        ``target``, when a file is already there and ``overwrite`` is not asked. The old settings file stays as it was.
        """
        target = Path(target)
        old_main, new_main = self.path.resolve(), target.resolve()
        if new_main == old_main:
            return self.save()
        if target.exists() and not overwrite:
            raise ValueError(f"{target} already exists — pass overwrite: true to replace it")
        old_settings, new_settings = self.settings_path.resolve(), settings_path_of(target).resolve()
        carried = [new_settings] if old_settings in self._texts else []
        if carried and new_settings.exists() and not overwrite:
            raise ValueError(f"{new_settings} already exists — pass overwrite: true to replace it")
        others = {
            key: text
            for key, text in self._texts.items()
            if key not in (old_main, old_settings) and self._saved.get(key) != text
        }
        with _DISK_LOCK:
            self._refuse_changed_on_disk(others)
            self._refuse_unwritable([*others, *carried, target])
            self._save_as_write(target, old_main, new_main, others)
        return [*others, *carried, new_main]

    def _save_as_write(self, target: Path, old_main: Path, new_main: Path, others: _Texts) -> None:
        """The writes of :meth:`save_as`, once nothing is refused (under :data:`_DISK_LOCK`)."""
        editor_files = frozenset(self._texts)  # a steps file held but not saved yet still counts as beside it
        old_settings, new_settings = self.settings_path.resolve(), settings_path_of(target).resolve()

        def moved(texts: _Texts) -> _Texts:
            out = {key: text for key, text in texts.items() if key not in (old_main, old_settings)}
            if old_main in texts:
                out[new_main] = rebase_includes(texts[old_main], old_main.parent, new_main.parent, editor_files)
            if old_settings in texts:
                out[new_settings] = texts[old_settings]
            return out

        held = moved(self._texts)
        old_sidecar, new_sidecar = self.sidecar_path, target.with_suffix(".layout.yaml")
        blocks = (self.positions, self.groups) if self._positions is not None or self._groups is not None else None
        for key, text in others.items():  # the included files first: a write cut short never leaves a graph naming them
            self._write(key, text)
        if new_settings in held:
            self._write(new_settings, held[new_settings])
        self._write(target, held[new_main])
        if blocks is not None:
            _layout().write_sidecar(new_sidecar, blocks[0], groups=blocks[1])
        elif old_sidecar.is_file():
            new_sidecar.write_bytes(old_sidecar.read_bytes())
        self._texts = held
        kept = {k: t for k, t in self._saved.items() if k not in (old_main, old_settings)}
        carried = {new_settings: held[new_settings]} if new_settings in held else {}
        self._saved = {**kept, **others, **carried, new_main: held[new_main]}
        self._history = [_Entry(moved(entry.texts), entry.renames, entry.members) for entry in self._history]
        self._redo = [_Entry(moved(entry.texts), entry.renames, entry.members) for entry in self._redo]
        self.path = target
        self._cache = None
        self._layout_dirty = False

    def moved(self, old: Path, new: Path) -> None:
        """The folder ``old`` holding this document's files is now ``new`` — a host moved (or copied) its folder and
        the editor serves the new one (``streamstudio.app.Studio.graph_dir``). Every path under ``old`` — the graph's
        own, every text held or read, what each was last saved as, every undo and redo entry's — becomes the same
        path under ``new``; nothing is written, the unsaved edits and the history stay, and a Save writes into
        ``new``. A file outside ``old`` (an include from another folder) keeps its path.

        ``old`` is compared as given and resolved, like the recovery file's rebase: a temporary folder is often named
        through a symbolic link (``/var/…`` for ``/private/var/…``) while the texts are keyed by resolved paths.

        A RELATIVE ``include:`` in a text the editor holds is rewritten so it still names its file
        (:func:`follow_includes` — one out of the moved folder changes, one inside it does not), in every history entry
        too, as Save As does; a graph whose include changed is unsaved afterwards, since the file on disk still names
        the old way.
        """

        held = frozenset(rebase_path(key, old, new) for key in self._texts)

        def move(texts: _Texts) -> _Texts:
            return {rebase_path(key, old, new): text for key, text in texts.items()}

        def follow(texts: _Texts) -> _Texts:
            moved: _Texts = {}
            for key, text in texts.items():
                after = rebase_path(key, old, new)
                moved[after] = follow_includes(text, key, after, old, new, held)
            return moved

        self.path = rebase_path(self.path, old, new)
        # What the disk holds moved as it was (the baseline Save checks against); what the editor holds follows its
        # includes, so a graph that named a file out of the old folder still names it — and is unsaved until Save.
        self._texts, self._saved, self._read = follow(self._texts), move(self._saved), move(self._read)
        self._history = [entry._replace(texts=follow(entry.texts)) for entry in self._history]
        self._redo = [entry._replace(texts=follow(entry.texts)) for entry in self._redo]
        self._cache = None

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

    @_structural
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
        allow: Collection[str] = (),
        verify: Optional[Callable[[_Derivation], None]] = None,
    ) -> None:
        """Validate a candidate text, then make it the document's — or refuse it, unwritten.

        ``allow`` are problems the edit may gain because gaining them is what it is FOR, each at most once and compared
        without its location: removing a wire into a required input leaves exactly that input open (``contract.stream
        (Stream) is required and nothing is wired into it``), and nothing else — any other problem it gains still
        refuses it. ``verify`` reads the candidate's derivation and raises to refuse it (a wire edit checks that the
        wire it wrote is drawn exactly as it was drawn).

        A file this edit changes for the FIRST time (an included steps file) becomes held here, with the text
        it was read as (:meth:`_text_of`) as its saved baseline — and that text is what it held before every
        earlier edit too, so each older history entry gets it: undoing past this edit restores the file,
        rather than falling back to the disk, which a Save may have changed since (measured before: undo
        after Save showed the saved ``RGB``, and the next Save wrote it back)."""
        self._refuse_locked(changes)
        before = self.check()
        candidate: _Texts = {**self._texts, **changes}
        derivation = derive(self.path, candidate, self._settings_in(candidate))
        after = check(derivation, self.path)
        gained = _gained(before, after)
        spare = Counter(_where_free(message) for message in allow)
        kept: List[Problem] = []
        for problem in gained:
            key = _where_free(problem.message)
            if spare[key] > 0:
                spare[key] -= 1
            else:
                kept.append(problem)
        if kept:
            where = f" at {loc}" if loc else ""
            raise ValueError(f"{what} refused{where}: " + "; ".join(problem.message for problem in kept))
        if verify is not None:
            verify(derivation)
        first_held = {key: self._read[key] for key in changes if key not in self._texts and key in self._read}
        self._hold(first_held)
        shifts: Tuple[_MemberShift, ...] = () if members is None else (members,)
        # the later members' names follow the edit (their places and groups) — and what the shift drops (a removed
        # member's) is kept with the edit, for the undo that brings the member back
        dropped = tuple(self._shift_members(*shift) for shift in shifts)
        renames: Tuple[Tuple[str, str], ...] = () if rename is None else (rename,)
        if self._joining:
            self._joined_shifts.extend(shifts)
            self._joined_dropped.extend(dropped)
            self._joined_renames.extend(renames)
        else:
            self._history.append(_Entry({**self._texts, **first_held}, renames, shifts, dropped))
            del self._history[: -self.history_depth]
            self._redo.clear()
        self._texts = candidate
        self._cache = (self._key(), derivation, after)

    def _refuse_locked(self, changes: Mapping[Path, str]) -> None:
        """Refuse every change but the settings file's of a graph that offers its settings only
        (``app: {graph: settings-only}``, :data:`streamstudio.settings.LOCKED`) — before anything is held."""
        if self.mode != SETTINGS_ONLY:
            return
        settings = self.settings_path.resolve()
        if any(key != settings and text != self._texts.get(key) for key, text in changes.items()):
            raise ValueError(LOCKED.format(name=self.path.stem))

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
        shifts and the renames the joined commits made (a removal of several list members; of several wires, a card
        moved out of a slot for one) are that entry's, in order; a refusal puts the positions and groups they moved
        back too."""
        texts, saved, cache = dict(self._texts), dict(self._saved), self._cache
        history, redo = list(self._history), list(self._redo)
        layout_held = (self._positions, self._positions_whole, self._groups)
        self._joining = True
        self._joined_shifts = []
        self._joined_dropped = []
        self._joined_renames = []
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
            _Entry(
                {**texts, **first_held},
                tuple(self._joined_renames),
                tuple(self._joined_shifts),
                tuple(self._joined_dropped),
            )
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
    "CARD_INSIDE_A_SUBGRAPH",
    "CARD_IN_A_FLOW",
    "CARD_IS_AN_OP",
    "CARD_ONE_OBJECT",
    "CARD_WIRE_ENDS",
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
    "WIRE_CHANGES_ORDER",
    "WIRE_ENTRY_ONLY",
    "WIRE_INSIDE_A_SUBGRAPH",
    "WIRE_IN_A_FLOW",
    "WIRE_NOT_YET",
    "WIRE_ONE_OBJECT",
    "Wire",
    "WireEnds",
    "WireKind",
    "check",
    "derive",
    "rebase_includes",
    "unreadable_reason",
]
