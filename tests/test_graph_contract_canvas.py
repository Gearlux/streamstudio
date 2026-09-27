"""A GraphContract becomes the canvas a workspace opens on — and the export writes back what was wired.

A consuming workspace declares, per graph, what the graph must deliver (``outputs``) and what
every record of a delivered stream carries (``records``). Packaged ``as_="pipeline"`` that
declaration is DRAWN, never hand-drawn: a blue "graph delivers" ROOT whose input sockets are
the outputs, the brown "every record carries" boundary subgraph, a Stream pre-wired into the
root, one group box carrying the graph's name — every title, colour and socket label coming
from the declaration, so the canvas cannot drift from the contract. On export the root writes
``delivered: {slot: what was wired}`` into the contract's own document entry, which is what the
workspace reads back and checks.
"""

import json
from typing import Any, Dict, List, Optional

import confluid
import pytest
from recordstream.ops.contract import GraphContract

import streamstudio
from streamstudio import graphio
from streamstudio.export import UnsupportedTopologyError
from streamstudio.graphio import workflow_to_yaml, yaml_to_workflow

CLASSIFICATION = """\
source: !class:recordstream.ops.contract.GraphContract
  name: classification source
  outputs: {stream: Stream, class_names: "List[str]"}
  records: {input: Image, target: Label}
"""

STREAM = "Taidal_recordstream_engine_Stream"
FILES_SOURCE = "Taidal_recordstream_source_FilesSource"
VALUE = "Taidal_Value"


def _build(document: str = CLASSIFICATION) -> Dict[str, Any]:
    return yaml_to_workflow(document, streamstudio.NODE_CLASS_MAPPINGS, as_="pipeline", name="ignored")


def _definitions(workflow: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(workflow["definitions"]["subgraphs"])


def _root_definition(workflow: Dict[str, Any]) -> Dict[str, Any]:
    roots = [d for d in _definitions(workflow) if graphio.GRAPH_CONTRACT_MARK in (d.get("extra") or {})]
    assert len(roots) == 1, f"expected one root definition, found {len(roots)}"
    return roots[0]


def _instance_of(workflow: Dict[str, Any], definition: Dict[str, Any]) -> Dict[str, Any]:
    nodes: List[Dict[str, Any]] = [n for n in workflow["nodes"] if n["type"] == definition["id"]]
    assert len(nodes) == 1
    return nodes[0]


def _node(workflow: Dict[str, Any], class_type: str) -> Dict[str, Any]:
    nodes: List[Dict[str, Any]] = [n for n in workflow["nodes"] if n["type"] == class_type]
    assert len(nodes) == 1, f"expected one {class_type}, found {len(nodes)}"
    return nodes[0]


def _link_into(workflow: Dict[str, Any], node: Dict[str, Any], input_name: str) -> Optional[List[Any]]:
    slot = next(i for i in node["inputs"] if i["name"] == input_name)
    if slot.get("link") is None:
        return None
    links: List[List[Any]] = workflow["links"]
    return next(link for link in links if link[0] == slot["link"])


class TestTheRootIsDrawnFromTheDeclaration:
    def test_the_root_carries_the_declaration_it_was_drawn_from(self) -> None:
        root = _root_definition(_build())
        assert root["extra"][graphio.GRAPH_CONTRACT_MARK] == {
            "graph": "source",
            "name": "classification source",
            "inputs": {},
            "outputs": {"stream": "Stream", "class_names": "List[str]"},
            "records": {"input": "Image", "target": "Label"},
        }

    def test_one_typed_input_socket_per_output_in_declaration_order(self) -> None:
        root = _root_definition(_build())
        assert [(i["name"], i["type"]) for i in root["inputs"]] == [
            ("stream", "RECORDSTREAM_SOURCE"),
            ("class_names", "STRING"),
        ]

    def test_the_root_is_titled_and_coloured_as_the_delivering_side(self) -> None:
        workflow = _build()
        instance = _instance_of(workflow, _root_definition(workflow))
        assert instance["title"] == "graph delivers — classification source"
        assert (instance["color"], instance["bgcolor"]) == graphio.ROLE_COLORS["delivers"]

    def test_every_socket_is_labelled_name_colon_kind(self) -> None:
        workflow = _build()
        instance = _instance_of(workflow, _root_definition(workflow))
        labels = {i["name"]: i["label"] for i in instance["inputs"]}
        assert labels["stream"] == "stream: Stream"
        assert labels["class_names"].startswith("class_names: List[str]")
        assert "required" in labels["class_names"]  # the one socket left open says what to do

    def test_the_root_has_no_outputs_it_is_the_end_of_the_graph(self) -> None:
        workflow = _build()
        assert _instance_of(workflow, _root_definition(workflow))["outputs"] == []


class TestTheStreamAndTheBoundary:
    def test_the_stream_is_pre_wired_into_the_root_and_its_source_is_left_open(self) -> None:
        workflow = _build()
        root = _instance_of(workflow, _root_definition(workflow))
        stream = _node(workflow, STREAM)
        wire = _link_into(workflow, root, "stream")
        assert wire is not None and wire[1] == stream["id"]
        assert _link_into(workflow, stream, "source") is None
        assert _link_into(workflow, root, "class_names") is None

    def test_the_streams_source_socket_says_it_is_required_here(self) -> None:
        stream = _node(_build(), STREAM)
        source = next(i for i in stream["inputs"] if i["name"] == "source")
        assert source["label"] == "source ← connect your dataset here (required for this graph)"
        assert stream["title"] == "records — Stream"

    def test_the_boundary_is_titled_with_the_record_contract_and_coloured_as_carrying(self) -> None:
        workflow = _build()
        boundary_def = next(
            d for d in _definitions(workflow) if graphio.GRAPH_CONTRACT_MARK not in (d.get("extra") or {})
        )
        boundary = _instance_of(workflow, boundary_def)
        assert boundary["title"] == "every record carries: input: Image, target: Label"
        assert (boundary["color"], boundary["bgcolor"]) == graphio.ROLE_COLORS["carries"]
        assert [(o["name"], o["type"]) for o in boundary_def["outputs"]][:2] == [
            ("input", "IMAGE"),
            ("target", "RECORDSTREAM_ITEM_LABEL"),
        ]

    def test_the_boundarys_op_feeds_the_stream(self) -> None:
        workflow = _build()
        stream = _node(workflow, STREAM)
        wire = _link_into(workflow, stream, "op_0")
        assert wire is not None and wire[5] == "RECORDSTREAM_OP"

    def test_no_class_names_declaration_node_the_root_socket_replaces_it(self) -> None:
        assert not [n for n in _build()["nodes"] if n["type"].endswith("ClassNamesOutput")]

    def test_one_group_box_carries_the_graphs_name(self) -> None:
        workflow = _build()
        assert [g["title"] for g in workflow["groups"]] == ["classification source"]
        x0, y0, w, h = workflow["groups"][0]["bounding"]
        for node in workflow["nodes"]:
            assert x0 <= node["pos"][0] and node["pos"][0] + node["size"][0] <= x0 + w
            assert y0 <= node["pos"][1] and node["pos"][1] + node["size"][1] <= y0 + h


class TestRefusals:
    def test_host_inputs_are_not_drawn_yet_and_say_so(self) -> None:
        document = CLASSIFICATION.replace("  outputs:", '  inputs: {files: "List[str]"}\n  outputs:')
        with pytest.raises(UnsupportedTopologyError, match=r"host inputs .* not drawn yet"):
            _build(document)

    def test_an_unknown_slot_kind_is_refused_by_the_contract_itself(self) -> None:
        document = CLASSIFICATION.replace('"List[str]"', "Widget")
        with pytest.raises(UnsupportedTopologyError, match=r"output 'class_names' declares the type 'Widget'"):
            _build(document)

    def test_a_document_without_records_gets_a_root_and_a_stream_and_no_boundary(self) -> None:
        document = "source: !class:recordstream.ops.contract.GraphContract\n  outputs: {stream: Stream}\n"
        workflow = _build(document)
        assert len(_definitions(workflow)) == 1  # the root only
        assert _node(workflow, STREAM)["inputs"][0]["name"] == "source"


def _wire(workflow: Dict[str, Any], src: Dict[str, Any], src_slot: int, dst: Dict[str, Any], dst_input: str) -> None:
    """Draw one wire on a workflow dict the way the editor would."""
    link_id = max([link[0] for link in workflow["links"]] + [0]) + 1
    slot = next(i for i, inp in enumerate(dst["inputs"]) if inp["name"] == dst_input)
    workflow["links"].append([link_id, src["id"], src_slot, dst["id"], slot, dst["inputs"][slot]["type"]])
    dst["inputs"][slot]["link"] = link_id
    src["outputs"][src_slot].setdefault("links", []).append(link_id)
    workflow["last_link_id"] = link_id


def _add(workflow: Dict[str, Any], class_type: str, widgets: List[Any]) -> Dict[str, Any]:
    cls = streamstudio.NODE_CLASS_MAPPINGS[class_type]
    node_id = max(n["id"] for n in workflow["nodes"]) + 1
    node = graphio._node(node_id, class_type, cls, [0, 0], widgets)
    workflow["nodes"].append(node)
    workflow["last_node_id"] = node_id
    return node


class TestTheExportWritesWhatWasWired:
    def test_the_untouched_seed_exports_the_contract_with_only_the_stream_delivered(self) -> None:
        text = workflow_to_yaml(_build(), streamstudio.NODE_CLASS_MAPPINGS)
        document = confluid.load(text)
        contract = document["source"]
        assert isinstance(contract, GraphContract)
        assert contract.outputs == {"stream": "Stream", "class_names": "List[str]"}
        assert contract.records == {"input": "Image", "target": "Label"}
        assert type(contract.delivered["stream"]).__name__ == "Stream"
        assert contract.missing() == ["class_names"]  # the workspace's refusal, not the exporter's

    def test_a_wired_source_and_a_value_list_land_in_delivered(self) -> None:
        workflow = _build()
        stream = _node(workflow, STREAM)
        root = _instance_of(workflow, _root_definition(workflow))
        source = _add(workflow, FILES_SOURCE, ["", "", "", "", "", ""])
        names = _add(workflow, VALUE, ['["cat", "dog"]'])
        _wire(workflow, source, 0, stream, "source")
        _wire(workflow, names, 0, root, "class_names")

        text = workflow_to_yaml(workflow, streamstudio.NODE_CLASS_MAPPINGS)
        document = confluid.load(text)
        contract = document["source"]
        assert contract.delivered["class_names"] == ["cat", "dog"]
        delivered_stream = contract.delivered["stream"]
        assert type(delivered_stream.source).__name__ == "FilesSource"
        contract.check()  # the workspace's own verification passes on this document
        assert type(contract.stream().ops[-1]).__name__ == "RecordContract"

    def test_the_roots_inner_display_nodes_are_not_reported_as_left_out(self) -> None:
        text = workflow_to_yaml(_build(), streamstudio.NODE_CLASS_MAPPINGS)
        assert "Taidal_DisplayAny" not in text

    def test_the_export_survives_a_json_round_trip_of_the_canvas(self) -> None:
        workflow = json.loads(json.dumps(_build()))
        document = confluid.load(workflow_to_yaml(workflow, streamstudio.NODE_CLASS_MAPPINGS))
        assert isinstance(document["source"], GraphContract)
