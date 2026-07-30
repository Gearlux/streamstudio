"""The canvas compiles to a ``flow:`` GRAPH, not to a linearized op list (2026-07-30).

The canvas IS a graph, so ``export.compile_flow_documents`` emits named steps whose
``from:`` / ``merge_from:`` / ``bind:`` keys ARE the wires the user drew — no per-record
context cells, and no lift back afterwards. These tests drive the compiler in the PROMPT /
API form it consumes in production and, for the branchy cases, EXECUTE the emitted document
through recordstream so the wiring is proven, not just shaped.
"""

from typing import Any, Dict, List

import numpy as np
import pytest
from recordstream import FlowGraph, Image, Record
from recordstream.flow import parse_flow

import streamstudio  # noqa: F401  (populates NODE_CLASS_MAPPINGS)
from streamstudio import NODE_CLASS_MAPPINGS
from streamstudio.export import UnsupportedTopologyError, compile_flow_documents, export_flow_yamls

CONVERT = "Taidal_recordstream_op_image_ConvertToImage"
THRESHOLD = "Taidal_recordstream_op_numpy_Threshold"
RENAME = "Taidal_recordstream_op_structure_RenameField"
SELECT = "Taidal_recordstream_op_structure_SelectFields"
MIX = "Taidal_MixRecords"


def _op(class_type: str, record: Any = None, **widgets: Any) -> Dict[str, Any]:
    """One prompt node: literal widgets plus an optional ``record`` wire ``[src_id, slot]``."""
    inputs: Dict[str, Any] = dict(widgets)
    if record is not None:
        inputs["record"] = record
    return {"class_type": class_type, "inputs": inputs}


def _flow_of(prompt: Dict[str, Any], **kwargs: Any) -> Any:
    docs = compile_flow_documents(prompt, NODE_CLASS_MAPPINGS, **kwargs)
    assert len(docs) == 1, f"expected ONE component, got {len(docs)}"
    return docs[0]


def _seed(value: float = 1.0) -> Record:
    return {"image": Image(np.full((4, 4, 3), value, dtype=np.float32))}


class TestLinearChain:
    def test_a_straight_chain_is_a_straight_flow(self) -> None:
        prompt = {
            "1": _op(RENAME, src="image", dst="a"),
            "2": _op(RENAME, record=["1", 0], src="a", dst="b"),
        }
        flow_map, outputs = _flow_of(prompt)
        assert list(flow_map) == ["source", "node_1", "node_2"]
        assert flow_map["source"] == {}  # the identity step naming the source record
        assert flow_map["node_1"]["from"] == "source"
        assert flow_map["node_2"]["from"] == "node_1"
        assert outputs == "node_2"

    def test_no_context_ops_are_emitted(self) -> None:
        # The whole point: wiring is step GRAMMAR now, not Save/Use/MergeFields instances.
        prompt = {
            "1": _op(RENAME, src="image", dst="a"),
            "2": _op(RENAME, record=["1", 0], src="a", dst="b"),
        }
        flow_map, _ = _flow_of(prompt)
        rendered = str(flow_map)
        for context_op in ("Save", "Use", "MergeFields", "Drop", "Capture", "Apply"):
            assert f"ops.context.{context_op}" not in rendered


class TestFanOut:
    def test_both_branches_name_the_fork_directly(self) -> None:
        # A fork used to cost a Save + a Use per branch; now each branch just says `from:`.
        prompt = {
            "1": _op(RENAME, src="image", dst="a"),
            "2": _op(RENAME, record=["1", 0], src="a", dst="left"),
            "3": _op(RENAME, record=["1", 0], src="a", dst="right"),
        }
        flow_map, _ = _flow_of(prompt)
        assert flow_map["node_2"]["from"] == "node_1"
        assert flow_map["node_3"]["from"] == "node_1"

    def test_a_branch_restarting_from_the_source_names_the_identity_step(self) -> None:
        # Two heads both read the SOURCE record and rejoin at a Mix — the second head cannot
        # ride the implicit "previous step", so it must name the identity step explicitly.
        prompt = {
            "1": _op(RENAME, src="image", dst="a"),
            "2": _op(RENAME, src="image", dst="b"),
            "3": _op(RENAME, src="image", dst="c"),
            "4": {
                "class_type": MIX,
                "inputs": {"input_record": ["1", 0], "target_record": ["2", 0], "metadata_record": ["3", 0]},
            },
        }
        flow_map, outputs = _flow_of(prompt)
        assert flow_map["node_1"]["from"] == "source"
        assert flow_map["node_2"]["from"] == "source"
        assert flow_map["node_3"]["from"] == "source"
        (out,) = list(FlowGraph(source=[_seed()], flow=dict(flow_map), outputs=outputs))
        assert {"a", "b", "c"} <= set(out)

    def test_the_emitted_document_parses_and_runs(self) -> None:
        prompt = {
            "1": _op(RENAME, src="image", dst="a"),
            "2": _op(RENAME, record=["1", 0], src="a", dst="left"),
            "3": _op(RENAME, record=["1", 0], src="a", dst="right"),
        }
        flow_map, outputs = _flow_of(prompt)
        (out,) = list(FlowGraph(source=[_seed()], flow=dict(flow_map), outputs=outputs))
        # Branch 3 is the deepest sink, so its rename is what the document yields.
        assert "right" in out


class TestFanIn:
    def _mix_prompt(self) -> Dict[str, Any]:
        return {
            "1": _op(RENAME, src="image", dst="base"),
            "2": _op(RENAME, record=["1", 0], src="base", dst="arg1"),
            "3": _op(RENAME, record=["1", 0], src="base", dst="arg2"),
            "4": _op(RENAME, record=["1", 0], src="base", dst="arg3"),
            "5": {
                "class_type": MIX,
                "inputs": {"input_record": ["2", 0], "target_record": ["3", 0], "metadata_record": ["4", 0]},
            },
        }

    def test_mix_becomes_from_plus_merge_from(self) -> None:
        flow_map, outputs = _flow_of(self._mix_prompt())
        step = flow_map["node_5"]
        assert step["from"] == "node_2"
        assert step["merge_from"] == ["node_3", "node_4"]
        assert "op" not in step  # a pure fan-in step carries no op
        assert outputs == "node_5"

    def test_arg_three_still_wins_a_key_collision(self) -> None:
        # merge_from unions in LISTED order, last write wins — arg-3 last reproduces the
        # canvas Mix's {**m1, **m2, **m3} priority the flat form got from `metadata_from`.
        prompt = self._mix_prompt()
        for nid in ("2", "3", "4"):
            prompt[nid]["inputs"]["dst"] = "collide"
        flow_map, outputs = _flow_of(prompt)
        steps, out = parse_flow(dict(flow_map), outputs)
        merge_step = next(s for s in steps if s.name == "node_5")
        assert merge_step.merge_from == ("node_3", "node_4")

    def test_the_merged_document_runs_and_carries_every_branch(self) -> None:
        flow_map, outputs = _flow_of(self._mix_prompt())
        (out,) = list(FlowGraph(source=[_seed()], flow=dict(flow_map), outputs=outputs))
        assert {"arg1", "arg2", "arg3"} <= set(out)


class TestDocument:
    def test_export_emits_a_flow_document_with_outputs(self) -> None:
        prompt = {"1": _op(RENAME, src="image", dst="a")}
        (doc,) = export_flow_yamls(prompt, NODE_CLASS_MAPPINGS)
        assert "flow:" in doc and "outputs: node_1" in doc
        assert "ops.context" not in doc

    def test_an_empty_graph_emits_an_empty_flow(self) -> None:
        (doc,) = export_flow_yamls({}, NODE_CLASS_MAPPINGS)
        assert "flow: {}" in doc

    def test_step_names_survive_subgraph_path_ids(self) -> None:
        # ComfyUI joins instance-path ids with ':' — illegal in a step name, so it becomes '_'.
        prompt = {"378:368:371": _op(RENAME, src="image", dst="a")}
        flow_map, outputs = _flow_of(prompt)
        assert list(flow_map) == ["source", "node_378_368_371"]
        assert outputs == "node_378_368_371"
        parse_flow(dict(flow_map), outputs)  # must not raise on the name

    def test_two_components_export_as_two_documents(self) -> None:
        prompt = {
            "1": _op(RENAME, src="image", dst="a"),
            "2": _op(RENAME, record=["1", 0], src="a", dst="b"),
            "3": _op(RENAME, src="image", dst="c"),
        }
        docs = compile_flow_documents(prompt, NODE_CLASS_MAPPINGS)
        assert len(docs) == 2


class TestMarks:
    def test_an_export_target_restricts_to_its_ancestor_cone(self) -> None:
        prompt = {
            "1": _op(RENAME, src="image", dst="a"),
            "2": _op(RENAME, record=["1", 0], src="a", dst="kept"),
            "3": _op(RENAME, record=["1", 0], src="a", dst="dropped"),
        }
        flow_map, outputs = _flow_of(prompt, target_ids=["2"])
        assert list(flow_map) == ["source", "node_1", "node_2"]
        assert outputs == "node_2"

    def test_an_unknown_target_raises(self) -> None:
        prompt = {"1": _op(RENAME, src="image", dst="a")}
        with pytest.raises(UnsupportedTopologyError, match="not found"):
            compile_flow_documents(prompt, NODE_CLASS_MAPPINGS, target_ids=["99"])

    def test_a_skip_mark_drops_the_node_and_its_downstream(self) -> None:
        prompt = {
            "1": _op(RENAME, src="image", dst="a"),
            "2": _op(RENAME, record=["1", 0], src="a", dst="b"),
        }
        notes: List[List[str]] = []
        flow_map, _ = _flow_of(prompt, skip_ids=["2"], chain_notes=notes)
        assert list(flow_map) == ["source", "node_1"]
        assert any("export-skip" in n for n in notes[0])
