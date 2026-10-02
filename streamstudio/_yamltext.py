"""Patch a YAML text at the line a marker came from — never re-dump the document.

Why a text patcher exists at all: re-emitting a loaded document loses what the loader never
kept. Measured on the shipped Bluetooth pane (``docs/rewrite.md`` §2.1): ``confluid.dump``
turns 275 lines with 148 comment lines into 179 lines with 0, and bakes ``$DATA_ROOT`` into
the value it resolved to. Patching the one line that changed keeps every other byte.

How a marker's line is found: PyYAML's COMPOSED node tree (``yaml.compose``) carries a start
mark for every node, and those marks coincide with the location confluid stamps on its
markers (``confluid.format_yaml_loc``) — a ``!class:`` marker is located at its tag, a
``_target_:`` mapping at its first key (both come from the same node's ``start_mark``). So a
marker's ``file:line:col`` names exactly one composed node, and every edit here is expressed
against that node's marks: the value span of a kwarg, the line span of a step, the character
span of a ``!ref:`` tag. Composing (not constructing) tolerates any tag, so the tree is built
with the plain SafeLoader and no constructor ever runs.

Two spellings are handled for a marker's kwargs — the block form (keys on their own lines)
and the one-line flow form (``{a: 1, b: 2}``) — plus the bare tag with no kwargs at all. A
multi-line block VALUE is replaced by one flow-style line, so a comment inside that value is
the one thing a patch may lose; everything outside the patched span survives byte for byte.

A step name is SCOPED to the steps mapping it is a key of: the ``steps:`` of a ``Subgraph`` marker is
a flow of its own, whose ``from:`` / ``merge_from:`` / ``bind:`` name ITS steps. So every walk that
follows a step name (a rename, the references to a step) skips the ``steps:`` value of a subgraph
marker it passes through (:func:`is_subgraph_marker`) — measured before this rule: renaming an outer
step ``grey`` also rewrote a subgraph's inner ``from: grey``, and the saved file stopped running. A
``!ref:`` names a top-level key, which a subgraph's inside may use too, so the ``!ref:`` walk is not
scoped.
"""

from typing import Any, Collection, Dict, List, Optional, Sequence, Tuple

import yaml

#: The step-grammar keys a rename must follow (``recordstream.flow.steps.RESERVED_STEP_KEYS``), spelled
#: here because this module knows YAML text and nothing about the engine.
_FROM_KEYS = ("from", "merge_from")
_BIND_KEY = "bind"
_REF_KEY = "_ref_"
_OUTPUTS_KEY = "outputs"
#: A subgraph marker's inner flow (``recordstream.flow.subgraph.Subgraph(steps, result)``): its short class
#: name as the ``!class:`` tag spells it, and the key holding the steps — the two things the scoping reads.
_SUBGRAPH_CLASS = "Subgraph"
_STEPS_KEY = "steps"


def is_subgraph_marker(node: yaml.Node) -> bool:
    """A ``!class:…Subgraph`` marker, told by the class's SHORT name in its tag — this module reads text and
    imports no engine code (the engine's own test is the class attribute ``Subgraph.FLOW_SUBGRAPH``).

    The ``!class:`` tag is the spelling the editor writes and the one recognised; a ``_target_:`` mapping
    naming a Subgraph is not scoped (a documented limit, not a second rule)."""
    tag = str(node.tag or "")
    return tag.startswith("!class:") and tag[len("!class:") :].rsplit(".", 1)[-1] == _SUBGRAPH_CLASS


#: One text edit: replace ``text[start:stop]`` by ``replacement``.
Edit = Tuple[int, int, str]


def spell(value: Any) -> str:  # a document value: a scalar or a plain container of them
    """One-line YAML for a value — what a patched line carries after the ``:``.

    PyYAML's own dumper decides the quoting (``smooth`` stays bare, ``'1.0'`` gets quotes so it
    stays a string, ``$DATA_ROOT/x`` needs none), containers come out in flow style, and the
    ``...`` document-end marker a bare scalar is dumped with is stripped.
    """
    dumped = yaml.safe_dump(value, default_flow_style=True, width=10**9).strip()
    if dumped.endswith("\n..."):
        dumped = dumped[: -len("\n...")].strip()
    return dumped


def spell_mapping(values: Dict[str, Any]) -> str:
    """A one-line flow mapping ``{a: 1, b: x}`` — ``{}`` when empty — for a freshly inserted marker."""
    return "{" + ", ".join(f"{key}: {spell(value)}" for key, value in values.items()) + "}"


class YamlText:
    """A YAML text and its composed node tree; every edit returns a NEW text.

    Args:
        text: The document text, exactly as read from the file.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self._root: Optional[yaml.Node] = None
        self._line_starts: Optional[List[int]] = None

    # -- the tree ----------------------------------------------------------------------------

    @property
    def root(self) -> yaml.Node:
        """The composed root node (a syntax error raises PyYAML's located error)."""
        if self._root is None:
            self._root = yaml.compose(self.text, Loader=yaml.SafeLoader)
        return self._root

    def node_at(self, line: int, col: int) -> yaml.Node:
        """The OUTERMOST node starting at the 1-based ``line:col`` — a marker's stamped location.

        Outermost matters: a ``_target_:`` mapping and its first key start on the same mark, and
        the mapping is the marker.
        """
        wanted = (line - 1, col - 1)
        for node in _walk(self.root):
            if (node.start_mark.line, node.start_mark.column) == wanted:
                return node
        raise ValueError(f"no YAML node starts at {line}:{col}")

    def path(self, *keys: str) -> Optional[yaml.Node]:
        """The node under a chain of mapping keys from the root, or ``None``."""
        node: Optional[yaml.Node] = self.root
        for key in keys:
            if not isinstance(node, yaml.MappingNode):
                return None
            found = self.pair(node, key)
            node = found[1] if found else None
        return node

    @staticmethod
    def pair(mapping: yaml.MappingNode, key: str) -> Optional[Tuple[yaml.ScalarNode, yaml.Node]]:
        """``(key node, value node)`` of ``key`` in ``mapping``, or ``None``."""
        for key_node, value in mapping.value:
            if isinstance(key_node, yaml.ScalarNode) and key_node.value == key:
                return key_node, value
        return None

    @staticmethod
    def keys(mapping: yaml.MappingNode) -> List[str]:
        return [key.value for key, _value in mapping.value if isinstance(key, yaml.ScalarNode)]

    # -- positions ---------------------------------------------------------------------------

    def line_start(self, line: int) -> int:
        """Character index where 0-based ``line`` starts (``len(text)`` past the last line)."""
        if self._line_starts is None:
            starts, at = [0], 0
            for piece in self.text.splitlines(keepends=True):
                at += len(piece)
                starts.append(at)
            self._line_starts = starts
        return self._line_starts[min(line, len(self._line_starts) - 1)]

    def line_count(self) -> int:
        return len(self.text.splitlines())

    def lines(self, start: int, stop: int) -> str:
        """The text of 0-based lines ``start`` up to (not including) ``stop``, line ends kept."""
        return self.text[self.line_start(start) : self.line_start(stop)]

    def nodes(self) -> List[yaml.Node]:
        """Every node of the tree, parents before children, in document order."""
        return _walk(self.root)

    def item_stop_line(self, sequence: yaml.SequenceNode, index: int) -> int:
        """The first 0-based line AFTER item ``index`` of a BLOCK sequence — where the next item's ``- `` is.

        The same rule as :meth:`pair_stop_line`: a comment between two items travels with the one above
        it, a multi-line last item ends where the next token starts, a one-line last item is its line.
        """
        item = sequence.value[index]
        if index + 1 < len(sequence.value):
            return int(sequence.value[index + 1].start_mark.line)
        end = item.end_mark
        if end.index >= len(self.text):
            return self.line_count()
        if end.line > item.start_mark.line:
            return int(end.line)
        return int(item.start_mark.line) + 1

    def pair_stop_line(self, key: yaml.ScalarNode, value: yaml.Node) -> int:
        """The first 0-based line AFTER a mapping pair — where the next sibling begins.

        A block value's end mark IS the next token's position (after the blank and comment lines
        between), so a comment written below a step travels with that step. A one-line value ends
        on the key's own line, so its pair is exactly that line. A value running to the end of
        the text has no next token; the pair then runs to the last line.
        """
        end = value.end_mark
        if end.index >= len(self.text):
            return self.line_count()
        if end.line > key.start_mark.line:
            return end.line
        return key.start_mark.line + 1

    # -- edits -------------------------------------------------------------------------------

    def apply(self, edits: Sequence[Edit]) -> str:
        """The text with every ``(start, stop, replacement)`` applied — bottom-up, so spans stay valid."""
        text = self.text
        for start, stop, replacement in sorted(edits, key=lambda edit: edit[0], reverse=True):
            text = text[:start] + replacement + text[stop:]
        return text

    def set_kwarg(self, line: int, col: int, key: str, value: Any) -> str:  # value: a document value
        """The text with ``key: value`` set on the marker located at 1-based ``line:col``.

        A key the marker already spells is rewritten in place (block or flow form; a multi-line
        block value becomes one flow-style line). A key it does not spell is inserted: right under
        the marker's head line in block form, appended inside the braces of a one-line flow
        mapping, or as ``{key: value}`` after a bare tag.
        """
        node = self.node_at(line, col)
        spelled = spell(value)
        if isinstance(node, yaml.MappingNode):
            found = self.pair(node, key)
            if found is not None:
                key_node, value_node = found
                if value_node.start_mark.line == value_node.end_mark.line:
                    return self.apply([(value_node.start_mark.index, value_node.end_mark.index, spelled)])
                stop = (
                    self.line_start(value_node.end_mark.line)
                    if value_node.end_mark.index < len(self.text)
                    else len(self.text)
                )
                return self.apply([(key_node.end_mark.index, stop, f": {spelled}\n")])
        return self._insert_pair(node, line, col, key, spelled)

    def set_marker(self, line: int, col: int, key: str, marker: str) -> str:
        """The text with ``key: <marker>`` set on the marker located at 1-based ``line:col`` — ``marker`` already
        spelled (``!class:x.Y {a: 1}``): a slot of the marker gets a new object.

        What the key holds is REPLACED and every comment outside it stays: a one-line value in place (a comment
        after it on its line stays too), a block value through its last line of CONTENT — the comment lines below
        it belong to what follows, where :meth:`set_kwarg` would take them along — and an empty ``key:`` gets the
        marker after its colon. A key the marker does not spell is inserted as :meth:`set_kwarg` inserts one.
        """
        node = self.node_at(line, col)
        if isinstance(node, yaml.MappingNode):
            found = self.pair(node, key)
            if found is not None:
                key_node, value_node = found
                if isinstance(value_node, yaml.ScalarNode) and value_node.tag == _NULL_TAG and value_node.value == "":
                    colon = self.text.index(":", key_node.end_mark.index)
                    return self.apply([(colon + 1, colon + 1, f" {marker}")])
                if value_node.start_mark.line == value_node.end_mark.line:
                    return self.apply([(value_node.start_mark.index, value_node.end_mark.index, marker)])
                stop = self.line_start(_last_content_line(value_node) + 1)
                return self.apply([(key_node.end_mark.index, stop, f": {marker}\n")])
        return self._insert_pair(node, line, col, key, marker)

    def _insert_pair(self, node: yaml.Node, line: int, col: int, key: str, spelled: str) -> str:
        """The text with ``key: <spelled>`` inserted into the marker ``node`` (located at ``line:col``), which does not
        spell ``key``: right under its head line in block form, appended inside the braces of a one-line flow mapping,
        or as ``{key: value}`` after a bare tag."""
        if isinstance(node, yaml.MappingNode):
            if node.flow_style:
                if node.start_mark.line != node.end_mark.line:
                    raise ValueError(f"the flow mapping at {line}:{col} spans several lines — spell it in block form")
                close = node.end_mark.index - 1
                if self.text[close] != "}":
                    raise ValueError(f"the flow mapping at {line}:{col} does not end in '}}'")
                insertion = f"{key}: {spelled}" if not node.value else f", {key}: {spelled}"
                return self.apply([(close, close, insertion)])
            first_key, first_value = node.value[0]
            indent = " " * first_key.start_mark.column
            insert_line = (
                first_key.start_mark.line
                if first_key.start_mark.line > node.start_mark.line
                else self.pair_stop_line(first_key, first_value)
            )
            at = self.line_start(insert_line)
            return self.apply([(at, at, f"{indent}{key}: {spelled}\n")])
        if isinstance(node, yaml.ScalarNode) and node.tag.startswith("!") and node.value == "":
            at = node.end_mark.index
            return self.apply([(at, at, f" {{{key}: {spelled}}}")])
        raise ValueError(f"the node at {line}:{col} is a {type(node).__name__}, not a marker mapping")

    def remove_pair_edit(self, key: yaml.ScalarNode, value: yaml.Node) -> Edit:
        """The edit removing a mapping pair's lines (its key line through the end of its value)."""
        return (self.line_start(key.start_mark.line), self.line_start(self.pair_stop_line(key, value)), "")

    def drop_pair_edit(self, mapping: yaml.MappingNode, key: str) -> Edit:
        """The edit removing the pair ``key`` of ``mapping``, in either spelling: its lines in block form, its
        text and ONE separating comma in a one-line flow mapping (``{op: x, from: a}`` → ``{op: x}``)."""
        index = next(i for i, (k, _v) in enumerate(mapping.value) if isinstance(k, yaml.ScalarNode) and k.value == key)
        key_node, value = mapping.value[index]
        if not mapping.flow_style:
            return self.remove_pair_edit(key_node, value)
        if index > 0:
            return (mapping.value[index - 1][1].end_mark.index, value.end_mark.index, "")
        if len(mapping.value) > 1:
            return (key_node.start_mark.index, mapping.value[1][0].start_mark.index, "")
        return (key_node.start_mark.index, value.end_mark.index, "")

    def insert_lines_edit(self, line: int, lines: Sequence[str]) -> Edit:
        """The edit inserting ``lines`` (no newlines) BEFORE 0-based ``line``."""
        at = self.line_start(line)
        return (at, at, "".join(f"{piece}\n" for piece in lines))

    @staticmethod
    def replace_scalar_edit(node: yaml.ScalarNode, replacement: str) -> Edit:
        return (node.start_mark.index, node.end_mark.index, replacement)

    def rename_edits(self, old: str, new: str, *, flow_key: Optional[str] = "flow") -> List[Edit]:
        """Every edit that renames the node ``old`` to ``new`` across the text.

        The key itself (a step under ``flow:``, or a top-level key), every ``from:`` /
        ``merge_from:`` that names it, the step half of every ``bind:`` reference
        (``old.attr`` / ``old[key]`` / ``old``), the root ``outputs:``, every ``!ref:old`` tag
        and every ``_ref_: old`` value. A slot KEY that happens to equal the name (a
        ``delivered: {stream: !ref:stream}`` entry) is a parameter name, not a node, and stays.
        ``flow_key=None`` is a text whose ROOT is the steps mapping (a steps file, a clipboard).
        The step references inside a subgraph's ``steps:`` name ITS steps and are never followed (the
        module docstring); a ``!ref:`` there names a top-level key like any other and is.
        """
        edits: List[Edit] = []
        root = self.root
        if not isinstance(root, yaml.MappingNode):
            return edits
        for container in (self.path(flow_key) if flow_key is not None else None, root):
            if isinstance(container, yaml.MappingNode):
                found = self.pair(container, old)
                if found is not None:
                    edits.append(self.replace_scalar_edit(found[0], new))
        outputs = self.pair(root, _OUTPUTS_KEY)
        if outputs is not None and isinstance(outputs[1], yaml.ScalarNode) and outputs[1].value == old:
            edits.append(self.replace_scalar_edit(outputs[1], new))
        ref_tag = f"!ref:{old}"
        for node in _walk(root):
            start = node.start_mark.index
            if node.tag == ref_tag and self.text[start : start + len(ref_tag)] == ref_tag:
                edits.append((start, start + len(ref_tag), f"!ref:{new}"))
            if not isinstance(node, yaml.MappingNode):
                continue
            for key_node, value in node.value:
                if isinstance(key_node, yaml.ScalarNode) and key_node.value == _REF_KEY:
                    edits.extend(self.replace_scalar_edit(s, new) for s in _scalars_of(value) if s.value == old)
        edits.extend(self._step_reference_edits(_walk(root, scoped=True), old, new))
        return _dedupe(edits)

    def rename(self, old: str, new: str, *, flow_key: str = "flow") -> str:
        return self.apply(self.rename_edits(old, new, flow_key=flow_key))

    def reference_edits(
        self, steps: yaml.MappingNode, old: str, new: str, *, only: Optional[Collection[str]] = None
    ) -> List[Edit]:
        """Every edit re-pointing a reference to the step ``old`` of ONE steps mapping at ``new``: the
        ``from:`` / ``merge_from:`` values and the step half of the ``bind:`` references of its steps
        (only the steps named in ``only``, when given) — never inside a nested subgraph's ``steps:``."""
        nodes: List[yaml.Node] = []
        for key_node, value in steps.value:
            if isinstance(key_node, yaml.ScalarNode) and (only is None or key_node.value in only):
                nodes.extend(_walk(value, scoped=True))
        return _dedupe(self._step_reference_edits(nodes, old, new))

    def step_rename_edits(self, steps: yaml.MappingNode, old: str, new: str) -> List[Edit]:
        """Every edit renaming the step ``old`` of ONE steps mapping (a subgraph's ``steps:``, a steps file):
        its key and every reference to it among that mapping's steps (:meth:`reference_edits`)."""
        edits: List[Edit] = []
        found = self.pair(steps, old)
        if found is not None:
            edits.append(self.replace_scalar_edit(found[0], new))
        return _dedupe(edits + self.reference_edits(steps, old, new))

    def _step_reference_edits(self, nodes: Sequence[yaml.Node], old: str, new: str) -> List[Edit]:
        """The ``from:`` / ``merge_from:`` / ``bind:`` values among ``nodes`` that name the step ``old``."""
        edits: List[Edit] = []
        for node in nodes:
            if not isinstance(node, yaml.MappingNode):
                continue
            for key_node, value in node.value:
                if not isinstance(key_node, yaml.ScalarNode):
                    continue
                if key_node.value in _FROM_KEYS:
                    edits.extend(self.replace_scalar_edit(s, new) for s in _scalars_of(value) if s.value == old)
                elif key_node.value == _BIND_KEY and isinstance(value, yaml.MappingNode):
                    for _param, ref in value.value:
                        if isinstance(ref, yaml.ScalarNode) and _bind_step(ref.value) == old:
                            edits.append(self.replace_scalar_edit(ref, new + ref.value[len(old) :]))
        return edits


#: The tag PyYAML's resolver gives a plain empty value (``key:`` with nothing after it).
_NULL_TAG = "tag:yaml.org,2002:null"


def _last_content_line(node: yaml.Node) -> int:
    """The 0-based line of the last character a value's own content occupies. A BLOCK collection's end mark is the
    NEXT token's position — past the blank and comment lines between — so the walk goes down to its last child; a
    scalar or a flow collection ends on its own last line (a block scalar's end mark sits at the start of the line
    after it)."""
    while isinstance(node, (yaml.MappingNode, yaml.SequenceNode)) and not node.flow_style and node.value:
        node = node.value[-1][1] if isinstance(node, yaml.MappingNode) else node.value[-1]
    end = node.end_mark
    return int(end.line) if end.column > 0 else int(end.line) - 1


def dedent(block: str, by: int) -> str:
    """``block`` with up to ``by`` leading spaces taken off every line — a line indented less (a comment
    written further left) loses what it has, so the relative indentation of the rest is kept."""
    out: List[str] = []
    for line in block.splitlines(keepends=True):
        spaces = len(line) - len(line.lstrip(" "))
        out.append(line[min(spaces, by) :])
    return "".join(out)


def indent_lines(block: str, prefix: str) -> List[str]:
    """``block``'s lines (no line ends) with ``prefix`` in front of each — a blank line stays empty."""
    return [prefix + line if line.strip() else "" for line in block.splitlines()]


def _bind_step(ref: str) -> str:
    """The step half of a ``bind:`` reference — ``step``, ``step.attr`` or ``step[key]``."""
    if ref.endswith("]") and "[" in ref:
        head = ref[: ref.index("[")]
        if "." not in head:
            return head
    return ref.partition(".")[0]


def _scalars_of(node: yaml.Node) -> List[yaml.ScalarNode]:
    if isinstance(node, yaml.ScalarNode):
        return [node]
    if isinstance(node, yaml.SequenceNode):
        return [item for item in node.value if isinstance(item, yaml.ScalarNode)]
    return []


def _walk(node: yaml.Node, *, scoped: bool = False) -> List[yaml.Node]:
    """Every node of the tree, parents before children, in document order — with ``scoped``, never the
    ``steps:`` value of a subgraph marker (a flow of its own; the module docstring)."""
    out: List[yaml.Node] = [node]
    if isinstance(node, yaml.MappingNode):
        inner_flow = scoped and is_subgraph_marker(node)
        for key, value in node.value:
            out.extend(_walk(key, scoped=scoped))
            if inner_flow and isinstance(key, yaml.ScalarNode) and key.value == _STEPS_KEY:
                continue
            out.extend(_walk(value, scoped=scoped))
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            out.extend(_walk(item, scoped=scoped))
    return out


def _dedupe(edits: List[Edit]) -> List[Edit]:
    seen: set[Tuple[int, int]] = set()
    unique: List[Edit] = []
    for edit in edits:
        if (edit[0], edit[1]) not in seen:
            seen.add((edit[0], edit[1]))
            unique.append(edit)
    return unique
