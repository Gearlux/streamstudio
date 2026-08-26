"""A flow document imports as a PIPELINE BLUEPRINT: a subgraph with a declared boundary and an op.

A flow document already states its own boundary — the head step's ``record`` input is fed by nothing
(that is where a source connects), and ``outputs:`` names the step whose record leaves. Until now
``--as subgraph`` ignored that and silently produced a FLAT graph (``graphio``: *"subgraph degrades to
graph — a flow-doc has no natural !ref-boundary inputs to promote"*), which was true only of the
``!ref:`` convention, not of the flow grammar's own structure.

The blueprint this produces is one rectangle carrying:

* a declared ``record`` INPUT, rendered even though nothing is connected to it (the same defect the
  ``Stream.source`` STRING widget had — an unconnected boundary that does not exist cannot be aimed at);
* a ``record`` OUTPUT from the ``outputs:`` step;
* an ``op`` OUTPUT (``RECORDSTREAM_OP``) from an *Extract as Op* terminator the importer places and
  promotes, so the rectangle drops straight into a ``Stream``'s ops without the user knowing that node
  exists;
* one extra ``record`` output per dead-end TAP — a record-chain step whose output feeds nothing.
  Taps ride ALONGSIDE the spine: a tap is a descendant of a spine step, never an ancestor of the
  terminator, so it stays outside the compiled cone (pinned below). That is what lets one shape be
  both a pipeline and a viewer.
"""

from typing import Any, Dict, List, Optional

import pytest

import streamstudio  # noqa: F401  (populates NODE_CLASS_MAPPINGS)
from streamstudio import NODE_CLASS_MAPPINGS
from streamstudio.bridge import OP_TYPE, RECORD_TYPE
from streamstudio.export import UnsupportedTopologyError
from streamstudio.graphio import yaml_to_workflow
from streamstudio.nodes import compile_subgraph_ops

CONTRACT = "recordstream.ops.contract.RecordContract"
TERMINATOR = "Taidal_SubgraphOp"
STREAM = "Taidal_recordstream_engine_Stream"
TOTENSOR = "Taidal_recordstream_op_torch_ToTensor"
CONVERT = "Taidal_recordstream_op_image_ConvertToImage"
EXTRACT = "Taidal_ExtractRecord"

# annotaide's seeded interface: one contract node per required record entry, chained.
SEED = f"""
flow:
  image:
    op: !class:{CONTRACT}
      name: image
      fields: {{image: Image}}
  class:
    from: image
    op: !class:{CONTRACT}
      name: class
      fields: {{class: Label}}
outputs: class
"""

# A document carrying a tap is BRANCHY (two steps `from:` the same one), which the flow importer
# refuses today — the boundary this file pins rather than works around.
SEED_WITH_TAP = f"""
flow:
  image:
    op: !class:{CONTRACT}
      name: image
      fields: {{image: Image}}
  peek:
    from: image
    op: !class:recordstream.ops.debug.PrintRecordOp {{}}
  class:
    from: image
    op: !class:{CONTRACT}
      name: class
      fields: {{class: Label}}
outputs: class
"""


def _subgraph(workflow: Dict[str, Any]) -> Dict[str, Any]:
    defs = (workflow.get("definitions") or {}).get("subgraphs") or []
    assert len(defs) == 1, f"expected exactly one subgraph definition, got {len(defs)}"
    sg: Dict[str, Any] = defs[0]
    return sg


def _slots(defs: List[Dict[str, Any]]) -> List[Any]:
    return [(d.get("name"), d.get("type")) for d in defs or []]


def _wrap(doc: str = SEED) -> Dict[str, Any]:
    return yaml_to_workflow(doc, NODE_CLASS_MAPPINGS, as_="subgraph", name="pipeline")


def _node_of(sg: Dict[str, Any], class_type: str) -> Optional[Dict[str, Any]]:
    return next((n for n in sg.get("nodes", []) if n.get("type") == class_type), None)


# --------------------------------------------------------------------------- #
# It wraps at all
# --------------------------------------------------------------------------- #


def test_a_flow_document_wraps_instead_of_degrading_to_a_flat_graph() -> None:
    """The regression: --as subgraph used to be accepted and ignored on a flow document."""
    workflow = _wrap()
    sg = _subgraph(workflow)
    assert sg["name"] == "pipeline"
    # A subgraph INSTANCE is typed by the definition's uuid (that is how ComfyUI references it).
    assert [n["type"] for n in workflow["nodes"]] == [sg["id"]], "the parent holds one instance node"


def test_the_steps_live_INSIDE_the_subgraph_not_on_the_parent_canvas() -> None:
    sg = _subgraph(_wrap())
    contracts = [n for n in sg["nodes"] if "RecordContract" in n["type"]]
    assert len(contracts) == 2


# --------------------------------------------------------------------------- #
# The declared boundary
# --------------------------------------------------------------------------- #


def test_the_record_input_is_declared_even_though_nothing_is_connected_to_it() -> None:
    """The whole point: an unconnected boundary that does not render cannot be aimed at."""
    sg = _subgraph(_wrap())
    assert _slots(sg["inputs"]) == [("record", RECORD_TYPE)]


def test_the_declared_input_reaches_the_head_step() -> None:
    """The socket is wired from the subgraph's inputNode (-10) to the step nothing else feeds."""
    sg = _subgraph(_wrap())
    from_boundary = [lk for lk in sg["links"] if _origin(lk) == -10]
    assert len(from_boundary) == 1
    head_id = _target(from_boundary[0])
    # The head is the step nothing INTERNAL feeds — everything else is fed by the step before it.
    fed_by_internal = {_target(lk) for lk in sg["links"] if _origin(lk) not in (-10, -20)}
    assert head_id not in fed_by_internal
    head = next(n for n in sg["nodes"] if n["id"] == head_id)
    assert "RecordContract" in head["type"]


def test_the_record_output_comes_from_the_outputs_step() -> None:
    sg = _subgraph(_wrap())
    assert ("record", RECORD_TYPE) in _slots(sg["outputs"])


def test_the_instance_shows_the_whole_boundary_unconnected() -> None:
    workflow = _wrap()
    instance = workflow["nodes"][0]
    assert [(i["name"], i["type"]) for i in instance["inputs"]] == [("record", RECORD_TYPE)]
    assert all(i["link"] is None for i in instance["inputs"])
    names = [o["name"] for o in instance["outputs"]]
    assert "record" in names and "op" in names
    assert all(o["links"] == [] for o in instance["outputs"])


# --------------------------------------------------------------------------- #
# The op connector
# --------------------------------------------------------------------------- #


def test_the_importer_places_the_terminator_so_the_user_never_has_to_find_it() -> None:
    sg = _subgraph(_wrap())
    assert _node_of(sg, TERMINATOR) is not None, "no 'Extract as Op' node was placed"


def test_the_op_output_is_type_equal_to_a_streams_op_slot() -> None:
    """ComfyUI connects on socket-type EQUALITY — this is what makes the rectangle droppable."""
    sg = _subgraph(_wrap())
    produced = dict(_slots(sg["outputs"]))["op"]
    consumed = NODE_CLASS_MAPPINGS[STREAM].INPUT_TYPES()["optional"]["op_0"][0]
    assert produced == consumed == OP_TYPE


def test_the_terminator_reads_the_final_step_so_the_compiled_pipeline_is_the_whole_chain() -> None:
    sg = _subgraph(_wrap())
    terminator = _node_of(sg, TERMINATOR)
    assert terminator is not None
    feeding = [lk for lk in sg["links"] if _target(lk) == terminator["id"]]
    assert len(feeding) == 1, "the terminator must be fed by exactly one record wire"
    source_id = _origin(feeding[0])
    # the step feeding it is the one `outputs:` named — the deepest sink of the chain
    final = next(n for n in sg["nodes"] if n["id"] == source_id)
    assert "RecordContract" in final["type"]


# --------------------------------------------------------------------------- #
# Taps ride alongside — one shape, pipeline AND viewer
# --------------------------------------------------------------------------- #


def test_a_tap_CANNOT_arrive_from_the_document_yet_and_the_refusal_names_the_steps() -> None:
    """A tap in the DOCUMENT makes it branchy, which the flow importer does not replay yet.

    Draw taps INSIDE the rectangle instead (a native promote makes each one a boundary output);
    the compiled op list is unaffected either way, which the next test pins. Importing a branchy
    document is tracked separately in TASKS.md — the point here is that it REFUSES, located,
    rather than silently dropping the tap.
    """
    with pytest.raises(UnsupportedTopologyError) as exc:
        _wrap(SEED_WITH_TAP)
    assert "peek" in str(exc.value) and "class" in str(exc.value)


def test_a_tap_does_not_enter_the_compiled_pipeline() -> None:
    """A tap is a DESCENDANT of a spine step, never an ancestor of the terminator."""
    prompt = {
        "10": {"class_type": TOTENSOR, "inputs": {"normalize": True, "mode": "", "field": "image", "output": ""}},
        "11": {"class_type": CONVERT, "inputs": {"record": ["10", 0], "field": "image", "output": ""}},
        "12": {
            "class_type": EXTRACT,
            "inputs": {"record": ["10", 0], "field": "image", "colormap": "viridis", "max_size": 512},
        },
        "99": {"class_type": TERMINATOR, "inputs": {"record": ["11", 0]}},
    }
    assert [type(o).__name__ for o in compile_subgraph_ops(prompt, "99")] == ["ToTensor", "ConvertToImage"]


# --------------------------------------------------------------------------- #
# compile_subgraph_ops — untested until now (the Stage 7 deletion took its pins)
# --------------------------------------------------------------------------- #


def test_a_chain_compiles_to_its_ops_in_order() -> None:
    prompt = {
        "10": {"class_type": TOTENSOR, "inputs": {"normalize": True, "mode": "", "field": "image", "output": ""}},
        "11": {"class_type": CONVERT, "inputs": {"record": ["10", 0], "field": "image", "output": ""}},
        "99": {"class_type": TERMINATOR, "inputs": {"record": ["11", 0]}},
    }
    assert [type(o).__name__ for o in compile_subgraph_ops(prompt, "99")] == ["ToTensor", "ConvertToImage"]


def test_an_unwired_terminator_compiles_to_nothing() -> None:
    """Pipeline([]) is the identity — legitimate for a terminator used as a graph, not an op."""
    assert compile_subgraph_ops({"99": {"class_type": TERMINATOR, "inputs": {}}}, "99") == []


def test_a_missing_prompt_compiles_to_nothing() -> None:
    assert compile_subgraph_ops(None, "99") == []
    assert compile_subgraph_ops({"99": {"class_type": TERMINATOR, "inputs": {}}}, None) == []


def test_a_branchy_cone_is_refused_and_NAMES_the_step() -> None:
    """A Pipeline is a straight op LIST; a fork that re-merges has no flat spelling."""
    prompt = {
        "10": {"class_type": TOTENSOR, "inputs": {"normalize": True, "mode": "", "field": "image", "output": ""}},
        "11": {"class_type": CONVERT, "inputs": {"record": ["10", 0], "field": "image", "output": ""}},
        "13": {
            "class_type": TOTENSOR,
            "inputs": {"record": ["10", 0], "normalize": False, "mode": "", "field": "x", "output": ""},
        },
        "14": {"class_type": "Taidal_MixRecords", "inputs": {"input_record": ["11", 0], "target_record": ["13", 0]}},
        "99": {"class_type": TERMINATOR, "inputs": {"record": ["14", 0]}},
    }
    with pytest.raises(UnsupportedTopologyError) as exc:
        compile_subgraph_ops(prompt, "99")
    assert "node_13" in str(exc.value), "the refusal must name the step that forks"


# --------------------------------------------------------------------------- #
# The other modes are untouched
# --------------------------------------------------------------------------- #


def test_graph_mode_still_lays_the_steps_out_flat() -> None:
    workflow = yaml_to_workflow(SEED, NODE_CLASS_MAPPINGS, as_="graph", name="pipeline")
    assert "definitions" not in workflow
    assert len([n for n in workflow["nodes"] if "RecordContract" in n["type"]]) == 2


def test_group_mode_still_wraps_in_a_visual_box() -> None:
    workflow = yaml_to_workflow(SEED, NODE_CLASS_MAPPINGS, as_="group", name="pipeline")
    assert workflow.get("groups"), "group mode must still emit a groups[] box"
    assert "definitions" not in workflow


# --------------------------------------------------------------------------- #


def _origin(link: Any) -> Any:
    return link["origin_id"] if isinstance(link, dict) else link[1]


def _target(link: Any) -> Any:
    return link["target_id"] if isinstance(link, dict) else link[3]
