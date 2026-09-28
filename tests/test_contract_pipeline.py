"""The CONTRACT PIPELINE — the canvas a workspace opens on for *set source…*.

A contract document (a ``flow:`` of ``RecordContract`` steps) states what a source must
deliver. Packaged ``as_="pipeline"`` it becomes a canvas that STATES that structurally:
a subgraph with a ``record`` input and one TYPED output per required entry, its ``op``
pre-wired into a ``Stream`` whose ``source`` is the single thing left to connect.

The contract ops themselves never appear — they are a declaration, and the typed boundary
is what they were describing.
"""

from typing import Any, Dict, Optional

import pytest

import streamstudio
from streamstudio import graphio
from streamstudio.bridge import base_item_sockets, item_packages, item_socket_type, item_socket_types
from streamstudio.export import UnsupportedTopologyError
from streamstudio.graphio import yaml_to_workflow

CLASSIFICATION = """\
flow:
  image:
    op: !class:recordstream.ops.contract.RecordContract
      name: "image"
      fields: {"image": "Image"}
  class:
    op: !class:recordstream.ops.contract.RecordContract
      name: "class"
      fields: {"class": "Label"}
    from: image
outputs: class
"""

DETECTION = CLASSIFICATION.replace('"class": "Label"', '"class": "Boxes"')
SEGMENTATION = CLASSIFICATION.replace('"class": "Label"', '"class": "Mask"')


def _build(document: str, name: str = "classification source") -> Dict[str, Any]:
    return yaml_to_workflow(document, streamstudio.NODE_CLASS_MAPPINGS, as_="pipeline", name=name)


def _subgraph(workflow: Dict[str, Any]) -> Dict[str, Any]:
    return workflow["definitions"]["subgraphs"][0]  # type: ignore[no-any-return]


#: recordstream's dataset-level class-vocabulary declaration, as the palette keys it.
CLASS_NAMES_OUTPUT = "Taidal_recordstream_value_contract_ClassNamesOutput"


def _declaration(workflow: Dict[str, Any]) -> Any:
    return next((n for n in workflow["nodes"] if n["type"] == CLASS_NAMES_OUTPUT), None)


#: recordstream's dataset-level class-vocabulary declaration, as the palette keys it.
CLASS_NAMES = "Taidal_recordstream_value_contract_ClassNames"


def _class_names_node(workflow: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return next((n for n in workflow["nodes"] if n["type"] == CLASS_NAMES), None)


class TestItemSocketTypes:
    def test_every_registered_item_type_has_a_socket(self) -> None:
        from recordstream.items import item_type_names

        assert set(item_socket_types()) == set(item_type_names())

    def test_the_socket_name_is_derived_not_listed(self) -> None:
        assert item_socket_type("MultiLabel") == "RECORDSTREAM_ITEM_MULTILABEL"

    def test_an_item_with_a_native_socket_reuses_it(self) -> None:
        """An ``Image`` entry rides ComfyUI's own IMAGE wire — two sockets for one entry is one
        too many, and the native is what every ComfyUI viewer accepts."""
        assert item_socket_type("Image") == "IMAGE"
        assert item_socket_type("Mask") == "MASK"

    def test_the_base_extract_carries_the_base_types_and_no_others(self) -> None:
        cls = streamstudio.NODE_CLASS_MAPPINGS["Taidal_ExtractRecord"]
        by_name = dict(zip(cls.RETURN_NAMES, cls.RETURN_TYPES))
        for item, socket in base_item_sockets().items():
            assert by_name[item.lower()] == socket
        domain = set(item_socket_types()) - set(item_socket_types("recordstream"))
        for item in domain:
            assert item.lower() not in by_name, f"{item} belongs on its own package's node"

    def test_a_native_item_gets_no_second_output(self) -> None:
        """``item_image`` beside ``image`` was the same entry twice."""
        names = list(streamstudio.NODE_CLASS_MAPPINGS["Taidal_ExtractRecord"].RETURN_NAMES)
        assert "item_image" not in names and "item_mask" not in names
        assert names.count("image") == 1 and names.count("mask") == 1

    @pytest.mark.parametrize("package", [p for p in item_packages() if p != "recordstream"])
    def test_each_domain_package_gets_its_own_extract_node(self, package: str) -> None:
        cls = streamstudio.NODE_CLASS_MAPPINGS[f"Taidal_ExtractRecord_{package}"]
        assert dict(zip(cls.RETURN_NAMES, cls.RETURN_TYPES)) == {
            name.lower(): socket for name, socket in item_socket_types(package).items()
        }

    def test_the_native_viewer_outputs_keep_their_slots(self) -> None:
        """A saved canvas addresses outputs POSITIONALLY — inserting would rewire every graph."""
        cls = streamstudio.NODE_CLASS_MAPPINGS["Taidal_ExtractRecord"]
        assert cls.RETURN_NAMES[:7] == ("image", "mask", "text", "int", "float", "array", "value")

    def test_no_output_name_is_duplicated(self) -> None:
        """Slot lookup by name resolves to the FIRST match — a duplicate silently mis-wires."""
        names = list(streamstudio.NODE_CLASS_MAPPINGS["Taidal_ExtractRecord"].RETURN_NAMES)
        assert len(names) == len(set(names))


class TestTheItemTypeNodeFamily:
    @pytest.mark.parametrize("item", sorted(item_socket_types()))
    def test_each_item_type_has_a_passthrough_node(self, item: str) -> None:
        cls = streamstudio.NODE_CLASS_MAPPINGS[f"Taidal_{item}Type"]
        socket = item_socket_type(item)
        assert cls.RETURN_TYPES == (socket,)
        assert [s[0] for s in cls.INPUT_TYPES()["required"].values()] == [socket]

    def test_it_passes_the_item_through_untouched(self) -> None:
        node = streamstudio.NODE_CLASS_MAPPINGS["Taidal_LabelType"]()
        sentinel = object()
        assert node.declare(label=sentinel) == (sentinel,)

    def test_the_display_name_reads_as_the_type(self) -> None:
        assert streamstudio.NODE_DISPLAY_NAME_MAPPINGS["Taidal_BoxesType"] == "Boxes Type"


class TestTheContractBecomesTypedSockets:
    def test_one_boundary_output_per_entry_typed_by_its_item(self) -> None:
        outputs = [(o["name"], o["type"]) for o in _subgraph(_build(CLASSIFICATION))["outputs"]]
        assert outputs == [
            ("image", "IMAGE"),
            ("class", "RECORDSTREAM_ITEM_LABEL"),
            ("op", "RECORDSTREAM_OP"),
        ]

    def test_another_workspace_gets_its_own_contract(self) -> None:
        outputs = [(o["name"], o["type"]) for o in _subgraph(_build(DETECTION))["outputs"]]
        assert outputs[1] == ("class", "RECORDSTREAM_ITEM_BOXES")

    def test_the_boundary_input_is_the_record(self) -> None:
        assert [(i["name"], i["type"]) for i in _subgraph(_build(CLASSIFICATION))["inputs"]] == [
            ("record", "RECORDSTREAM_RECORD")
        ]

    def test_the_contract_ops_are_a_declaration_not_nodes(self) -> None:
        kinds = [n["type"] for n in _subgraph(_build(CLASSIFICATION))["nodes"]]
        assert not any("RecordContract" in k for k in kinds), kinds

    def test_each_entry_is_extracted_and_declared(self) -> None:
        nodes = _subgraph(_build(CLASSIFICATION))["nodes"]
        extracts = [n for n in nodes if n["type"] == "Taidal_ExtractRecord"]
        assert [n["widgets_values"][0] for n in extracts] == ["image", "class"]
        assert {n["type"] for n in nodes} >= {"Taidal_ImageType", "Taidal_LabelType"}

    def test_the_extract_feeds_its_type_node_on_the_item_socket(self) -> None:
        sub = _subgraph(_build(CLASSIFICATION))
        by_id = {n["id"]: n for n in sub["nodes"]}
        wired = [
            (by_id[link["origin_id"]]["type"], by_id[link["target_id"]]["type"], link["type"])
            for link in sub["links"]
            if link["origin_id"] in by_id and link["target_id"] in by_id
        ]
        assert ("Taidal_ExtractRecord", "Taidal_ImageType", "IMAGE") in wired
        assert ("Taidal_ExtractRecord", "Taidal_LabelType", "RECORDSTREAM_ITEM_LABEL") in wired


class TestTheRecordBuilderNamesTheEntries:
    """Each Build Record input SLOT NAME is the record key the compiler renames to.

    The compiler turns a builder entry into ``RenameField(field -> label)``, so a socket left
    at the seeded ``entry_0`` would build a record carrying ``entry_0`` instead of ``image`` —
    a contract that says one thing on the canvas and another at runtime.
    """

    def test_the_sockets_are_named_after_the_contract_entries(self) -> None:
        sub = _subgraph(_build(CLASSIFICATION))
        builder = next(n for n in sub["nodes"] if n["type"] == "Taidal_RecordBuilder")
        assert [i["name"] for i in builder["inputs"]] == ["image", "class"]

    def test_no_socket_keeps_the_seeded_placeholder(self) -> None:
        sub = _subgraph(_build(DETECTION))
        builder = next(n for n in sub["nodes"] if n["type"] == "Taidal_RecordBuilder")
        assert not any(i["name"].startswith("entry_") for i in builder["inputs"])

    def test_every_entry_reaches_the_builder_from_its_type_node(self) -> None:
        sub = _subgraph(_build(CLASSIFICATION))
        by_id = {n["id"]: n for n in sub["nodes"]}
        builder = next(n for n in sub["nodes"] if n["type"] == "Taidal_RecordBuilder")
        feeding = {
            builder["inputs"][link["target_slot"]]["name"]: by_id[link["origin_id"]]["type"]
            for link in sub["links"]
            if link["target_id"] == builder["id"] and link["origin_id"] in by_id
        }
        assert feeding == {"image": "Taidal_ImageType", "class": "Taidal_LabelType"}


class TestTheParentCanvasIsTheBlueprintAndTheStream:
    """The blueprint, a Stream, and whatever the contract must ASK the author for.

    One wire to draw, one socket open, and one field to fill. The blueprint's ``record`` input
    stays UNWIRED on purpose: the subgraph is an OP of the Stream, so the Stream feeds it at
    run time. Wiring it on the canvas — from the Stream's own output, through a walker —
    draws a loop that says the opposite of what runs.
    """

    def test_the_canvas_is_the_blueprint_the_stream_and_the_declaration(self) -> None:
        kinds = [n["type"] for n in _build(CLASSIFICATION)["nodes"]]
        assert "Taidal_recordstream_engine_Stream" in kinds
        assert CLASS_NAMES_OUTPUT in kinds
        assert len(kinds) == 3
        assert "Taidal_WalkDataset" not in kinds, "the parent canvas places no walker"

    def test_a_contract_asking_nothing_extra_is_just_the_blueprint_and_the_stream(self) -> None:
        """The CON case: no class vocabulary to ask for, so no third node appears.

        Segmentation is now the con case — ``Boxes`` grew ``classes`` (its labels ARE
        classes; user instruction 2026-08-31), so detection ASKS like classification does;
        a ``Mask`` still carries no vocabulary field."""
        assert len(_build(SEGMENTATION)["nodes"]) == 2

    def test_the_only_wire_is_the_op_into_the_stream(self) -> None:
        workflow = _build(CLASSIFICATION)
        by_id = {n["id"]: n for n in workflow["nodes"]}
        assert len(workflow["links"]) == 1
        link = workflow["links"][0]
        assert by_id[link[1]]["outputs"][link[2]]["name"] == "op"
        assert by_id[link[3]]["inputs"][link[4]]["name"] == "op_0"

    def test_the_blueprints_record_input_is_left_for_the_stream_to_feed(self) -> None:
        workflow = _build(CLASSIFICATION)
        instance = workflow["nodes"][0]  # the subgraph INSTANCE, not the Stream or a declaration
        record = next(i for i in instance["inputs"] if i["name"] == "record")
        assert record["link"] is None


class TestTheStreamIsPlacedAndPreWired:
    def test_the_canvas_carries_the_blueprint_and_a_stream(self) -> None:
        workflow = _build(CLASSIFICATION)
        kinds = [n["type"] for n in workflow["nodes"]]
        assert "Taidal_recordstream_engine_Stream" in kinds
        # The subgraph INSTANCE is keyed by the definition's uuid, not a registered node type.
        assert kinds[0] == workflow["definitions"]["subgraphs"][0]["id"]

    def test_the_op_is_already_wired_into_the_stream(self) -> None:
        workflow = _build(CLASSIFICATION)
        stream = next(n for n in workflow["nodes"] if n["type"] == "Taidal_recordstream_engine_Stream")
        op_0 = next(i for i in stream["inputs"] if i["name"] == "op_0")
        assert op_0["link"] is not None, "the op slot must arrive connected"

    def test_the_source_is_the_ONE_thing_left_open(self) -> None:
        """The single open SOCKET — the one wiring decision the workspace cannot make.

        The class-names declaration is also unanswered, but it is a FIELD, not a socket:
        it has nothing to connect, so the canvas still shows exactly one dangling wire.
        """
        workflow = _build(CLASSIFICATION)
        stream = next(n for n in workflow["nodes"] if n["type"] == "Taidal_recordstream_engine_Stream")
        source = next(i for i in stream["inputs"] if i["name"] == "source")
        assert source["link"] is None and source["type"] == "RECORDSTREAM_SOURCE"


class TestRefusals:
    def test_a_document_declaring_no_contract_is_refused_by_name(self) -> None:
        document = """\
flow:
  resize:
    op: !class:recordstream.ops.image.ConvertToImage {width: 8, height: 8}
outputs: resize
"""
        with pytest.raises(UnsupportedTopologyError, match="declares no RecordContract"):
            _build(document)

    def test_an_unregistered_item_type_names_the_known_ones(self) -> None:
        document = CLASSIFICATION.replace('"Label"', '"Sparkle"')
        with pytest.raises(UnsupportedTopologyError, match="Sparkle"):
            _build(document)


class TestSubgraphInnerLinksAreDicts:
    """A subgraph definition's inner links are DICTS; a top-level graph's are 6-arrays.

    Emitting arrays produced a definition the frontend read as ZERO links: the nodes appeared,
    the sockets appeared, and nothing was connected — reported as "subgraph nodes are NOT
    connected", twice. streamstudio already knew the asymmetry on the way IN (``_link_six``
    normalises both); it did not know it on the way OUT.
    """

    def test_every_inner_link_is_a_dict_with_the_keys_comfyui_reads(self) -> None:
        for link in _subgraph(_build(CLASSIFICATION))["links"]:
            assert isinstance(link, dict), f"a 6-array here is read as no link at all: {link}"
            assert set(link) == {"id", "origin_id", "origin_slot", "target_id", "target_slot", "type"}

    def test_the_flow_wrap_mode_emits_dicts_too(self) -> None:
        """Both wrap modes go through the same envelope — neither may drift back to arrays."""
        flow_document = """\
flow:
  resize:
    op: !class:recordstream.ops.image.ConvertToImage {width: 8, height: 8}
outputs: resize
"""
        workflow = yaml_to_workflow(flow_document, streamstudio.NODE_CLASS_MAPPINGS, as_="subgraph", name="ops")
        links = workflow["definitions"]["subgraphs"][0]["links"]
        assert links and all(isinstance(link, dict) for link in links)

    def test_the_parent_canvas_still_uses_the_array_form(self) -> None:
        """Only the INNER links change shape — a top-level link stays a 6-array."""
        for link in _build(CLASSIFICATION)["links"]:
            assert isinstance(link, list) and len(link) == 6

    def test_every_inner_link_points_at_a_node_or_a_boundary(self) -> None:
        """A link id that resolves to nothing is how the wires silently disappeared."""
        sub = _subgraph(_build(CLASSIFICATION))
        known = {n["id"] for n in sub["nodes"]} | {-10, -20}
        for link in sub["links"]:
            assert link["origin_id"] in known and link["target_id"] in known, link

    def test_every_connected_input_names_a_link_that_exists(self) -> None:
        sub = _subgraph(_build(CLASSIFICATION))
        ids = {link["id"] for link in sub["links"]}
        for node in sub["nodes"]:
            for socket in node.get("inputs", []):
                if socket.get("link") is not None:
                    assert socket["link"] in ids, f"{node['type']}.{socket['name']} -> missing link"


class TestTheClassVocabularyIsAskedForOnTheCanvas:
    """A dataset-level answer only the graph author has — PLACED, unfilled, and marked as such.

    It has to be VISIBLE. A field the user is expected to fill, on a node they must know to
    look at, is indistinguishable from a feature that does not exist — which is exactly how
    the first cut was reported: *"where is the output node (coloured)? I DO NOT SEE IT"*.
    """

    def test_a_classification_contract_places_the_declaration(self) -> None:
        assert _declaration(_build(CLASSIFICATION)) is not None

    def test_a_contract_with_no_vocabulary_does_not_place_it(self) -> None:
        """The CON case: a ``Mask`` has no ``classes`` field, so it is not asked. (``Boxes``
        DOES now — its labels are classes — so detection gets the declaration node.)"""
        assert _declaration(_build(SEGMENTATION)) is None

    def test_a_boxes_contract_IS_asked_for_its_vocabulary(self) -> None:
        assert _declaration(_build(DETECTION)) is not None

    def test_it_arrives_EMPTY_never_guessed(self) -> None:
        assert _declaration(_build(CLASSIFICATION))["widgets_values"] == [""]

    def test_it_is_marked_by_COLOUR_and_by_TITLE(self) -> None:
        """Colour alone is no affordance for anyone who cannot see it — the title says so too."""
        node = _declaration(_build(CLASSIFICATION))
        assert node["color"] and node["bgcolor"] and node["color"] != node["bgcolor"]
        assert "class_names" in node["title"] and "fill" in node["title"].lower()

    def test_it_is_on_the_parent_canvas_beside_the_stream(self) -> None:
        """The blueprint compiles to a per-RECORD Pipeline; a dataset constant does not belong in it."""
        workflow = _build(CLASSIFICATION)
        for definition in workflow.get("definitions", {}).get("subgraphs", []):
            assert all(n["type"] != CLASS_NAMES_OUTPUT for n in definition.get("nodes", []))
        assert _declaration(workflow) in workflow["nodes"]

    def test_it_needs_no_wire_and_disturbs_none(self) -> None:
        workflow = _build(CLASSIFICATION)
        node = _declaration(workflow)
        assert not node.get("inputs"), "a declaration has nothing to connect"
        assert [ln for ln in workflow["links"] if node["id"] in (ln[1], ln[3])] == []
        stream = next(n for n in workflow["nodes"] if n["type"] == "Taidal_recordstream_engine_Stream")
        assert next(i for i in stream["inputs"] if i["name"] == "op_0")["link"] is not None
        assert next(i for i in stream["inputs"] if i["name"] == "source")["link"] is None


class TestTheStreamsWidgetSeed:
    def test_the_stream_is_seeded_with_one_value_per_widget(self) -> None:
        """A seed longer than the class's widget list assigns values to the wrong knobs."""
        import streamstudio as _ss

        stream = next(n for n in _build(CLASSIFICATION)["nodes"] if n["type"] == "Taidal_recordstream_engine_Stream")
        widgets = list(_ss.NODE_CLASS_MAPPINGS["Taidal_recordstream_engine_Stream"].INPUT_TYPES()["required"])
        assert len(stream["widgets_values"]) == len(widgets)


class TestWhatAWireBECOMESInTheDocument:
    """A config document references a NODE, never one of its attributes.

    Attribute references were removed from the config layer, so a dotted ``!ref:node.attr`` is
    refused the moment anything loads the document. The exporter used to emit exactly that for
    every value-node wire — writing a file happily that could never be opened. This is how it
    surfaced: a source's class names wired into the field that names the classes produced
    ``names: !ref:source.class_names``, and the workspace reported a broken graph.

    So a wire becomes one of two things, decided by the output's NAME: the two outputs that ARE
    the node (``source``, ``object``) become a whole-node ``!ref:``, and every other declared
    ``@output`` is EVALUATED here and written as a literal.
    """

    OUT = "Taidal_recordstream_value_contract_ClassNamesOutput"
    SCAN = "Taidal_recordstream_value_contract_ClassNamesScan"

    def _graph(self, from_output: str, into: str) -> Dict[str, Any]:
        import streamstudio as _ss

        scan_out = _ss.NODE_CLASS_MAPPINGS[self.SCAN].RETURN_NAMES
        slot = scan_out.index(from_output)
        return {
            "last_node_id": 2,
            "last_link_id": 1,
            "groups": [],
            "config": {},
            "extra": {},
            "version": 0.4,
            "nodes": [
                {
                    "id": 1,
                    "type": self.SCAN,
                    "pos": [0, 0],
                    "size": [280, 120],
                    "flags": {},
                    "order": 0,
                    "mode": 0,
                    "inputs": [],
                    "widgets_values": ["class"],
                    "properties": {"Node name for S&R": self.SCAN},
                    "outputs": [
                        {"name": n, "type": "STRING", "links": [1] if n == from_output else None} for n in scan_out
                    ],
                },
                {
                    "id": 2,
                    "type": self.OUT,
                    "pos": [300, 0],
                    "size": [280, 120],
                    "flags": {},
                    "order": 1,
                    "mode": 0,
                    "widgets_values": [""],
                    "properties": {"Node name for S&R": self.OUT},
                    "inputs": [{"name": into, "type": "STRING", "link": 1, "widget": {"name": into}}],
                    "outputs": [],
                },
            ],
            "links": [[1, 1, slot, 2, 0, "STRING"]],
        }

    def test_a_declared_output_is_EVALUATED_and_written_as_a_literal(self) -> None:
        """The wire a user actually draws — and it must produce a document that LOADS."""
        import streamstudio as _ss

        doc = graphio.workflow_to_yaml(self._graph("class_names", "names"), _ss.NODE_CLASS_MAPPINGS)
        assert "_ref_" not in doc, "a reference to an attribute cannot be loaded"

    def test_the_node_itself_is_a_whole_node_reference(self) -> None:
        import streamstudio as _ss

        doc = graphio.workflow_to_yaml(self._graph("object", "names"), _ss.NODE_CLASS_MAPPINGS)
        assert "_ref_: class_names_scan" in doc and ".class_names" not in doc

    def test_the_rule_asks_the_CLASS_which_outputs_are_attributes(self) -> None:
        """Neither a slot index nor a name list can tell an attribute from a structural socket.

        Indexing got it wrong twice — a value node's slot 0 IS a declared ``@output``, while an
        op node's ``op`` is NOT (it is the constructed op, i.e. the node). Asking the class is
        exact and cannot go stale when a builder grows a socket.
        """
        import streamstudio as _ss

        scan = _ss.NODE_CLASS_MAPPINGS[self.SCAN]
        declared = graphio._declared_output_names(scan)
        assert "class_names" in declared, "a declared @output is an attribute -> folded"
        assert "object" not in declared, "the producer socket is the NODE -> referenced"

    def test_the_folded_value_is_read_from_the_producer_WITH_ITS_WIRES(self) -> None:
        """The producer is built with its own inputs resolved, not from its widgets alone.

        A node whose value depends on an INPUT — a class-name walker reading a source —
        evaluated from widgets alone returns its empty default, and the document is written
        happily carrying the wrong answer. Silently. Measured on a real graph: a walker wired
        to a folder source exported ``names: []`` while the folder held ten classes.

        Pinned with a three-node chain, so the MIDDLE node's value exists only because its own
        wire was resolved: A states the vocabulary in a widget, B holds only what A hands it,
        and C folds B.
        """
        import streamstudio as _ss

        vocab = "Taidal_recordstream_value_contract_ClassNamesOutput"

        def node(i: int, widget: str, link: Any, out_links: Any) -> Dict[str, Any]:
            inputs = (
                [] if link is None else [{"name": "names", "type": "STRING", "link": link, "widget": {"name": "names"}}]
            )
            return {
                "id": i,
                "type": vocab,
                "pos": [i * 300, 0],
                "size": [280, 120],
                "flags": {},
                "order": i,
                "mode": 0,
                "widgets_values": [widget],
                "properties": {"Node name for S&R": vocab},
                "inputs": inputs,
                "outputs": [
                    {"name": "class_names", "type": "STRING", "links": out_links},
                    {"name": "object", "type": "RECORDSTREAM_OBJECT", "links": None},
                ],
            }

        workflow = {
            "last_node_id": 3,
            "last_link_id": 2,
            "groups": [],
            "config": {},
            "extra": {},
            "version": 0.4,
            "nodes": [node(1, "cat,dog", None, [1]), node(2, "", 1, [2]), node(3, "", 2, None)],
            "links": [[1, 1, 0, 2, 0, "STRING"], [2, 2, 0, 3, 0, "STRING"]],
        }
        doc = graphio.workflow_to_yaml(workflow, _ss.NODE_CLASS_MAPPINGS)
        # B holds nothing of its own; the vocabulary reaches C only through B's resolved wire.
        assert "cat" in doc and "dog" in doc

    def test_a_cycle_in_the_wires_is_refused_not_recursed(self) -> None:
        import streamstudio as _ss

        vocab = "Taidal_recordstream_value_contract_ClassNamesOutput"

        def node(i: int, link: int, out_links: Any) -> Dict[str, Any]:
            return {
                "id": i,
                "type": vocab,
                "pos": [i * 300, 0],
                "size": [280, 120],
                "flags": {},
                "order": i,
                "mode": 0,
                "widgets_values": [""],
                "properties": {"Node name for S&R": vocab},
                "inputs": [{"name": "names", "type": "STRING", "link": link, "widget": {"name": "names"}}],
                "outputs": [
                    {"name": "class_names", "type": "STRING", "links": out_links},
                    {"name": "object", "type": "RECORDSTREAM_OBJECT", "links": None},
                ],
            }

        workflow = {
            "last_node_id": 2,
            "last_link_id": 2,
            "groups": [],
            "config": {},
            "extra": {},
            "version": 0.4,
            "nodes": [node(1, 2, [1]), node(2, 1, [2])],
            "links": [[1, 1, 0, 2, 0, "STRING"], [2, 2, 0, 1, 0, "STRING"]],
        }
        with pytest.raises(UnsupportedTopologyError):
            graphio.workflow_to_yaml(workflow, _ss.NODE_CLASS_MAPPINGS)
