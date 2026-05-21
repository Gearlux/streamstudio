"""Graph ⇄ Confluid YAML round-trip tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fluxstudio.graph import Graph, GraphEdge, GraphNode


def _example_graph() -> Graph:
    return Graph(
        nodes=[
            GraphNode(
                id="n0",
                op_path="fluxstudio._fixtures:NumericArraySource",
                params={"count": 3, "size": 4},
            ),
            GraphNode(
                id="n1",
                op_path="dataflux.ops.numpy:RescaleOp",
                params={"in_min": 0.0, "in_max": 255.0},
            ),
            GraphNode(
                id="n2",
                op_path="dataflux.ops.numpy:StandardizeOp",
                params={"mean": 0.5, "std": 0.5},
            ),
        ],
        edges=[
            GraphEdge(source="n0", target="n1"),
            GraphEdge(source="n1", target="n2"),
        ],
    )


def test_graph_to_yaml_uses_class_tags() -> None:
    g = _example_graph()
    text = g.to_confluid_yaml()
    # Tags emit with the !class:X() Instance form Confluid needs to materialize
    # the marker into a live object during load.
    assert "!class:dataflux.core.Flux()" in text
    assert "!class:dataflux.ops.numpy.RescaleOp()" in text
    assert "!class:dataflux.ops.numpy.StandardizeOp()" in text


def test_graph_round_trip_preserves_structure() -> None:
    g = _example_graph()
    text = g.to_confluid_yaml()
    g2 = Graph.from_confluid_yaml(text)
    assert [n.op_path for n in g2.nodes] == [n.op_path for n in g.nodes]
    for original, parsed in zip(g.nodes, g2.nodes):
        assert original.params == parsed.params
    assert len(g2.edges) == len(g.edges) == 2


def test_load_tiny_pipeline_yaml_succeeds(tiny_pipeline_yaml: Path) -> None:
    g = Graph.from_confluid_yaml(tiny_pipeline_yaml.read_text())
    assert [n.op_path for n in g.nodes] == [
        "fluxstudio._fixtures:NumericArraySource",
        "dataflux.ops.numpy:RescaleOp",
        "dataflux.ops.numpy:StandardizeOp",
    ]


def test_empty_graph_serialization_raises() -> None:
    g = Graph(nodes=[], edges=[])
    with pytest.raises(ValueError, match="empty Graph"):
        g.to_confluid_yaml()


def test_yaml_without_top_level_flux_raises() -> None:
    text = "!class:somewhere.else.Thing\n"
    with pytest.raises(ValueError):
        Graph.from_confluid_yaml(text)


def test_yaml_with_non_class_source_raises() -> None:
    text = "!class:dataflux.core.Flux\nsource: 42\nops: []\n"
    with pytest.raises(ValueError, match="source"):
        Graph.from_confluid_yaml(text)


def test_yaml_with_plain_dict_top_level_raises() -> None:
    # No !class: tag — pyyaml parses it as a plain dict, which Graph rejects.
    plain = yaml.safe_dump({"foo": "bar"})
    with pytest.raises(ValueError):
        Graph.from_confluid_yaml(plain)


def test_yaml_with_non_class_op_raises() -> None:
    text = (
        "!class:dataflux.core.Flux\n"
        "source: !class:fluxstudio._fixtures.NumericArraySource\n"
        "  count: 1\n"
        "ops:\n"
        "  - 'plain string instead of class'\n"
    )
    with pytest.raises(ValueError, match="!class:"):
        Graph.from_confluid_yaml(text)


def test_inconsistent_edges_raise() -> None:
    g = Graph(
        nodes=[
            GraphNode(id="n0", op_path="x:Y"),
            GraphNode(id="n1", op_path="x:Z"),
        ],
        edges=[GraphEdge(source="ghost", target="n1")],
    )
    with pytest.raises(ValueError, match="unseen source"):
        g.to_confluid_yaml()
