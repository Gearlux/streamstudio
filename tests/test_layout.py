"""Node positions: a deterministic layered layout, and a sidecar that remembers where the user put things.

The document holds no positions (``docs/rewrite.md`` §2.1: positions live in a NAMED sidecar,
``<document>.layout.yaml``, a ``positions:`` block keyed by node name beside a ``groups:`` block the
canvas draws — a group is a title, a colour and member names, stored as the page spells it).
``layered_layout`` draws a graph nobody has arranged
yet — longest-path layering along the wires, one column per layer, one row per node within a layer,
in the order the model lists them. ``positions`` merges the two: a node named in the sidecar keeps its
place, the rest are laid out BELOW the lowest kept row so they never land on top of something the user
arranged, and a sidecar name the model no longer has is dropped (``stale_positions`` reports it).

The tests build tiny stand-ins for the model — ``layout`` depends only on the SHAPE (``.nodes`` with a
``.name``, ``.wires`` with ``.source``/``.target``), never on ``streamstudio.document``.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple, cast

import pytest

from streamstudio.layout import (
    COLUMN_DX,
    MARGIN,
    ROW_DY,
    Group,
    layered_layout,
    positions,
    read_groups,
    read_sidecar,
    rename_member,
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
        assert lines[1:] == ["positions:", "  a: [40.0, 40.0]", "  b: [380.0, 40.0]"]

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
            ("positions: {a: 7}\n", "2:16", "not a [x, y] pair"),
            ("positions: {a: [1, 2, 3]}\n", "2:16", "not a [x, y] pair"),
            ("positions: {a: [x, 1]}\n", "2:17", "not a number"),
            ("positions: [1, 2]\n", "2:12", "not a mapping"),
            ("a: [1, 2]\n", "2:1", "no block 'a'"),
            ("- 1\n- 2\n", "1:1", "not a mapping"),
            ("positions: {a: [1, 2\nb: 3\n", "3:2", "expected ',' or ']'"),
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


GROUPS: List[Group] = [
    {"id": "g1", "title": "front end", "color": "#3b82f6", "nodes": ["a", "stream.ops[1]"]},
]


class TestGroups:
    """The ``groups:`` block: what the canvas draws around nodes, kept beside the positions and interpreted by
    the server ONLY to re-key a renamed member — everything else in a group is the page's own."""

    def test_groups_round_trip_beside_the_positions_byte_stable(self, tmp_path: Path) -> None:
        side = tmp_path / "g.layout.yaml"
        write_sidecar(side, {"a": (40.0, 40.0), "stream.ops[1]": (380.0, 240.0)}, groups=GROUPS)
        first = side.read_bytes()
        assert read_groups(side) == GROUPS
        assert read_sidecar(side) == {"a": (40.0, 40.0), "stream.ops[1]": (380.0, 240.0)}
        write_sidecar(side, read_sidecar(side), groups=read_groups(side))
        assert side.read_bytes() == first
        assert side.read_text().splitlines()[1:] == [
            "positions:",
            "  a: [40.0, 40.0]",
            "  stream.ops[1]: [380.0, 240.0]",
            "groups:",
            "- id: g1",
            "  title: front end",
            "  color: '#3b82f6'",
            "  nodes: [a, 'stream.ops[1]']",
        ]

    def test_a_sidecar_without_a_groups_block_reads_as_no_groups(self, tmp_path: Path) -> None:
        side = tmp_path / "g.layout.yaml"
        write_sidecar(side, {"a": (40.0, 40.0)})
        assert "groups" not in side.read_text()
        assert read_groups(side) == []
        assert read_groups(tmp_path / "absent.layout.yaml") == []

    def test_groups_alone_write_no_positions_block(self, tmp_path: Path) -> None:
        side = tmp_path / "g.layout.yaml"
        write_sidecar(side, {}, groups=GROUPS)
        assert side.read_text().splitlines()[1] == "groups:"
        assert read_sidecar(side) == {}
        assert read_groups(side) == GROUPS

    def test_a_group_is_stored_as_the_page_spelled_it(self, tmp_path: Path) -> None:
        """Opaque: a member the model may not have and a key the server never reads both come back as given."""
        side = tmp_path / "g.layout.yaml"
        # a key Group does not declare: cast, because that is exactly what the page may send
        odd: List[Group] = [
            cast(Group, {"id": "g2", "title": "x", "color": "red", "nodes": ["nosuch"], "note": "kept"})
        ]
        write_sidecar(side, {}, groups=odd)
        assert read_groups(side) == odd

    @pytest.mark.parametrize(
        "text, where, why",
        [
            ("groups: {a: 1}\n", "2:9", "not a list of groups"),
            ("groups: [1]\n", "2:10", "a group is not a mapping"),
        ],
    )
    def test_a_malformed_groups_block_is_refused_by_file_line_and_column(
        self, tmp_path: Path, text: str, where: str, why: str
    ) -> None:
        side = tmp_path / "g.layout.yaml"
        side.write_text("# header\n" + text)
        with pytest.raises(ValueError) as caught:
            read_groups(side)
        message = str(caught.value)
        assert message.startswith(f"{side}:{where}: "), message
        assert why in message

    def test_rename_member_re_keys_every_group_naming_the_node_and_nothing_else(self) -> None:
        groups: List[Group] = [
            {"id": "g1", "title": "front", "color": "#111", "nodes": ["a", "b"]},
            {"id": "g2", "title": "back", "color": "#222", "nodes": ["c"]},
            {"id": "g3", "title": "odd", "color": "#333"},  # no member list: left as it is
        ]
        renamed = rename_member(groups, "a", "z")
        assert renamed == [
            {"id": "g1", "title": "front", "color": "#111", "nodes": ["z", "b"]},
            {"id": "g2", "title": "back", "color": "#222", "nodes": ["c"]},
            {"id": "g3", "title": "odd", "color": "#333"},
        ]
        assert groups[0]["nodes"] == ["a", "b"]  # the input is not edited in place
        assert rename_member(groups, "nosuch", "z") == groups

    def test_bounds_and_parent_are_declared_and_round_trip_as_sent(self, tmp_path: Path) -> None:
        """An EMPTY box (Add Group) or a resized one carries ``bounds: [x, y, w, h]``; a box drawn inside a
        subgraph names its level in ``parent`` (``""`` = the top). Both are typed on ``Group`` and stored as
        the page sent them — the server still reads nothing in a group but its member list."""
        side = tmp_path / "g.layout.yaml"
        boxes: List[Group] = [
            {"id": "g1", "title": "empty", "color": "#111", "nodes": [], "bounds": [10.0, 20.0, 300.0, 200.0]},
            {"id": "g2", "title": "inside", "color": "#222", "nodes": ["prep/grey"], "parent": "prep"},
            {"id": "g3", "title": "top", "color": "#333", "nodes": ["a"], "parent": "", "bounds": [0.0, 0.0, 1.5, 2.5]},
        ]
        write_sidecar(side, {"a": (40.0, 40.0)}, groups=boxes)
        assert read_groups(side) == boxes
        assert set(Group.__annotations__) >= {"bounds", "parent"}

    def test_rename_member_keeps_every_key_it_does_not_read(self) -> None:
        """CON case of the one place the server reads a group: renaming a member rewrites ``nodes`` and
        leaves ``bounds``, ``parent`` and any key the page invents exactly as they were."""
        group = cast(
            Group,
            {"id": "g1", "title": "t", "color": "#111", "nodes": ["a"], "bounds": [1.0, 2.0, 3.0, 4.0], "parent": "p"},
        )
        group_with_extra = cast(Group, {**group, "note": "kept"})
        assert rename_member([group_with_extra], "a", "b") == [{**group_with_extra, "nodes": ["b"]}]

    def test_renaming_a_subgraph_re_keys_the_members_inside_it_and_the_level_a_box_lives_on(self) -> None:
        """A node inside a subgraph is named ``prep/grey`` and a box drawn inside names its level ``prep`` in
        ``parent``: renaming the subgraph ``prep`` renames every name UNDER it (a step name holds no ``/``, so
        ``prep/`` can only mean inside ``prep``) — and a sibling that merely starts with the same letters stays."""
        groups: List[Group] = [
            {"id": "g1", "title": "in", "color": "#111", "nodes": ["prep/grey", "prep/inner/x"], "parent": "prep"},
            {"id": "g2", "title": "deeper", "color": "#222", "nodes": ["prep/inner/x"], "parent": "prep/inner"},
            {"id": "g3", "title": "top", "color": "#333", "nodes": ["prep", "prepare", "prepare/x"], "parent": ""},
        ]
        assert rename_member(groups, "prep", "pre") == [
            {"id": "g1", "title": "in", "color": "#111", "nodes": ["pre/grey", "pre/inner/x"], "parent": "pre"},
            {"id": "g2", "title": "deeper", "color": "#222", "nodes": ["pre/inner/x"], "parent": "pre/inner"},
            {"id": "g3", "title": "top", "color": "#333", "nodes": ["pre", "prepare", "prepare/x"], "parent": ""},
        ]


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


def test_a_node_without_wires_goes_below_the_wired_ones_in_its_column() -> None:
    """An unwired node (a graph's contract root whose host inputs no step names) must not push the
    chain down a row: within a column the wired nodes take the top rows, the unwired ones follow, each
    group in the model's order. Measured before this rule on the shipped Bluetooth pane: the contract
    root took row 0 of the first column and the first step dropped to row 1 while the rest stayed up."""
    g = Graph([Node("root"), Node("a"), Node("b"), Node("c")], [Wire("a", "b"), Wire("b", "c")])
    pos = layered_layout(g)
    assert pos["a"] == (column(0), row(0))
    assert pos["root"] == (column(0), row(1))
    assert [pos[n][1] for n in "abc"] == [row(0)] * 3


def test_positions_without_a_sidecar_place_the_unwired_node_below_too(tmp_path: Path) -> None:
    """The server's merge (``positions``) lays out fresh nodes by the SAME row rule as
    ``layered_layout`` — measured before: the rule lived only in ``layered_layout``, so the page, which
    asks the server, still drew the Bluetooth pane's contract root above its first step."""
    g = Graph([Node("root"), Node("a"), Node("b"), Node("c")], [Wire("a", "b"), Wire("b", "c")])
    assert positions(g, tmp_path / "absent.layout.yaml") == layered_layout(g)


def test_a_column_of_only_unwired_nodes_keeps_the_models_order() -> None:
    """CON case: with no wires at all nothing is reordered — the column reads top to bottom as the model."""
    g = Graph([Node("z"), Node("y"), Node("x")], [])
    pos = layered_layout(g)
    assert [pos[n] for n in "zyx"] == [(column(0), row(0)), (column(0), row(1)), (column(0), row(2))]


def test_bte_view_derives_into_twenty_four_layers_one_column_apart() -> None:
    """The shipped Bluetooth pane is a 24-step chain plus its contract root (the host's inputs, which no
    step names by entry, so it carries no wire): the steps stay one row, 340 px apart, and the root sits
    under the first step."""
    waivefront = pytest.importorskip("waivefront")
    try:
        from streamstudio.document import Document
    except ImportError as exc:  # document.py lands in the same phase from another change
        pytest.skip(f"streamstudio.document is not importable yet: {exc}")
    path = Path(waivefront.__file__).parent / "annotation" / "bte_view.yaml"
    model = Document.open(path).model
    pos = layered_layout(model)
    assert len(pos) == 25
    steps = {name: xy for name, xy in pos.items() if name != "contract"}
    xs = sorted(x for x, _ in steps.values())
    assert xs == [column(i) for i in range(24)]
    assert {y for _, y in steps.values()} == {row(0)}
    assert pos["contract"] == (column(0), row(1))
