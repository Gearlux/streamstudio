"""Tests for ``fluxstudio.graphio`` — the whole-graph YAML ↔ workflow.json converter.

Covers the shared helpers (value coercion, the class→node_key reverse index, the
eager/lazy output-slot rule), a programmatic source→ops→engine round-trip built from
``create_comfy_node`` mappings, and an end-to-end round-trip on the real sonair MNIST
training config (skipped when the example file isn't checked out).

NOTE: no ``from __future__ import annotations`` — the bridge classifiers inspect real
annotation objects (mirrors ``tests/test_export.py``).
"""

import json
from pathlib import Path
from typing import Any, Dict

import confluid
import pytest
from confluid import Class, Instance, LazyClass, Reference
from dataflux import Flux
from dataflux.ops.torch import RescaleOp
from dataflux.sources import HuggingFaceSource
from test_export import ChainLikeOp, ConstTxConfig, ThresholdLikeOp  # local @configurable fixtures

from fluxstudio.bridge import create_comfy_node
from fluxstudio.graphio import (
    _kwarg_to_widget,
    _node_key_for_class,
    _output_slot,
    _strip_index,
    workflow_to_yaml,
    yaml_to_workflow,
)

_WORKSPACE = Path(__file__).resolve().parents[2]
_MNIST_CFG = _WORKSPACE / "sonair" / "examples" / "train_efficientvit_b0_mnist.yaml"


def _engine_mappings() -> Dict[str, Any]:
    """A small NODE_CLASS_MAPPINGS: a source, an op, and the Flux engine."""
    return {
        "src": create_comfy_node(HuggingFaceSource, "src"),
        "rescale": create_comfy_node(RescaleOp, "rescale"),
        "flux": create_comfy_node(Flux, "flux"),
    }


# --- helper units ----------------------------------------------------------------------


def test_kwarg_to_widget_coercions() -> None:
    assert _kwarg_to_widget(None) == ""
    assert _kwarg_to_widget(Path("/tmp/x")) == "/tmp/x"
    assert _kwarg_to_widget([1, 2, 3]) == "1,2,3"
    assert _kwarg_to_widget(7) == 7
    assert _kwarg_to_widget("mnist") == "mnist"
    # a function reference → its importable dotted path (so collate_fn etc. survive)
    assert _kwarg_to_widget(json.dumps).endswith("json.dumps")


def test_node_key_reverse_index() -> None:
    nm = _engine_mappings()
    assert _node_key_for_class(HuggingFaceSource, nm) == "src"
    assert _node_key_for_class(RescaleOp, nm) == "rescale"
    assert _node_key_for_class(Flux, nm) == "flux"
    assert _node_key_for_class(dict, nm) is None  # not a node


def test_output_slot_rule() -> None:
    nm = _engine_mappings()
    src_node = nm["src"]
    op_node = nm["rescale"]
    assert _output_slot(src_node, is_lazy=False) == 0  # source → its `source` output
    assert _output_slot(op_node, is_lazy=False) == 1  # op → the `op` value output


# --- programmatic round-trip (source → ops → engine) -----------------------------------


def test_yaml_to_workflow_engine_pipeline() -> None:
    nm = _engine_mappings()
    doc = {
        "pipeline": Class(Flux, source=Reference("raw"), ops=[Instance(RescaleOp, in_min=0.0, in_max=1.0)]),
        "raw": Instance(HuggingFaceSource, path="mnist"),
    }
    wf = yaml_to_workflow(doc, nm)
    types = sorted(n["type"] for n in wf["nodes"])
    assert types == ["flux", "rescale", "src"]
    assert wf["version"] == 0.4
    # one link source→flux.source, one link rescale.op→flux.op_0
    link_types = sorted(link[5] for link in wf["links"])
    assert link_types == ["DATAFLUX_OP", "DATAFLUX_SOURCE"]
    # the op wires from its slot-1 (`op`) output; the source from slot-0
    by_id = {n["id"]: n for n in wf["nodes"]}
    for link in wf["links"]:
        _lid, src_id, src_slot, _tid, _tslot, _t = link
        if by_id[src_id]["type"] == "rescale":
            assert src_slot == 1
        if by_id[src_id]["type"] == "src":
            assert src_slot == 0


def test_engine_pipeline_round_trips_to_yaml() -> None:
    nm = _engine_mappings()
    doc = {
        "pipeline": Class(Flux, source=Reference("raw"), ops=[Instance(RescaleOp, in_min=0.0, in_max=2.0)]),
        "raw": Instance(HuggingFaceSource, path="mnist"),
    }
    wf = yaml_to_workflow(doc, nm)
    yaml_text = workflow_to_yaml(wf, nm)
    # The exported YAML must re-resolve to a Fluid graph with a Flux engine + the op + source.
    graph = confluid.resolve(yaml_text)
    assert isinstance(graph, dict)
    flux_markers = [v for v in graph.values() if isinstance(v, (Class, Instance)) and "Flux" in str(v.target)]
    assert len(flux_markers) == 1
    flux = flux_markers[0]
    # the Flux references its source + carries one op
    assert "source" in flux.kwargs
    assert isinstance(flux.kwargs.get("ops"), list) and len(flux.kwargs["ops"]) == 1


def test_strip_index_maps_list_sockets_to_param() -> None:
    # op/source sockets map to their (differently-named) list param; object-list sockets (logger_0,
    # callbacks_2) strip the trailing _<digit> to the param name; non-indexed sockets are unchanged.
    assert _strip_index("op_0") == "ops"
    assert _strip_index("source_3") == "fluxes"
    assert _strip_index("logger_0") == "logger"
    assert _strip_index("callbacks_2") == "callbacks"
    assert _strip_index("model") == "model"
    assert _strip_index("train_set") == "train_set"


def test_object_list_member_round_trips_as_logger_n() -> None:
    # A list-valued OBJECT member (a Trainer's `logger`) imports into the node's dynamic logger_0 /
    # logger_1 sockets (its _admits_object_list family) and exports back into one `logger:` list —
    # the importer/exporter respect how the node handles lists, instead of a single by-name socket.
    import pytorch_lightning as pl
    from pytorch_lightning.loggers import CSVLogger, TensorBoardLogger

    nm = {
        "trainer": create_comfy_node(pl.Trainer, "trainer"),
        "tb": create_comfy_node(TensorBoardLogger, "tb"),
        "csv": create_comfy_node(CSVLogger, "csv"),
    }
    doc = {
        "lightning": LazyClass(
            pl.Trainer,
            max_epochs=5,
            logger=[
                Instance(TensorBoardLogger, save_dir="./l", name="a"),
                Instance(CSVLogger, save_dir="./l", name="b"),
            ],
        ),
    }
    wf = yaml_to_workflow(doc, nm)

    # IMPORT: both loggers wire into logger_0 / logger_1 — NOT a single bare `logger` socket.
    trainer_node = next(n for n in wf["nodes"] if n["type"] == "trainer")
    logger_links = {
        i["name"]: i.get("link") is not None for i in trainer_node["inputs"] if i["name"].startswith("logger")
    }
    assert logger_links.get("logger_0") is True
    assert logger_links.get("logger_1") is True
    assert "logger" not in logger_links

    # EXPORT: logger_0 / logger_1 collapse back into one `logger:` list of two refs.
    yaml_text = workflow_to_yaml(wf, nm)
    graph = confluid.resolve(yaml_text)
    trainer_markers = [
        v for v in graph.values() if isinstance(v, (Class, Instance, LazyClass)) and "Trainer" in str(v.target)
    ]
    assert len(trainer_markers) == 1
    logger_val = trainer_markers[0].kwargs.get("logger")
    assert isinstance(logger_val, list) and len(logger_val) == 2


def test_unmapped_node_is_skipped_not_fatal() -> None:
    """A child class with no FluxStudio node is dropped (logged), not a hard failure."""
    nm = {"src": create_comfy_node(HuggingFaceSource, "src")}  # no node for `dict`
    doc = {"raw": Instance(HuggingFaceSource, path="mnist")}
    wf = yaml_to_workflow(doc, nm)
    assert len(wf["nodes"]) == 1  # just the source; nothing crashes


def test_dropped_member_warning_is_interpolated() -> None:
    """The 'no FluxStudio node' drop warning must interpolate the class name.

    logflow is loguru-backed, so the message MUST use ``{}`` brace fields — a stdlib
    ``%s`` template is left verbatim and the args are silently ignored, so the operator
    sees a useless ``Skipping %s.%s`` line. Regression pin for that formatting bug.
    """
    from loguru import logger as _loguru

    # `RescaleOp` is a real op but has no node in this mapping, so its child Instance is
    # dropped-with-warning while the engine + source still emit.
    nm = {"flux": create_comfy_node(Flux, "flux"), "src": create_comfy_node(HuggingFaceSource, "src")}
    doc = {
        "pipeline": Class(Flux, source=Reference("raw"), ops=[Instance(RescaleOp, in_min=0.0, in_max=1.0)]),
        "raw": Instance(HuggingFaceSource, path="mnist"),
    }
    messages: list = []
    sink_id = _loguru.add(lambda m: messages.append(m.record["message"]), level="WARNING", format="{message}")
    try:
        wf = yaml_to_workflow(doc, nm)
    finally:
        _loguru.remove(sink_id)

    assert sorted(n["type"] for n in wf["nodes"]) == ["flux", "src"]  # RescaleOp dropped
    dropped = [m for m in messages if "no FluxStudio node" in m]
    assert dropped, "expected a drop warning for the unmapped RescaleOp member"
    assert "dataflux.ops.torch.RescaleOp" in dropped[0]  # the args were interpolated …
    assert "%s" not in dropped[0]  # … not left as a literal stdlib template


# --- export skips non-Taidal nodes (comment block) --------------------------------------


def _canvas_mappings() -> Dict[str, Any]:
    """Mappings mimicking a live canvas: source + op + the static adapter nodes."""
    from fluxstudio.nodes import NODE_CLASS_MAPPINGS as static_nodes

    return {
        "src": create_comfy_node(HuggingFaceSource, "src"),
        "rescale": create_comfy_node(RescaleOp, "rescale"),
        **static_nodes,
    }


def _canvas_workflow() -> Dict[str, Any]:
    """A realistic stepping canvas: src → Walk → op → op → Extract → Preview (+ a 2nd chain)."""
    op_widgets = [0.0, 255.0, 0.0, 1.0, False]
    return {
        "nodes": [
            {"id": 1, "type": "src", "widgets_values": ["mnist", "", "image", "label", "", ""], "inputs": []},
            {
                "id": 2,
                "type": "Taidal_WalkDataset",
                "widgets_values": [0, "increment", True],
                "inputs": [{"name": "source", "type": "DATAFLUX_SOURCE", "link": 1}],
            },
            {
                "id": 3,
                "type": "rescale",
                "widgets_values": op_widgets,
                "inputs": [{"name": "sample", "type": "DATAFLUX_SAMPLE", "link": 2}],
            },
            {
                "id": 4,
                "type": "rescale",
                "widgets_values": op_widgets,
                "inputs": [{"name": "sample", "type": "DATAFLUX_SAMPLE", "link": 3}],
            },
            {
                "id": 5,
                "type": "Taidal_ExtractSample",
                "widgets_values": ["input", "viridis", 512],
                "inputs": [{"name": "sample", "type": "DATAFLUX_SAMPLE", "link": 4}],
            },
            {"id": 6, "type": "PreviewImage", "widgets_values": [], "inputs": [{"name": "images", "link": 5}]},
            {
                "id": 7,
                "type": "rescale",
                "widgets_values": op_widgets,
                "inputs": [{"name": "sample", "type": "DATAFLUX_SAMPLE", "link": 6}],
            },
        ],
        "links": [
            [1, 1, 0, 2, 0, "DATAFLUX_SOURCE"],
            [2, 2, 0, 3, 0, "DATAFLUX_SAMPLE"],
            [3, 3, 0, 4, 0, "DATAFLUX_SAMPLE"],
            [4, 4, 0, 5, 0, "DATAFLUX_SAMPLE"],
            [5, 5, 0, 6, 0, "IMAGE"],
            [6, 2, 0, 7, 0, "DATAFLUX_SAMPLE"],
        ],
    }


def test_export_skips_native_and_adapter_nodes_with_comment() -> None:
    # Native ComfyUI nodes (PreviewImage) and the canvas-only adapters (Walk/Extract) no longer
    # fail the whole-graph export — they're left out and listed in a leading comment block.
    yaml_text = workflow_to_yaml(_canvas_workflow(), _canvas_mappings())
    head = yaml_text.splitlines()
    assert head[0].startswith("# NOTE:")
    comments = "\n".join(line for line in head if line.startswith("#"))
    assert "node 2 (Taidal_WalkDataset): canvas-only Taidal adapter" in comments
    assert "node 5 (Taidal_ExtractSample): canvas-only Taidal adapter" in comments
    assert "node 6 (PreviewImage): native ComfyUI node" in comments
    # The Taidal nodes still export — and the document stays parseable (comments are invisible).
    # The source is named by its class: its only consumer (Walk) is skipped, and skipped
    # consumers' socket names are UI plumbing, never ref names.
    graph = confluid.resolve(yaml_text)
    assert "HuggingFaceSource" in str(graph["hugging_face_source"].target)
    assert "Flux" in str(graph["pipeline"].target) and "Flux" in str(graph["pipeline_2"].target)


def test_export_folds_sample_chains_into_flux_pipelines() -> None:
    # The canvas WIRING must survive: sample-chained ops fold into `pipeline: !class:Flux`
    # entries — `source:` traced through the (skipped) Walk adapter back to the source node,
    # `ops:` the ordered inline op list. No `sample:` kwarg leaks (it would crash on reload),
    # and the chained ops do NOT appear as standalone top-level entries.
    yaml_text = workflow_to_yaml(_canvas_workflow(), _canvas_mappings())
    assert "sample:" not in yaml_text
    graph = confluid.resolve(yaml_text)
    assert not any(k.startswith("rescale_op") for k in graph)  # folded, not standalone
    assert yaml_text.count("!ref 'hugging_face_source'") == 2  # both pipelines reference the source
    pipeline, pipeline_2 = graph["pipeline"], graph["pipeline_2"]
    for flux in (pipeline, pipeline_2):
        assert "Flux" in str(flux.target)
        # resolve() substitutes the !ref: with the shared source marker (identity sharing)
        assert "HuggingFaceSource" in str(getattr(flux.kwargs.get("source"), "target", ""))
    assert len(pipeline.kwargs["ops"]) == 2  # node 3 → node 4
    assert len(pipeline_2.kwargs["ops"]) == 1  # node 7
    assert all("RescaleOp" in str(op) for op in pipeline.kwargs["ops"])
    # in_max rode along on the inline op entries
    assert "in_max: 255.0" in yaml_text


def test_export_folds_fan_out_into_tee() -> None:
    # Fan-out inside a chain (one op's sample output feeding two ops) folds into a nested Tee,
    # exactly like the ops-export shape.
    workflow = _canvas_workflow()
    workflow["nodes"].append(
        {
            "id": 8,
            "type": "rescale",
            "widgets_values": [0.0, 99.0, 0.0, 1.0, False],
            "inputs": [{"name": "sample", "type": "DATAFLUX_SAMPLE", "link": 7}],
        }
    )
    workflow["links"].append([7, 3, 0, 8, 0, "DATAFLUX_SAMPLE"])  # node 3 now fans out to 4 AND 8
    yaml_text = workflow_to_yaml(workflow, _canvas_mappings())
    graph = confluid.resolve(yaml_text)
    ops = graph["pipeline"].kwargs["ops"]
    assert len(ops) == 2  # head op + the Tee
    assert "Tee" in str(ops[1])
    assert "in_max: 99.0" in yaml_text  # the second branch's op rode inside the Tee


def test_export_failing_taidal_node_is_isolated_with_warning() -> None:
    # A Taidal node whose spec fails to build (unresolvable TAIDAL_CALLABLE_PATH here) is
    # skipped-with-comment + warning — the rest of the graph still exports.
    from loguru import logger as _loguru

    class _BadOp:
        TAIDAL_NODE_KIND = "op"
        TAIDAL_CALLABLE_PATH = "nonexistent.module:Nope"

        @classmethod
        def INPUT_TYPES(cls) -> Dict[str, Any]:
            return {"required": {}}

    nm = {"src": create_comfy_node(HuggingFaceSource, "src"), "bad": _BadOp}
    workflow = {
        "nodes": [
            {"id": 1, "type": "src", "widgets_values": ["mnist", "", "image", "label", "", ""], "inputs": []},
            {"id": 2, "type": "bad", "widgets_values": [], "inputs": []},
        ],
        "links": [],
    }
    messages: list = []
    sink_id = _loguru.add(lambda m: messages.append(m.record["message"]), level="WARNING", format="{message}")
    try:
        yaml_text = workflow_to_yaml(workflow, nm)
    finally:
        _loguru.remove(sink_id)

    assert "node 2 (bad): failed to serialize" in yaml_text
    # The healthy source node still exports (unconsumed → named by its class).
    assert "HuggingFaceSource" in str(confluid.resolve(yaml_text)["hugging_face_source"].target)
    assert any("could not serialize" in m for m in messages)


def test_export_dropped_config_wire_is_commented() -> None:
    # A wire from a skipped node into a REAL config member (DatasetSplit.source here) is
    # dropped — the member falls back to its default — and noted in the comment block.
    from dataflux.sources import DatasetSplit

    nm = {"split": create_comfy_node(DatasetSplit, "split")}
    workflow = {
        "nodes": [
            {"id": 1, "type": "LoadImage", "widgets_values": [], "inputs": []},
            {
                "id": 2,
                "type": "split",
                "widgets_values": [],
                "inputs": [{"name": "source", "type": "DATAFLUX_SOURCE", "link": 1}],
            },
        ],
        "links": [[1, 1, 0, 2, 0, "DATAFLUX_SOURCE"]],
    }
    yaml_text = workflow_to_yaml(workflow, nm)
    assert "node 1 (LoadImage): native ComfyUI node" in yaml_text
    assert "dataset_split.source <- node 1 (LoadImage)" in yaml_text
    split_marker = confluid.resolve(yaml_text)["dataset_split"]
    assert "source" not in split_marker.kwargs

    # Prompt-form graph with a DANGLING link (source node id absent entirely) → "unknown".
    prompt = {"2": {"class_type": "split", "inputs": {"source": ["99", 0]}}}
    assert "dataset_split.source <- node 99 (unknown)" in workflow_to_yaml(prompt, nm)


# --- subgraph-instance inlining (the real ComfyUI save format) --------------------------


def _chain_subgraph_def(def_id: str) -> Dict[str, Any]:
    """A 2-op subgraph saved the way current ComfyUI writes it — inner links as DICTS.

    Boundary: input slot 0 (sample) → inner head 21; input slot 1 (k) → 21's `in_max`
    WIDGET input; inner chain 21 → 22; 22 → output slot 0.
    """
    return {
        "id": def_id,
        "name": "Chain",
        "inputNode": {"id": -10},
        "outputNode": {"id": -20},
        "inputs": [{"name": "sample", "type": "DATAFLUX_SAMPLE"}, {"name": "k", "type": "FLOAT"}],
        "outputs": [{"name": "sample", "type": "DATAFLUX_SAMPLE"}],
        "nodes": [
            {
                "id": 21,
                "type": "rescale",
                "widgets_values": [0.0, 9.0, 0.0, 1.0, False],
                "inputs": [
                    {"name": "sample", "type": "DATAFLUX_SAMPLE", "link": 197},
                    {"name": "in_max", "type": "FLOAT", "widget": {"name": "in_max"}, "link": 198},
                ],
                "outputs": [{"name": "sample", "type": "DATAFLUX_SAMPLE", "links": [194]}],
            },
            {
                "id": 22,
                "type": "rescale",
                "widgets_values": [0.0, 7.0, 0.0, 1.0, False],
                "inputs": [{"name": "sample", "type": "DATAFLUX_SAMPLE", "link": 194}],
                "outputs": [{"name": "sample", "type": "DATAFLUX_SAMPLE", "links": [199]}],
            },
        ],
        "links": [
            {
                "id": 194,
                "origin_id": 21,
                "origin_slot": 0,
                "target_id": 22,
                "target_slot": 0,
                "type": "DATAFLUX_SAMPLE",
            },
            {
                "id": 197,
                "origin_id": -10,
                "origin_slot": 0,
                "target_id": 21,
                "target_slot": 0,
                "type": "DATAFLUX_SAMPLE",
            },
            {"id": 198, "origin_id": -10, "origin_slot": 1, "target_id": 21, "target_slot": 1, "type": "FLOAT"},
            {
                "id": 199,
                "origin_id": 22,
                "origin_slot": 0,
                "target_id": -20,
                "target_slot": 0,
                "type": "DATAFLUX_SAMPLE",
            },
        ],
    }


def _two_instance_workflow() -> Dict[str, Any]:
    """src → instance#1 → Preview, src → instance#2, Primitive → instance#1's k input."""
    return {
        "nodes": [
            {
                "id": 1,
                "type": "src",
                "widgets_values": ["mnist", "", "image", "label", "", ""],
                "inputs": [],
                "outputs": [{"name": "source", "type": "DATAFLUX_SOURCE", "links": [1, 3]}],
            },
            {
                "id": 2,
                "type": "sg-chain",
                "widgets_values": [],
                "inputs": [
                    {"name": "sample", "type": "DATAFLUX_SAMPLE", "link": 1},
                    {"name": "k", "type": "FLOAT", "widget": {"name": "k"}, "link": 4},
                ],
                "outputs": [{"name": "sample", "type": "DATAFLUX_SAMPLE", "links": [2]}],
            },
            {
                "id": 3,
                "type": "sg-chain",
                "widgets_values": [],
                "inputs": [{"name": "sample", "type": "DATAFLUX_SAMPLE", "link": 3}],
                "outputs": [{"name": "sample", "type": "DATAFLUX_SAMPLE", "links": []}],
            },
            {
                "id": 4,
                "type": "PrimitiveNode",
                "widgets_values": [5.0],
                "inputs": [],
                "outputs": [{"name": "FLOAT", "type": "FLOAT", "links": [4]}],
            },
            {
                "id": 5,
                "type": "PreviewImage",
                "widgets_values": [],
                "inputs": [{"name": "images", "type": "IMAGE", "link": 2}],
                "outputs": [],
            },
        ],
        "links": [
            [1, 1, 0, 2, 0, "DATAFLUX_SAMPLE"],
            [2, 2, 0, 5, 0, "IMAGE"],
            [3, 1, 0, 3, 0, "DATAFLUX_SAMPLE"],
            [4, 4, 0, 2, 1, "FLOAT"],
        ],
        "definitions": {"subgraphs": [_chain_subgraph_def("sg-chain")]},
    }


def test_inline_subgraphs_dict_links_and_boundary_restitch() -> None:
    # The real save format stores a definition's inner links as DICTS — positional indexing
    # raised `KeyError: 1` and aborted the whole export ("Export failed: 1"). The inliner must
    # parse them, re-stitch boundary wires, and renumber link ids per instance (3× the same
    # definition reuses the same inner ids).
    from fluxstudio.graphio import _inline_subgraphs

    flat = _inline_subgraphs(_two_instance_workflow())

    assert all(n["type"] != "sg-chain" for n in flat["nodes"])
    rescales = [n["id"] for n in flat["nodes"] if n["type"] == "rescale"]
    assert len(rescales) == 4  # two copies per instance
    link_ids = [link[0] for link in flat["links"]]
    assert len(link_ids) == len(set(link_ids))  # no collisions across instance copies

    edges = {(link[1], link[2], link[3], link[4]) for link in flat["links"]}
    by_id = {n["id"]: n for n in flat["nodes"]}
    heads = [nid for nid in rescales if (1, 0, nid, 0) in edges]  # fed by the source
    assert len(heads) == 2  # one head per instance
    for head in heads:
        tails = [t for (o, _os, t, ts) in edges if o == head and ts == 0 and t in rescales]
        assert len(tails) == 1  # inner chain stays within its own instance copy
        # node input/output link refs were rewritten to the new ids
        head_in_link = by_id[head]["inputs"][0]["link"]
        assert any(link[0] == head_in_link and link[1] == 1 for link in flat["links"])
    # instance#1's k input re-stitched onto the inner head's in_max WIDGET input
    assert any((4, 0, head, 1) in edges for head in heads)
    # instance#1's output re-originates at its tail and still feeds the Preview node
    assert any((o, 0, 5, 0) in edges and o in rescales for (o, _os, t, _ts) in edges if t == 5)


def test_export_inlines_subgraph_instances_end_to_end() -> None:
    # The miniature of the real `impairments.json` shape: subgraph instances + a Primitive
    # feeding a widget input + native viewers. Export must succeed, fold each instance's
    # inner chain into a Flux pipeline (source attached), and INLINE the Primitive's value
    # into the wired widget (the legacy PrimitiveNode folds like the new-style primitives).
    yaml_text = workflow_to_yaml(_two_instance_workflow(), _canvas_mappings())
    graph = confluid.resolve(yaml_text)
    assert yaml_text.count("!ref 'hugging_face_source'") == 2
    for key in ("pipeline", "pipeline_2"):
        flux = graph[key]
        assert "Flux" in str(flux.target)
        assert "HuggingFaceSource" in str(getattr(flux.kwargs.get("source"), "target", ""))
        assert len(flux.kwargs["ops"]) == 2  # the definition's inner 2-op chain
    assert "node 4 (PrimitiveNode): native ComfyUI node" in yaml_text  # still not its own entry
    assert "in_max: 5.0" in yaml_text  # the Primitive's widget value, inlined
    assert "<- node 4 (PrimitiveNode)" not in yaml_text  # no dropped-wire note anymore


def test_inline_subgraphs_nested_definitions_flatten() -> None:
    from fluxstudio.graphio import _inline_subgraphs

    inner_def = {
        "id": "sg-inner",
        "inputNode": {"id": -10},
        "outputNode": {"id": -20},
        "nodes": [
            {
                "id": 31,
                "type": "rescale",
                "widgets_values": [0.0, 9.0, 0.0, 1.0, False],
                "inputs": [{"name": "sample", "type": "DATAFLUX_SAMPLE", "link": 100}],
                "outputs": [{"name": "sample", "type": "DATAFLUX_SAMPLE", "links": [101]}],
            }
        ],
        "links": [
            {
                "id": 100,
                "origin_id": -10,
                "origin_slot": 0,
                "target_id": 31,
                "target_slot": 0,
                "type": "DATAFLUX_SAMPLE",
            },
            {
                "id": 101,
                "origin_id": 31,
                "origin_slot": 0,
                "target_id": -20,
                "target_slot": 0,
                "type": "DATAFLUX_SAMPLE",
            },
        ],
    }
    outer_def = {
        "id": "sg-outer",
        "inputNode": {"id": -10},
        "outputNode": {"id": -20},
        "nodes": [
            {
                "id": 41,
                "type": "sg-inner",
                "widgets_values": [],
                "inputs": [{"name": "sample", "type": "DATAFLUX_SAMPLE", "link": 110}],
                "outputs": [{"name": "sample", "type": "DATAFLUX_SAMPLE", "links": [111]}],
            }
        ],
        "links": [
            {
                "id": 110,
                "origin_id": -10,
                "origin_slot": 0,
                "target_id": 41,
                "target_slot": 0,
                "type": "DATAFLUX_SAMPLE",
            },
            {
                "id": 111,
                "origin_id": 41,
                "origin_slot": 0,
                "target_id": -20,
                "target_slot": 0,
                "type": "DATAFLUX_SAMPLE",
            },
        ],
    }
    workflow = {
        "nodes": [
            {
                "id": 1,
                "type": "src",
                "widgets_values": ["mnist", "", "image", "label", "", ""],
                "inputs": [],
                "outputs": [{"name": "source", "type": "DATAFLUX_SOURCE", "links": [1]}],
            },
            {
                "id": 2,
                "type": "sg-outer",
                "widgets_values": [],
                "inputs": [{"name": "sample", "type": "DATAFLUX_SAMPLE", "link": 1}],
                "outputs": [{"name": "sample", "type": "DATAFLUX_SAMPLE", "links": []}],
            },
        ],
        "links": [[1, 1, 0, 2, 0, "DATAFLUX_SAMPLE"]],
        "definitions": {"subgraphs": [inner_def, outer_def]},
    }
    flat = _inline_subgraphs(workflow)
    types = [n["type"] for n in flat["nodes"]]
    assert "sg-outer" not in types and "sg-inner" not in types
    assert types.count("rescale") == 1
    rescale = next(n for n in flat["nodes"] if n["type"] == "rescale")
    assert any(link[1] == 1 and link[3] == rescale["id"] for link in flat["links"])  # src still feeds it


def test_inline_subgraphs_self_referential_definition_terminates() -> None:
    # A definition containing an instance of itself can never flatten — the depth cap must
    # stop the loop (warning) instead of hanging.
    from fluxstudio.graphio import _inline_subgraphs

    cyclic = {
        "id": "sg-cycle",
        "inputNode": {"id": -10},
        "outputNode": {"id": -20},
        "nodes": [{"id": 51, "type": "sg-cycle", "widgets_values": [], "inputs": [], "outputs": []}],
        "links": [],
    }
    workflow = {
        "nodes": [{"id": 1, "type": "sg-cycle", "widgets_values": [], "inputs": [], "outputs": []}],
        "links": [],
        "definitions": {"subgraphs": [cyclic]},
    }
    flat = _inline_subgraphs(workflow)  # must return, not hang
    assert flat["definitions"] == {"subgraphs": []}


def test_export_all_taidal_graph_has_no_comment_block() -> None:
    # When every node is exportable the output is the plain document — no comment header
    # (the clean round-trip stays byte-identical to the pre-skip behaviour).
    nm = _engine_mappings()
    doc = {
        "pipeline": Class(Flux, source=Reference("raw"), ops=[Instance(RescaleOp, in_min=0.0, in_max=2.0)]),
        "raw": Instance(HuggingFaceSource, path="mnist"),
    }
    yaml_text = workflow_to_yaml(yaml_to_workflow(doc, nm), nm)
    assert not yaml_text.startswith("#")


# --- end-to-end on the real MNIST config -----------------------------------------------


@pytest.mark.skipif(not _MNIST_CFG.exists(), reason="sonair MNIST example not checked out")
def test_mnist_config_round_trip() -> None:
    import fluxstudio

    nm = fluxstudio.NODE_CLASS_MAPPINGS
    wf = yaml_to_workflow(str(_MNIST_CFG), nm)
    # trainer + model + 2 sources + logger
    type_tails = {n["type"].split("_")[-1] for n in wf["nodes"]}
    assert "LightningClassificationTrainer" in type_tails
    assert "TimmModel" in type_tails
    assert sum(t == "HuggingFaceSource" for t in (n["type"].split("_")[-1] for n in wf["nodes"])) == 2

    # the model is wired into the trainer via the object node's LAZY output (slot 1), since
    # the MNIST config declares the model `!lazy:`.
    model_id = next(n["id"] for n in wf["nodes"] if n["type"].endswith("TimmModel"))
    model_links = [link for link in wf["links"] if link[1] == model_id]
    assert model_links and model_links[0][2] == 1

    yaml_text = workflow_to_yaml(wf, nm)
    graph = confluid.resolve(yaml_text)
    assert "trainer" in graph  # runnable root named by its role
    # the model re-exports as !lazy: (deferred), the sources/logger as !class:
    assert isinstance(graph["model"], LazyClass)
    assert isinstance(graph["train_set"], (Class, Instance)) and not isinstance(graph["train_set"], LazyClass)


def _engine_doc() -> Dict[str, Any]:
    return {
        "pipeline": Class(Flux, source=Reference("raw"), ops=[Instance(RescaleOp, in_min=0.0, in_max=1.0)]),
        "raw": Instance(HuggingFaceSource, path="mnist"),
    }


def test_import_as_group_wraps_nodes() -> None:
    nm = _engine_mappings()
    wf = yaml_to_workflow(_engine_doc(), nm, as_="group", name="my_pipe")
    assert len(wf["groups"]) == 1
    grp = wf["groups"][0]
    assert grp["title"] == "my_pipe" and len(grp["bounding"]) == 4
    # the box encloses every node
    bx, by, bw, bh = grp["bounding"]
    for n in wf["nodes"]:
        assert bx <= n["pos"][0] and n["pos"][0] + n["size"][0] <= bx + bw


def test_import_as_subgraph_builds_definition() -> None:
    nm = _engine_mappings()
    wf = yaml_to_workflow(_engine_doc(), nm, as_="subgraph", name="reusable")
    sgs = wf["definitions"]["subgraphs"]
    assert len(sgs) == 1
    sg = sgs[0]
    assert sg["name"] == "reusable"
    # internal nodes are the flat pipeline; one instance node references the subgraph by id
    assert len(sg["nodes"]) == 3
    assert wf["nodes"][0]["type"] == sg["id"]
    # the pipeline root (the Flux engine, nothing consumes it) is the single output
    assert len(sg["outputs"]) == 1


def test_subgraph_round_trips_to_flat_yaml() -> None:
    nm = _engine_mappings()
    wf = yaml_to_workflow(_engine_doc(), nm, as_="subgraph", name="reusable")
    yaml_text = workflow_to_yaml(wf, nm)  # inlines the subgraph
    graph = confluid.resolve(yaml_text)
    flux_markers = [v for v in graph.values() if isinstance(v, (Class, Instance)) and "Flux" in str(v.target)]
    assert len(flux_markers) == 1
    assert isinstance(flux_markers[0].kwargs.get("ops"), list)


def test_subgraph_free_ref_becomes_input_socket() -> None:
    """An unresolved !ref: (no top-level definition) becomes a named subgraph input boundary."""
    nm = _engine_mappings()
    # `raw` is NOT defined → a free input named `raw`
    doc = {"pipeline": Class(Flux, source=Reference("raw"), ops=[Instance(RescaleOp, in_min=0.0, in_max=1.0)])}
    wf = yaml_to_workflow(doc, nm, as_="subgraph", name="component")
    sg = wf["definitions"]["subgraphs"][0]
    assert [i["name"] for i in sg["inputs"]] == ["raw"]
    assert sg["inputs"][0]["type"] == "DATAFLUX_SOURCE"


@pytest.mark.skipif(not _MNIST_CFG.exists(), reason="sonair MNIST example not checked out")
def test_mnist_instantiate_path_matches_marker_path() -> None:
    """The instantiate=True (materialize solidify=False) walk yields the same node set."""
    import fluxstudio

    nm = fluxstudio.NODE_CLASS_MAPPINGS
    markers = yaml_to_workflow(str(_MNIST_CFG), nm, instantiate=False)
    live = yaml_to_workflow(str(_MNIST_CFG), nm, instantiate=True)
    assert {n["type"] for n in markers["nodes"]} == {n["type"] for n in live["nodes"]}


# --- value-node dotted refs + primitive inlining ----------------------------------------


def _value_mappings() -> Dict[str, Any]:
    """Mappings with a source, two ops, and a constant value node (test_export fixtures)."""
    return {
        "src": create_comfy_node(HuggingFaceSource, "src"),
        "chain": create_comfy_node(ChainLikeOp, "chain"),
        "threshold": create_comfy_node(ThresholdLikeOp, "threshold"),
        "txcfg": create_comfy_node(ConstTxConfig, "txcfg"),
    }


def test_value_node_wire_exports_dotted_ref_in_pipeline() -> None:
    # The chained-op path: the consumer folds into a `pipeline: !class:Flux` entry, and its
    # wired container params reference the value node's @output by DOTTED ref (a plain
    # !ref would inject the config INSTANCE where the tuple/dict belongs).
    nm = _value_mappings()
    prompt = {
        "1": {"class_type": "src", "inputs": {"path": "mnist"}},
        "163": {"class_type": "txcfg", "inputs": {"quantize": False}},
        "35": {
            "class_type": "chain",
            "inputs": {"sample": ["1", 0], "tx_disabled": ["163", 0], "overrides": ["163", 1]},
        },
    }
    yaml_text = workflow_to_yaml(prompt, nm)
    # Named by its CLASS (a value node's entry is the config object, not the param it feeds).
    assert "const_tx_config:" in yaml_text
    assert "!ref 'const_tx_config.disabled'" in yaml_text
    assert "!ref 'const_tx_config.probability_overrides'" in yaml_text


def test_value_node_wire_exports_dotted_ref_standalone() -> None:
    # The standalone path (an op-value with no sample wiring) takes the same dotted ref.
    nm = _value_mappings()
    prompt = {
        "163": {"class_type": "txcfg", "inputs": {"quantize": False}},
        "35": {"class_type": "chain", "inputs": {"tx_disabled": ["163", 0]}},
    }
    yaml_text = workflow_to_yaml(prompt, nm)
    assert "!ref 'const_tx_config.disabled'" in yaml_text


def test_primitive_wire_inlines_literal_prompt_form() -> None:
    nm = _value_mappings()
    prompt = {
        "1": {"class_type": "src", "inputs": {"path": "mnist"}},
        "100": {"class_type": "PrimitiveFloat", "inputs": {"value": 3.5}},
        "2": {"class_type": "threshold", "inputs": {"sample": ["1", 0], "gain": ["100", 0]}},
    }
    yaml_text = workflow_to_yaml(prompt, nm)
    assert "gain: 3.5" in yaml_text
    assert "dropped" not in yaml_text  # the wire folded — no dropped-wire comment


def test_primitive_wire_inlines_literal_ui_form() -> None:
    # UI workflow.json: a native primitive isn't in the Taidal mappings, but its single
    # widget is captured by the normalizer so the wire still inlines.
    nm = _value_mappings()
    wf = {
        "nodes": [
            {"id": 100, "type": "PrimitiveFloat", "widgets_values": [3.5], "inputs": [], "outputs": []},
            {
                "id": 2,
                "type": "threshold",
                "widgets_values": [],
                "inputs": [{"name": "gain", "type": "FLOAT", "link": 7}],
                "outputs": [],
            },
        ],
        "links": [[7, 100, 0, 2, 0, "FLOAT"]],
    }
    yaml_text = workflow_to_yaml(wf, nm)
    assert "gain: 3.5" in yaml_text


def test_legacy_primitive_node_wire_inlines_literal_ui_form() -> None:
    # The legacy VIRTUAL PrimitiveNode only exists in saved UI workflows (the frontend
    # folds it out of API prompts); its first widget is the value, the rest is
    # control_after_generate plumbing.
    nm = _value_mappings()
    wf = {
        "nodes": [
            {
                "id": 108,
                "type": "PrimitiveNode",
                "widgets_values": ["time_y_bottom", "fixed", ""],
                "inputs": [],
                "outputs": [],
            },
            {
                "id": 2,
                "type": "threshold",
                "widgets_values": [],
                "inputs": [{"name": "low_level", "type": "STRING", "link": 7}],
                "outputs": [],
            },
        ],
        "links": [[7, 108, 0, 2, 0, "STRING"]],
    }
    yaml_text = workflow_to_yaml(wf, nm)
    assert "low_level: time_y_bottom" in yaml_text


def test_linked_converted_widget_placeholder_is_ignored() -> None:
    # A widget converted to an input AND wired keeps a stale placeholder in
    # widgets_values (the real impairments.json showed ThresholdOp.low_level holding
    # '>'); the wire is authoritative — the leftover value must not export as config.
    nm = _value_mappings()
    wf = {
        "nodes": [
            {
                "id": 2,
                "type": "threshold",
                # positional order: low_level, gain — low_level's entry is stale garbage
                "widgets_values": [">", 2.0],
                "inputs": [
                    {"name": "low_level", "type": "STRING", "link": 9, "widget": {"name": "low_level"}},
                    {"name": "gain", "type": "FLOAT", "link": None, "widget": {"name": "gain"}},
                ],
                "outputs": [],
            },
            {"id": 99, "type": "UnknownMathNode", "widgets_values": [], "inputs": [], "outputs": []},
        ],
        "links": [[9, 99, 0, 2, 0, "STRING"]],
    }
    yaml_text = workflow_to_yaml(wf, nm)
    assert "low_level: '>'" not in yaml_text and "low_level: '>='" not in yaml_text
    assert "gain: 2.0" in yaml_text  # the UNlinked converted widget keeps its value
    assert "dropped" in yaml_text or "UnknownMathNode" in yaml_text  # the wire itself is noted


def test_single_op_wire_exports_ref() -> None:
    # ConfigureOp's wired `target` (one DATAFLUX_OP value) references the op's standalone
    # entry by name — the op-value producer is not folded into the pipeline.
    from dataflux.ops.configure import ConfigureOp

    nm = {
        "src": create_comfy_node(HuggingFaceSource, "src"),
        "cfg": create_comfy_node(ConfigureOp, "cfg"),
        "threshold": create_comfy_node(ThresholdLikeOp, "threshold"),
    }
    prompt = {
        "1": {"class_type": "src", "inputs": {"path": "mnist"}},
        "8": {"class_type": "threshold", "inputs": {"gain": 2.0}},
        "10": {"class_type": "cfg", "inputs": {"sample": ["1", 0], "target": ["8", 1], "param": "low_level"}},
    }
    yaml_text = workflow_to_yaml(prompt, nm)
    assert "!ref 'threshold_like_op'" in yaml_text  # the op-value entry, referenced by name
    assert "threshold_like_op:" in yaml_text


def test_import_of_hoisted_constant_evaluates_consumer_and_places_config_node() -> None:
    # confluid.resolve INSTANTIATES a dotted !ref (documented caveat), so on import the
    # consumer widget carries the EVALUATED value and the config lands as a standalone
    # value node carrying its settings — re-wire on canvas to re-establish the live link.
    nm = _value_mappings()
    doc = {
        "const_tx_config": Class(ConstTxConfig, quantize=False),
        "chain_like_op": Class(ChainLikeOp, tx_disabled=Reference("const_tx_config.disabled")),
    }
    wf = yaml_to_workflow(doc, nm)
    types = sorted(n["type"] for n in wf["nodes"])
    assert types == ["chain", "txcfg"]
    chain_node = next(n for n in wf["nodes"] if n["type"] == "chain")
    assert "QuantizeOp" in str(chain_node["widgets_values"])
