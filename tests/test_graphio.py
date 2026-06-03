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
