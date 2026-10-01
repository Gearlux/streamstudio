"""Where a node is drawn: a deterministic first layout, and a sidecar that keeps what the user moved.

The confluid document is the only truth of a graph (``docs/rewrite.md`` §2.1) and it says nothing
about pixels — adding ``x:``/``y:`` keys to it would turn a pipeline file into a drawing file and
make every drag a document edit. Positions therefore live in a SIDECAR, ``<document>.layout.yaml``,
a mapping ``{node name: [x, y]}``. It is keyed by the node NAME (a flow step's key, ``stream.ops[1]``,
``contract``, ``prep/grey`` for a node inside the subgraph ``prep`` — the one naming scheme the engine,
the tools and the canvas share; each subgraph's inside is a model of its own, laid out on its own by the
same rule), never by an id
written into the YAML: a sidecar is a convenience, and a graph must load, run and export with the
sidecar deleted. The known cost, measured on the prototypes, is that RENAMING a node orphans its
entry — the editor's rename re-keys the sidecar in the same operation, and :func:`stale_positions`
reports what is left so the caller can say so.

The sidecar holds two blocks. ``positions:`` is the mapping above. ``groups:`` is the list of boxes
the canvas draws around nodes — a title, a colour and member names (:class:`Group`). A group lives
HERE and nowhere else because the document has no spelling for one: a group is how a person arranged
the drawing, not something the pipeline does, so writing it into the YAML would make a cosmetic
choice a document edit (with its undo entry and its re-validation) and a graph would stop being the
same graph when someone boxed two nodes. The server keeps a group OPAQUE — it is stored as the page
spelled it, extra keys included — and interprets it in exactly one place, :func:`rename_member`,
because a rename must re-key every reference to a node in one operation, the group's member list
included.

The first layout is the plainest one that reads as a pipeline: longest-path layering along the
wires (a node's layer is one past the deepest node feeding it; a node nothing feeds sits in layer
0), one column per layer, the layer's nodes stacked down its column in the order the model lists
them. No crossing minimisation — a pipeline is a one-direction chain with a few forks, and a stable
result (the same document always draws the same way) is worth more than a prettier one that
moves when a step is added. :func:`positions` merges the two: a node the sidecar names keeps its
place, the rest are laid out in their layer's column starting BELOW the lowest kept card's bottom,
so a step an agent added never lands on top of something the user arranged.

Within a column a card starts below the card above it: the next card's y is the previous card's y plus
``max(ROW_DY, card_height(previous) + CARD_GAP)``, rounded up to the canvas's 10-px snap grid. ``ROW_DY`` is the
MINIMUM pitch, so a column of cards up to six rows tall (181 px) keeps exactly the fixed 200-px grid, and a taller
card pushes the next one down instead of being drawn over it (measured 2026-10-01: the Bluetooth pane's ``contract``
root sat at (40, 240), under its 373-px ``known`` card but for the last 8 px; it now sits at (40, 440)). The server
never sees the drawing, so :func:`card_height` ESTIMATES a card from what it draws — a title, one row per input
socket, parameter and output socket, a note line, a subgraph's inner-steps bar — with part heights calibrated on the
page's own drawn cards so the estimate is never below a measured one. The page's own *arrange* (dagre over the
MEASURED cards) is a separate, client-side layout and is not touched by this one.

Only the model's SHAPE is read (``.nodes`` with a ``.name``, ``.wires`` with ``.source`` and
``.target``), so this module imports no document code and a test drives it with three dataclasses. The rows a
card draws (``inputs``, ``params``, ``outputs``, ``note``, ``kind``) are read with ``getattr`` and a default,
so a node without them is a title-only card and keeps the fixed ``ROW_DY`` pitch.
"""

import math
from pathlib import Path
from typing import Callable, Dict, List, Mapping, NotRequired, Optional, Protocol, Sequence, Tuple, TypedDict, cast

import yaml

#: Horizontal distance between two layers, in canvas pixels — a node card plus a wire's breathing room.
COLUMN_DX = 340
#: The SHORTEST vertical distance between two cards of one layer: a card up to six rows tall (181 px) takes exactly
#: this, a taller one pushes the next card further down (:func:`card_height`, ``CARD_GAP``).
ROW_DY = 200
#: Where the first column and the first row start, so a node is never flush against the canvas edge.
MARGIN = 40
#: The canvas's snap grid (React Flow's ``snapGrid`` in ``frontend/src/canvas/Canvas.tsx``, ``GRID`` in
#: ``frontend/src/layout.ts``): a first-layout corner lands on it like a dragged or arranged one, because a corner off
#: it jumps to it on the first drag. ``MARGIN``, ``COLUMN_DX`` and ``ROW_DY`` are multiples of it.
GRID = 10

#: How tall the page draws a node card (``frontend/src/canvas/GenericNode.tsx`` + ``styles.css``), in canvas pixels,
#: part by part — calibrated 2026-10-01 on 197 drawn cards (every graph of the waivefront annotation panes, the
#: annotaide workspace templates and a one-subgraph flow; headless chromium, the React Flow node's height at zoom 1):
#: the sum is exact on 195 cards and 1 px over on the two subgraph boundary pills, never under. The title: the name,
#: the kind badge and the class line.
CARD_TITLE = 37
#: One row of a card: an input socket, a parameter (with its widget) or an output socket.
CARD_ROW = 24
#: The note line under the rows (a types-only declaration, an alias).
CARD_NOTE = 20
#: A subgraph card's bar: its inner steps and the open button.
CARD_SUBGRAPH_BAR = 26
#: The clearance kept under a card before the next card of its column: what ``ROW_DY`` leaves under the tallest card
#: that fits it (six rows: 200 - 181), so every card that fitted the fixed pitch keeps its place.
CARD_GAP = 19

#: A node position in canvas pixels, ``(x, y)`` of its top-left corner.
Position = Tuple[float, float]

#: The sidecar's first line. It names the writer and the two ways to get a fresh layout, because a reader
#: who finds this file beside a graph must be able to tell what it is without opening the code.
SIDECAR_HEADER = (
    "# streamstudio layout sidecar — where the canvas draws each node ({node name: [x, y]} in canvas pixels) and the "
    "boxes it draws around them; written by streamstudio.layout.write_sidecar. Delete a position line to lay that "
    "node out again, the file to lay out everything."
)

#: The blocks a sidecar may hold, in the order they are written.
BLOCKS: Tuple[str, ...] = ("positions", "groups")


class Group(TypedDict):
    """One box the canvas draws around nodes, as the page spells it.

    ``nodes`` names the members by node name (the one naming scheme); a member the model no longer has
    is kept and simply not drawn. ``bounds`` (``[x, y, w, h]`` in canvas pixels) is the box of a group the
    page does not derive from its members — an EMPTY box dropped by *Add Group*, or one the user resized;
    ``parent`` names the level the box is drawn on (a subgraph's node name, ``""`` = the top level). Both
    are declared so a reader of this type sees them, and stored exactly as sent: the server reads neither.
    Every other key is the page's own and comes back as given.
    """

    id: str
    title: str
    color: str
    nodes: NotRequired[List[str]]
    bounds: NotRequired[List[float]]
    parent: NotRequired[str]


class Named(Protocol):
    """Anything with a node name — the model's nodes."""

    @property
    def name(self) -> str: ...


class Ended(Protocol):
    """Anything joining two node names — the model's wires."""

    @property
    def source(self) -> str: ...

    @property
    def target(self) -> str: ...


class GraphLike(Protocol):
    """The shape of a model this module reads: named nodes and the wires between them."""

    @property
    def nodes(self) -> Sequence[Named]: ...

    @property
    def wires(self) -> Sequence[Ended]: ...


def sidecar_path(document: Path) -> Path:
    """The sidecar beside ``document``: ``panes/bte_view.yaml`` -> ``panes/bte_view.layout.yaml``."""
    return document.with_suffix(".layout.yaml")


# ----------------------------------------------------------------------------------------------
# layering
# ----------------------------------------------------------------------------------------------
def _layers(model: GraphLike) -> Dict[str, int]:
    """Longest-path layer per node name.

    Nodes are placed in rounds: a node whose feeders are all placed takes the layer one past the
    deepest of them (layer 0 when nothing feeds it). When no node is free the wires hold a cycle, and
    the EARLIEST listed node still waiting is placed as if nothing fed it — so the drawing follows the
    order the document lists its nodes, and the walk always ends. A wire naming a node the model does
    not list is ignored: it cannot be drawn. Nothing here depends on hashing, so two calls agree.
    """
    order = [node.name for node in model.nodes]
    known = set(order)
    feeds: Dict[str, List[str]] = {name: [] for name in order}
    for wire in model.wires:
        if wire.source in known and wire.target in known and wire.source != wire.target:
            feeds[wire.target].append(wire.source)

    layer: Dict[str, int] = {}
    waiting = list(order)
    while waiting:
        ready = [name for name in waiting if all(parent in layer for parent in feeds[name])]
        if not ready:
            ready = [waiting[0]]
        for name in ready:
            layer[name] = 1 + max((layer[parent] for parent in feeds[name] if parent in layer), default=-1)
        waiting = [name for name in waiting if name not in layer]
    return layer


def card_height(node: Named) -> int:
    """How tall the page draws ``node``'s card, in canvas pixels — an upper bound (see ``CARD_TITLE``).

    Counted from what the card draws: the title, one row per input socket, parameter and output socket, the note
    line when there is a note, the inner-steps bar of a subgraph. The counts are read from the node's ``inputs``,
    ``params``, ``outputs``, ``note`` and ``kind`` when it has them and as none when it does not, so this module
    still needs only a node's NAME (``Named``): a shape without them is a title alone, which keeps ``ROW_DY``.
    """
    rows = sum(len(getattr(node, part, ())) for part in ("inputs", "params", "outputs"))
    height = CARD_TITLE + rows * CARD_ROW
    if getattr(node, "note", ""):
        height += CARD_NOTE
    if getattr(node, "kind", "") == "subgraph":
        height += CARD_SUBGRAPH_BAR
    return height


def _pitch(node: Named) -> float:
    """How far below ``node``'s top the next card of its column starts: its estimated bottom plus ``CARD_GAP``, up
    to the snap grid, and never less than ``ROW_DY`` — measured: ``known`` (373 px) → 400, a six-row card → 200."""
    return float(max(ROW_DY, math.ceil((card_height(node) + CARD_GAP) / GRID) * GRID))


def _stack(model: GraphLike, names: List[str], top: float) -> Dict[str, Position]:
    """``names`` laid out from ``top`` down: each in its layer's column, taking the column's rows in the ONE row
    order (:func:`_row_order`), each card below the one above it by that card's pitch (:func:`_pitch`)."""
    layers = _layers(model)
    nodes = {node.name: node for node in model.nodes}
    tops: Dict[int, float] = {}
    placed: Dict[str, Position] = {}
    for name in _row_order(model, names):
        depth = layers[name]
        y = tops.get(depth, top)
        placed[name] = (float(MARGIN + depth * COLUMN_DX), y)
        tops[depth] = y + _pitch(nodes[name])
    return placed


def _row_order(model: GraphLike, names: List[str]) -> List[str]:
    """``names`` in the order they take rows: the ones that carry a wire first, then the unwired ones,
    each group in the given order. The ONE row rule — ``layered_layout`` and the server's merge
    (``positions``) both go through it, so the page and a fresh layout never disagree."""
    touched = {wire.source for wire in model.wires} | {wire.target for wire in model.wires}
    return [n for n in names if n in touched] + [n for n in names if n not in touched]


def layered_layout(model: GraphLike) -> Dict[str, Position]:
    """A position per node: one column per layer, the layer's nodes stacked down it.

    Within a column the nodes that carry a wire take the top rows and the unwired ones follow, each
    group in the model's order. Without that, an unwired node that the model lists first — a graph's
    contract root whose host inputs no step names by entry — takes row 0 of the first column and pushes
    the first step of the chain down a row while the rest stays up (measured on the shipped Bluetooth
    pane, 2026-09-28). A graph with no wires at all keeps the model's order.

    Each card starts below the one above it by that card's pitch (:func:`_pitch`): ``ROW_DY`` for a card up to six
    rows, more for a taller one. Measured on the shipped Bluetooth pane, 2026-10-01: the fixed pitch put the root
    at (40, 240), under the 373-px ``known`` card (40..413) but for its last 8 px; it now sits at (40, 440).
    """
    return _stack(model, [node.name for node in model.nodes], float(MARGIN))


# ----------------------------------------------------------------------------------------------
# the sidecar
# ----------------------------------------------------------------------------------------------
def _located(path: Path, mark: yaml.Mark, message: str) -> ValueError:
    """A refusal a reader can jump to: ``file:line:col: message`` (marks are 0-based, editors 1-based)."""
    return ValueError(f"{path}:{mark.line + 1}:{mark.column + 1}: {message}")


def _coordinate(path: Path, node: yaml.Node) -> float:
    if not isinstance(node, yaml.ScalarNode):
        raise _located(path, node.start_mark, "a coordinate is not a number")
    try:
        return float(node.value)
    except ValueError:
        raise _located(path, node.start_mark, f"the coordinate {node.value!r} is not a number") from None


def _root(path: Path) -> Optional[yaml.MappingNode]:
    """The sidecar's composed top-level mapping — ``None`` when there is no file or it holds nothing.

    The file is read as a YAML node tree rather than loaded, because a refusal must name the LINE: a
    malformed block is reported as ``<file>:<line>:<col>``, like every other diagnostic in this workspace.
    """
    if not path.exists():
        return None
    text = path.read_text()
    try:
        root = yaml.compose(text)
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        where = f"{path}:{mark.line + 1}:{mark.column + 1}" if mark is not None else str(path)
        raise ValueError(f"{where}: {exc.problem or 'the file is not valid YAML'}") from None
    if root is None:
        return None
    if not isinstance(root, yaml.MappingNode):
        raise _located(path, root.start_mark, "the sidecar is not a mapping of blocks ('positions:', 'groups:')")
    return root


def _block(path: Path, root: Optional[yaml.MappingNode], name: str) -> Optional[yaml.Node]:
    """The value node of block ``name`` — every top-level key is checked, so a stray one is refused by line."""
    if root is None:
        return None
    found: Optional[yaml.Node] = None
    for key_node, value_node in root.value:
        key = str(key_node.value)
        if key not in BLOCKS:
            blocks = ", ".join(f"'{block}:'" for block in BLOCKS)
            raise _located(path, key_node.start_mark, f"the sidecar has no block {key!r} — its blocks are {blocks}")
        if key == name:
            found = value_node
    if isinstance(found, yaml.ScalarNode) and found.tag == "tag:yaml.org,2002:null":
        return None  # `positions:` with nothing under it
    return found


def read_sidecar(path: Path) -> Dict[str, Position]:
    """The ``positions:`` block of a sidecar, in file order — ``{}`` when there is no file, block or entry."""
    block = _block(path, _root(path), "positions")
    if block is None:
        return {}
    if not isinstance(block, yaml.MappingNode):
        raise _located(path, block.start_mark, "the 'positions:' block is not a mapping of node name to [x, y]")
    positions: Dict[str, Position] = {}
    for key_node, value_node in block.value:
        name = str(key_node.value)
        if not isinstance(value_node, yaml.SequenceNode) or len(value_node.value) != 2:
            raise _located(path, value_node.start_mark, f"the position of {name!r} is not a [x, y] pair")
        x_node, y_node = value_node.value
        positions[name] = (_coordinate(path, x_node), _coordinate(path, y_node))
    return positions


def read_groups(path: Path) -> List[Group]:
    """The ``groups:`` block of a sidecar, each group as the page spelled it — ``[]`` without a file or block.

    Only the SHAPE is checked (a list, each entry a mapping), by line; what a group holds is the page's.
    """
    root = _root(path)
    block = _block(path, root, "groups")
    if block is None:
        return []
    if not isinstance(block, yaml.SequenceNode):
        raise _located(path, block.start_mark, "the 'groups:' block is not a list of groups")
    for item in block.value:
        if not isinstance(item, yaml.MappingNode):
            raise _located(path, item.start_mark, "a group is not a mapping (id, title, color, nodes)")
    loaded = yaml.safe_load(path.read_text())
    # The shape was checked on the node tree above; the values are the page's own, stored as it sent them.
    return [cast(Group, dict(group)) for group in loaded["groups"]]


def write_sidecar(path: Path, positions: Mapping[str, Position], groups: Optional[Sequence[Group]] = None) -> None:
    """Write the sidecar: the provenance line, a ``positions:`` block, then a ``groups:`` block.

    A block with nothing in it is left out, so a graph nobody grouped keeps a file of positions alone.
    Coordinates are written as floats so that reading and writing again reproduces the bytes — a ``40``
    read back as ``40.0`` would otherwise rewrite the file on every save.
    """
    if not path.parent.is_dir():
        raise ValueError(f"{path}: the directory {path.parent} does not exist, so the sidecar cannot be written")
    blocks: Dict[str, object] = {}
    if positions:
        blocks["positions"] = {name: [float(x), float(y)] for name, (x, y) in positions.items()}
    if groups:
        blocks["groups"] = [dict(group) for group in groups]
    body = yaml.safe_dump(blocks, sort_keys=False, default_flow_style=None) if blocks else ""
    path.write_text(f"{SIDECAR_HEADER}\n{body}")


def rename_key(name: str, old: str, new: str) -> str:
    """``name`` after the node ``old`` is renamed ``new``: ``old`` itself, and every name DERIVED from it — a
    node inside the subgraph ``old`` (``old/…``: ``prep/grey``, ``prep/inner/x``), a nested marker in one of its
    slots (``old.…``: ``stream.source``, ``maybe.op/grey``) and a member of a list it holds at the top level
    (``old[…``: ``ops[1]``). A step name holds no ``/``, ``.`` or ``[`` (the engine refuses the first two, YAML
    keys of the third are not steps), so each can only mean under ``old``; ``prepare`` is not under ``prep``,
    and ``stream.ops[10]`` is not under ``stream.ops[1]`` (measured before: renaming ``stream`` re-laid
    ``stream.ops[0]`` and ``stream.ops[1]/scaled`` out and left a group naming the old names)."""
    if name == old:
        return new
    if name.startswith((f"{old}/", f"{old}.", f"{old}[")):
        return new + name[len(old) :]
    return name


def rename_member(groups: Sequence[Group], old: str, new: str) -> List[Group]:
    """``groups`` with every member named ``old`` (or under it, :func:`rename_key`) renamed — and the level a
    box is drawn on (``parent``) when it is ``old`` or under it. New dicts; the input is left as it is.

    The one place the server reads inside a group: a rename is ONE operation over every reference to a
    node (its key, its wires, its position, its group), so a group must not keep naming a node that is gone
    — nor a level: renaming the subgraph ``prep`` moves the boxes drawn inside it along with its nodes.
    """
    renamed: List[Group] = []
    for group in groups:
        copy = dict(group)
        members = copy.get("nodes")
        if isinstance(members, list):
            copy["nodes"] = [rename_key(member, old, new) if isinstance(member, str) else member for member in members]
        parent = copy.get("parent")
        if isinstance(parent, str) and parent:
            copy["parent"] = rename_key(parent, old, new)
        renamed.append(cast(Group, copy))
    return renamed


def map_members(groups: Sequence[Group], moved: Callable[[str], Optional[str]]) -> List[Group]:
    """``groups`` with every member name — and the level a box is drawn on (``parent``) — passed through
    ``moved``: a new name, the same one, or ``None`` for a node that is gone (dropped from ``nodes``; a
    ``parent`` that is gone is kept as it was, the page draws the box where it can). New dicts, input untouched.

    The shift of a list's positional member names (a Convert or a Paste of ``ops`` members) goes through here,
    as a rename goes through :func:`rename_member`."""
    mapped: List[Group] = []
    for group in groups:
        copy = dict(group)
        members = copy.get("nodes")
        if isinstance(members, list):
            kept = [moved(member) if isinstance(member, str) else member for member in members]
            copy["nodes"] = [member for member in kept if member is not None]
        parent = copy.get("parent")
        if isinstance(parent, str) and parent:
            copy["parent"] = moved(parent) or parent
        mapped.append(cast(Group, copy))
    return mapped


# ----------------------------------------------------------------------------------------------
# the merge
# ----------------------------------------------------------------------------------------------
def positions(model: GraphLike, sidecar: Path, *, kept: Optional[Mapping[str, Position]] = None) -> Dict[str, Position]:
    """Every node's position, in the model's order: the sidecar's where it names the node, laid out otherwise.

    Fresh nodes keep their layer's column and stack among THEMSELVES (:func:`layered_layout`'s rule), starting
    below the lowest BOTTOM of the kept nodes — a kept card's top plus its pitch, so a tall card reaches further
    down than a short one below it — never on top of an arranged one. A sidecar name the model no longer has is
    left out; :func:`stale_positions` names it. ``kept`` replaces the sidecar's positions when the caller
    holds a whole set of its own (the editor's, after a list's members were re-keyed — the sidecar then
    places a member under a name another member had).
    """
    kept = read_sidecar(sidecar) if kept is None else dict(kept)
    names = [node.name for node in model.nodes]
    fresh = [name for name in names if name not in kept]
    placed: Dict[str, Position] = {}
    if fresh:
        bottoms = (kept[node.name][1] + _pitch(node) for node in model.nodes if node.name in kept)
        placed = _stack(model, fresh, max(bottoms, default=float(MARGIN)))
    return {name: kept[name] if name in kept else placed[name] for name in names}


def stale_positions(model: GraphLike, sidecar: Path) -> List[str]:
    """The sidecar's names the model no longer has, in file order — a renamed or deleted node's old entry."""
    known = {node.name for node in model.nodes}
    return [name for name in read_sidecar(sidecar) if name not in known]
