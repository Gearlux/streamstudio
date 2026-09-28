"""A nested-container literal in a document lands on the canvas as a `Value` node (2026-09-07).

A param typed as a nested sequence (`MeasureSymbolClock.windows_hz: [[lo, hi], ...]`) has no
widget: the bridge gives it a `RECORDSTREAM_VALUE` socket, and until now the importer simply
dropped the document's literal — the exported chain ran on the op's constructor default, and
the canvas was not the truth for that value (user instruction: "the canvas must be the truth").
Now the importer places a `Value` node holding the literal and wires it in; both exporters
inline it back into the kwarg; the whole-graph export does not list the node as skipped.
"""

from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

import streamstudio  # noqa: F401  (populates NODE_CLASS_MAPPINGS)
from streamstudio import NODE_CLASS_MAPPINGS
from streamstudio.export import compile_flow_documents
from streamstudio.graphio import flow_yaml_to_workflow, ui_workflow_to_prompt, workflow_to_yaml, yaml_to_workflow

waivefront = pytest.importorskip("waivefront")
confluid = pytest.importorskip("confluid")

VALUE_NODE = "Taidal_Value"
WINDOWS = [[900000.0, 1100000.0], [1800000.0, 2200000.0]]


def _bte_yaml() -> str:
    from waivefront.annotation import templates

    return Path(templates()["signal"]["view_bte"]).with_suffix(".yaml").read_text()


def _workflow(doc: str) -> Dict[str, Any]:
    return flow_yaml_to_workflow(doc, NODE_CLASS_MAPPINGS, as_="graph", name="bte_view")


def _clock(workflow: Dict[str, Any]) -> Dict[str, Any]:
    return next(n for n in workflow["nodes"] if str(n["type"]).endswith("MeasureSymbolClock"))


def _source_of(workflow: Dict[str, Any], node: Dict[str, Any], input_name: str) -> Tuple[Dict[str, Any], int]:
    link_id = next(inp["link"] for inp in node["inputs"] if inp["name"] == input_name)
    link = next(lk for lk in workflow["links"] if lk[0] == link_id)
    return next(n for n in workflow["nodes"] if n["id"] == link[1]), int(link[2])


def _name(target: Any) -> str:
    return target.__name__ if isinstance(target, type) else str(target).rsplit(".", 1)[-1]


class TestTheImportPlacesAValueNode:
    def test_the_literal_is_on_the_canvas_wired_into_the_socket(self) -> None:
        workflow = _workflow(_bte_yaml())
        values = [n for n in workflow["nodes"] if n["type"] == VALUE_NODE]
        assert len(values) == 1, "one Value node — the clock's windows"
        source, slot = _source_of(workflow, _clock(workflow), "windows_hz")
        assert source is values[0] and slot == 0

    def test_the_canvas_shows_numbers_not_strings(self) -> None:
        """PyYAML reads `0.9e6` (no sign in the exponent) as a STRING; the op accepts either, the
        canvas must show the numbers the author meant."""
        workflow = _workflow(_bte_yaml())
        (value,) = [n for n in workflow["nodes"] if n["type"] == VALUE_NODE]
        assert value["widgets_values"] == [repr(WINDOWS)]

    def test_a_document_without_such_a_param_places_no_value_node(self) -> None:
        """CON case: a chain of scalar-widget nodes draws exactly what it drew before.

        The YAML beside a template is a ``flow:`` SOURCE for the canvases drawn from one and a
        converter-produced TWIN (the whole-graph form) for the rest, so the import goes through the
        dispatcher that accepts both; the fft pane ships the twin (2026-09-28).
        """
        from waivefront.annotation import templates

        source = Path(templates()["signal"]["view_fft"]).with_suffix(".yaml")
        if not source.is_file():
            pytest.skip("the fft template ships no YAML source")
        workflow = yaml_to_workflow(source.read_text(), NODE_CLASS_MAPPINGS, as_="graph", name="fft")
        assert not [n for n in workflow["nodes"] if n["type"] == VALUE_NODE]


def _ops_of(document_text: str) -> Dict[str, Dict[str, Any]]:
    """``{class name: kwargs}`` of every op in a whole-graph document — inline under the Stream's
    ``ops`` (the flow importer's canvas) or hoisted as its own entry (the ops-document importer's)."""
    document = confluid.load(document_text, until="document")
    found: Dict[str, Dict[str, Any]] = {}
    for value in document.values():
        target = getattr(value, "target", None)
        if target is None:
            continue
        if _name(target) == "Stream":
            for op in value.kwargs.get("ops", []):
                if getattr(op, "target", None) is not None:
                    found[_name(op.target)] = dict(op.kwargs)
        else:
            found[_name(target)] = dict(value.kwargs)
    return found


class TestBothExportersInlineItBack:
    def test_the_whole_graph_export_carries_the_windows(self) -> None:
        text = workflow_to_yaml(_workflow(_bte_yaml()), NODE_CLASS_MAPPINGS)
        assert _ops_of(text)["MeasureSymbolClock"]["windows_hz"] == WINDOWS

    def test_the_inlined_value_node_is_not_listed_as_skipped(self) -> None:
        text = workflow_to_yaml(_workflow(_bte_yaml()), NODE_CLASS_MAPPINGS)
        assert VALUE_NODE not in text, "an inlined Value node is consumed, not left out"

    def test_the_flow_export_carries_the_windows(self) -> None:
        prompt = ui_workflow_to_prompt(_workflow(_bte_yaml()), NODE_CLASS_MAPPINGS)
        ((flow_map, _outputs),) = compile_flow_documents(prompt, NODE_CLASS_MAPPINGS)
        clock = next(s["op"] for s in flow_map.values() if "op" in s and _name(s["op"].target) == "MeasureSymbolClock")
        assert list(map(list, clock.kwargs["windows_hz"])) == WINDOWS

    def test_the_round_trip_is_stable(self) -> None:
        """Whole-graph export → whole-graph import (the ops-document importer) → export: the Value
        node is drawn again from the inlined literal, and the literal comes back the same."""
        once = workflow_to_yaml(_workflow(_bte_yaml()), NODE_CLASS_MAPPINGS)
        again = yaml_to_workflow(once, NODE_CLASS_MAPPINGS, name="bte_view")
        assert [n for n in again["nodes"] if n["type"] == VALUE_NODE], "the ops-document importer draws it too"
        twice = workflow_to_yaml(again, NODE_CLASS_MAPPINGS)
        assert _ops_of(twice)["MeasureSymbolClock"] == _ops_of(once)["MeasureSymbolClock"]
