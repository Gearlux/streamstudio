"""Graph model + Confluid-YAML translation.

A `Graph` is the JSON-shaped object the React frontend exchanges with the
backend. It lowers to a Confluid document where every node is a YAML
`!class:dotted.path:ClassName` tag whose body is the constructor params, e.g.::

    !class:dataflux.core:Flux
    source: !class:waivefront.processing:Some
      param1: value
    ops:
      - !class:dataflux.ops.numpy:RescaleOp
        low: 0.0
        high: 1.0

Branching DAGs are out-of-scope for the slice — only linear pipelines are
supported. We use a tag-aware PyYAML Loader/Dumper rather than `confluid.load`
because Graph round-trip must NOT instantiate ops (we want to introspect /
edit the graph without importing every op the user has typed).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import yaml
from logflow import get_logger
from pydantic import BaseModel, Field

logger = get_logger(__name__)

_CLASS_TAG_PREFIX = "!class:"


def _op_path_to_tag_suffix(op_path: str) -> str:
    """`'pkg.sub:Class'` (dataflux.discovery format) → `'pkg.sub.Class()'`
    (Confluid !class: Instance form). Trailing parens are mandatory: without
    them Confluid keeps the marker as a deferred Class and the Flux's source
    and ops never materialize.
    """
    if ":" in op_path:
        module, _, name = op_path.rpartition(":")
        return f"{module}.{name}()"
    if op_path.endswith("()"):
        return op_path
    return f"{op_path}()"


def _tag_suffix_to_op_path(suffix: str) -> str:
    """Inverse: `'pkg.sub.Class()'` or `'pkg.sub.Class'` → `'pkg.sub:Class'`."""
    bare = suffix[:-2] if suffix.endswith("()") else suffix
    if "." not in bare:
        return bare
    module, _, name = bare.rpartition(".")
    return f"{module}:{name}"


class _ClassNode:
    """In-memory placeholder for a `!class:X` YAML tag during round-trip.

    Holds the dotted target plus the mapping body (constructor kwargs). We
    deliberately keep this dumb — it never imports or instantiates the target.
    """

    def __init__(self, target: str, params: Optional[Dict[str, Any]] = None) -> None:
        self.target = target
        self.params: Dict[str, Any] = params or {}


class _GraphLoader(yaml.SafeLoader):
    pass


class _GraphDumper(yaml.SafeDumper):
    pass


def _construct_class(loader: yaml.Loader, suffix: str, node: yaml.Node) -> _ClassNode:
    params: Dict[str, Any]
    if isinstance(node, yaml.MappingNode):
        raw = loader.construct_mapping(node, deep=True)
        params = {str(k): v for k, v in raw.items()}
    elif isinstance(node, yaml.ScalarNode) and (node.value or "") == "":
        params = {}
    else:
        raise yaml.YAMLError(f"!class:{suffix} must be a mapping or empty scalar; got {type(node).__name__}")
    return _ClassNode(target=suffix, params=params)


def _represent_class(dumper: yaml.Dumper, data: _ClassNode) -> yaml.Node:
    tag = f"{_CLASS_TAG_PREFIX}{data.target}"
    if not data.params:
        return dumper.represent_scalar(tag, "")
    return dumper.represent_mapping(tag, data.params)


_GraphLoader.add_multi_constructor(_CLASS_TAG_PREFIX, _construct_class)
# yaml's typeshed declares add_representer's callback against the base Dumper class;
# our _GraphDumper subclass triggers a benign variance complaint.
_GraphDumper.add_representer(_ClassNode, _represent_class)  # type: ignore[arg-type]


class VisualizerSpec(BaseModel):
    """Per-node visualizer override. `type_hint` is consumed by the registry."""

    type_hint: Optional[str] = None
    enabled: bool = True


class GraphNode(BaseModel):
    """A single op in the pipeline. The first node is treated as the source."""

    id: str
    op_path: str = Field(..., description="Confluid !class: target, e.g. 'dataflux.ops.numpy:RescaleOp'")
    params: Dict[str, Any] = Field(default_factory=dict)
    viz: Optional[VisualizerSpec] = None


class GraphEdge(BaseModel):
    """A connection between two nodes. In the slice, edges are inferred from node
    order — but we keep an explicit list so the frontend can render them and so
    branching can be added later without breaking the wire format.
    """

    source: str
    target: str


class Graph(BaseModel):
    nodes: List[GraphNode]
    edges: List[GraphEdge] = Field(default_factory=list)

    def linear_order(self) -> List[GraphNode]:
        """List order is canonical. Explicit edges, if present, must agree so a
        future branching extension can't silently drop nodes.
        """
        if not self.nodes:
            return []
        if self.edges:
            seen = {self.nodes[0].id}
            for edge in self.edges:
                if edge.source not in seen:
                    raise ValueError(
                        f"Edge {edge.source!r}→{edge.target!r} references unseen source; "
                        "graph order cannot be linearized."
                    )
                seen.add(edge.target)
        return list(self.nodes)

    def to_confluid_yaml(self) -> str:
        ordered = self.linear_order()
        if not ordered:
            raise ValueError("Cannot serialize an empty Graph.")
        source = ordered[0]
        ops = ordered[1:]
        flux = _ClassNode(
            target="dataflux.core.Flux()",
            params={
                "source": _ClassNode(target=_op_path_to_tag_suffix(source.op_path), params=dict(source.params)),
                "ops": [
                    _ClassNode(target=_op_path_to_tag_suffix(node.op_path), params=dict(node.params)) for node in ops
                ],
            },
        )
        return yaml.dump(flux, Dumper=_GraphDumper, sort_keys=False)

    @classmethod
    def from_confluid_yaml(cls, text: str) -> "Graph":
        loaded = yaml.load(text, Loader=_GraphLoader)
        if not isinstance(loaded, _ClassNode):
            raise ValueError("Top-level document must be a !class:dataflux.core.Flux mapping.")
        # Accept both 'X.Flux' (deferred) and 'X.Flux()' (instance) so a hand-
        # authored YAML round-trips even before the user adds the parens.
        bare_target = loaded.target[:-2] if loaded.target.endswith("()") else loaded.target
        if not bare_target.endswith(".Flux"):
            raise ValueError(f"Expected top-level Flux, got !class:{loaded.target}.")

        body = loaded.params
        if not isinstance(body, dict):
            raise ValueError("Flux body must be a mapping with 'source' and 'ops'.")

        source_node = body.get("source")
        if not isinstance(source_node, _ClassNode):
            raise ValueError("Flux 'source' must be a !class: mapping.")

        nodes: List[GraphNode] = [
            GraphNode(
                id="n0",
                op_path=_tag_suffix_to_op_path(source_node.target),
                params=dict(source_node.params),
            ),
        ]
        edges: List[GraphEdge] = []

        ops_block = body.get("ops") or []
        if not isinstance(ops_block, list):
            raise ValueError("Flux 'ops' must be a list of !class: mappings.")
        for i, entry in enumerate(ops_block, start=1):
            if not isinstance(entry, _ClassNode):
                raise ValueError(f"Op #{i} must be a !class: mapping; got {type(entry).__name__}.")
            node_id = f"n{i}"
            nodes.append(
                GraphNode(
                    id=node_id,
                    op_path=_tag_suffix_to_op_path(entry.target),
                    params=dict(entry.params),
                ),
            )
            edges.append(GraphEdge(source=nodes[i - 1].id, target=node_id))

        return cls(nodes=nodes, edges=edges)
