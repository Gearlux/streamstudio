"""A graph's SETTINGS — the values a person changed, in a file of their own beside the graph.

A graph names the parameters a person tunes without the canvas in its ``app:`` block (``[Class, param]`` pairs). The
values they change go into ``<graph>.settings.yaml`` beside the graph — never into the graph — one line each::

    flow.classify.op.line_db: 9.5        # a flow step, by its NAME (two nodes of one class are set apart)
    sink.output_dir: /data/out           # a top-level object, by its KEY
    AcfSlice.max_points: 512             # a node with no name (a member of an ops: list), by its CLASS —
                                         # only while that class is used by one node of the graph

The graph and its settings are loaded as ONE document by confluid's own ``include:`` rule — a held, never-written
document ``include: [<graph>, <settings>]``, so a setting wins over what the graph spells, and every location names
the real file (:func:`composed`). Nothing else is added to confluid: no second precedence rule, no new spelling.

A setting that names none of the graph's settings is REFUSED where it is written (:func:`read_settings`) — confluid by
itself warns and ignores an unknown key, and measured before (2026-10-05) a misspelt ``line_dbb`` loaded with a warning
while the pane ran the graph's value. So is a ``flow:`` block (confluid merges it by moving the step to the END of the
flow, which breaks the schedule — measured: ``from: 'classify' does not name an EARLIER step``) and a class block that
two nodes with no name share.

A graph may offer its settings and nothing else: ``app: {graph: settings-only}`` (:data:`GraphMode`). The editor then
refuses every other edit (:data:`LOCKED`).

This module reads YAML text and plain data only; the editor's document model calls it (``streamstudio.document``), and
:func:`load_graph` is what a host runs.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, NamedTuple, Optional, Sequence, Set, Tuple, cast, get_args

import confluid
import yaml

from streamstudio._yamltext import YamlText, spell

#: The suffix of a graph's settings file: ``view_bte.yaml`` → ``view_bte.settings.yaml``.
SETTINGS_SUFFIX = ".settings.yaml"

#: What a graph lets a person change: everything (the default), or its settings alone.
GraphMode = Literal["editable", "settings-only"]
GRAPH_MODES: Tuple[str, ...] = get_args(GraphMode)
EDITABLE: GraphMode = "editable"
SETTINGS_ONLY: GraphMode = "settings-only"

#: The refusal of any edit of a settings-only graph that is not one of its settings.
LOCKED = "{name} offers its settings only — its graph is fixed where it is shipped"

_APP = "app"
_INPUTS = "inputs"
_MODE = "graph"
_FLOW = "flow"
_OP = "op"
#: ``<param>__lo`` / ``<param>__hi``: a range field written as its two halves, which an ``app:`` block may name — one
#: field, ``<param>``.
_RANGE_HALVES = ("__lo", "__hi")


class SettingsError(ValueError):
    """A settings file that cannot be applied to its graph — the message starts with its ``file:line:col``."""


class SettingNode(NamedTuple):
    """What the settings need of one node of a graph's drawing.

    Args:
        name: The node's name, in the drawing's one scheme (a step by its key, ``stream.ops[1]``, ``prep/grey``).
        class_path: Its dotted class path.
        params: Its constructor parameters, the gate a branch sets left out.
    """

    name: str
    class_path: str
    params: Tuple[str, ...]


class SettingField(NamedTuple):
    """One setting of a graph: a node's parameter, the key that names it in the settings file, and the ``app:`` entry
    that declares it.

    Args:
        node: The node's name.
        param: The parameter (a range's two halves are one parameter).
        key: Its key in the settings file — ``flow.<step>.op.<param>``, ``<key>.<param>`` or ``<Class>.<param>``.
        loc: The ``file:line:col`` of the ``app:`` entry.
    """

    node: str
    param: str
    key: str
    loc: str


class Declared(NamedTuple):
    """A graph's settings, as its ``app:`` block declares them.

    Args:
        declared: Whether the graph has an ``app:`` block listing its inputs.
        mode: What the graph lets a person change (:data:`GraphMode`).
        fields: Every setting, in the block's order — one per node of a class the block names.
        unaddressable: A node's parameter the block names that NO setting can reach — two nodes with no name share its
            class, so a class line would set both — with why, in words.
        problems: ``(message, loc)`` of every entry that names nothing, and of a mode word the graph does not know.
    """

    declared: bool
    mode: GraphMode
    fields: Tuple[SettingField, ...]
    unaddressable: Mapping[Tuple[str, str], str]
    problems: Tuple[Tuple[str, str], ...]

    def field(self, node: str, param: str) -> Optional[SettingField]:
        """The setting of ``node.param``, or ``None`` when it is not one."""
        return next((field for field in self.fields if field.node == node and field.param == param), None)


def settings_path(graph: Path) -> Path:
    """The settings file of ``graph`` — beside it, under its name (``view_bte.yaml`` → ``view_bte.settings.yaml``)."""
    return Path(graph).with_suffix(SETTINGS_SUFFIX)


def graph_name(settings: Path) -> str:
    """The graph a settings file belongs to, by name (``view_bte.settings.yaml`` → ``view_bte``)."""
    name = Path(settings).name
    return name[: -len(SETTINGS_SUFFIX)] if name.endswith(SETTINGS_SUFFIX) else Path(settings).stem


def is_settings_file(path: Path) -> bool:
    """Whether ``path`` is a graph's settings file (never a graph of its own)."""
    return Path(path).name.endswith(SETTINGS_SUFFIX)


def _at(file: Path, node: yaml.Node) -> str:
    return f"{file}:{node.start_mark.line + 1}:{node.start_mark.column + 1}"


def _short(class_path: str) -> str:
    return class_path.rsplit(".", 1)[-1]


def _field_param(param: str, known: Sequence[str]) -> Optional[str]:
    """The parameter an ``app:`` entry names among ``known``: itself, or ``<param>`` for one of a range's halves."""
    if param in known:
        return param
    for half in _RANGE_HALVES:
        if param.endswith(half) and param[: -len(half)] in known:
            return param[: -len(half)]
    return None


def _app_entry(entry: yaml.Node) -> Optional[Tuple[str, str]]:
    """``(class, param)`` of one ``app.inputs`` entry — a sequence of two scalars — else ``None``."""
    if not isinstance(entry, yaml.SequenceNode) or len(entry.value) != 2:
        return None
    first, second = entry.value
    if not isinstance(first, yaml.ScalarNode) or not isinstance(second, yaml.ScalarNode):
        return None
    return str(first.value), str(second.value)


def graph_mode(text: str, file: Path) -> Tuple[GraphMode, Optional[Tuple[str, str]]]:
    """What the graph ``text`` lets a person change — its ``app: {graph: …}`` word, ``editable`` without one — and the
    located problem of a word that is not a :data:`GraphMode` (the graph is then ``editable``)."""
    try:
        word = YamlText(text).path(_APP, _MODE)
    except yaml.YAMLError:
        return EDITABLE, None
    if word is None:
        return EDITABLE, None
    if isinstance(word, yaml.ScalarNode) and word.value in GRAPH_MODES:
        return cast(GraphMode, word.value), None  # one of the Literal's own values, checked just above
    spelled = word.value if isinstance(word, yaml.ScalarNode) else "a block"
    loc = _at(file, word)
    return EDITABLE, (
        f"{loc}: graph: {spelled} is not what a graph lets a person change — one of: " f"{', '.join(GRAPH_MODES)}",
        loc,
    )


def declared_settings(text: str, file: Path, nodes: Sequence[SettingNode], *, gate: str) -> Declared:
    """The settings the graph ``text`` (the file ``file``) declares, resolved against its drawing's ``nodes``.

    Every ``app.inputs`` entry ``[Class, param]`` (the short or the dotted class name) gives one field per node of
    that class, in the block's order; ``<param>__lo`` / ``<param>__hi`` name ONE field. A field's key names its node:
    a flow step by its name (``flow.<step>.op``), a top-level object by its key, any other node by its class while
    the class is that node's alone — else the node is :attr:`Declared.unaddressable`. An entry that is not a pair,
    names the gate a branch sets (``gate``), no node or no parameter of its class is a located problem in words.
    """
    mode, mode_problem = graph_mode(text, file)
    problems: List[Tuple[str, str]] = [mode_problem] if mode_problem is not None else []
    try:
        yaml_text = YamlText(text)
        inputs = yaml_text.path(_APP, _INPUTS)
        root = yaml_text.root
    except yaml.YAMLError:
        return Declared(False, mode, (), {}, tuple(problems))
    if not isinstance(inputs, yaml.SequenceNode):
        return Declared(False, mode, (), {}, tuple(problems))
    # The drawing names a top-level object by its key — a plain name; a nested one or a list member by a path.
    top: Set[str] = set(YamlText.keys(root)) if isinstance(root, yaml.MappingNode) else set()
    flow = yaml_text.path(_FLOW)
    steps: Set[str] = set()
    if isinstance(flow, yaml.MappingNode):
        steps = {
            key
            for key, value in ((k.value, v) for k, v in flow.value if isinstance(k, yaml.ScalarNode))
            if isinstance(value, yaml.MappingNode) and YamlText.pair(value, _OP) is not None
        }
    classed = [node for node in nodes if node.class_path]
    classes = ", ".join(sorted({_short(node.class_path) for node in classed})) or "none"
    users: Dict[str, List[str]] = {}
    for node in classed:
        users.setdefault(node.class_path, []).append(node.name)

    def owner(node: SettingNode) -> Optional[str]:
        if node.name in steps:
            return f"{_FLOW}.{node.name}.{_OP}"
        if node.name in top and not any(mark in node.name for mark in ".[/"):
            return node.name
        if len(users[node.class_path]) == 1:
            return _short(node.class_path)
        return None

    fields: List[SettingField] = []
    unaddressable: Dict[Tuple[str, str], str] = {}
    for entry in inputs.value:
        loc = _at(file, entry)
        pair = _app_entry(entry)
        if pair is None:
            problems.append((f"{loc}: an app input is a [Class, param] pair", loc))
            continue
        class_name, param = pair
        spelled = f"[{class_name}, {param}]"
        if param == gate:
            problems.append(
                (f"{loc}: the app input {spelled} is the gate a branch sets, not a setting — it is never a field", loc)
            )
            continue
        matched = [node for node in classed if class_name in (node.class_path, _short(node.class_path))]
        if not matched:
            problems.append(
                (f"{loc}: the app input {spelled} names no node of this graph (its classes are: {classes})", loc)
            )
            continue
        known = matched[0].params
        field_param = _field_param(param, known)
        if field_param is None:
            listed = ", ".join(sorted(known)) or "none"
            problems.append(
                (
                    f"{loc}: the app input {spelled} names no parameter of {class_name} (its parameters are: {listed})",
                    loc,
                )
            )
            continue
        shared: List[str] = []
        for node in matched:
            if any(field.node == node.name and field.param == field_param for field in fields):
                continue  # the other half of a range, or the same pair twice: one field
            if (node.name, field_param) in unaddressable:
                continue
            head = owner(node)
            if head is None:
                sharing = users[node.class_path]
                why = (
                    f"{_short(node.class_path)} is the class of {len(sharing)} nodes with no name "
                    f"({', '.join(sharing)})"
                    " — a setting cannot tell them apart; give them names (flow steps) to set each"
                )
                unaddressable[(node.name, field_param)] = why
                shared.append(why)
                continue
            fields.append(SettingField(node.name, field_param, f"{head}.{field_param}", loc))
        if shared:
            problems.append((f"{loc}: the app input {spelled} names {shared[0]}", loc))
    return Declared(True, mode, tuple(fields), unaddressable, tuple(problems))


def _first_leaf(node: yaml.Node, path: List[str]) -> Tuple[List[str], Optional[yaml.Node]]:
    """The key path down to the first value of ``node`` that is not a mapping."""
    while isinstance(node, yaml.MappingNode) and node.value:
        key, node = node.value[0]
        path = [*path, str(key.value)]
    return path, node


def read_settings(
    text: str, file: Path, declared: Declared, graph: str
) -> Tuple[List[Tuple[SettingField, Any]], List[Tuple[str, str]]]:  # each value: what the line holds, any YAML value
    """The settings the text of ``file`` sets, each with its value — and ``(message, loc)`` of every line that is not
    one of ``graph``'s settings (:class:`Declared`).

    A line is ``<key>: value`` — or a block ``<Class>:`` / ``<key>:`` holding ``<param>: value`` lines, the same
    settings spelled as one block (confluid reads both alike). A key that is none of the graph's settings, a class that
    two nodes with no name share and a ``flow:`` block are refused where they are written. A text that does not parse
    answers nothing here: loading the two together reports it, in the settings file (:func:`load_graph`).
    """
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
        values = yaml.safe_load(text)
    except yaml.YAMLError:
        return [], []
    if root is None:
        return [], []
    if not isinstance(root, yaml.MappingNode) or not isinstance(values, dict):
        loc = _at(file, root)
        return [], [
            (
                f"{loc}: a settings file maps each setting to its value, one per line (flow.<step>.op.<param>: "
                "value)",
                loc,
            )
        ]
    by_key = {field.key: field for field in declared.fields}
    owners = {field.key.rsplit(".", 1)[0] for field in declared.fields}
    shared = {why.split(" ", 1)[0]: why for why in declared.unaddressable.values()}
    listed = ", ".join(sorted(by_key)) if by_key else "none — it has no app: block naming any"
    entries: List[Tuple[SettingField, Any]] = []
    problems: List[Tuple[str, str]] = []

    def take(key: str, key_node: yaml.Node, value: Any) -> None:  # value: what the line holds, any YAML value
        loc = _at(file, key_node)
        field = by_key.get(key)
        if field is not None:
            entries.append((field, value))
        elif key.count(".") == 1 and key.split(".")[0] in shared:
            problems.append((f"{loc}: {shared[key.split('.')[0]]}", loc))
        else:
            problems.append((f"{loc}: {key} is not a setting of {graph} — its settings are: {listed}", loc))

    for key_node, value_node in root.value:
        key = str(key_node.value)
        value = values.get(key_node.value)
        if key == _FLOW and isinstance(value_node, yaml.MappingNode):
            path, leaf = _first_leaf(value_node, [_FLOW])
            leaf_value: Any = yaml.safe_load(yaml.serialize(leaf)) if leaf is not None else None
            loc = _at(file, key_node)
            problems.append(
                (
                    f"{loc}: a flow: block moves its steps to the end of the flow — "
                    f"write each setting on a line of its own: {'.'.join(path)}: {spell(leaf_value)}",
                    loc,
                )
            )
            continue
        if (key in owners or key in shared) and isinstance(value_node, yaml.MappingNode) and isinstance(value, dict):
            for inner_node, _inner_value in value_node.value:
                take(f"{key}.{inner_node.value}", inner_node, value.get(inner_node.value))
            continue
        take(key, key_node, value)
    return entries, problems


def set_setting(text: str, field: SettingField, value: Any) -> str:  # value: a document value — a scalar or a list
    """The settings ``text`` with ``field`` set to ``value``: its own line replaced where the text has it (spelled as
    one line, or as a param of its owner's block), else appended as a line of its own. Every other line — comments
    included — stays as it was."""
    spelled = spell(value)
    owner, param = field.key.rsplit(".", 1)
    yaml_text = YamlText(text)
    root = yaml_text.root if text.strip() else None
    if isinstance(root, yaml.MappingNode):
        found = YamlText.pair(root, field.key)
        if found is not None:
            return yaml_text.apply([yaml_text.replace_value_edit(found[0], found[1], spelled)])
        block = YamlText.pair(root, owner)
        if block is not None and isinstance(block[1], yaml.MappingNode) and not block[1].flow_style:
            inner = YamlText.pair(block[1], param)
            if inner is not None:
                return yaml_text.apply([yaml_text.replace_value_edit(inner[0], inner[1], spelled)])
            return yaml_text.apply([yaml_text.append_pair_edit(block[1], param, spelled)])
    lead = "" if text == "" or text.endswith("\n") else "\n"
    return f"{text}{lead}{field.key}: {spelled}\n"


def composed(graph: Path, settings: Path) -> Tuple[Path, str]:
    """The document that is ``graph`` with ``settings`` over it — ``(path, text)``: confluid's ``include:`` of the two,
    in that order, so a setting wins over what the graph spells. HELD, never written: its path sits beside the settings
    (a hidden name), its text is handed to ``confluid.load(texts=…)``, and every location names the graph or the
    settings file, never it."""
    wrapper = Path(settings).parent / f".{graph_name(settings)}.composed.yaml"
    entries = ", ".join(json.dumps(str(Path(path).resolve())) for path in (graph, settings))
    return wrapper.resolve(), f"include: [{entries}]\n"


def load_graph(
    graph: Path,
    settings: Optional[Path] = None,
    *,
    until: Literal["raw", "document", "settled", "objects"] = "objects",
    solidify: bool = True,
) -> Any:  # what confluid.load answers: a mapping of top-level objects, or the root object
    """``confluid.load`` of ``graph`` with its ``settings`` over it — what a host runs (a pane, a model, a source).

    ``settings`` is the settings file, wherever it is (a host keeps a shipped graph's in its own folder); ``None``, or
    a file that is not there, loads the graph as it is. A settings file that cannot be applied — a line that is none
    of the graph's settings, a class two nodes with no name share, a ``flow:`` block, a text that does not parse — is
    a :class:`SettingsError` whose message starts with the line's ``file:line:col``. Nothing is written.
    """
    graph = Path(graph)
    if settings is None or not Path(settings).is_file():
        return confluid.load(graph.resolve(), until=until, solidify=solidify)
    settings = Path(settings).resolve()
    text = settings.read_text()
    # The document model is this module's caller; imported here, at the one call that needs it, to keep the two
    # modules' imports one way (the model reads the settings through this module).
    from streamstudio.document import derive

    derivation = derive(graph, {settings: text}, settings=settings)
    refused = [problem for problem in derivation.model.problems if problem.loc.startswith(f"{settings}:")]
    if refused:
        raise SettingsError("\n".join(problem.message for problem in refused))
    wrapper, wrapper_text = composed(graph, settings)
    return confluid.load(wrapper, until=until, solidify=solidify, texts={wrapper: wrapper_text, settings: text})
