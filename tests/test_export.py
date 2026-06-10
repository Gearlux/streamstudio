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
from pathlib import Path
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
from fluxstudio.export import (
    UnsupportedTopologyError,
    chain_output_paths,
    compile_op_chains,
    compile_ops,
    export_ops_yaml,
    export_ops_yamls,
)

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


# --------------------------------------------------------------------------- multiple chains


def _three_chain_prompt() -> Dict[str, Any]:
    """Three disconnected op chains: heads 2, 5, 10 (10 numeric-sorts AFTER 5), one two-op chain."""
    return {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": _rescale_node(["1", 0], in_min=0.0, in_max=255.0),
        "3": _rescale_node(["2", 0], in_min=0.0, in_max=1.0, out_max=10.0),
        "5": _rescale_node(["1", 0], in_min=0.0, in_max=2.0),
        "10": _rescale_node(None, in_min=0.0, in_max=3.0),  # unconnected sample → its own head
    }


def test_compile_op_chains_single_chain_matches_compile_ops() -> None:
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": _rescale_node(["1", 0], in_min=0.0, in_max=255.0),
        "3": _rescale_node(["2", 0], in_min=0.0, in_max=1.0, out_max=10.0),
    }
    chains = compile_op_chains(prompt, _op_mappings())
    assert len(chains) == 1
    assert [o.kwargs for o in chains[0]] == [o.kwargs for o in compile_ops(prompt, _op_mappings())]


def test_compile_op_chains_one_list_per_chain_in_head_order() -> None:
    chains = compile_op_chains(_three_chain_prompt(), _op_mappings())
    assert len(chains) == 3
    # Deterministic numeric-aware head order: 2 < 5 < 10; chain 2→3 linearizes fully.
    assert [op.kwargs["in_max"] for op in chains[0]] == [255.0, 1.0]
    assert [op.kwargs["in_max"] for op in chains[1]] == [2.0]
    assert [op.kwargs["in_max"] for op in chains[2]] == [3.0]
    assert all(isinstance(op, Instance) and op.target is RescaleOp for chain in chains for op in chain)


def test_compile_op_chains_empty_graph_yields_no_chains() -> None:
    prompt = {"1": {"class_type": "WalkDatasetNode", "inputs": {}}}
    assert compile_op_chains(prompt, _op_mappings()) == []


def test_export_ops_yamls_one_doc_per_chain() -> None:
    docs = export_ops_yamls(_three_chain_prompt(), _op_mappings())
    assert len(docs) == 3
    # Each document is individually a complete, loadable {ops: [...]} config.
    for doc, expected_len in zip(docs, (2, 1, 1)):
        assert "ops:" in doc and "!class:dataflux.ops.torch.RescaleOp()" in doc
        ops = _load_ops(doc)
        assert [type(o).__name__ for o in ops] == ["RescaleOp"] * expected_len


def test_export_ops_yamls_single_chain_matches_strict_export() -> None:
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": _rescale_node(["1", 0], in_min=0.0, in_max=255.0),
    }
    assert export_ops_yamls(prompt, _op_mappings()) == [export_ops_yaml(prompt, _op_mappings())]


def test_export_ops_yamls_empty_graph_yields_single_empty_doc() -> None:
    prompt = {"1": {"class_type": "WalkDatasetNode", "inputs": {}}}
    docs = export_ops_yamls(prompt, _op_mappings())
    assert len(docs) == 1
    assert _load_ops(docs[0]) == []


def test_chain_output_paths_single_keeps_base() -> None:
    assert chain_output_paths("out/ops.yaml", 1) == [Path("out/ops.yaml")]


def test_chain_output_paths_multi_suffixes_stem() -> None:
    paths = chain_output_paths("out/ops.yaml", 3)
    assert [p.as_posix() for p in paths] == ["out/ops_1.yaml", "out/ops_2.yaml", "out/ops_3.yaml"]


# --------------------------------------------------------------------------- error branches


def test_multiple_heads_raise_in_strict_compile_ops() -> None:
    prompt = {
        "2": _rescale_node(["x", 0], in_min=0.0, in_max=255.0),
        "3": _rescale_node(["y", 0], in_min=0.0, in_max=1.0),
    }
    with pytest.raises(UnsupportedTopologyError, match="single connected chain"):
        compile_ops(prompt, _op_mappings())


def test_widget_driven_by_link_is_omitted_with_note() -> None:
    # A widget wired from another node (e.g. a Math node computing a threshold) is a runtime
    # value a static op-list cannot hold — the kwarg is OMITTED (constructor default) and the
    # omission noted per chain, instead of aborting the export.
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": _rescale_node(["1", 0], in_min=["9", 0], in_max=255.0),  # in_min wired, not literal
    }
    notes: list = []
    chains = compile_op_chains(prompt, _op_mappings(), chain_notes=notes)
    assert len(chains) == 1
    assert chains[0][0].kwargs == {"in_max": 255.0}  # in_min omitted
    assert len(notes) == 1 and "'in_min' is wired from node 9" in notes[0][0]

    doc = export_ops_yamls(prompt, _op_mappings())[0]
    assert doc.startswith("# NOTE:") and "'in_min' is wired from node 9" in doc
    ops = _load_ops(doc)
    assert len(ops) == 1  # the doc stays individually loadable


def test_op_to_instance_without_accumulator_still_raises_on_link() -> None:
    # The strict direct-call form keeps the legible error (no silent omission without a note).
    from fluxstudio.export import _op_to_instance

    node = _rescale_node(["1", 0], in_min=["9", 0], in_max=255.0)
    with pytest.raises(UnsupportedTopologyError, match="driven by a link"):
        _op_to_instance("2", node, _op_mappings())


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


def test_cycle_component_next_to_valid_chain_raises() -> None:
    # A valid chain (head 2) PLUS a two-node cycle (3↔4): the cycle members are reachable
    # from no chain head, so even the per-chain compiler must reject the graph.
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": _rescale_node(["1", 0], in_min=0.0, in_max=255.0),
        "3": _rescale_node(["4", 0], in_min=0.0, in_max=1.0),
        "4": _rescale_node(["3", 0], in_min=0.0, in_max=2.0),
    }
    with pytest.raises(UnsupportedTopologyError, match="form a cycle"):
        compile_op_chains(prompt, _op_mappings())


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
