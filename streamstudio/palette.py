"""The palette: every configurable class the registry knows, described as a typed node — derived, never written.

The canvas, the search palette an author types into, the tool schemas an LLM reads and the model a
document derives all need the same answer about a class: what KIND of node it is, which parameters it
takes and how each is drawn, which sockets it has and what may be wired into them. Writing that answer
per class — a node file, a manifest, a socket table — is what drifted every time a constructor gained
a parameter (``docs/rewrite.md`` §2.1). So nothing here is written per class. Everything is read off
declarations that already exist for other reasons, and each reading names its source:

* the **kind** comes from the class's shape — a ``recordstream.Algorithm`` subclass, a ``GraphContract``,
  else the ``@configurable`` category (``op`` / ``source`` / ``engine`` / ``sink`` / ``value`` /
  ``contract``), else a no-arg ``run()`` (a runnable) or a no-arg ``__call__`` (a value producer);
  anything left is an object a runnable's slot takes — a model, a loss, a tracker — which is drawn
  like a value producer: no record flows through it, it only offers its ``object`` output;
* the **parameter widgets** are ``confluid.to_pydantic(cls).model_json_schema()``: an ``Interval`` mark
  is already ``minimum``/``maximum`` there and a ``Literal`` already an ``enum``, so a form, a tool
  schema and this palette cannot disagree about a range or an option list;
* the **entry sockets** are the declarations the chain checker reads — ``algorithm_spec`` for an
  Algorithm's ``Input``/``Output`` slots, ``consumes`` / ``produces`` / ``flags`` / ``reports`` for any
  other op. An op that declares TYPES rather than entry names (``consumes = (Mask,)``, the
  ``Transform`` spelling) gets no entry socket: the palette records the types and marks the node
  ``tuple_form``, and the run shows the truth (``docs/rewrite.md`` §5);
* the **picker** a parameter gets is read off its name, its annotation and its class
  (:func:`picker_of`): a folder or a file for a path-named
  parameter, the HuggingFace pickers on a ``HuggingFace*`` source, or what a producer stamps on its class;
* what a constructor parameter can be **wired from** is read off its annotation alone
  (:func:`socket_type`): a registered item class names its own socket, a ``Stream`` is ``Stream``, an
  ``Iterable`` is a ``Source`` of records, ``Any`` meets anything, another class is an ``object``; a
  scalar, a mapping, a ``Literal`` or a list of strings is a typed parameter, not a socket. The one
  containment beyond identity is that a ``Stream`` may be wired where a ``Source`` is asked for — a
  Stream is iterable — and an item class where its base class is asked for.

A class whose introspection fails is still listed: kind ``op``, no sockets, and the reason in
``problem``. The registry today holds one such class (a ``DataLoader`` whose constructor names an
``Iterable`` it never imported); dropping it would hide a real defect, and raising would empty the
palette for everyone else.
The full entry-point scan is the expensive part — measured 2026-10-05 at load 23: 12 s for the imports of 134 entry
points, 6 s to build the settings of 825 classes — so the palette is built once per process and
``palette(refresh=True)`` is the only thing that rebuilds it. The editor's server never runs the scan itself: its
catalogue is built by a helper process (:mod:`streamstudio.catalogue`), from :func:`registered` and :func:`describe`
— the list without settings (:func:`listed`), then every class with them.

This module reads recordstream and confluid only; the document, the app and the canvas build on it.
"""

import collections.abc
import difflib
import inspect
import types
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import PurePath
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Tuple,
    TypedDict,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from confluid import get_registry, load_configurables, marks, output_specs, parse_param_docs, to_pydantic
from confluid.introspect import NO_DEFAULT, slots
from confluid.mandatory import is_mandatory_annotation
from confluid.registry import resolve_class
from recordstream.algorithm import Algorithm, algorithm_spec
from recordstream.core.stream import JointStream, Stream
from recordstream.flow import FlowGraph
from recordstream.items import NDArrayItem, item_types
from recordstream.ops.contract import ANY_TYPE, GraphContract

from streamstudio import lookups

#: A socket's type: a registered item class name (``Image``, ``Spectrogram``), one of the graph words
#: (``Record``, ``Stream``, ``Source``, ``Op``, ``Sink``), a JSON-schema scalar for a declared value
#: (``number``, ``integer``, ``string``, ``boolean``, ``object``, ``List[str]``) or the ``*`` wildcard.
SocketType = str

#: What a node IS. ``host`` and ``alias`` are document-level kinds (the entries a host provides to a graph
#: without a contract; a ``!ref`` to another node) and never come out of the palette.
NodeKind = Literal["source", "op", "algorithm", "engine", "sink", "runnable", "value", "contract", "host", "alias"]
NODE_KINDS: Tuple[str, ...] = get_args(NodeKind)

RECORD: SocketType = "Record"
STREAM: SocketType = "Stream"
SOURCE: SocketType = "Source"
OP: SocketType = "Op"
SINK: SocketType = "Sink"
OBJECT: SocketType = "object"
NUMBER: SocketType = "number"
INTEGER: SocketType = "integer"
STRING: SocketType = "string"
BOOLEAN: SocketType = "boolean"
LIST_OF_STR: SocketType = "List[str]"
ANY: SocketType = ANY_TYPE

#: The ``@configurable`` categories that name a node kind outright.
_CATEGORY_KINDS: Dict[str, NodeKind] = {
    "op": "op",
    "source": "source",
    "engine": "engine",
    "sink": "sink",
    "value": "value",
    "contract": "contract",
}
#: Classes a parameter annotation names when it takes a stream — recordstream's three engines.
_STREAM_CLASSES: Tuple[type, ...] = (Stream, JointStream, FlowGraph)
#: Annotation origins that mean "a sequence of records" — what a ``Stream.source`` is declared as.
_SOURCE_ORIGINS: Tuple[type, ...] = (collections.abc.Iterable, collections.abc.Iterator, collections.abc.Generator)
#: Annotation origins that hold ONE kind of element; the element decides the socket.
_ELEMENT_ORIGINS: Tuple[type, ...] = (
    list,
    tuple,
    set,
    frozenset,
    collections.abc.Sequence,
    collections.abc.MutableSequence,
    collections.abc.Set,
)
#: Annotation origins that are typed parameters, never sockets.
_MAPPING_ORIGINS: Tuple[type, ...] = (dict, collections.abc.Mapping, collections.abc.MutableMapping)
#: Scalar classes that are typed parameters, never sockets.
_SCALARS: Tuple[type, ...] = (str, bytes, int, float, bool, complex)
#: The JSON-schema word for a declared scalar value (``bool`` before ``int``: a bool IS an int).
_SCALAR_SOCKETS: Tuple[Tuple[type, SocketType], ...] = (
    (bool, BOOLEAN),
    (int, INTEGER),
    (float, NUMBER),
    (str, STRING),
)
#: A graph-contract slot kind (``recordstream.ops.contract.GraphSlotKind``) whose socket word differs.
_SLOT_KIND_SOCKETS: Dict[str, SocketType] = {"float": NUMBER, "int": INTEGER, "str": STRING, "bool": BOOLEAN}
#: Containment beyond identity and item subclassing: a Stream is iterable, so it serves as a Source.
_WIDER: Dict[SocketType, Tuple[SocketType, ...]] = {STREAM: (SOURCE,)}

#: The pickers the canvas page renders — a parameter's JSON-schema fragment carries one as ``picker``
#: (:func:`picker_of`): a folder chooser, a file chooser, and the HuggingFace lookups — the datasets this machine has
#: cached, the columns and the splits of the dataset the node names, and the columns again for a list of them.
PickerKind = Literal["directory", "file", "hf_dataset", "hf_feature", "hf_split", "hf_metadata"]
PICKER_KINDS: Tuple[str, ...] = get_args(PickerKind)
#: A parameter NAME holding one of these words is a path (a deliberate affordance on the name, not a type inference:
#: a ``str`` named ``output_dir`` should get the chooser)…
_PATH_WORDS = ("path", "dir", "root", "folder")
#: … a folder for certain when it holds one of these — they beat a file word (``checkpoint_dir`` is a folder) …
_FOLDER_WORDS = ("dir", "root", "folder")
#: … and one file when it holds one of these and no folder word (``checkpoint_path``, ``log_file``).
_FILE_WORDS = ("file", "checkpoint", "ckpt")
#: The parameters of a HuggingFace dataset source (a class whose name starts ``HuggingFace``) that name a hub dataset,
#: one of its columns, its split, a list of its columns — ``path`` is a hub id here, never a folder.
_HF_CLASS_PREFIX = "HuggingFace"
_HF_PICKERS: Dict[str, PickerKind] = {
    "path": "hf_dataset",
    "repo_id": "hf_dataset",
    "input_feature": "hf_feature",
    "target_feature": "hf_feature",
    "split": "hf_split",
    "metadata_features": "hf_metadata",
}
#: The class attribute a producing package stamps to name a parameter's picker itself (``{param: tag}``) — the tag is
#: the producer's; the page renders the pickers it knows and a plain field for any other.
_WIDGET_STAMP = "__taidal_widget_params__"


class SocketDict(TypedDict):
    """One socket of a node spec — the JSON shape the canvas and the tools consume."""

    name: str
    type: SocketType
    required: bool
    doc: str


@dataclass(frozen=True)
class NodeSpec:
    """One class as a node: its kind, its parameter widgets, its sockets and what it declares.

    Args:
        class_path: The dotted ``module.QualName`` — how a document spells the class (``!class:<path>``).
        name: The registry key (bare while unambiguous), or the class name when it is not registered.
        category: The ``@configurable`` category, ``""`` when untagged.
        group: The ``@configurable`` group, ``""`` when untagged.
        kind: What the node is — see :data:`NodeKind`.
        params_schema: The JSON-schema ``properties`` of the constructor, from ``confluid.to_pydantic`` — a
            parameter that gets a picker carries it as ``picker`` (:func:`picker_of`).
        params_defs: The ``$defs`` those properties refer to (a nested config's schema), ``{}`` when none.
        inputs: What can be wired INTO the node, in declaration order.
        outputs: What the node offers, in declaration order.
        consumes: ``{record entry: item type}`` the node needs — the by-name declaration.
        produces: ``{record entry: item type}`` the node writes — the by-name declaration.
        consumes_types: The item types a TYPES-ONLY declaration names as needed (the tuple spelling).
        produces_types: The item types a TYPES-ONLY declaration names as written.
        flags: The boolean findings the node raises — each is a record outcome output.
        reports: The report names the node writes under — each is a ``report:<name>`` output.
        doc: The first paragraph of the class docstring, on one line.
        tuple_form: True when the node declares types rather than entry names, so its needs cannot be
            checked by name before a run.
        problem: Why the class could not be introspected, ``""`` when it could.
    """

    class_path: str
    name: str
    category: str
    group: str
    kind: NodeKind
    params_schema: Dict[str, Any] = field(default_factory=dict)
    params_defs: Dict[str, Any] = field(default_factory=dict)
    inputs: List[SocketDict] = field(default_factory=list)
    outputs: List[SocketDict] = field(default_factory=list)
    consumes: Dict[str, str] = field(default_factory=dict)
    produces: Dict[str, str] = field(default_factory=dict)
    consumes_types: List[str] = field(default_factory=list)
    produces_types: List[str] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)
    reports: List[str] = field(default_factory=list)
    doc: str = ""
    tuple_form: bool = False
    problem: str = ""


# ----------------------------------------------------------------------------------------------
# type -> socket
# ----------------------------------------------------------------------------------------------
def _is_union(annotation: Any) -> bool:
    return get_origin(annotation) in (Union, types.UnionType)


def _strip(annotation: Any) -> Any:
    """``Annotated[X, ...]`` -> ``X``; ``Optional[X]`` -> ``X``; any other annotation unchanged."""
    while hasattr(annotation, "__metadata__"):
        annotation = annotation.__origin__
    if _is_union(annotation):
        members = [member for member in get_args(annotation) if member is not type(None)]
        if len(members) == 1:
            return _strip(members[0])
    return annotation


def _declared_types() -> Dict[str, type]:
    """The item vocabulary by name: every registered item type, plus ``NDArrayItem``, the array-item base.

    ``NDArrayItem`` is not registered (nothing is stored AS one) but it is what the shipped array ops
    declare — ``Threshold.consumes = (NDArrayItem,)`` means "any array item" — so a socket typed by it
    must meet an ``Image`` or a ``Mask``, which are its subclasses. Read live, because item types are
    registered by whichever packages are imported.
    """
    return {**{klass.__name__: klass for klass in item_types()}, NDArrayItem.__name__: NDArrayItem}


def _item_class_name(cls: type) -> Optional[str]:
    """The declared item type ``cls`` is (itself, or its nearest base in the vocabulary), or ``None``."""
    known = set(_declared_types().values())
    return next((klass.__name__ for klass in cls.__mro__ if klass in known), None)


def socket_type(annotation: Any) -> Optional[SocketType]:
    """What a constructor parameter with this annotation can be WIRED from — ``None`` for a typed parameter.

    Read off the annotation alone, so the rule is the same for every class and needs no per-class
    table: a registered item class names its socket; ``Stream`` / ``JointStream`` / ``FlowGraph`` are
    ``Stream``; an ``Iterable`` of records is a ``Source``; ``Any`` (and an unannotated parameter) is
    the wildcard; a list takes its element's answer, so ``List[Stream]`` is ``Stream`` and ``List[Any]``
    is ``*``; any other class is an ``object`` — a model, a loss, a callable. Scalars, mappings,
    ``Literal`` choices and lists of scalars are parameters an author types, hence ``None``. A union of
    several answers is ambiguous and stays a parameter.
    """
    plain = _strip(annotation)
    if plain is Any or plain is inspect.Parameter.empty or plain is object:
        return ANY
    origin = get_origin(plain)
    if origin is Literal:
        return None
    if _is_union(plain):
        answers = {socket_type(member) for member in get_args(plain) if member is not type(None)}
        return answers.pop() if len(answers) == 1 else None
    if origin in _ELEMENT_ORIGINS:
        members = get_args(plain)
        return socket_type(members[0]) if members else ANY
    if origin in _SOURCE_ORIGINS:
        return SOURCE
    if origin in _MAPPING_ORIGINS:
        return None
    if origin is collections.abc.Callable:
        return OBJECT
    if origin is not None:
        plain = origin
    if not isinstance(plain, type):
        return None
    if plain in _SOURCE_ORIGINS:
        return SOURCE
    if plain in _ELEMENT_ORIGINS:
        return ANY
    if plain in _MAPPING_ORIGINS or issubclass(plain, _SCALARS):
        return None
    if issubclass(plain, _STREAM_CLASSES):
        return STREAM
    item = _item_class_name(plain)
    if item is not None:
        return item
    if plain.__module__ == "builtins":
        return None
    return OBJECT


def _built_socket(target: Callable[..., Any]) -> SocketType:
    """What a value node's ``object`` output carries: the scalar its class IS (a ``float`` subclass is a ``number``, and
    meets a number setting — user pick 2026-10-09: any node whose result is a number), else ``object``. The document
    model's ``_socket_of_built`` is the same rule."""
    if inspect.isclass(target):
        for scalar, word in _SCALAR_SOCKETS:  # bool before int: a bool IS an int
            if issubclass(target, scalar):
                return word
    return OBJECT


def _arms(annotation: Any) -> List[Any]:
    """The members of a union (``Optional`` / ``Union`` / ``X | Y``), each with ``Annotated`` taken off; ``[X]``
    for anything else."""
    while hasattr(annotation, "__metadata__"):
        annotation = annotation.__origin__
    if _is_union(annotation):
        return [arm for member in get_args(annotation) for arm in _arms(member)]
    return [annotation]


def picker_of(target: Callable[..., Any], name: str, annotation: Any) -> Optional[str]:  # any annotation form
    """The picker the parameter ``name`` of ``target`` gets — a :data:`PickerKind`, a producer's stamped tag, or
    ``None`` for a plain field. The rules, in their order:

    1. a ``__taidal_widget_params__`` stamp naming the parameter wins (``{"output_dir": "PLATFORM_DATASET_ID"}``);
    2. on a ``HuggingFace*`` class: ``path`` / ``repo_id`` → ``hf_dataset``, ``input_feature`` / ``target_feature`` →
       ``hf_feature``, ``split`` → ``hf_split``, ``metadata_features`` → ``hf_metadata``;
    3. a ``Literal`` gets none — its choices are already a list — nor does a bool / int / float parameter
       (``checkpoint_strict``: a chooser would land on a switch);
    4. a name holding ``file`` / ``checkpoint`` / ``ckpt`` and no folder word → ``file``; a name holding ``path`` /
       ``dir`` / ``root`` / ``folder``, or a ``pathlib.Path`` annotation → ``directory``.
    """
    stamped = getattr(target, _WIDGET_STAMP, None)
    if isinstance(stamped, dict) and name in stamped:
        return str(stamped[name])
    if inspect.isclass(target) and target.__name__.startswith(_HF_CLASS_PREFIX) and name in _HF_PICKERS:
        return _HF_PICKERS[name]
    arms = [arm for arm in _arms(annotation) if arm is not type(None)]
    if any(get_origin(arm) is Literal for arm in arms):
        return None
    if any(arm in (bool, int, float) for arm in arms) and str not in arms:
        return None
    lowered = name.lower()
    if any(word in lowered for word in _FILE_WORDS) and not any(word in lowered for word in _FOLDER_WORDS):
        return "file"
    if any(isinstance(arm, type) and issubclass(arm, PurePath) for arm in arms):
        return "directory"
    if any(word in lowered for word in _PATH_WORDS):
        return "directory"
    return None


def _mark_pickers(target: Callable[..., Any], properties: Dict[str, Any]) -> Dict[str, Any]:  # JSON-schema properties
    """``properties`` with ``picker`` added to the fragment of every parameter :func:`picker_of` gives one — and
    ``lookup: true`` beside a producer's own tag that an installed plugin supplies a list for
    (:mod:`streamstudio.lookups`): the page draws that list, where a tag it does not know otherwise stays a plain
    field."""
    for slot in slots(target):
        fragment = properties.get(slot.name)
        if not isinstance(fragment, dict):
            continue
        picker = picker_of(target, slot.name, slot.annotation)
        if picker is not None:
            marked = {**fragment, "picker": picker}
            if picker not in PICKER_KINDS and lookups.lists(picker):
                marked["lookup"] = True
            properties[slot.name] = marked
    return properties


def value_socket_type(annotation: Any) -> SocketType:
    """The socket type of a DECLARED value — an Algorithm slot, an ``@output`` — which always has one.

    A wireable annotation answers as :func:`socket_type` does; a scalar takes its JSON-schema word,
    a mapping is ``object``, a list of strings is ``List[str]`` (the contract vocabulary's spelling);
    anything else is the wildcard.
    """
    wired = socket_type(annotation)
    if wired is not None:
        return wired
    plain = _strip(annotation)
    origin = get_origin(plain)
    if origin in _MAPPING_ORIGINS or plain is dict:
        return OBJECT
    if origin in _ELEMENT_ORIGINS:
        members = get_args(plain)
        return LIST_OF_STR if members and members[0] is str else ANY
    if isinstance(plain, type):
        for scalar, word in _SCALAR_SOCKETS:
            if issubclass(plain, scalar):
                return word
    return ANY


def slot_socket_type(kind: str) -> SocketType:
    """A graph-contract slot kind as a socket type: the four scalar kinds take their JSON words, the rest stay."""
    return _SLOT_KIND_SOCKETS.get(kind, kind)


def _item_or_any(annotation: Any) -> str:
    """The chain checker's vocabulary for a declared slot: the registered item type name, else ``*``."""
    plain = _strip(annotation)
    if isinstance(plain, type):
        item = _item_class_name(plain)
        if item is not None:
            return item
    return ANY


def types_meet(source: SocketType, target: SocketType) -> bool:
    """Whether a wire from a ``source`` socket may enter a ``target`` socket.

    Identical types meet; the wildcard meets everything; a ``Stream`` serves where a ``Source`` is asked
    for (a Stream is iterable); an item class serves where one of its bases is asked for (an ``Image``
    is an ``NDArrayItem``, never the reverse). Nothing else meets — ``number`` into ``integer`` is refused.
    """
    if source == target or ANY in (source, target):
        return True
    if target in _WIDER.get(source, ()):
        return True
    by_name = _declared_types()
    wider, narrower = by_name.get(target), by_name.get(source)
    return wider is not None and narrower is not None and issubclass(narrower, wider)


# ----------------------------------------------------------------------------------------------
# kind
# ----------------------------------------------------------------------------------------------
def _own_method(target: type, name: str) -> Optional[Callable[..., Any]]:
    """The method ``name`` a class or one of its bases DEFINES — never the metaclass's (``type.__call__``)."""
    for klass in target.__mro__:
        if klass is object:
            continue
        member = vars(klass).get(name)
        if member is not None:
            return member if callable(member) else None
    return None


def _takes_nothing(method: Optional[Callable[..., Any]]) -> bool:
    if method is None:
        return False
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return False
    return not [name for name in signature.parameters if name not in ("self", "cls")]


def kind_of(target: Callable[..., Any]) -> NodeKind:
    """The node kind of a class or builder function — see the module docstring for the order of the rules."""
    if inspect.isclass(target):
        if issubclass(target, GraphContract):
            return "contract"
        if issubclass(target, Algorithm):
            return "algorithm"
    category = marks(target).category
    if category in _CATEGORY_KINDS:
        return _CATEGORY_KINDS[category]
    if inspect.isclass(target):
        if _takes_nothing(_own_method(target, "run")):
            return "runnable"
        if _takes_nothing(_own_method(target, "__call__")):
            return "value"
    return "value"


# ----------------------------------------------------------------------------------------------
# describing one class
# ----------------------------------------------------------------------------------------------
def class_path_of(target: Any) -> str:
    """How a document spells ``target``: ``module.QualName`` (``!class:<path>``), or its repr when it has neither."""
    module = getattr(target, "__module__", None)
    qualname = getattr(target, "__qualname__", None) or getattr(target, "__name__", None)
    return f"{module}.{qualname}" if module and qualname else repr(target)


def _summary(target: Any) -> str:
    """The first paragraph of the docstring, on one line — what a palette entry shows under its name."""
    text = inspect.getdoc(target) or ""
    first = text.strip().split("\n\n", 1)[0]
    return " ".join(first.split())


def _schema(target: Callable[..., Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """``(properties, $defs)`` of the constructor's JSON schema.

    pydantic warns about a default it cannot serialise (a function, an enum member); the palette is not
    the place to act on that, and one such warning per rebuild of 441 classes is noise, so its schema
    warnings are muted — by the module they come from, so this file does not import pydantic itself.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", module=r"pydantic\.json_schema")
        schema = to_pydantic(target).model_json_schema()
    return dict(schema.get("properties", {})), dict(schema.get("$defs", {}))


def _socket(name: str, kind: SocketType, required: bool = False, doc: str = "") -> SocketDict:
    return {"name": name, "type": kind, "required": required, "doc": doc}


def _param_docs(target: Callable[..., Any]) -> Dict[str, str]:
    try:
        return parse_param_docs(target)
    except Exception:  # a docstring the Args: parser cannot read is a lost help text, not a lost node
        return {}


def _wired_inputs(target: Callable[..., Any]) -> List[SocketDict]:
    """The constructor parameters something can be wired into, in signature order."""
    docs = _param_docs(target)
    wired: List[SocketDict] = []
    for slot in slots(target):
        if slot.kind not in ("positional_only", "keyword"):
            continue
        kind = socket_type(slot.annotation)
        if kind is None:
            continue
        required = slot.default is NO_DEFAULT or is_mandatory_annotation(slot.annotation)
        wired.append(_socket(slot.name, kind, required, docs.get(slot.name, "")))
    return wired


def _declared_outputs(target: Callable[..., Any]) -> List[SocketDict]:
    """One socket per ``@output`` property, typed by the getter's return annotation."""
    declared: List[SocketDict] = []
    for spec in output_specs(target):  # type: ignore[arg-type]  # output_specs walks __mro__ when there is one
        member = getattr(target, spec["name"], None)
        getter = member.fget if isinstance(member, property) else None
        annotation: Any = Any
        if getter is not None:
            try:
                annotation = get_type_hints(getter).get("return", Any)
            except Exception:  # an unresolvable return annotation: the socket survives as the wildcard
                annotation = Any
        declared.append(_socket(spec["name"], value_socket_type(annotation), False, spec["description"]))
    return declared


def _type_names(declared: Any) -> List[str]:
    """The tuple spelling of ``consumes`` / ``produces`` / ``handles`` as type names; ``[]`` for anything else."""
    if isinstance(declared, (tuple, list)):
        return [getattr(member, "__name__", str(member)) for member in declared]
    return []


class _OpDeclarations:
    """What a plain op declares as class attributes — read once, without building the op."""

    def __init__(self, target: Callable[..., Any]) -> None:
        consumes = getattr(target, "consumes", None)
        produces = getattr(target, "produces", None)
        self.consumes: Dict[str, str] = (
            {str(key): str(value) for key, value in consumes.items()} if isinstance(consumes, dict) else {}
        )
        self.produces: Dict[str, str] = (
            {str(key): str(value) for key, value in produces.items()} if isinstance(produces, dict) else {}
        )
        # ``Transform``: an empty ``consumes`` means "the same types as ``handles``" (recordstream.transform).
        self.consumes_types: List[str] = _type_names(consumes) or _type_names(getattr(target, "handles", None))
        self.produces_types: List[str] = _type_names(produces)
        flags = getattr(target, "flags", None)
        self.flags: List[str] = [str(flag) for flag in flags] if isinstance(flags, (tuple, list)) else []
        reports = getattr(target, "reports", None)
        self.reports: List[str] = [str(reports)] if isinstance(reports, str) and reports else []

    @property
    def tuple_form(self) -> bool:
        return bool(self.consumes_types or self.produces_types)


def _record_in() -> SocketDict:
    return _socket("record", RECORD, True, "the record flowing through the chain")


def _record_out() -> SocketDict:
    return _socket("record", RECORD, False, "the record after this node")


def _op_out() -> SocketDict:
    return _socket("op", OP, False, "this node as an op — what a Stream's ops list holds")


def _check_settings(target: Callable[..., Any]) -> None:
    """Raise what building the settings would raise, without building them — for the list, which leaves them out.

    Building them (``confluid.to_pydantic``) refuses a constructor whose signature or annotations cannot be read —
    torch's ``DataLoader`` names an ``Iterable`` it never imported — and that refusal is the class's ``problem``.
    confluid makes that check first, cheaply (``inspect.signature`` and ``get_type_hints`` of the constructor); it is
    repeated here, and only when it fails is ``to_pydantic`` called, so the problem is in confluid's own words.
    Measured 2026-10-05 over 825 classes: 1.8 s with this check, 6.5 s with ``to_pydantic`` for every class (both at
    load ~70). That the list and the full description agree is pinned over every installed class
    (``tests/test_catalogue.py``).
    """
    if inspect.isclass(target):
        function = target.__dict__.get("__init__") or target.__init__  # type: ignore[misc]  # mypy: no __init__ on type
    else:
        function = target
    if function is object.__init__:
        return
    try:
        inspect.signature(function)
        get_type_hints(function, include_extras=True)
    except (TypeError, ValueError, NameError):
        to_pydantic(target)


def describe(target: Callable[..., Any], *, settings: bool = True) -> NodeSpec:
    """The :class:`NodeSpec` of one class or builder function. Never raises: a failure is the spec's ``problem``.

    Args:
        target: The class or builder function.
        settings: Build the constructor's settings (``params_schema``, ``params_defs``). ``False`` leaves them empty —
            what the catalogue's list carries (:func:`listed`): building them is most of a description's cost (6 s of
            6.5 s for 825 classes, measured 2026-10-05), and a class's settings are asked for one class at a time.
    """
    class_path = class_path_of(target)
    tags = marks(target)
    common = dict(
        class_path=class_path,
        name=get_registry().key_for(target) or getattr(target, "__name__", class_path),
        category=tags.category or "",
        group=tags.group or "",
        doc=_summary(target),
    )
    try:
        kind = kind_of(target)
        properties: Dict[str, Any] = {}
        defs: Dict[str, Any] = {}
        if settings:
            properties, defs = _schema(target)
            properties = _mark_pickers(target, properties)
        else:
            _check_settings(target)
        wired = _wired_inputs(target)
        inputs: List[SocketDict] = []
        outputs: List[SocketDict] = []
        consumes: Dict[str, str] = {}
        produces: Dict[str, str] = {}
        declared = _OpDeclarations(target)
        if kind == "algorithm":
            spec = algorithm_spec(target)  # type: ignore[arg-type]  # kind_of has established the subclass
            inputs = [_record_in(), *wired]
            inputs += [_socket(s.name, value_socket_type(s.annotation), s.required, s.doc) for s in spec.inputs]
            outputs = [_record_out(), _op_out()]
            outputs += [_socket(s.name, value_socket_type(s.annotation), False, s.doc) for s in spec.outputs]
            consumes = {s.name: _item_or_any(s.annotation) for s in spec.inputs if s.required}
            produces = {s.name: _item_or_any(s.annotation) for s in spec.outputs}
            declared = _OpDeclarations(object)  # the slots ARE the declaration; the properties are not read
        elif kind == "op":
            consumes, produces = declared.consumes, declared.produces
            inputs = [_record_in(), *wired]
            inputs += [
                _socket(entry, slot_socket_type(kind_), True, f"the record entry {entry!r}")
                for entry, kind_ in consumes.items()
            ]
            outputs = [_record_out(), _op_out()]
            outputs += [
                _socket(entry, slot_socket_type(kind_), False, f"the record entry {entry!r}")
                for entry, kind_ in produces.items()
            ]
            outputs += [_socket(flag, RECORD, False, f"the record when {flag!r} is raised") for flag in declared.flags]
            outputs += [
                _socket(f"report:{name}", OBJECT, False, f"what this node reports under {name!r}")
                for name in declared.reports
            ]
        elif kind == "contract":
            inputs, outputs = [], []  # a contract's sockets are its declared slots — per document, not per class
        else:
            inputs = wired
            outputs = _declared_outputs(target)
            if kind == "source":
                outputs = [_socket("stream", SOURCE, False, "the records this source yields"), *outputs]
            elif kind == "engine":
                outputs = [_socket("stream", STREAM, False, "the stream this engine yields"), *outputs]
            elif kind == "sink":
                outputs = [_socket("sink", SINK, False, "this sink — what a processor's sink slot holds"), *outputs]
            elif kind == "value":
                outputs = [*outputs, _socket("object", _built_socket(target), False, "the built object itself")]
        return NodeSpec(
            kind=kind,
            params_schema=properties,
            params_defs=defs,
            inputs=inputs,
            outputs=outputs,
            consumes=consumes,
            produces=produces,
            consumes_types=declared.consumes_types,
            produces_types=declared.produces_types,
            flags=declared.flags,
            reports=declared.reports,
            tuple_form=declared.tuple_form,
            **common,  # type: ignore[arg-type]  # the dict holds the five str fields above
        )
    except Exception as exc:  # broad on purpose: any failure of a foreign class's introspection is the problem field
        return NodeSpec(kind="op", problem=f"{type(exc).__name__}: {exc}", **common)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------------------------
# the palette
# ----------------------------------------------------------------------------------------------
_cache: Optional[List[NodeSpec]] = None
_cache_scanned = False
_index: Dict[str, NodeSpec] = {}


def _remember(spec: NodeSpec) -> None:
    _index[spec.class_path] = spec
    _index.setdefault(spec.name, spec)


#: The fields a catalogue LIST leaves out — a class's settings, asked for one class at a time (:func:`listed`).
SETTINGS_FIELDS: Tuple[str, ...] = ("params_schema", "params_defs")


def listed(spec: NodeSpec) -> Dict[str, Any]:
    """``spec`` as the catalogue's list carries it: every field but the settings (:data:`SETTINGS_FIELDS`).

    The list is what the page searches and draws entries from — names, kinds, sockets, descriptions; the settings are
    94 % of the full catalogue's 11.6 MB (measured 2026-10-05) and are needed for the one class someone selects.
    """
    return {key: value for key, value in asdict(spec).items() if key not in SETTINGS_FIELDS}


def registered(scan: bool = True) -> List[Callable[..., Any]]:
    """Every class and builder function the registry holds, in registry-key order — after importing every
    ``confluid.configurables`` entry point when ``scan`` (the expensive part of a catalogue: 12 s of 21 s measured
    2026-10-05 at load 23)."""
    if scan:
        load_configurables()
    registry = get_registry()
    targets = (registry.get_class(key) for key in sorted(registry.list_classes()))
    return [target for target in targets if target is not None]


def palette(refresh: bool = False, *, scan: bool = True) -> List[NodeSpec]:
    """Every registered class as a :class:`NodeSpec`, sorted by class path; built once, cached per process.

    Args:
        refresh: Rebuild even when a palette is cached — after a plugin was installed, say.
        scan: Import the ``confluid.configurables`` entry points first (the full installed catalogue —
            measured 6.6 s for 124 entry points). ``False`` describes what the registry already holds:
            the cheap set a test or a script that imported its own classes wants. Once a scan has run
            its imports stay, so a later call without ``scan`` sees the full set too.
    """
    global _cache, _cache_scanned
    if _cache is not None and not refresh and (_cache_scanned or not scan):
        return _cache
    specs = [describe(target) for target in registered(scan=scan)]
    specs.sort(key=lambda spec: spec.class_path)
    _index.clear()
    for spec in specs:
        _remember(spec)
    _cache = specs
    _cache_scanned = _cache_scanned or scan
    return specs


def spec_for(class_path: str) -> NodeSpec:
    """The spec of one class, by dotted path or registry key.

    Reads the cached palette (building the cheap set when nothing is cached yet — it never runs the
    entry-point scan on its own; call :func:`palette` once for that), then falls back to resolving the
    name the way a document does (``confluid.registry.resolve_class``: a registry key, else an import),
    so a class a test or a script defined is described even though nothing registered it.

    Raises:
        KeyError: The name resolves to nothing; the message names the close matches the palette holds.
    """
    if _cache is None:
        palette(scan=False)
    known = _index.get(class_path)
    if known is not None:
        return known
    target = resolve_class(class_path)
    if target is not None:
        spec = describe(target)
        _remember(spec)
        return spec
    close = difflib.get_close_matches(class_path, list(_index), n=5, cutoff=0.6)
    hint = f"did you mean {', '.join(close)}?" if close else "call palette() to scan the installed entry points"
    raise KeyError(f"no node class {class_path!r} in the palette of {len(_cache or [])} classes; {hint}")
