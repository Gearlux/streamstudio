"""Tests for ``fluxstudio.export`` — the canvas op-graph → Confluid YAML compiler.

Covers the bridge attrs the compiler relies on (``TAIDAL_NODE_KIND`` /
``TAIDAL_CALLABLE_PATH`` / ``TAIDAL_CONSTANT``, stamped in ``fluxstudio.bridge``), the
DAG→sequential compilation shapes (flat fan-out with stash/unstash isolation, the
``Mix Samples`` fan-in translation), the widget-wire folding pass (native primitive
inlining, ``constant=True`` value-node hoisting + dotted ``!ref:``, ``op_N`` embedding,
omission notes), every ``UnsupportedTopologyError`` branch, and the end-to-end
**Pipeline Parity** round-trip mandated by DataFlux: a dumped op-list, reloaded through
``confluid.load`` + ``confluid.materialize``, must reproduce the exact transform a
hand-built ``Flux`` of the same ops produces.

NOTE: no ``from __future__ import annotations`` — the bridge's ``_op_call_signature``
/ ``_is_source`` classifiers inspect real annotation objects, and ``RescaleOp`` etc.
carry real annotations we want resolved, not stringized.
"""

import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union, cast

import confluid
import pytest
import torch
from confluid import configurable, output
from confluid.fluid import Instance, Reference
from dataflux import Flux, Sample
from dataflux.ops.stash import StashInputOp, StashTargetOp, UnstashInputOp, UnstashTargetOp
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


# --------------------------------------------------------------------------- local fixture classes
# Defined at module level so ``resolve_callable("test_export:<Name>")`` (the dump/load
# round-trip) can re-import them — fluxstudio tests must not depend on waivefront-torchsig.


@configurable(category="op", group="test_export", constant=True)
class ConstTxConfig:
    """A pure-config value producer (the ImpairmentsTxConfig shape).

    Args:
        quantize: Enable the quantize stage.
        clock_drift: Enable the clock-drift stage.
    """

    def __init__(self, quantize: bool = True, clock_drift: bool = True) -> None:
        self.quantize = quantize
        self.clock_drift = clock_drift

    @property
    @output
    def disabled(self) -> Tuple[str, ...]:
        """Class names whose enable widget is False."""
        stages = (("QuantizeOp", self.quantize), ("ClockDriftOp", self.clock_drift))
        return tuple(name for name, on in stages if not on)

    @property
    @output
    def probability_overrides(self) -> Dict[str, float]:
        """Per-stage probability overrides."""
        return {"QuantizeOp": 0.25} if not self.quantize else {}

    def __call__(self) -> Tuple[Tuple[str, ...], Dict[str, float]]:
        return self.disabled, self.probability_overrides


@configurable(category="op", group="test_export", constant=True)
class ConstFactor:
    """A scalar-output constant (for recursion / dedup tests).

    Args:
        value: Base value.
    """

    def __init__(self, value: float = 2.0) -> None:
        self.value = value

    @property
    @output
    def factor(self) -> float:
        """The doubled base value."""
        return self.value * 2

    def __call__(self) -> float:
        return self.factor


@configurable(category="op", group="test_export", constant=True)
class ConstOffset:
    """A constant whose own widget may be wired from another constant.

    Args:
        base: Base offset.
    """

    def __init__(self, base: float = 1.0) -> None:
        self.base = base

    @property
    @output
    def offset(self) -> float:
        """base + 1."""
        return self.base + 1

    def __call__(self) -> float:
        return self.offset


@configurable(category="op", group="test_export")
class PlainValueConfig:
    """A value producer WITHOUT the constant mark (nudge-note test).

    Args:
        value: Some value.
    """

    def __init__(self, value: float = 1.0) -> None:
        self.value = value

    @property
    @output
    def doubled(self) -> float:
        """value * 2."""
        return self.value * 2

    def __call__(self) -> float:
        return self.doubled


@configurable(category="op", group="test_export")
class ChainLikeOp:
    """An op with container params fed from a constant (the ImpairmentsChain shape).

    Args:
        probability: Outer gate probability.
        tx_disabled: Stage class names to exclude.
        overrides: Per-stage probability overrides.
    """

    def __init__(
        self,
        probability: float = 0.5,
        tx_disabled: Tuple[str, ...] = (),
        overrides: Optional[Dict[str, float]] = None,
    ) -> None:
        self.probability = probability
        self.tx_disabled = tuple(tx_disabled)
        self.overrides = dict(overrides) if overrides else {}

    def __call__(self, sample: Sample) -> Sample:
        return sample


@configurable(category="op", group="test_export")
class ThresholdLikeOp:
    """An op with one expression-capable param and one numeric-only param.

    Args:
        low_level: Bound — numeric literal or a '{meta_key}' expression.
        gain: Numeric gain.
    """

    def __init__(self, low_level: Optional[Union[float, int, str]] = None, gain: float = 1.0) -> None:
        self.low_level = low_level
        self.gain = gain

    def __call__(self, sample: Sample) -> Sample:
        return sample


@configurable(category="op", group="test_export")
class AddOp:
    """Add a constant to ``sample.input``.

    Args:
        amount: Added to the input.
    """

    def __init__(self, amount: float = 0.0) -> None:
        self.amount = amount

    def __call__(self, sample: Sample) -> Sample:
        return sample._replace(input=sample.input + self.amount)


@configurable(category="op", group="test_export")
class WriteMetaOp:
    """Write the current ``sample.input`` into ``metadata[key]``.

    Args:
        key: Metadata key to write.
    """

    def __init__(self, key: str = "k") -> None:
        self.key = key

    def __call__(self, sample: Sample) -> Sample:
        sample.meta[self.key] = sample.input
        return sample


@configurable(category="op", group="test_export")
class SetTargetOp:
    """Replace ``sample.target`` with a constant.

    Args:
        value: The new target.
    """

    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self, sample: Sample) -> Sample:
        return sample._replace(target=self.value)


def _op_mappings() -> Dict[str, Any]:
    """node_mappings with the generated op/value nodes the tests wire up + a boundary node."""
    return {
        "Taidal_RescaleOp": create_comfy_node(RescaleOp, "Taidal_RescaleOp"),
        "Taidal_Tee": create_comfy_node(Tee, "Taidal_Tee"),
        "Taidal_ConstTxConfig": create_comfy_node(ConstTxConfig, "Taidal_ConstTxConfig"),
        "Taidal_ConstFactor": create_comfy_node(ConstFactor, "Taidal_ConstFactor"),
        "Taidal_ConstOffset": create_comfy_node(ConstOffset, "Taidal_ConstOffset"),
        "Taidal_PlainValueConfig": create_comfy_node(PlainValueConfig, "Taidal_PlainValueConfig"),
        "Taidal_ChainLikeOp": create_comfy_node(ChainLikeOp, "Taidal_ChainLikeOp"),
        "Taidal_ThresholdLikeOp": create_comfy_node(ThresholdLikeOp, "Taidal_ThresholdLikeOp"),
        "Taidal_AddOp": create_comfy_node(AddOp, "Taidal_AddOp"),
        "Taidal_WriteMetaOp": create_comfy_node(WriteMetaOp, "Taidal_WriteMetaOp"),
        "Taidal_SetTargetOp": create_comfy_node(SetTargetOp, "Taidal_SetTargetOp"),
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


def test_fan_out_compiles_to_flat_stash_sequence() -> None:
    # DAG→sequential: the fork sample is stashed after node 2 and restored before the
    # second branch, so the branchy wiring becomes ONE flat op-list (no Tee emitted).
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": _rescale_node(["1", 0], in_min=0.0, in_max=255.0),
        "3": _rescale_node(["2", 0], in_min=0.0, in_max=1.0, out_max=10.0),
        "4": _rescale_node(["2", 0], in_min=0.0, in_max=1.0, out_max=99.0),
    }
    notes: List[List[str]] = []
    chains = compile_op_chains(prompt, _op_mappings(), chain_notes=notes)
    assert len(chains) == 1
    ops = chains[0]
    assert [op.target for op in ops] == [
        RescaleOp,
        StashInputOp,
        StashTargetOp,
        RescaleOp,
        UnstashInputOp,
        UnstashTargetOp,
        RescaleOp,
    ]
    # Deterministic numeric-aware branch order: node 3 before node 4.
    assert ops[3].kwargs["out_max"] == 10.0
    assert ops[6].kwargs["out_max"] == 99.0
    # One fork → per-field stash keys, each restore matching its snapshot.
    assert ops[1].kwargs["key"] == "__taidal_stash_2_input" == ops[4].kwargs["key"]
    assert ops[2].kwargs["key"] == "__taidal_stash_2_target" == ops[5].kwargs["key"]
    assert any("stash/unstash" in note for note in notes[0])


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
    with pytest.raises(UnsupportedTopologyError, match="single connected component"):
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


# --------------------------------------------------------------------------- constant folding


def _load_doc(yaml_doc: str) -> Dict[str, Any]:
    """Load a full exported document (constants + ops), as ``confluid.load`` sees it."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(yaml_doc)
        path = fh.name
    try:
        return cast(Dict[str, Any], confluid.load(path))
    finally:
        os.unlink(path)


def _ref_target(value: Any) -> str:
    """The dotted target of a confluid ``Reference`` kwarg (References have no value-eq)."""
    assert isinstance(value, Reference), f"expected a Reference, got {type(value).__name__}: {value!r}"
    return str(value.target)


def test_constant_value_node_hoists_with_dotted_ref() -> None:
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "163": {"class_type": "Taidal_ConstTxConfig", "inputs": {"quantize": False, "clock_drift": True}},
        "35": {
            "class_type": "Taidal_ChainLikeOp",
            "inputs": {
                "sample": ["1", 0],
                "probability": 0.7,
                "tx_disabled": ["163", 0],
                "overrides": ["163", 1],
            },
        },
    }
    notes: List[List[str]] = []
    constants: List[Dict[str, Any]] = []
    chains = compile_op_chains(prompt, _op_mappings(), chain_notes=notes, chain_constants=constants)
    assert len(chains) == 1 and notes[0] == []  # nothing dropped — folding handled the wires
    assert list(constants[0]) == ["const_tx_config"]
    chain_op = chains[0][0]
    assert chain_op.kwargs["probability"] == 0.7
    assert _ref_target(chain_op.kwargs["tx_disabled"]) == "const_tx_config.disabled"
    assert _ref_target(chain_op.kwargs["overrides"]) == "const_tx_config.probability_overrides"

    doc = export_ops_yamls(prompt, _op_mappings())[0]
    # The dumper's scalar form for refs is ``!ref 'name'`` (the loader accepts !ref: too).
    assert "const_tx_config:" in doc and "!ref 'const_tx_config.disabled'" in doc
    assert doc.index("const_tx_config:") < doc.index("ops:")  # constants dump above the op-list
    assert "quantize: false" in doc  # the SETTINGS export verbatim, not the derived values


def test_constant_hoist_round_trip_materializes() -> None:
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "163": {"class_type": "Taidal_ConstTxConfig", "inputs": {"quantize": False, "clock_drift": True}},
        "35": {
            "class_type": "Taidal_ChainLikeOp",
            "inputs": {"sample": ["1", 0], "tx_disabled": ["163", 0], "overrides": ["163", 1]},
        },
    }
    ops = _load_ops(export_ops_yamls(prompt, _op_mappings())[0])
    chain = ops[0]
    assert type(chain).__name__ == "ChainLikeOp"
    assert tuple(chain.tx_disabled) == ("QuantizeOp",)
    assert chain.overrides == {"QuantizeOp": 0.25}


def test_constant_hoisted_once_for_multiple_consumers() -> None:
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "163": {"class_type": "Taidal_ConstTxConfig", "inputs": {"quantize": False}},
        "2": {"class_type": "Taidal_ChainLikeOp", "inputs": {"sample": ["1", 0], "tx_disabled": ["163", 0]}},
        "3": {"class_type": "Taidal_ChainLikeOp", "inputs": {"sample": ["2", 0], "overrides": ["163", 1]}},
    }
    constants: List[Dict[str, Any]] = []
    chains = compile_op_chains(prompt, _op_mappings(), chain_constants=constants)
    assert len(chains) == 1 and list(constants[0]) == ["const_tx_config"]


def test_two_constants_of_same_class_get_deduped_names() -> None:
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "20": {"class_type": "Taidal_ConstFactor", "inputs": {"value": 2.0}},
        "21": {"class_type": "Taidal_ConstFactor", "inputs": {"value": 3.0}},
        "2": {"class_type": "Taidal_ThresholdLikeOp", "inputs": {"sample": ["1", 0], "low_level": ["20", 0]}},
        "3": {"class_type": "Taidal_ThresholdLikeOp", "inputs": {"sample": ["2", 0], "low_level": ["21", 0]}},
    }
    constants: List[Dict[str, Any]] = []
    chains = compile_op_chains(prompt, _op_mappings(), chain_constants=constants)
    assert list(constants[0]) == ["const_factor", "const_factor_2"]
    assert _ref_target(chains[0][0].kwargs["low_level"]) == "const_factor.factor"
    assert _ref_target(chains[0][1].kwargs["low_level"]) == "const_factor_2.factor"


def test_constant_wired_from_constant_chains_refs() -> None:
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "20": {"class_type": "Taidal_ConstFactor", "inputs": {"value": 3.0}},
        "21": {"class_type": "Taidal_ConstOffset", "inputs": {"base": ["20", 0]}},
        "2": {"class_type": "Taidal_ThresholdLikeOp", "inputs": {"sample": ["1", 0], "low_level": ["21", 0]}},
    }
    constants: List[Dict[str, Any]] = []
    notes: List[List[str]] = []
    compile_op_chains(prompt, _op_mappings(), chain_notes=notes, chain_constants=constants)
    assert notes[0] == []
    # Both constants hoist; the inner one references the outer through its own kwarg.
    assert set(constants[0]) == {"const_factor", "const_offset"}
    assert _ref_target(constants[0]["const_offset"].kwargs["base"]) == "const_factor.factor"

    ops = _load_ops(export_ops_yamls(prompt, _op_mappings())[0])
    assert ops[0].low_level == 7.0  # (3 * 2) + 1


def test_constant_with_unfoldable_wire_notes_and_keeps_default() -> None:
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "21": {"class_type": "Taidal_ConstOffset", "inputs": {"base": ["99", 0]}},  # node 99 unknown
        "2": {"class_type": "Taidal_ThresholdLikeOp", "inputs": {"sample": ["1", 0], "low_level": ["21", 0]}},
    }
    notes: List[List[str]] = []
    constants: List[Dict[str, Any]] = []
    compile_op_chains(prompt, _op_mappings(), chain_notes=notes, chain_constants=constants)
    assert "base" not in constants[0]["const_offset"].kwargs  # omitted → constructor default
    assert any("hoisted constant" in note and "'base'" in note for note in notes[0])
    ops = _load_ops(export_ops_yamls(prompt, _op_mappings())[0])
    assert ops[0].low_level == 2.0  # default base 1.0 + 1


def test_untagged_value_node_notes_constant_nudge() -> None:
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "50": {"class_type": "Taidal_PlainValueConfig", "inputs": {"value": 4.0}},
        "2": {"class_type": "Taidal_ThresholdLikeOp", "inputs": {"sample": ["1", 0], "low_level": ["50", 0]}},
    }
    notes: List[List[str]] = []
    constants: List[Dict[str, Any]] = []
    chains = compile_op_chains(prompt, _op_mappings(), chain_notes=notes, chain_constants=constants)
    assert constants[0] == {}  # not hoisted — the mark is the explicit opt-in
    assert "low_level" not in chains[0][0].kwargs
    assert any("@configurable(constant=True)" in note for note in notes[0])


def test_value_node_slot_out_of_range_notes() -> None:
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "163": {"class_type": "Taidal_ConstTxConfig", "inputs": {}},
        "2": {"class_type": "Taidal_ChainLikeOp", "inputs": {"sample": ["1", 0], "tx_disabled": ["163", 5]}},
    }
    notes: List[List[str]] = []
    compile_op_chains(prompt, _op_mappings(), chain_notes=notes)
    assert any("output slot 5 has no declared @output" in note for note in notes[0])


def test_metadata_expression_hint_gated_on_str_param() -> None:
    # ``low_level`` admits str → the omission note suggests the '{meta_key}' mechanism;
    # ``gain`` is numeric-only → plain omission note, no misleading hint.
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": {
            "class_type": "Taidal_ThresholdLikeOp",
            "inputs": {"sample": ["1", 0], "low_level": ["99", 0], "gain": ["98", 0]},
        },
    }
    notes: List[List[str]] = []
    compile_op_chains(prompt, _op_mappings(), chain_notes=notes)
    low_note = next(note for note in notes[0] if "'low_level'" in note)
    gain_note = next(note for note in notes[0] if "'gain'" in note)
    assert "{meta_key}" in low_note
    assert "{meta_key}" not in gain_note


# --------------------------------------------------------------------------- primitive folding


def test_primitive_value_nodes_fold_inline() -> None:
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "100": {"class_type": "PrimitiveFloat", "inputs": {"value": 3.5}},
        "101": {"class_type": "PrimitiveString", "inputs": {"value": "{snr}"}},
        "102": {"class_type": "PrimitiveBoolean", "inputs": {"value": False}},
        "163": {"class_type": "Taidal_ConstTxConfig", "inputs": {"quantize": ["102", 0]}},
        "2": {
            "class_type": "Taidal_ThresholdLikeOp",
            "inputs": {"sample": ["1", 0], "low_level": ["101", 0], "gain": ["100", 0]},
        },
        "3": {"class_type": "Taidal_ChainLikeOp", "inputs": {"sample": ["2", 0], "tx_disabled": ["163", 0]}},
    }
    notes: List[List[str]] = []
    constants: List[Dict[str, Any]] = []
    chains = compile_op_chains(prompt, _op_mappings(), chain_notes=notes, chain_constants=constants)
    assert notes[0] == []
    assert chains[0][0].kwargs["gain"] == 3.5
    assert chains[0][0].kwargs["low_level"] == "{snr}"
    # A primitive feeding a CONSTANT's widget folds into the hoisted entry too.
    assert constants[0]["const_tx_config"].kwargs["quantize"] is False


def test_primitive_fed_by_primitive_follows_the_wire() -> None:
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "100": {"class_type": "PrimitiveFloat", "inputs": {"value": 9.0}},
        "101": {"class_type": "PrimitiveFloat", "inputs": {"value": ["100", 0]}},
        "2": {"class_type": "Taidal_ThresholdLikeOp", "inputs": {"sample": ["1", 0], "gain": ["101", 0]}},
    }
    chains = compile_op_chains(prompt, _op_mappings())
    assert chains[0][0].kwargs["gain"] == 9.0


# --------------------------------------------------------------------------- op-value embedding


def test_op_value_links_embed_into_higher_order_op() -> None:
    # Node 8 is consumed as a DATAFLUX_OP value (op_0) and has no sample wiring — it embeds
    # as a nested instance inside Tee.branches instead of joining the sample sequence.
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "8": _rescale_node(None, in_min=0.0, in_max=1.0, out_max=5.0),
        "9": {"class_type": "Taidal_Tee", "inputs": {"sample": ["1", 0], "op_0": ["8", 1]}},
    }
    chains = compile_op_chains(prompt, _op_mappings())
    assert len(chains) == 1 and len(chains[0]) == 1
    tee = chains[0][0]
    assert tee.target is Tee
    branches = tee.kwargs["branches"]
    assert len(branches) == 1 and len(branches[0]) == 1  # each op its own 1-op branch
    assert branches[0][0].target is RescaleOp and branches[0][0].kwargs["out_max"] == 5.0


# --------------------------------------------------------------------------- Mix Samples fan-in


def _mix_prompt() -> Dict[str, Any]:
    """The impairments.json shape: fork → impaired branch X / regions branch Z → Mix → tail.

    Mix takes input+target from the X branch (node 31) and metadata from the Z branch
    (node 51); both branches descend from the fork at node 2.
    """
    return {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["1", 0], "amount": 10.0}},
        "3": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["2", 0], "amount": 100.0}},
        "31": {"class_type": "Taidal_SetTargetOp", "inputs": {"sample": ["3", 0], "value": 42.0}},
        "4": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["2", 0], "amount": 1.0}},
        "5": {"class_type": "Taidal_WriteMetaOp", "inputs": {"sample": ["4", 0], "key": "regions"}},
        "51": {"class_type": "Taidal_SetTargetOp", "inputs": {"sample": ["5", 0], "value": 7.0}},
        "60": {
            "class_type": "Taidal_MixSamples",
            "inputs": {"input_sample": ["31", 0], "target_sample": ["31", 0], "metadata_sample": ["51", 0]},
        },
        "7": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["60", 0], "amount": 1000.0}},
    }


def test_mix_samples_compiles_to_stash_merge() -> None:
    notes: List[List[str]] = []
    chains = compile_op_chains(_mix_prompt(), _op_mappings(), chain_notes=notes)
    assert len(chains) == 1
    targets = [op.target for op in chains[0]]
    assert targets == [
        AddOp,  # fork (node 2)
        StashInputOp,
        StashTargetOp,
        AddOp,  # branch X (node 3)
        SetTargetOp,  # node 31 (Mix input/target source)
        StashInputOp,
        StashTargetOp,
        UnstashInputOp,  # back to the fork for branch Z
        UnstashTargetOp,
        AddOp,  # node 4
        WriteMetaOp,  # node 5
        SetTargetOp,  # node 51
        UnstashInputOp,  # the Mix merge: input ← node 31, target ← node 31
        UnstashTargetOp,
        AddOp,  # tail (node 7)
    ]
    assert chains[0][12].kwargs["key"] == "__taidal_stash_31_input"
    assert chains[0][13].kwargs["key"] == "__taidal_stash_31_target"
    assert any("stash/unstash" in note for note in notes[0])


def test_mix_samples_execution_parity() -> None:
    ops = _load_ops(export_ops_yamls(_mix_prompt(), _op_mappings())[0])

    def src() -> List[Sample]:
        return [Sample(input=1.0, target=None, metadata={})]

    out = list(Flux(source=src(), ops=ops))[0]
    # Canvas semantics: input/target from the impaired X branch (then +1000 on the tail),
    # the regions metadata from the Z branch merged in.
    assert out.input == 1111.0  # 1 + 10 + 100 + 1000
    assert out.target == 42.0  # X branch target survives Z's SetTargetOp(7)
    assert out.meta["regions"] == 12.0  # Z branch wrote input as of 1 + 10 + 1


def test_deepest_sink_scheduled_last() -> None:
    # Two sinks: the short viewer-style branch (node 3) runs first; the deepest tail
    # (node 5) runs last so the document's final sample is the longest path's output.
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["1", 0], "amount": 10.0}},
        "3": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["2", 0], "amount": 1.0}},
        "4": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["2", 0], "amount": 2.0}},
        "5": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["4", 0], "amount": 3.0}},
    }
    chains = compile_op_chains(prompt, _op_mappings())
    amounts = [op.kwargs["amount"] for op in chains[0] if op.target is AddOp]
    assert amounts == [10.0, 1.0, 2.0, 3.0]

    ops = _load_ops(export_ops_yamls(prompt, _op_mappings())[0])
    out = list(Flux(source=[Sample(input=0.0, target=None, metadata={})], ops=ops))[0]
    assert out.input == 15.0  # 0 + 10 (fork) + 2 + 3 — the deep branch, not the +1 side branch


def test_integration_impairments_shape() -> None:
    """One document combining every feature, in the real impairments.json shape:

    fork → impairments branch (ChainLikeOp fed by a hoisted constant + a primitive) /
    threshold branch with SUBGRAPH-inlined ids ("142:N") writing regions metadata, plus a
    runtime-wired ThresholdLikeOp param (omitted with the metadata hint) → Mix → tail.
    """
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "107": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["1", 0], "amount": 10.0}},
        # impairments branch: constant config + primitive probability
        "163": {"class_type": "Taidal_ConstTxConfig", "inputs": {"quantize": False}},
        "108": {"class_type": "PrimitiveFloat", "inputs": {"value": 0.9}},
        "35": {
            "class_type": "Taidal_ChainLikeOp",
            "inputs": {
                "sample": ["107", 0],
                "probability": ["108", 0],
                "tx_disabled": ["163", 0],
                "overrides": ["163", 1],
            },
        },
        # threshold branch (subgraph-inlined ids): runtime-wired low_level + regions metadata
        "142:141": {
            "class_type": "Taidal_ThresholdLikeOp",
            "inputs": {"sample": ["107", 0], "low_level": ["142:140", 0]},
        },
        "142:144": {"class_type": "Taidal_WriteMetaOp", "inputs": {"sample": ["142:141", 0], "key": "regions"}},
        "60": {
            "class_type": "Taidal_MixSamples",
            "inputs": {"input_sample": ["35", 0], "target_sample": ["35", 0], "metadata_sample": ["142:144", 0]},
        },
        "61": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["60", 0], "amount": 1000.0}},
    }
    notes: List[List[str]] = []
    constants: List[Dict[str, Any]] = []
    chains = compile_op_chains(prompt, _op_mappings(), chain_notes=notes, chain_constants=constants)
    assert len(chains) == 1
    assert list(constants[0]) == ["const_tx_config"]
    # the runtime wire dropped with the metadata-expression hint; the stash summary noted
    assert any("'low_level'" in note and "{meta_key}" in note for note in notes[0])
    assert any("stash/unstash" in note for note in notes[0])

    doc = export_ops_yamls(prompt, _op_mappings())[0]
    ops = _load_ops(doc)
    chain_op = next(op for op in ops if type(op).__name__ == "ChainLikeOp")
    assert chain_op.probability == 0.9  # primitive folded inline
    assert tuple(chain_op.tx_disabled) == ("QuantizeOp",)  # hoisted constant resolved

    out = list(Flux(source=[Sample(input=1.0, target=None, metadata={})], ops=ops))[0]
    assert out.input == 1011.0  # 1 + 10, impairments branch passes through, + 1000 tail
    assert out.meta["regions"] == 11.0  # threshold branch wrote the fork-state input


def test_component_merging_two_streams_raises() -> None:
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "9": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["1", 0], "amount": 1.0}},
        "3": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["9", 0], "amount": 2.0}},
        "60": {
            "class_type": "Taidal_MixSamples",
            "inputs": {"input_sample": ["2", 0], "target_sample": ["2", 0], "metadata_sample": ["3", 0]},
        },
    }
    with pytest.raises(UnsupportedTopologyError, match="distinct sample streams"):
        compile_op_chains(prompt, _op_mappings())
