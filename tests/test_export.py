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
from dataflux.ops.capture import CaptureOutputOp
from dataflux.ops.configure import ConfigureOp
from dataflux.ops.formula import FormulaOp
from dataflux.ops.parallel import Parallel
from dataflux.ops.stash import StashInputOp, StashTargetOp, UnstashInputOp, UnstashTargetOp
from dataflux.ops.tee import Tee
from dataflux.ops.torch import RescaleOp
from dataflux.ops.transform_chain import TransformChain
from dataflux.sources import HuggingFaceSource

from fluxstudio.bridge import create_comfy_node
from fluxstudio.export import (
    UnsupportedTopologyError,
    _marked_ids,
    _resolve_marked,
    _subgraph_cone_excluded,
    chain_output_paths,
    collect_export_marks,
    compile_op_chains,
    compile_ops,
    export_ops_yaml,
    export_ops_yamls,
)
from fluxstudio.nodes import SubgraphOpExtractNode

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


@configurable(category="op", group="test_export")
class EmitOp:
    """A sample-transforming op that records a per-call value as an ``@output`` (the NoiseFloorOp
    ``applied_snr_db`` shape — the value is set DURING the call, so it must be captured, not recomputed).

    Args:
        base: Base added to the seen input to form the recorded value.
        amount: Added to ``sample.input`` (the op's actual transform).
    """

    def __init__(self, base: float = 100.0, amount: float = 0.0) -> None:
        self.base = base
        self.amount = amount
        self._emitted: Optional[float] = None

    @property
    @output
    def emitted(self) -> Optional[float]:
        """The value recorded on the last call (``base + the input seen``)."""
        return self._emitted

    def __call__(self, sample: Sample) -> Sample:
        self._emitted = self.base + sample.input
        return sample._replace(input=sample.input + self.amount)


@configurable(category="op", group="test_export")
class ConfigureLikeOp:
    """Two single-op slots named the HELIOS way (compute/target) — surfaced via the __taidal_op_params__
    stamp, NOT the dataflux name allowlist. Applies compute then target so a round-trip run is meaningful.
    """

    def __init__(self, compute: Optional[object] = None, target: Optional[object] = None, key: str = "k") -> None:
        self.compute, self.target, self.key = compute, target, key

    def __call__(self, sample: Sample) -> Sample:
        if self.compute is not None:
            sample = self.compute(sample)  # type: ignore[operator]
        if self.target is not None:
            sample = self.target(sample)  # type: ignore[operator]
        return sample


ConfigureLikeOp.__taidal_op_params__ = ("compute", "target")  # type: ignore[attr-defined]
ConfigureLikeOp.__taidal_op_list_params__ = ()  # type: ignore[attr-defined]


@configurable(category="op", group="test_export")
class IfLikeOp:
    """Two op-LIST slots named the HELIOS way (true/false_transforms) — surfaced via the
    __taidal_op_list_params__ stamp (param-name-prefixed socket families). Applies both lists in order.
    """

    def __init__(
        self, true_transforms: Optional[List[object]] = None, false_transforms: Optional[List[object]] = None
    ) -> None:
        self.true_transforms, self.false_transforms = true_transforms, false_transforms

    def __call__(self, sample: Sample) -> Sample:
        for op in (self.true_transforms or []) + (self.false_transforms or []):
            sample = op(sample)  # type: ignore[operator]
        return sample


IfLikeOp.__taidal_op_params__ = ()  # type: ignore[attr-defined]
IfLikeOp.__taidal_op_list_params__ = ("true_transforms", "false_transforms")  # type: ignore[attr-defined]


def _op_mappings() -> Dict[str, Any]:
    """node_mappings with the generated op/value nodes the tests wire up + a boundary node."""
    return {
        "Taidal_ConfigureLikeOp": create_comfy_node(ConfigureLikeOp, "Taidal_ConfigureLikeOp"),
        "Taidal_IfLikeOp": create_comfy_node(IfLikeOp, "Taidal_IfLikeOp"),
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
        "Taidal_EmitOp": create_comfy_node(EmitOp, "Taidal_EmitOp"),
        "Taidal_ConfigureOp": create_comfy_node(ConfigureOp, "Taidal_ConfigureOp"),
        "Taidal_Parallel": create_comfy_node(Parallel, "Taidal_Parallel"),
        "Taidal_SubgraphOp": SubgraphOpExtractNode,
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


def test_sink_node_is_ignored_by_ops_export() -> None:
    """A sink is an OBJECT-kind node, not an op — the ops-export (which compiles only op-kind nodes
    into ``{ops:[...]}`` for ``Flux.from_ops_yaml``) ignores it. Sinks serialize via the whole-graph
    ``DatasetProcessor`` path (graphio), never the op-list; this pins that boundary so a sink can
    never leak into an op-list (which ``Flux`` would reject)."""
    from dataflux.storage.hdf5 import HDF5Sink

    nm = {**_op_mappings(), "Taidal_HDF5Sink": create_comfy_node(HDF5Sink, "Taidal_HDF5Sink")}
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": _rescale_node(["1", 0], in_min=0.0, in_max=255.0),
        "9": {"class_type": "Taidal_HDF5Sink", "inputs": {"path": "/tmp/out.h5"}},  # disconnected object node
    }
    docs = export_ops_yamls(prompt, nm)
    assert len(docs) == 1  # only the op chain forms a component
    assert "RescaleOp" in docs[0]
    assert "HDF5Sink" not in docs[0]  # the sink never appears in the op-list


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
    # The single (final) unstash of each key removes it (default remove=True → no explicit kwarg).
    assert ops[4].kwargs.get("remove", True) is True
    assert ops[5].kwargs.get("remove", True) is True
    assert any("stash/unstash" in note for note in notes[0])


def test_fan_out_non_final_unstash_keeps_key_final_removes() -> None:
    # 3-way fork: the fork is restored before branches 4 AND 5 → TWO unstashes per field. The
    # non-final unstash must KEEP the key (remove=False) or branch 5 can't restore the fork; the
    # final unstash removes it (default remove=True), so the __taidal_stash_* snapshot is cleaned up.
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": _rescale_node(["1", 0], in_min=0.0, in_max=255.0),
        "3": _rescale_node(["2", 0], in_min=0.0, in_max=1.0, out_max=10.0),
        "4": _rescale_node(["2", 0], in_min=0.0, in_max=1.0, out_max=20.0),
        "5": _rescale_node(["2", 0], in_min=0.0, in_max=1.0, out_max=30.0),
    }
    ops = compile_op_chains(prompt, _op_mappings())[0]
    in_unstash = [op for op in ops if op.target is UnstashInputOp and op.kwargs["key"] == "__taidal_stash_2_input"]
    assert len(in_unstash) == 2
    assert in_unstash[0].kwargs.get("remove") is False  # non-final keeps the key
    assert in_unstash[1].kwargs.get("remove", True) is True  # final removes it (default)


def test_compiled_fan_out_removes_stash_keys_from_metadata() -> None:
    # End-to-end: after running the compiled fan-out, the internal __taidal_stash_* snapshots are
    # gone from the sample metadata, so they never leak into a downstream sink.
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["1", 0], "amount": 1.0}},
        "3": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["2", 0], "amount": 2.0}},
        "4": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["2", 0], "amount": 3.0}},
    }
    ops = _load_ops(export_ops_yaml(prompt, _op_mappings()))
    out = list(Flux(source=[Sample(input=0.0, target=0.0, metadata={})], ops=ops))[0]
    assert not any(str(k).startswith("__taidal_stash_") for k in out.meta)


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


def test_configure_op_single_op_wire_embeds_nested_target() -> None:
    """The helios 'Configure' trick exports statically: a ConfigureOp's wired `target`
    (a single DATAFLUX_OP wire) and `op_N` compute chain embed as nested instances, so the
    per-sample runtime value (e.g. ThresholdOp.low_level) becomes expressible in YAML."""
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "8": {"class_type": "Taidal_ThresholdLikeOp", "inputs": {"gain": 2.0}},  # op-value only
        "9": {"class_type": "Taidal_AddOp", "inputs": {"amount": 5.0}},  # compute op-value
        "10": {
            "class_type": "Taidal_ConfigureOp",
            "inputs": {"sample": ["1", 0], "target": ["8", 1], "op_0": ["9", 1], "param": "low_level"},
        },
    }
    notes: List[List[str]] = []
    chains = compile_op_chains(prompt, _op_mappings(), chain_notes=notes)
    assert len(chains) == 1 and len(chains[0]) == 1 and notes[0] == []
    cfg = chains[0][0]
    assert cfg.target is ConfigureOp
    assert cfg.kwargs["target"].target is ThresholdLikeOp
    assert cfg.kwargs["target"].kwargs["gain"] == 2.0
    assert [o.target for o in cfg.kwargs["ops"]] == [AddOp]

    # Round-trip + execution parity: input 3.0 → compute Add(5) → low_level := 8.0.
    ops = _load_ops(export_ops_yamls(prompt, _op_mappings())[0])
    out = list(Flux(source=[Sample(input=3.0, target=None, metadata={})], ops=ops))[0]
    assert out.meta["low_level"] == 8.0
    assert type(ops[0].target).__name__ == "ThresholdLikeOp" and ops[0].target.low_level == 8.0


def test_stamped_single_op_wires_embed_both_compute_and_target() -> None:
    """A bridged-helios composite's TWO single-op slots (compute AND target — named the helios way,
    surfaced via the __taidal_op_params__ stamp) each embed as a nested instance on export."""
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": {"class_type": "Taidal_AddOp", "inputs": {"amount": 1.0}},  # compute op-value
        "3": {"class_type": "Taidal_AddOp", "inputs": {"amount": 10.0}},  # target op-value
        "4": {
            "class_type": "Taidal_ConfigureLikeOp",
            "inputs": {"sample": ["1", 0], "compute": ["2", 1], "target": ["3", 1], "key": "k"},
        },
    }
    notes: List[List[str]] = []
    chains = compile_op_chains(prompt, _op_mappings(), chain_notes=notes)
    assert len(chains) == 1 and len(chains[0]) == 1 and notes[0] == []
    cfg = chains[0][0]
    assert cfg.target is ConfigureLikeOp
    assert cfg.kwargs["compute"].target is AddOp and cfg.kwargs["compute"].kwargs["amount"] == 1.0
    assert cfg.kwargs["target"].target is AddOp and cfg.kwargs["target"].kwargs["amount"] == 10.0

    # Round-trip + execution parity: 0 → compute +1 → target +10 = 11.
    ops = _load_ops(export_ops_yamls(prompt, _op_mappings())[0])
    out = list(Flux(source=[Sample(input=0.0, target=None, metadata={})], ops=ops))[0]
    assert out.input == 11.0


def test_stamped_op_list_families_embed_into_named_kwargs() -> None:
    """A bridged-helios composite with TWO op-LIST families (true_transforms_N / false_transforms_N)
    embeds each family into its OWN constructor kwarg on export (not a single shared op-list)."""
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": {"class_type": "Taidal_AddOp", "inputs": {"amount": 1.0}},
        "3": {"class_type": "Taidal_AddOp", "inputs": {"amount": 2.0}},
        "4": {"class_type": "Taidal_AddOp", "inputs": {"amount": 10.0}},
        "5": {
            "class_type": "Taidal_IfLikeOp",
            "inputs": {
                "sample": ["1", 0],
                "true_transforms_0": ["2", 1],
                "true_transforms_1": ["3", 1],
                "false_transforms_0": ["4", 1],
            },
        },
    }
    notes: List[List[str]] = []
    chains = compile_op_chains(prompt, _op_mappings(), chain_notes=notes)
    assert len(chains) == 1 and len(chains[0]) == 1 and notes[0] == []
    iff = chains[0][0]
    assert iff.target is IfLikeOp
    assert [o.kwargs["amount"] for o in iff.kwargs["true_transforms"]] == [1.0, 2.0]
    assert [o.kwargs["amount"] for o in iff.kwargs["false_transforms"]] == [10.0]

    # Round-trip + execution parity: 0 → (+1 +2) then (+10) = 13.
    ops = _load_ops(export_ops_yamls(prompt, _op_mappings())[0])
    out = list(Flux(source=[Sample(input=0.0, target=None, metadata={})], ops=ops))[0]
    assert out.input == 13.0


def _value_chain_prompt(**threshold_extra: Any) -> Dict[str, Any]:
    """The user's Threshold-subgraph shape: fork → [chain op → Extract → Math] → widget wire.

    Node 2 is the fork (the Magnitude analogue), node 3 the value-computing op (the MaxOp
    analogue), 4/5 the canvas Extract/Math adapters, 6 the consumer whose ``low_level`` is
    wired from the Math output.
    """
    return {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["1", 0], "amount": 10.0}},
        "3": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["2", 0], "amount": 5.0}},
        "4": {"class_type": "Taidal_ExtractSample", "inputs": {"sample": ["3", 0], "field": "input"}},
        "5": {"class_type": "Taidal_Math", "inputs": {"formula": "a * 0.2", "a": ["4", 4]}},
        "6": {
            "class_type": "Taidal_ThresholdLikeOp",
            "inputs": {"sample": ["2", 0], "low_level": ["5", 3], "gain": 1.5, **threshold_extra},
        },
    }


def test_extract_math_value_chain_compiles_to_configure_op() -> None:
    """The on-canvas ``op → Extract → Math → widget`` pattern compiles to a ConfigureOp:
    the value derivation re-runs per sample inside the wrap, the Math step becomes a
    FormulaOp, and the pure chain op leaves the sample schedule (no dead-end branch)."""
    notes: List[List[str]] = []
    chains = compile_op_chains(_value_chain_prompt(), _op_mappings(), chain_notes=notes)
    assert len(chains) == 1 and notes[0] == []  # the wire compiled — no omission note
    ops = chains[0]
    # Just the fork op + the wrapped consumer: node 3 is a PURE value branch (excluded),
    # so there is no fan-out and no stash/unstash insertion.
    assert [op.target for op in ops] == [AddOp, ConfigureOp]
    wrap = ops[1]
    assert wrap.kwargs["param"] == "low_level"
    assert wrap.kwargs["target"].target is ThresholdLikeOp
    assert wrap.kwargs["target"].kwargs == {"gain": 1.5}  # low_level injected per sample, not baked
    compute = wrap.kwargs["ops"]
    assert [c.target for c in compute] == [AddOp, FormulaOp]
    assert compute[0].kwargs["amount"] == 5.0
    assert compute[1].kwargs == {"formula": "a * 0.2", "var": "a"}

    # Round-trip + execution parity: 1 + 10 = 11 → compute (11 + 5) * 0.2 = 3.2 → injected.
    loaded = _load_ops(export_ops_yamls(_value_chain_prompt(), _op_mappings())[0])
    out = list(Flux(source=[Sample(input=1.0, target=None, metadata={})], ops=loaded))[0]
    assert out.meta["low_level"] == pytest.approx(3.2)
    assert loaded[1].target.low_level == pytest.approx(3.2)


def test_value_chain_multi_var_math_keeps_note() -> None:
    prompt = _value_chain_prompt()
    prompt["5"]["inputs"]["b"] = ["4", 3]  # a second wired Math var — not compilable
    notes: List[List[str]] = []
    chains = compile_op_chains(prompt, _op_mappings(), chain_notes=notes)
    assert any("'low_level'" in note for note in notes[0])
    assert all(op.target is not ConfigureOp for chain in chains for op in chain)


def test_value_chain_extract_target_field_keeps_note() -> None:
    prompt = _value_chain_prompt()
    prompt["4"]["inputs"]["field"] = "target"  # only the input-field view is compilable
    notes: List[List[str]] = []
    compile_op_chains(prompt, _op_mappings(), chain_notes=notes)
    assert any("'low_level'" in note for note in notes[0])


def test_value_chain_origin_mismatch_keeps_note() -> None:
    # The value chain roots at a DIFFERENT sample stream point than the consumer's own
    # predecessor — the derivation can't re-run on the consumer's incoming sample.
    prompt = _value_chain_prompt()
    prompt["9"] = {"class_type": "Taidal_AddOp", "inputs": {"sample": ["1", 0], "amount": 7.0}}
    prompt["4"]["inputs"]["sample"] = ["9", 0]
    notes: List[List[str]] = []
    compile_op_chains(prompt, _op_mappings(), chain_notes=notes)
    assert any("'low_level'" in note for note in notes[0])


def test_impure_value_chain_keeps_branch_and_wraps() -> None:
    # The chain op ALSO feeds a real downstream op → it stays in the sample schedule
    # (its other consumer needs it) AND re-runs inside the ConfigureOp compute.
    prompt = _value_chain_prompt()
    prompt["7"] = {"class_type": "Taidal_WriteMetaOp", "inputs": {"sample": ["3", 0], "key": "side"}}
    chains = compile_op_chains(prompt, _op_mappings())
    ops = chains[0]
    wraps = [op for op in ops if op.target is ConfigureOp]
    assert len(wraps) == 1
    scheduled_adds = [op for op in ops if op.target is AddOp and op.kwargs.get("amount") == 5.0]
    assert len(scheduled_adds) == 1  # still scheduled for its real consumer
    assert wraps[0].kwargs["ops"][0].kwargs["amount"] == 5.0  # and embedded in the compute


def _output_capture_prompt() -> Dict[str, Any]:
    """Node 2's @output (``emitted``, slot 2) wired into node 3's ``low_level`` param.

    Node 2 is a sample-ancestor of node 3 (3.sample ← 2), so the capture is on the bus when 3 reads it.
    """
    return {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": {"class_type": "Taidal_EmitOp", "inputs": {"sample": ["1", 0], "base": 100.0, "amount": 0.0}},
        "3": {
            "class_type": "Taidal_ThresholdLikeOp",
            "inputs": {"sample": ["2", 0], "low_level": ["2", 2], "gain": 1.5},
        },
    }


def test_output_capture_compiles_to_configure_with_unstash() -> None:
    """An op-@output → param wire compiles to CaptureOutputOp (producer) + ConfigureOp(UnstashInputOp)
    (consumer); the captured value reaches the param per sample, and the param is not baked."""
    notes: List[List[str]] = []
    chains = compile_op_chains(_output_capture_prompt(), _op_mappings(), chain_notes=notes)
    assert len(chains) == 1 and notes[0] == []  # the wire compiled — no omission note
    ops = chains[0]
    assert [op.target for op in ops] == [CaptureOutputOp, ConfigureOp]
    cap = ops[0]
    assert cap.kwargs["op"].target is EmitOp
    assert cap.kwargs["captures"] == {"emitted": "__taidal_output_2_emitted"}
    cfg = ops[1]
    assert cfg.kwargs["param"] == "low_level"
    assert cfg.kwargs["target"].target is ThresholdLikeOp
    assert cfg.kwargs["target"].kwargs == {"gain": 1.5}  # low_level NOT baked — injected per sample
    unstash = cfg.kwargs["ops"][0]
    assert unstash.target is UnstashInputOp and unstash.kwargs["key"] == "__taidal_output_2_emitted"

    # Round-trip + parity: EmitOp(base=100) on input 1.0 records 101 -> ThresholdLikeOp.low_level := 101.
    loaded = _load_ops(export_ops_yamls(_output_capture_prompt(), _op_mappings())[0])
    out = list(Flux(source=[Sample(input=1.0, target=None, metadata={})], ops=loaded))[0]
    assert out.meta["low_level"] == pytest.approx(101.0)
    assert loaded[1].target.low_level == pytest.approx(101.0)


def test_output_capture_cross_branch_keeps_note() -> None:
    """A producer that is NOT a sample-ancestor of the consumer (separate branches off the source)
    can't put its value on the consumer's bus — the wire drops with a sample-ancestor note."""
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": {"class_type": "Taidal_EmitOp", "inputs": {"sample": ["1", 0]}},
        "3": {"class_type": "Taidal_ThresholdLikeOp", "inputs": {"sample": ["1", 0], "low_level": ["2", 2]}},
    }
    notes: List[List[str]] = []
    chains = compile_op_chains(prompt, _op_mappings(), chain_notes=notes)
    all_notes = [n for chain_notes_list in notes for n in chain_notes_list]
    assert any("'low_level'" in n and "sample-ancestor" in n for n in all_notes)
    assert all(op.target is not ConfigureOp for chain in chains for op in chain)  # no capture wrap emitted


def test_output_capture_producer_and_consumer_chain() -> None:
    """A node may be BOTH a capture consumer (its param configured from an upstream @output) AND a
    capture producer (its own @output captured downstream): the producer CaptureOutputOp reads the
    inner op's @output through the consumer ConfigureOp's .target wrapper. End-to-end parity."""
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": {"class_type": "Taidal_EmitOp", "inputs": {"sample": ["1", 0], "base": 100.0}},
        "3": {"class_type": "Taidal_EmitOp", "inputs": {"sample": ["2", 0], "base": ["2", 2]}},
        "4": {"class_type": "Taidal_ThresholdLikeOp", "inputs": {"sample": ["3", 0], "low_level": ["3", 2]}},
    }
    loaded = _load_ops(export_ops_yamls(prompt, _op_mappings())[0])
    out = list(Flux(source=[Sample(input=1.0, target=None, metadata={})], ops=loaded))[0]
    # node2 records 100+1=101; node3.base := 101 -> records 101+1=102; node4.low_level := 102.
    assert out.meta["low_level"] == pytest.approx(102.0)


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


# --------------------------------------------------------------------------- export-skip mark


def test_export_skip_mark_drops_node_and_downstream() -> None:
    # Mark the head of the viewer branch (node 3): it AND its sample-descendant (node 4)
    # leave the export; the main line is untouched and no fork stash is needed anymore.
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["1", 0], "amount": 10.0}},
        "3": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["2", 0], "amount": 1.0}},  # viz branch head
        "4": {"class_type": "Taidal_WriteMetaOp", "inputs": {"sample": ["3", 0], "key": "viz"}},
        "5": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["2", 0], "amount": 2.0}},  # main line
    }
    notes: List[List[str]] = []
    chains = compile_op_chains(prompt, _op_mappings(), chain_notes=notes, skip_ids=["3"])
    assert len(chains) == 1
    assert [op.kwargs.get("amount") for op in chains[0]] == [10.0, 2.0]  # flat, no stash, no branch
    assert any("export-skip mark" in note and "node 3" in note and "node 4" in note for note in notes[0])


def test_export_skip_subgraph_prefix_excludes_inlined_instance() -> None:
    # Marking a subgraph-INSTANCE id ("142") excludes every inlined inner node ("142:...").
    # The closure is forward-only: node 2 fed the marked instance but sits BEFORE it on
    # the sample path, so it still exports.
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["1", 0], "amount": 10.0}},
        "142:141": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["2", 0], "amount": 1.0}},
        "142:144": {"class_type": "Taidal_WriteMetaOp", "inputs": {"sample": ["142:141", 0], "key": "k"}},
    }
    chains = compile_op_chains(prompt, _op_mappings(), skip_ids=["142"])
    assert [op.kwargs.get("amount") for op in chains[0]] == [10.0]

    # A second consumer of node 2 just continues the line — still one flat chain.
    prompt["5"] = {"class_type": "Taidal_AddOp", "inputs": {"sample": ["2", 0], "amount": 2.0}}
    chains = compile_op_chains(prompt, _op_mappings(), skip_ids=["142"])
    assert [op.kwargs.get("amount") for op in chains[0]] == [10.0, 2.0]


def test_export_skip_never_prunes_before_the_mark() -> None:
    # A skip mark means "this node and everything BEHIND it" — never the nodes before.
    # Node 3 feeds ONLY the marked node 4, but it sits before the mark on the sample
    # path, so it stays in the schedule (a dangling branch is faithful; to drop the
    # whole branch, mark its head — see test_export_skip_mark_drops_node_and_downstream).
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["1", 0], "amount": 10.0}},
        "3": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["2", 0], "amount": 1.0}},  # feeder of 4
        "4": {"class_type": "Taidal_WriteMetaOp", "inputs": {"sample": ["3", 0], "key": "viz"}},  # marked
        "5": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["2", 0], "amount": 2.0}},  # main line
    }
    notes: List[List[str]] = []
    chains = compile_op_chains(prompt, _op_mappings(), chain_notes=notes, skip_ids=["4"])
    amounts = [op.kwargs.get("amount") for op in chains[0] if op.target is AddOp]
    assert amounts == [10.0, 1.0, 2.0]  # node 3 survives; only the marked node is gone
    skip_notes = [note for note in notes[0] if "export-skip mark" in note]
    assert skip_notes and "node 4" in skip_notes[0] and "node 3" not in skip_notes[0]


def test_export_skip_closure_through_mix() -> None:
    # A Mix consuming a marked branch is data-downstream of it → the Mix and its tail are
    # excluded too (a merge of a canvas-only stream is canvas-only).
    prompt = dict(_mix_prompt())
    chains = compile_op_chains(prompt, _op_mappings(), skip_ids=["4"])  # mark the Z branch head
    targets = [op.target for op in chains[0]]
    assert "MixSamplesNode" not in str(targets)
    flat = [op for op in chains[0] if op.target is AddOp]
    assert [op.kwargs["amount"] for op in flat] == [10.0, 100.0]  # the X branch survives; tail (post-Mix) gone


def test_export_skip_after_mix_keeps_the_mix() -> None:
    # The reported scenario, no target mark involved: skipping the node right AFTER the
    # Mix prunes the tail only — the Mix and the whole pipeline computing it still export
    # ("prune all nodes that are behind the mark, NOT before").
    prompt = dict(_mix_prompt())
    chains = compile_op_chains(prompt, _op_mappings(), skip_ids=["7"])  # the post-Mix tail
    assert len(chains) == 1
    amounts = [op.kwargs.get("amount") for op in chains[0] if op.target is AddOp]
    assert amounts == [10.0, 100.0, 1.0]  # full Mix recipe; only the +1000 tail is gone

    ops = _load_ops(export_ops_yamls(prompt, _op_mappings(), skip_ids=["7"])[0])
    out = list(Flux(source=[Sample(input=1.0, target=None, metadata={})], ops=ops))[0]
    assert out.input == 111.0 and out.target == 42.0 and out.meta["regions"] == 12.0


# --------------------------------------------------------------------------- export targets


def test_target_mode_exports_ancestor_cone() -> None:
    # Selecting the Mix as the export target inverts the selection: the doc contains
    # exactly the ops that COMPUTE it — the post-Mix tail and any side branch off the
    # fork are simply not scheduled (no marks needed on each viz helper).
    prompt = dict(_mix_prompt())
    prompt["90"] = {"class_type": "Taidal_WriteMetaOp", "inputs": {"sample": ["2", 0], "key": "viz"}}  # side branch
    notes: List[List[str]] = []
    chains = compile_op_chains(prompt, _op_mappings(), chain_notes=notes, target_ids=["60"])
    assert len(chains) == 1
    amounts = [op.kwargs.get("amount") for op in chains[0] if op.target is AddOp]
    assert amounts == [10.0, 100.0, 1.0]  # the cone — no +1000 tail
    assert not any(op.target is WriteMetaOp and op.kwargs.get("key") == "viz" for op in chains[0])
    assert any("compiled for export target: node 60" in note for note in notes[0])

    # Execution: the document's final state IS the Mix result.
    ops = _load_ops(export_ops_yamls(prompt, _op_mappings(), target_ids=["60"])[0])
    out = list(Flux(source=[Sample(input=1.0, target=None, metadata={})], ops=ops))[0]
    assert out.input == 111.0 and out.target == 42.0 and out.meta["regions"] == 12.0


def test_target_mode_one_doc_per_target() -> None:
    # Two targets → two complete documents, each that target's own recipe (the shared
    # prefix is duplicated — every doc stands alone).
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["1", 0], "amount": 10.0}},
        "3": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["2", 0], "amount": 1.0}},
        "4": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["2", 0], "amount": 2.0}},
    }
    chains = compile_op_chains(prompt, _op_mappings(), target_ids=["3", "4"])
    assert len(chains) == 2
    assert [op.kwargs["amount"] for op in chains[0]] == [10.0, 1.0]
    assert [op.kwargs["amount"] for op in chains[1]] == [10.0, 2.0]


def test_target_mode_keeps_value_chain_folding() -> None:
    # The cone restricts only the SCHEDULE — value chains, constants, and op-values still
    # resolve from the full prompt (the ConfigureOp wrap survives target mode).
    chains = compile_op_chains(_value_chain_prompt(), _op_mappings(), target_ids=["6"])
    assert [op.target for op in chains[0]] == [AddOp, ConfigureOp]


def test_target_unknown_or_cone_conflict_raises() -> None:
    prompt = dict(_mix_prompt())
    with pytest.raises(UnsupportedTopologyError, match="not found in the graph"):
        compile_op_chains(prompt, _op_mappings(), target_ids=["999"])
    # Node 4 is INSIDE the Mix's dependency cone — skip-marking it contradicts the target.
    with pytest.raises(UnsupportedTopologyError, match="conflict with the export target"):
        compile_op_chains(prompt, _op_mappings(), skip_ids=["4"], target_ids=["60"])


def test_skip_downstream_of_target_keeps_cone() -> None:
    # The reported scenario: MixSamples is the target, the NEXT node (the tail) carries
    # the skip mark. The cone is immune to the skip closures — "not exported" means the
    # marked node and everything after it; the nodes before it (the cone) still export.
    prompt = dict(_mix_prompt())
    notes: List[List[str]] = []
    chains = compile_op_chains(prompt, _op_mappings(), chain_notes=notes, skip_ids=["7"], target_ids=["60"])
    assert len(chains) == 1
    amounts = [op.kwargs.get("amount") for op in chains[0] if op.target is AddOp]
    assert amounts == [10.0, 100.0, 1.0]  # the full cone — no +1000 tail
    assert any("export-skip mark" in note and "node 7" in note for note in notes[0])
    assert any("compiled for export target: node 60" in note for note in notes[0])

    # Execution parity: identical to the plain target export — the Mix result is final.
    ops = _load_ops(export_ops_yamls(prompt, _op_mappings(), skip_ids=["7"], target_ids=["60"])[0])
    out = list(Flux(source=[Sample(input=1.0, target=None, metadata={})], ops=ops))[0]
    assert out.input == 111.0 and out.target == 42.0 and out.meta["regions"] == 12.0


def test_collect_export_marks_reads_top_level_and_subgraph_inner_nodes() -> None:
    # The marks live in node.properties — the ops-export PROMPT strips those, so they are read
    # from the UI workflow (top-level AND every subgraph definition's inner nodes). This mirrors
    # the reported case: an export-target placed on a node INSIDE a subgraph.
    workflow = {
        "nodes": [
            {"id": 61, "type": "Taidal_AddOp", "properties": {"taidal_export": "skip"}},
            {"id": 7, "type": "Taidal_AddOp", "properties": {}},  # unmarked
        ],
        "definitions": {
            "subgraphs": [
                {
                    "id": "sg-uuid",
                    "nodes": [
                        {"id": 371, "type": "Taidal_AddOp", "properties": {"taidal_export": "target"}},
                        {"id": 372, "type": "Taidal_AddOp", "properties": {"taidal_export": "skip"}},
                    ],
                }
            ]
        },
    }
    skip, target = collect_export_marks(workflow)
    assert sorted(skip) == ["372", "61"]  # top-level AND subgraph-inner skip marks
    assert target == ["371"]  # the inner target mark is collected (the bug: it used to be missed)


def test_collect_export_marks_handles_propertyless_and_non_dict_inputs() -> None:
    # A prompt-form dict (no properties) and a non-dict both yield no marks — never raises.
    assert collect_export_marks({"1": {"class_type": "Taidal_AddOp", "inputs": {}}}) == ([], [])
    assert collect_export_marks(None) == ([], [])
    assert collect_export_marks({"nodes": [{"id": 1}]}) == ([], [])  # node without properties


def test_resolve_marked_matches_subgraph_path_segments() -> None:
    # A subgraph instance expands to ':'-joined prompt ids; a mark resolves by SEGMENT match,
    # so it reaches a node wherever it sits on the instance path.
    prompt: Dict[str, Any] = {"378:368:371": {}, "378:368:373": {}, "61": {}, "2": {}}
    assert _resolve_marked({"371"}, prompt) == {"378:368:371"}  # node INSIDE a (nested) subgraph
    assert _resolve_marked({"378"}, prompt) == {"378:368:371", "378:368:373"}  # whole INSTANCE subtree
    assert _resolve_marked({"368"}, prompt) == {"378:368:371", "378:368:373"}  # a nested instance
    assert _resolve_marked({"61"}, prompt) == {"61"}  # a plain top-level node
    assert _resolve_marked({"999"}, prompt) == set()  # unknown id matches nothing
    # _marked_ids (skip path) uses the same resolution
    assert _marked_ids(["371"], prompt) == {"378:368:371"}


def test_target_inside_subgraph_restricts_to_its_cone() -> None:
    # The reported bug, end to end: a target placed INSIDE a subgraph (the prompt id carries the
    # instance prefix '9:') must export ONLY its ancestor cone — the sibling taps off the same fork
    # and the unrelated branch are NOT scheduled. Target given by the inner id, as collect_* returns.
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["1", 0], "amount": 1.0}},  # the fork
        "9:3": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["2", 0], "amount": 2.0}},  # TARGET (inner)
        "9:4": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["2", 0], "amount": 3.0}},  # sibling tap
        "5": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["2", 0], "amount": 4.0}},  # other branch
    }
    chains = compile_op_chains(prompt, _op_mappings(), target_ids=["3"])
    assert len(chains) == 1
    assert [op.kwargs["amount"] for op in chains[0]] == [1.0, 2.0]  # only 9:3 and its ancestor 2
    # the deepest node IS the target (the cone's final op)
    ops = _load_ops(export_ops_yamls(prompt, _op_mappings(), target_ids=["3"])[0])
    out = list(Flux(source=[Sample(input=0.0, target=None, metadata={})], ops=ops))[0]
    assert out.input == 3.0  # 0 +1 (fork) +2 (target) — the sibling +3 / branch +4 are absent


def test_target_downstream_of_mix_includes_metadata_sample_branch() -> None:
    # A target DOWNSTREAM of a Mix Samples node must pull in the metadata_sample branch too — the
    # cone follows ALL THREE Mix args (input/target/metadata), so a detection/regions branch that
    # reaches the target only through the Mix's metadata_sample IS exported (the regions producer is
    # an ancestor via the Mix). Mirrors the impairments graph: RegionPowerAnalysisOp reads regions
    # the Signal-Regions branch wrote, merged in by Mix Samples.
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["1", 0], "amount": 1.0}},  # shared upstream
        "det": {"class_type": "Taidal_WriteMetaOp", "inputs": {"sample": ["2", 0], "key": "regions"}},  # detection
        "60": {
            "class_type": "Taidal_MixSamples",
            "inputs": {"input_sample": ["2", 0], "target_sample": ["2", 0], "metadata_sample": ["det", 0]},
        },
        "371": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["60", 0], "amount": 2.0}},  # TARGET, post-Mix
    }
    chains = compile_op_chains(prompt, _op_mappings(), target_ids=["371"])
    targets = [op.target for chain in chains for op in chain]
    assert WriteMetaOp in targets  # the metadata producer (the regions branch) is in the cone


def test_skip_subgraph_inner_id_excludes_that_node_and_downstream() -> None:
    # A skip mark on a node inside a subgraph (inner id '3' → prompt '9:3') is honoured the same way.
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["1", 0], "amount": 1.0}},
        "9:3": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["2", 0], "amount": 2.0}},  # SKIP (inner)
        "9:5": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["9:3", 0], "amount": 3.0}},  # downstream
    }
    chains = compile_op_chains(prompt, _op_mappings(), skip_ids=["3"])
    assert [op.kwargs["amount"] for chain in chains for op in chain] == [1.0]  # only the pre-mark op survives


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


# --------------------------------------------------------------------------- subgraph-as-op


def _subgraph_op_into_parallel_prompt() -> Dict[str, Any]:
    """source(1) → AddOp(2,+1) → AddOp(3,+2) → terminator(4); Parallel(5) applies the cone op."""
    return {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["1", 0], "amount": 1.0}},
        "3": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["2", 0], "amount": 2.0}},
        "4": {"class_type": "Taidal_SubgraphOp", "inputs": {"sample": ["3", 0]}},
        "5": {"class_type": "Taidal_Parallel", "inputs": {"sample": ["1", 0], "op_0": ["4", 0]}},
    }


def test_subgraph_op_into_parallel_embeds_a_transformchain() -> None:
    chains = compile_op_chains(_subgraph_op_into_parallel_prompt(), _op_mappings())
    # ONE pipeline (the Parallel consumer): the terminator's cone ops are NOT scheduled standalone.
    assert len(chains) == 1
    assert len(chains[0]) == 1
    parallel = chains[0][0]
    assert parallel.target is Parallel
    embedded = parallel.kwargs["ops"]
    assert len(embedded) == 1
    chain = embedded[0]
    assert chain.target is TransformChain
    inner = chain.kwargs["ops"]
    assert [op.target for op in inner] == [AddOp, AddOp]
    assert [op.kwargs["amount"] for op in inner] == [1.0, 2.0]


def test_subgraph_op_round_trip_applies_the_inner_pipeline() -> None:
    ops = _load_ops(export_ops_yaml(_subgraph_op_into_parallel_prompt(), _op_mappings()))
    out = list(Flux(source=[Sample(input=0.0, target=None, metadata={})], ops=ops))[0]
    # Parallel applied per-sample runs its op inline: TransformChain([+1, +2]) → 0 + 1 + 2.
    assert out.input == 3.0


def test_subgraph_op_into_configure_target_embeds_a_transformchain() -> None:
    # The terminator's op output wired into a SINGLE-op slot (ConfigureOp.target).
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["1", 0], "amount": 5.0}},
        "4": {"class_type": "Taidal_SubgraphOp", "inputs": {"sample": ["2", 0]}},
        "5": {
            "class_type": "Taidal_ConfigureOp",
            "inputs": {"sample": ["1", 0], "target": ["4", 0], "param": "amount", "key": "k"},
        },
    }
    chains = compile_op_chains(prompt, _op_mappings())
    assert len(chains) == 1
    cfg = chains[0][0]
    assert cfg.target is ConfigureOp
    chain = cfg.kwargs["target"]
    assert chain.target is TransformChain
    assert [op.kwargs["amount"] for op in chain.kwargs["ops"]] == [5.0]


def test_subgraph_op_fan_out_inner_dag_flattens_with_stash() -> None:
    # An inner fork (node 2 → 3 and 4) reconverged by Mix → the embedded TransformChain carries
    # the auto-inserted stash/unstash sequence (DAG→sequential inside the subgraph too).
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["1", 0], "amount": 1.0}},
        "3": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["2", 0], "amount": 2.0}},
        "4": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["2", 0], "amount": 3.0}},
        "5": {
            "class_type": "Taidal_MixSamples",
            "inputs": {"input_sample": ["3", 0], "target_sample": ["4", 0], "metadata_sample": ["3", 0]},
        },
        "6": {"class_type": "Taidal_SubgraphOp", "inputs": {"sample": ["5", 0]}},
        "7": {"class_type": "Taidal_Parallel", "inputs": {"sample": ["1", 0], "op_0": ["6", 0]}},
    }
    chains = compile_op_chains(prompt, _op_mappings())
    assert len(chains) == 1
    inner = chains[0][0].kwargs["ops"][0].kwargs["ops"]
    targets = [op.target for op in inner]
    assert AddOp in targets and StashInputOp in targets and UnstashInputOp in targets


def test_subgraph_op_unconsumed_terminator_schedules_inner_as_a_graph() -> None:
    # No consumer of the terminator's op output → the inner ops compile as an ordinary pipeline
    # (the "use the subgraph as a graph" path), NOT excluded.
    prompt = {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["1", 0], "amount": 1.0}},
        "3": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["2", 0], "amount": 2.0}},
        "4": {"class_type": "Taidal_SubgraphOp", "inputs": {"sample": ["3", 0]}},
    }
    chains = compile_op_chains(prompt, _op_mappings())
    assert len(chains) == 1
    assert [op.target for op in chains[0]] == [AddOp, AddOp]


def _subgraph_op_target_mode_prompt() -> Dict[str, Any]:
    """The compile-its-OWN-cone case (what ``compile_subgraph_ops`` does): terminator(5) whose
    ``op`` output IS consumed (Parallel 6), compiled in TARGET MODE for the terminator.

    Cone ops 2 and 4 also have their sample read by OFF-CONE viewer nodes (8, 9), so
    ``_sample_consumed_outside`` keeps them while the connective op 3 is self-excluded — so WITHOUT
    the target exemption the cone fragments into ``{2}`` and ``{4}`` (2 components) and the strict
    ``export_ops_yaml`` raises ``Found 2 independent op components``. This is the minimal form of
    the user's real impairments graph (where the off-cone consumer is the Spectral-Analysis viewer
    branch and the self-excluded backbone is Squeeze→Crop→…→NoiseFloor).
    """
    return {
        "1": {"class_type": "WalkDatasetNode", "inputs": {}},
        "2": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["1", 0], "amount": 1.0}},
        "3": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["2", 0], "amount": 2.0}},
        "4": {"class_type": "Taidal_AddOp", "inputs": {"sample": ["3", 0], "amount": 3.0}},
        "5": {"class_type": "Taidal_SubgraphOp", "inputs": {"sample": ["4", 0]}},
        "6": {"class_type": "Taidal_Parallel", "inputs": {"sample": ["1", 0], "op_0": ["5", 0]}},
        "8": {"class_type": "WalkDatasetNode", "inputs": {"sample": ["2", 0]}},  # off-cone viewer
        "9": {"class_type": "WalkDatasetNode", "inputs": {"sample": ["4", 0]}},  # off-cone viewer
    }


def test_subgraph_op_target_mode_compiles_full_cone_despite_self_exclusion() -> None:
    # `compile_subgraph_ops` compiles the terminator's OWN cone via target mode. Even though the
    # terminator's `op` output is consumed (Parallel 6) — which normally drops its cone from the
    # standalone schedule — the target exemption keeps the cone, so it compiles to ONE chain
    # instead of fragmenting into the `Found N independent op components` error.
    prompt = _subgraph_op_target_mode_prompt()
    chains = compile_op_chains(prompt, _op_mappings(), target_ids=["5"])
    assert len(chains) == 1
    assert [op.target for op in chains[0]] == [AddOp, AddOp, AddOp]
    # End-to-end: the cone applies in order, 0 +1 +2 +3 = 6.
    ops = _load_ops(export_ops_yaml(prompt, _op_mappings(), target_ids=["5"]))
    out = list(Flux(source=[Sample(input=0.0, target=None, metadata={})], ops=ops))[0]
    assert out.input == 6.0


def test_subgraph_cone_exclusion_applies_only_when_terminator_is_not_the_target() -> None:
    # Scope guard: the SAME consumed terminator, compiled WITHOUT naming it a target (the
    # whole-graph standalone path), still excludes its cone (it embeds in the Parallel) — the
    # exemption must not leak into the non-target compile.
    prompt = _subgraph_op_target_mode_prompt()
    assert "3" in _subgraph_cone_excluded(prompt, _op_mappings())  # no exempt → connective op excluded
    assert "3" not in _subgraph_cone_excluded(prompt, _op_mappings(), exempt={"5"})  # exempt target → kept
