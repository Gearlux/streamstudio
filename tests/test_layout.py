"""Node positions: a deterministic layered layout, and a sidecar that remembers where the user put things.

The document holds no positions (``docs/rewrite.md`` §2.1: positions live in a NAMED sidecar,
``<document>.layout.yaml``, keyed by node name). ``layered_layout`` draws a graph nobody has arranged
yet — longest-path layering along the wires, one column per layer, one row per node within a layer,
in the order the model lists them. ``positions`` merges the two: a node named in the sidecar keeps its
place, the rest are laid out BELOW the lowest kept row so they never land on top of something the user
arranged, and a sidecar name the model no longer has is dropped (``stale_positions`` reports it).

The tests build tiny stand-ins for the model — ``layout`` depends only on the SHAPE (``.nodes`` with a
``.name``, ``.wires`` with ``.source``/``.target``), never on ``streamstudio.document``.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from streamstudio.layout import (
    COLUMN_DX,
    MARGIN,
    ROW_DY,
    layered_layout,
    positions,
    read_sidecar,
    sidecar_path,
    stale_positions,
    write_sidecar,
)


@dataclass
class Node:
    name: str


@dataclass
class Wire:
    source: str
    target: str


@dataclass
class Graph:
    nodes: List[Node] = field(default_factory=list)
    wires: List[Wire] = field(default_factory=list)


def chain(*names: str) -> Graph:
    return Graph([Node(n) for n in names], [Wire(a, b) for a, b in zip(names, names[1:])])


def column(layer: int) -> float:
    return MARGIN + layer * COLUMN_DX


def row(index: int) -> float:
    return MARGIN + index * ROW_DY


class TestLayeredLayout:
    def test_the_constants_are_the_canvas_grid(self) -> None:
        assert (COLUMN_DX, ROW_DY, MARGIN) == (340, 200, 40)

    def test_a_chain_of_five_lays_out_in_one_row(self) -> None:
        pos = layered_layout(chain("a", "b", "c", "d", "e"))
        assert pos == {
            "a": (column(0), row(0)),
            "b": (column(1), row(0)),
            "c": (column(2), row(0)),
            "d": (column(3), row(0)),
            "e": (column(4), row(0)),
        }

    def test_a_fork_puts_the_two_branches_in_two_rows_of_the_same_layer(self) -> None:
        g = Graph(
            [Node("a"), Node("b"), Node("c"), Node("d")],
            [Wire("a", "b"), Wire("a", "c"), Wire("b", "d"), Wire("c", "d")],
        )
        pos = layered_layout(g)
        assert pos["a"] == (column(0), row(0))
        assert pos["b"] == (column(1), row(0))
        assert pos["c"] == (column(1), row(1))
        assert pos["d"] == (column(2), row(0))

    def test_longest_path_wins_when_a_node_is_reached_two_ways(self) -> None:
        # a -> d directly and a -> b -> c -> d: d sits AFTER c, not beside b
        g = Graph(
            [Node("a"), Node("b"), Node("c"), Node("d")],
            [Wire("a", "b"), Wire("b", "c"), Wire("c", "d"), Wire("a", "d")],
        )
        assert layered_layout(g)["d"] == (column(3), row(0))

    def test_a_node_with_no_wires_sits_in_layer_zero(self) -> None:
        g = Graph([Node("a"), Node("b"), Node("lonely")], [Wire("a", "b")])
        pos = layered_layout(g)
        assert pos["lonely"] == (column(0), row(1))

    def test_rows_follow_the_order_the_model_lists_the_nodes(self) -> None:
        g = Graph([Node("second"), Node("first")], [])
        pos = layered_layout(g)
        assert pos["second"] == (column(0), row(0))
        assert pos["first"] == (column(0), row(1))

    def test_a_wire_to_an_unknown_node_is_ignored(self) -> None:
        g = Graph([Node("a"), Node("b")], [Wire("a", "b"), Wire("a", "ghost"), Wire("ghost", "b")])
        assert layered_layout(g) == {"a": (column(0), row(0)), "b": (column(1), row(0))}

    def test_a_cycle_does_not_hang_and_stays_deterministic(self) -> None:
        g = Graph([Node("a"), Node("b")], [Wire("a", "b"), Wire("b", "a")])
        first = layered_layout(g)
        assert first == layered_layout(g)
        assert first["a"] == (column(0), row(0))
        assert first["b"] == (column(1), row(0))

    def test_two_calls_agree(self) -> None:
        g = Graph(
            [Node(n) for n in "abcdefg"],
            [Wire("a", "b"), Wire("a", "c"), Wire("c", "d"), Wire("b", "d"), Wire("d", "e"), Wire("e", "g")],
        )
        assert layered_layout(g) == layered_layout(g)

    def test_an_empty_model_lays_out_nothing(self) -> None:
        assert layered_layout(Graph()) == {}


class TestSidecar:
    def test_the_sidecar_sits_beside_the_document(self) -> None:
        assert sidecar_path(Path("panes/bte_view.yaml")) == Path("panes/bte_view.layout.yaml")

    def test_round_trip_is_byte_stable(self, tmp_path: Path) -> None:
        side = tmp_path / "g.layout.yaml"
        wanted: Dict[str, Tuple[float, float]] = {"window": (40.0, 40.0), "stream.ops[1]": (380, 240), "c": (720.5, 40)}
        write_sidecar(side, wanted)
        first = side.read_bytes()
        got = read_sidecar(side)
        assert got == {"window": (40.0, 40.0), "stream.ops[1]": (380.0, 240.0), "c": (720.5, 40.0)}
        assert list(got) == ["window", "stream.ops[1]", "c"]
        write_sidecar(side, got)
        assert side.read_bytes() == first

    def test_the_file_is_one_line_per_node_under_a_provenance_comment(self, tmp_path: Path) -> None:
        side = tmp_path / "g.layout.yaml"
        write_sidecar(side, {"a": (40.0, 40.0), "b": (380.0, 40.0)})
        lines = side.read_text().splitlines()
        assert lines[0].startswith("# ")
        assert "streamstudio.layout" in lines[0]
        assert lines[1:] == ["a: [40.0, 40.0]", "b: [380.0, 40.0]"]

    def test_a_missing_sidecar_reads_as_empty(self, tmp_path: Path) -> None:
        assert read_sidecar(tmp_path / "absent.layout.yaml") == {}

    def test_an_empty_or_comment_only_sidecar_reads_as_empty(self, tmp_path: Path) -> None:
        side = tmp_path / "g.layout.yaml"
        side.write_text("")
        assert read_sidecar(side) == {}
        side.write_text("# nothing yet\n")
        assert read_sidecar(side) == {}

    @pytest.mark.parametrize(
        "text, where, why",
        [
            ("a: 7\n", "2:4", "not a [x, y] pair"),
            ("a: [1, 2, 3]\n", "2:4", "not a [x, y] pair"),
            ("a: [x, 1]\n", "2:5", "not a number"),
            ("- 1\n- 2\n", "1:1", "not a mapping"),
            ("a: [1, 2\nb: 3\n", "3:2", "expected ',' or ']'"),
        ],
    )
    def test_a_malformed_sidecar_is_refused_by_file_line_and_column(
        self, tmp_path: Path, text: str, where: str, why: str
    ) -> None:
        side = tmp_path / "g.layout.yaml"
        side.write_text("# header\n" + text if not text.startswith("-") else text)
        with pytest.raises(ValueError) as caught:
            read_sidecar(side)
        message = str(caught.value)
        assert message.startswith(f"{side}:{where}: "), message
        assert why in message

    def test_writing_into_a_missing_directory_is_refused_by_name(self, tmp_path: Path) -> None:
        side = tmp_path / "nowhere" / "g.layout.yaml"
        with pytest.raises(ValueError, match=str(tmp_path / "nowhere")):
            write_sidecar(side, {"a": (0.0, 0.0)})


class TestPositions:
    def test_without_a_sidecar_the_layout_is_the_layered_one(self, tmp_path: Path) -> None:
        g = chain("a", "b", "c")
        assert positions(g, tmp_path / "g.layout.yaml") == layered_layout(g)

    def test_a_moved_node_keeps_its_place_and_the_rest_go_below_it(self, tmp_path: Path) -> None:
        g = chain("a", "b", "c")
        side = tmp_path / "g.layout.yaml"
        write_sidecar(side, {"b": (1000.0, 900.0)})
        pos = positions(g, side)
        assert pos["b"] == (1000.0, 900.0)
        # the fresh nodes keep their LAYER (x) and start one row under the lowest kept node
        assert pos["a"] == (column(0), 900.0 + ROW_DY)
        assert pos["c"] == (column(2), 900.0 + ROW_DY)
        assert list(pos) == ["a", "b", "c"]  # model order, whatever the sidecar's

    def test_fresh_nodes_stack_rows_among_themselves(self, tmp_path: Path) -> None:
        g = Graph([Node("a"), Node("b"), Node("c"), Node("d")], [Wire("a", "b"), Wire("a", "c"), Wire("a", "d")])
        side = tmp_path / "g.layout.yaml"
        write_sidecar(side, {"a": (40.0, 40.0), "b": (380.0, 40.0)})
        pos = positions(g, side)
        assert pos["c"] == (column(1), 40.0 + ROW_DY)
        assert pos["d"] == (column(1), 40.0 + 2 * ROW_DY)

    def test_a_saved_layout_comes_back_unchanged(self, tmp_path: Path) -> None:
        g = chain("a", "b", "c")
        side = tmp_path / "g.layout.yaml"
        arranged = {"a": (10.0, 10.0), "b": (500.0, 300.0), "c": (900.0, 10.0)}
        write_sidecar(side, arranged)
        assert positions(g, side) == arranged

    def test_a_stale_name_is_reported_not_kept(self, tmp_path: Path) -> None:
        g = chain("a", "b")
        side = tmp_path / "g.layout.yaml"
        write_sidecar(side, {"a": (40.0, 40.0), "renamed_away": (380.0, 40.0), "b": (720.0, 40.0)})
        assert stale_positions(g, side) == ["renamed_away"]
        assert "renamed_away" not in positions(g, side)
        assert stale_positions(g, tmp_path / "absent.layout.yaml") == []

    def test_two_calls_agree(self, tmp_path: Path) -> None:
        g = Graph([Node(n) for n in "abcde"], [Wire("a", "b"), Wire("a", "c"), Wire("b", "d")])
        side = tmp_path / "g.layout.yaml"
        write_sidecar(side, {"c": (700.0, 700.0)})
        assert positions(g, side) == positions(g, side)


def test_bte_view_derives_into_twenty_four_layers_one_column_apart() -> None:
    """The shipped Bluetooth pane is a 24-step chain: every step is its own layer, 340 px from the last."""
    waivefront = pytest.importorskip("waivefront")
    try:
        from streamstudio.document import Document
    except ImportError as exc:  # document.py lands in the same phase from another change
        pytest.skip(f"streamstudio.document is not importable yet: {exc}")
    path = Path(waivefront.__file__).parent / "annotation" / "bte_view.yaml"
    model = Document.open(path).model
    pos = layered_layout(model)
    assert len(pos) == 24
    xs = sorted(x for x, _ in pos.values())
    assert xs == [column(i) for i in range(24)]
    assert {y for _, y in pos.values()} == {row(0)}
