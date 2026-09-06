"""The ``app:`` block: a source document declares the ComfyUI APP view its canvas opens in.

ComfyUI's App view is driven by ``extra.linearMode`` + ``extra.linearData`` in the workflow
JSON — a list of ``[node id, widget name]`` inputs and a list of output node ids. Until now the
importer wrote ``"extra": {}`` unconditionally, so a template that wanted an app view had to have
the block pasted back in by hand AFTER every regeneration. Measured on the shipped detection sink
before this change::

    SHIPPED    : extra = {"linearMode": true, "linearData": {"inputs": [[5, "store"]], "outputs": ["1"]}}
    REGENERATED: extra = {}

— i.e. running the command in that file's own header silently dropped its app view, which is the
hand-edit-the-generated-artifact trap the template-first rule exists to prevent.

Nodes are addressed by CLASS NAME, not by id or by config path: a node id is assigned by the
importer (regenerating renumbers it) and a config path differs between the two document shapes a
template can have — a ``flow:`` step graph keys its ops by step name, a whole-graph config nests
them under arbitrary keys. The class name is the one spelling that reads the same in both, and an
ambiguous one is REFUSED rather than guessed.
"""

from typing import Any, Dict, List

import pytest

import streamstudio  # noqa: F401  (populates NODE_CLASS_MAPPINGS)
from streamstudio import NODE_CLASS_MAPPINGS
from streamstudio.graphio import AppViewError, yaml_to_workflow

CONTRACT = "recordstream.ops.contract.RecordContract"
PRINT_OP = "recordstream.ops.debug.PrintRecordOp"

#: A flow document (the shape a view template has) declaring an app view over one widget.
FLOW = f"""
flow:
  first:
    op: !class:{CONTRACT}
      name: input
      fields: {{image: Image}}
  second:
    from: first
    op: !class:{PRINT_OP} {{}}
outputs: second
app:
  inputs:
    - [RecordContract, name]
  outputs: [PrintRecordOp]
"""

#: The same declaration on a WHOLE-GRAPH config (the shape a sink template has) — nested
#: markers under one top-level key, no step names anywhere.
WHOLE = f"""
pipeline: !class:recordstream.core.stream.Stream
  ops:
    - !class:{CONTRACT}
      name: input
      fields: {{image: Image}}
    - !class:{PRINT_OP} {{}}
app:
  inputs:
    - [RecordContract, name]
  outputs: [Stream]
"""


def _wf(doc: str, **kw: Any) -> Dict[str, Any]:
    return yaml_to_workflow(doc, NODE_CLASS_MAPPINGS, name="pipeline", **kw)


def _ids_by_class(workflow: Dict[str, Any]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for node in workflow.get("nodes", []):
        out[str(node.get("type", "")).rsplit("_", 1)[-1]] = int(node["id"])
    return out


class TestTheDeclarationIsEmitted:
    def test_a_flow_document_declares_its_app_view(self) -> None:
        workflow = _wf(FLOW)
        ids = _ids_by_class(workflow)
        extra = workflow["extra"]
        assert extra["linearMode"] is True
        assert extra["linearData"]["inputs"] == [[ids["RecordContract"], "name"]]
        # ComfyUI stores output node ids as STRINGS (inputs keep the numeric id) — the shipped
        # detection sink reads {"inputs": [[5, "store"]], "outputs": ["1"]}.
        assert extra["linearData"]["outputs"] == [str(ids["PrintRecordOp"])]

    def test_a_whole_graph_config_declares_it_the_same_way(self) -> None:
        """The two document shapes share one spelling — that is why nodes are addressed by class."""
        workflow = _wf(WHOLE)
        ids = _ids_by_class(workflow)
        extra = workflow["extra"]
        assert extra["linearMode"] is True
        assert extra["linearData"]["inputs"] == [[ids["RecordContract"], "name"]]
        assert extra["linearData"]["outputs"] == [str(ids["Stream"])]

    def test_a_document_without_the_block_keeps_an_empty_extra(self) -> None:
        """No `app:` = no app view. The existing templates must not grow one by accident."""
        without = FLOW.split("app:")[0]
        assert _wf(without)["extra"] == {}

    def test_the_block_is_not_drawn_as_a_node(self) -> None:
        """`app:` is a DECLARATION about the canvas, not a value on it — a whole-graph import
        walks every top-level key looking for node markers, and this one must be skipped."""
        assert len(_wf(WHOLE)["nodes"]) == len(_wf(WHOLE.split("app:")[0])["nodes"])


class TestAnUnusableDeclarationIsRefused:
    """Silence is the failure mode to avoid: ComfyUI's `pruneLinearData` DROPS an entry whose
    node does not resolve, so a wrong name yields an empty form with no error. Every one of
    these is caught at import time instead, and the message names what was available."""

    def test_a_class_no_node_carries_is_named(self) -> None:
        doc = FLOW.replace("[RecordContract, name]", "[NoSuchOp, name]")
        with pytest.raises(AppViewError) as exc:
            _wf(doc)
        assert "NoSuchOp" in str(exc.value)
        assert "RecordContract" in str(exc.value), "the message lists what the canvas does carry"

    def test_a_class_on_two_nodes_is_ambiguous(self) -> None:
        """Two nodes of one class cannot be told apart by class name, so the declaration is
        refused with both ids rather than resolved to whichever came first."""
        doc = f"""
flow:
  first:
    op: !class:{CONTRACT}
      name: input
      fields: {{image: Image}}
  second:
    from: first
    op: !class:{CONTRACT}
      name: target
      fields: {{target: Label}}
outputs: second
app:
  inputs:
    - [RecordContract, name]
  outputs: [RecordContract]
"""
        with pytest.raises(AppViewError) as exc:
            _wf(doc)
        message = str(exc.value)
        assert "RecordContract" in message and "2 nodes" in message

    def test_a_widget_the_class_does_not_have_is_named(self) -> None:
        doc = FLOW.replace("[RecordContract, name]", "[RecordContract, nmae]")
        with pytest.raises(AppViewError) as exc:
            _wf(doc)
        assert "nmae" in str(exc.value)
        assert "name" in str(exc.value), "the message lists the widgets the class does have"

    def test_a_wired_socket_is_not_a_widget(self) -> None:
        """An app view exposes WIDGETS. A record/op socket is wired on the canvas and has no
        field to render, so naming one is a mistake worth catching."""
        doc = FLOW.replace("[RecordContract, name]", "[RecordContract, record]")
        with pytest.raises(AppViewError) as exc:
            _wf(doc)
        assert "record" in str(exc.value)

    @pytest.mark.parametrize(
        "block, fragment",
        [
            ("app: [RecordContract, name]", "mapping"),
            ("app:\n  inputs: [RecordContract, name]\n  outputs: [PrintRecordOp]", "pair"),
            ("app:\n  inputs:\n    - [RecordContract, name]", "outputs"),
        ],
    )
    def test_a_malformed_block_says_what_shape_it_wanted(self, block: str, fragment: str) -> None:
        doc = FLOW.split("app:")[0] + block + "\n"
        with pytest.raises(AppViewError) as exc:
            _wf(doc)
        assert fragment in str(exc.value)


class TestPackaging:
    def test_a_grouped_import_keeps_the_declaration(self) -> None:
        """`--as_ group` only adds a visual box, so the app view rides through unchanged."""
        workflow = _wf(FLOW, as_="group")
        assert workflow["extra"]["linearMode"] is True
        assert workflow["extra"]["linearData"]["inputs"]

    def test_a_subgraph_import_refuses_it(self) -> None:
        """Subgraph packaging moves every node INSIDE `definitions.subgraphs[]`, and ComfyUI's
        App view only renders a subgraph-internal widget when it has been PROMOTED onto the
        instance node (`resolveNodeWidget` takes the `isPromotedWidgetView` branch there).
        Emitting ids the renderer silently skips would ship an empty form, so this combination
        is refused and the message says to import the template flat."""
        with pytest.raises(AppViewError) as exc:
            _wf(FLOW, as_="subgraph")
        assert "subgraph" in str(exc.value)


class TestTheRegressionThatMotivatedIt:
    def test_regenerating_a_declaring_template_preserves_its_app_view(self) -> None:
        """The whole point: a template whose YAML declares `app:` survives the command in its
        own regenerate header, with no hand-editing of the generated JSON afterwards."""
        first = _wf(WHOLE)
        second = _wf(WHOLE)
        assert first["extra"] == second["extra"]
        assert first["extra"]["linearMode"] is True
        inputs: List[Any] = first["extra"]["linearData"]["inputs"]
        assert inputs and all(len(entry) == 2 for entry in inputs)
