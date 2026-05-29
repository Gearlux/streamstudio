"""Tests for ``fluxstudio.export`` — the canvas op-graph → Confluid YAML compiler.

Covers the bridge attrs the compiler relies on (``TAIDAL_NODE_KIND`` /
``TAIDAL_CALLABLE_PATH``, stamped in ``fluxstudio.bridge``), the linear / fan-out
(``Tee``) compilation shapes, every ``UnsupportedTopologyError`` branch, and the
end-to-end **Pipeline Parity** round-trip mandated by DataFlux: a dumped op-list,
reloaded through ``confluid.load`` + ``confluid.materialize``, must reproduce the
exact transform a hand-built ``Flux`` of the same ops produces.

NOTE: no ``from __future__ import annotations`` — the bridge's ``_op_call_signature``
/ ``_is_source`` classifiers inspect real annotation objects, and ``RescaleOp`` etc.
carry real annotations we want resolved, not stringized.
"""

import os
import tempfile
from typing import Any, Dict, List

import confluid
import pytest
import torch
from confluid.fluid import Instance
from dataflux import Flux, Sample
from dataflux.ops.tee import Tee
from dataflux.ops.torch import RescaleOp
from dataflux.sources import HuggingFaceSource

from fluxstudio.bridge import create_comfy_node
from fluxstudio.export import UnsupportedTopologyError, compile_ops, export_ops_yaml

# A non-op boundary node class (stands in for WalkDatasetNode / a source): no TAIDAL_NODE_KIND.
_BoundaryNode = type("WalkDatasetNode", (object,), {})


def _op_mappings() -> Dict[str, Any]:
    """node_mappings with the generated RescaleOp op node + a boundary node."""
    return {
        "Taidal_RescaleOp": create_comfy_node(RescaleOp, "Taidal_RescaleOp"),
        "WalkDatasetNode": _BoundaryNode,
    }


def _rescale_node(sample_src: Any, **inputs: Any) -> Dict[str, Any]:
    cfg = {"sample": sample_src, **inputs}
    return {"class_type": "Taidal_RescaleOp", "inputs": cfg}


def _load_ops(yaml_doc: str) -> Any:
    """Reload an exported op-list to LIVE ops, exactly as a consumer would.

    ``confluid.load`` leaves markers nested under a mapping key deferred; ``materialize``
    flows the top-level list of markers (the same thing ``Flux.from_ops_yaml`` does).
    """
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(yaml_doc)
        path = fh.name
    try:
        return confluid.materialize(confluid.load(path)["ops"])
    finally:
        os.unlink(path)


# --------------------------------------------------------------------------- bridge attrs


def test_op_node_stamps_kind_and_callable_path() -> None:
    node = create_comfy_node(RescaleOp, "Taidal_RescaleOp")
    assert node.TAIDAL_NODE_KIND == "op"
    assert node.TAIDAL_CALLABLE_PATH == "dataflux.ops.torch:RescaleOp"


def test_source_node_stamps_source_kind() -> None:
    node = create_comfy_node(HuggingFaceSource, "Taidal_HuggingFaceSource")
    assert node.TAIDAL_NODE_KIND == "source"
    assert node.TAIDAL_CALLABLE_PATH == "dataflux.sources:HuggingFaceSource"


def test_generic_node_stamps_generic_kind() -> None:
    def scale(value: int) -> int:
        return value * 2

    node = create_comfy_node(scale, "Taidal_scale")
    assert node.TAIDAL_NODE_KIND == "generic"


# --------------------------------------------------------------------------- compile shapes


def test_empty_graph_yields_no_ops() -> None:
    prompt = {"1": {"class_type": "WalkDatasetNode", "inputs": {}}}
    assert compile_ops(prompt, _op_mappings()) == []


def test_linear_chain_compiles_to_ordered_instances() -> None:
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": _rescale_node(["1", 0], in_min=0.0, in_max=255.0),
        "3": _rescale_node(["2", 0], in_min=0.0, in_max=1.0, out_max=10.0),
    }
    ops = compile_ops(prompt, _op_mappings())
    assert len(ops) == 2
    assert all(isinstance(o, Instance) for o in ops)
    assert ops[0].target is RescaleOp
    assert ops[0].kwargs == {"in_min": 0.0, "in_max": 255.0}
    assert ops[1].kwargs == {"in_min": 0.0, "in_max": 1.0, "out_max": 10.0}


def test_export_yaml_contains_class_tags() -> None:
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": _rescale_node(["1", 0], in_min=0.0, in_max=255.0),
    }
    doc = export_ops_yaml(prompt, _op_mappings())
    assert "ops:" in doc
    assert "!class:dataflux.ops.torch.RescaleOp()" in doc


def test_fan_out_compiles_to_tee() -> None:
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": _rescale_node(["1", 0], in_min=0.0, in_max=255.0),
        "3": _rescale_node(["2", 0], in_min=0.0, in_max=1.0, out_max=10.0),
        "4": _rescale_node(["2", 0], in_min=0.0, in_max=1.0, out_max=99.0),
    }
    ops = compile_ops(prompt, _op_mappings())
    assert len(ops) == 2
    assert ops[0].target is RescaleOp
    tee = ops[1]
    assert isinstance(tee, Instance) and tee.target is Tee
    branches = tee.kwargs["branches"]
    assert len(branches) == 2
    # Deterministic numeric-aware branch order: node 3 before node 4.
    assert branches[0][0].kwargs["out_max"] == 10.0
    assert branches[1][0].kwargs["out_max"] == 99.0


# --------------------------------------------------------------------------- error branches


def test_multiple_heads_raise() -> None:
    prompt = {
        "2": _rescale_node(["x", 0], in_min=0.0, in_max=255.0),
        "3": _rescale_node(["y", 0], in_min=0.0, in_max=1.0),
    }
    with pytest.raises(UnsupportedTopologyError, match="single connected chain"):
        compile_ops(prompt, _op_mappings())


def test_widget_driven_by_link_raises() -> None:
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": _rescale_node(["1", 0], in_min=["1", 0], in_max=255.0),  # in_min wired, not literal
    }
    with pytest.raises(UnsupportedTopologyError, match="driven by a link"):
        compile_ops(prompt, _op_mappings())


def test_unresolvable_callable_path_raises() -> None:
    mappings = {
        "Taidal_Mystery": type("Taidal_Mystery", (object,), {"TAIDAL_NODE_KIND": "op", "TAIDAL_CALLABLE_PATH": None})
    }
    prompt = {"1": {"class_type": "Taidal_Mystery", "inputs": {}}}
    with pytest.raises(UnsupportedTopologyError, match="TAIDAL_CALLABLE_PATH"):
        compile_ops(prompt, mappings)


def test_cycle_raises() -> None:
    # Two op nodes each feeding the other → no head, cycle.
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": _rescale_node(["1", 0], in_min=0.0, in_max=255.0),
        "3": _rescale_node(["2", 0], in_min=0.0, in_max=1.0),
    }
    # Make node 2 also a successor of node 3 → 2→3→2 cycle, and no boundary head.
    prompt["2"]["inputs"]["sample"] = ["3", 0]
    with pytest.raises(UnsupportedTopologyError):
        compile_ops(prompt, _op_mappings())


# --------------------------------------------------------------------------- Pipeline Parity


def test_round_trip_pipeline_parity() -> None:
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": _rescale_node(["1", 0], in_min=0.0, in_max=255.0),
        "3": _rescale_node(["2", 0], in_min=0.0, in_max=1.0, out_max=10.0),
    }
    ops = _load_ops(export_ops_yaml(prompt, _op_mappings()))
    assert [type(o).__name__ for o in ops] == ["RescaleOp", "RescaleOp"]

    def src() -> List[Sample]:
        return [Sample(input=torch.tensor([0.0, 128.0, 255.0]), target=None, metadata={})]

    got = list(Flux(source=src(), ops=ops))[0].input
    expected = RescaleOp(0.0, 1.0, out_max=10.0)(RescaleOp(0.0, 255.0)(src()[0])).input
    assert torch.allclose(got, expected)
