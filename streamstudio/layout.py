"""Where a node is drawn: a deterministic first layout, and a sidecar that keeps what the user moved.

The confluid document is the only truth of a graph (``docs/rewrite.md`` §2.1) and it says nothing
about pixels — adding ``x:``/``y:`` keys to it would turn a pipeline file into a drawing file and
make every drag a document edit. Positions therefore live in a SIDECAR, ``<document>.layout.yaml``,
a mapping ``{node name: [x, y]}``. It is keyed by the node NAME (a flow step's key, ``stream.ops[1]``,
``contract`` — the one naming scheme the engine, the tools and the canvas share), never by an id
written into the YAML: a sidecar is a convenience, and a graph must load, run and export with the
sidecar deleted. The known cost, measured on the prototypes, is that RENAMING a node orphans its
entry — the editor's rename re-keys the sidecar in the same operation, and :func:`stale_positions`
reports what is left so the caller can say so.

The first layout is the plainest one that reads as a pipeline: longest-path layering along the
wires (a node's layer is one past the deepest node feeding it; a node nothing feeds sits in layer
0), one column per layer, one row per node within a layer in the order the model lists them. No
crossing minimisation — a pipeline is a one-direction chain with a few forks, and a stable
result (the same document always draws the same way) is worth more than a prettier one that
moves when a step is added. :func:`positions` merges the two: a node the sidecar names keeps its
place, the rest are laid out in their layer's column starting one row BELOW the lowest kept node,
so a step an agent added never lands on top of something the user arranged.

Only the model's SHAPE is read (``.nodes`` with a ``.name``, ``.wires`` with ``.source`` and
``.target``), so this module imports no document code and a test drives it with three dataclasses.
"""

from pathlib import Path
from typing import Dict, List, Mapping, Protocol, Sequence, Tuple

import yaml

#: Horizontal distance between two layers, in canvas pixels — a node card plus a wire's breathing room.
COLUMN_DX = 340
#: Vertical distance between two rows of one layer — the tallest common node card plus a margin.
ROW_DY = 200
#: Where the first column and the first row start, so a node is never flush against the canvas edge.
MARGIN = 40

#: A node position in canvas pixels, ``(x, y)`` of its top-left corner.
Position = Tuple[float, float]

#: The sidecar's first line. It names the writer and the two ways to get a fresh layout, because a reader
#: who finds this file beside a graph must be able to tell what it is without opening the code.
SIDECAR_HEADER = (
    "# streamstudio layout sidecar — {node name: [x, y]} in canvas pixels, keyed by node name; written by "
    "streamstudio.layout.write_sidecar. Delete a line to lay that node out again, the file to lay out everything."
)


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


def _place(layer: int, row: int) -> Position:
    return (float(MARGIN + layer * COLUMN_DX), float(MARGIN + row * ROW_DY))


def layered_layout(model: GraphLike) -> Dict[str, Position]:
    """A position per node: one column per layer, one row per node within it, in the model's order."""
    layers = _layers(model)
    rows: Dict[int, int] = {}
    placed: Dict[str, Position] = {}
    for node in model.nodes:
        depth = layers[node.name]
        placed[node.name] = _place(depth, rows.get(depth, 0))
        rows[depth] = rows.get(depth, 0) + 1
    return placed


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


def read_sidecar(path: Path) -> Dict[str, Position]:
    """The positions a sidecar holds, in file order — ``{}`` when there is no file (or it is empty).

    The file is read as a YAML node tree rather than loaded, because a refusal must name the LINE: a
    value that is not a ``[x, y]`` pair is reported as ``<file>:<line>:<col>``, like every other
    diagnostic in this workspace.
    """
    if not path.exists():
        return {}
    text = path.read_text()
    try:
        root = yaml.compose(text)
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        where = f"{path}:{mark.line + 1}:{mark.column + 1}" if mark is not None else str(path)
        raise ValueError(f"{where}: {exc.problem or 'the file is not valid YAML'}") from None
    if root is None:
        return {}
    if not isinstance(root, yaml.MappingNode):
        raise _located(path, root.start_mark, "the sidecar is not a mapping of node name to [x, y]")
    positions: Dict[str, Position] = {}
    for key_node, value_node in root.value:
        name = str(key_node.value)
        if not isinstance(value_node, yaml.SequenceNode) or len(value_node.value) != 2:
            raise _located(path, value_node.start_mark, f"the position of {name!r} is not a [x, y] pair")
        x_node, y_node = value_node.value
        positions[name] = (_coordinate(path, x_node), _coordinate(path, y_node))
    return positions


def write_sidecar(path: Path, positions: Mapping[str, Position]) -> None:
    """Write ``positions`` as the sidecar: the provenance line, then one ``name: [x, y]`` line per node.

    Coordinates are written as floats so that reading and writing again reproduces the bytes — a
    ``40`` read back as ``40.0`` would otherwise rewrite the file on every save.
    """
    if not path.parent.is_dir():
        raise ValueError(f"{path}: the directory {path.parent} does not exist, so the sidecar cannot be written")
    body = yaml.safe_dump(
        {name: [float(x), float(y)] for name, (x, y) in positions.items()},
        sort_keys=False,
        default_flow_style=None,
    )
    path.write_text(f"{SIDECAR_HEADER}\n{body}" if positions else f"{SIDECAR_HEADER}\n")


# ----------------------------------------------------------------------------------------------
# the merge
# ----------------------------------------------------------------------------------------------
def positions(model: GraphLike, sidecar: Path) -> Dict[str, Position]:
    """Every node's position, in the model's order: the sidecar's where it names the node, laid out otherwise.

    Fresh nodes keep their layer's column and take rows among THEMSELVES, starting one row below the
    lowest kept node — never on top of an arranged one. A sidecar name the model no longer has is
    left out; :func:`stale_positions` names it.
    """
    kept = read_sidecar(sidecar)
    names = [node.name for node in model.nodes]
    fresh = [name for name in names if name not in kept]
    placed: Dict[str, Position] = {}
    if fresh:
        layers = _layers(model)
        lowest = max((kept[name][1] for name in names if name in kept), default=float(MARGIN) - ROW_DY)
        shift = lowest + ROW_DY - MARGIN
        rows: Dict[int, int] = {}
        for name in fresh:
            depth = layers[name]
            x, y = _place(depth, rows.get(depth, 0))
            placed[name] = (x, y + shift)
            rows[depth] = rows.get(depth, 0) + 1
    return {name: kept[name] if name in kept else placed[name] for name in names}


def stale_positions(model: GraphLike, sidecar: Path) -> List[str]:
    """The sidecar's names the model no longer has, in file order — a renamed or deleted node's old entry."""
    known = {node.name for node in model.nodes}
    return [name for name in read_sidecar(sidecar) if name not in known]
