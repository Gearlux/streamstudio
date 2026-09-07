"""The App view's Run button for a PANE graph: ``Preview on Selection`` and ``Selection``
(user request 2026-09-06: "see the effect of the changed parameters" on the selected band).

A pane graph has no record source — the annotation workspace feeds the record — so the App's
Run could show nothing for it. The preview node compiles the chain feeding it from the PROMPT
(the form's current values inside, nothing saved), posts it to the workspace's preview route,
and renders the answer as an IMAGE (the App's output panel lists media). The selection node
shows the record window the page currently selects. The workspace's address is a widget the
launcher fills in (``taidal_workspace``).
"""

import io
import json
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pytest
from PIL import Image

import streamstudio  # noqa: F401  (populates NODE_CLASS_MAPPINGS)
from streamstudio import NODE_CLASS_MAPPINGS
from streamstudio import nodes as nodes_module
from streamstudio.bridge import DEFAULT_WORKSPACE_URL, PREVIEW_NODE_CLASS_TYPE, RECORD_TYPE, SELECTION_NODE_CLASS_TYPE
from streamstudio.graphio import AppViewError, yaml_to_workflow
from streamstudio.nodes import PreviewOnSelectionNode, ShowSelectionNode, WorkspaceUnavailable, pane_document

TOTENSOR = "Taidal_recordstream_op_torch_ToTensor"
CONVERT = "Taidal_recordstream_op_image_ConvertToImage"
_JS = Path(__file__).resolve().parents[1] / "streamstudio" / "web" / "open_graph.js"

PROMPT: Dict[str, Any] = {
    "10": {"class_type": TOTENSOR, "inputs": {"normalize": True, "mode": "", "field": "image", "output": ""}},
    "11": {"class_type": CONVERT, "inputs": {"record": ["10", 0], "field": "image", "output": ""}},
    "77": {
        "class_type": PREVIEW_NODE_CLASS_TYPE,
        "inputs": {"record": ["11", 0], "workspace_url": "http://127.0.0.1:1", "width": 320, "height": 200},
    },
}
ANSWER = {
    "record": "files#0",
    "window": {"x0": 2402e6, "x1": 2404e6, "y0": 0.0, "y1": 0.02},
    "payload": {
        "note": "BLE advertising: ADV_IND",
        "cycle_hz": [0.0, 1.0, 2.0],
        "power_db": [-30.0, -20.0, -25.0],
        "steps": {"burst": {"ran": True, "detail": "328 µs"}, "classic": {"ran": False, "because": "bredr is false"}},
    },
}


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(np.zeros((4, 6, 3), dtype=np.uint8)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def preview_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(nodes_module, "_preview_dir", lambda: tmp_path)
    return tmp_path


class TestTheNodesAreRegisteredWithTheRightShape:
    def test_both_are_output_nodes_in_the_mappings(self) -> None:
        assert NODE_CLASS_MAPPINGS[PREVIEW_NODE_CLASS_TYPE] is PreviewOnSelectionNode
        assert NODE_CLASS_MAPPINGS[SELECTION_NODE_CLASS_TYPE] is ShowSelectionNode
        assert PreviewOnSelectionNode.OUTPUT_NODE and ShowSelectionNode.OUTPUT_NODE

    def test_the_preview_reads_the_prompt_and_never_pulls_its_record(self) -> None:
        spec = PreviewOnSelectionNode.INPUT_TYPES()
        assert spec["hidden"] == {"prompt": "PROMPT", "unique_id": "UNIQUE_ID"}
        assert spec["optional"]["record"] == (RECORD_TYPE, {"lazy": True})
        assert PreviewOnSelectionNode().check_lazy_status(record=None) == []

    def test_the_workspace_address_is_a_widget_with_the_default_port(self) -> None:
        for cls in (PreviewOnSelectionNode, ShowSelectionNode):
            assert cls.INPUT_TYPES()["required"]["workspace_url"][1]["default"] == DEFAULT_WORKSPACE_URL


class TestTheDocumentIsThePanesOwnConversion:
    def test_the_chain_feeding_the_node_converts_without_the_node_itself(self) -> None:
        doc = pane_document(PROMPT, "77")
        assert "ToTensor" in doc and "ConvertToImage" in doc
        assert "PreviewOnSelection" not in doc

    def test_the_forms_current_value_rides_along(self) -> None:
        """The document is built from the LIVE prompt — an unsaved widget value is in it."""
        changed = json.loads(json.dumps(PROMPT))
        changed["10"]["inputs"]["normalize"] = False
        assert "normalize: false" in pane_document(changed, "77")
        assert "normalize: false" not in pane_document(PROMPT, "77")

    def test_an_unwired_node_says_what_to_wire(self) -> None:
        with pytest.raises(WorkspaceUnavailable, match="wire the pane's last op"):
            pane_document({"77": PROMPT["77"] | {"inputs": {}}}, "77")


class TestPreviewOnSelection:
    def test_run_posts_the_document_and_shows_the_answer_as_an_image(
        self, preview_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        posted: List[Dict[str, Any]] = []

        def fake_json(url: str, route: str, body: Any = None) -> Dict[str, Any]:
            posted.append({"url": url, "route": route, "body": body})
            return ANSWER

        monkeypatch.setattr(nodes_module, "_workspace_json", fake_json)
        out = PreviewOnSelectionNode().preview("http://127.0.0.1:1", 320, 200, prompt=PROMPT, unique_id="77")

        assert posted == [
            {"url": "http://127.0.0.1:1", "route": "/api/preview", "body": {"document": pane_document(PROMPT, "77")}}
        ]
        (image,) = out["ui"]["images"]
        assert image["type"] == "temp" and image["subfolder"] == ""
        path = preview_dir / image["filename"]
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert Image.open(path).size == (320, 200)
        assert json.loads(out["result"][0])["record"] == "files#0"

    def test_a_refusal_is_an_image_that_says_why_never_a_blank(
        self, preview_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(url: str, route: str, body: Any = None) -> Dict[str, Any]:
            raise WorkspaceUnavailable("no record is selected in the annotaide page")

        monkeypatch.setattr(nodes_module, "_workspace_json", refuse)
        out = PreviewOnSelectionNode().preview("http://127.0.0.1:1", 320, 200, prompt=PROMPT, unique_id="77")
        assert (preview_dir / out["ui"]["images"][0]["filename"]).is_file()
        assert "no record is selected" in json.loads(out["result"][0])["error"]

    def test_a_workspace_that_is_down_is_named(self, preview_dir: Path) -> None:
        out = PreviewOnSelectionNode().preview("http://127.0.0.1:1", 320, 200, prompt=PROMPT, unique_id="77")
        assert "no workspace answers at http://127.0.0.1:1" in json.loads(out["result"][0])["error"]


class TestShowSelection:
    def test_the_selected_window_is_fetched_and_shown(self, preview_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state = {"record": "files#0", "window": {"x0": 1.0, "x1": 2.0, "y0": 3.0, "y1": 4.0}}
        asked: List[str] = []
        monkeypatch.setattr(nodes_module, "_workspace_json", lambda url, route, body=None: state)

        def fake_request(url: str, route: str, body: Any = None, timeout: float = 0) -> bytes:
            asked.append(route)
            return _png_bytes()

        monkeypatch.setattr(nodes_module, "_workspace_request", fake_request)
        out = ShowSelectionNode().show("http://127.0.0.1:1", 64, 24)
        assert asked and asked[0].startswith("/api/recordings/files%230/thumbnail.png?")
        for part in ("w=64", "h=24", "x0=1.0", "x1=2.0", "y0=3.0", "y1=4.0"):
            assert part in asked[0]
        assert (preview_dir / out["ui"]["images"][0]["filename"]).read_bytes() == _png_bytes()
        assert json.loads(out["result"][0]) == state

    def test_nothing_selected_is_said_in_the_image(self, preview_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        idle = {"record": None, "note": "no page has reported yet — reload the annotaide page"}
        monkeypatch.setattr(nodes_module, "_workspace_json", lambda url, route, body=None: idle)
        out = ShowSelectionNode().show("http://127.0.0.1:1", 64, 24)
        assert "reload the annotaide page" in json.loads(out["result"][0])["error"]
        assert (preview_dir / out["ui"]["images"][0]["filename"]).is_file()


CONTRACT = "recordstream.ops.contract.RecordContract"
PRINT_OP = "recordstream.ops.debug.PrintRecordOp"
FLOW_WITH_PREVIEW = f"""
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
  preview: true
"""


class TestTheImporterPlacesThePreviewNodes:
    def test_preview_true_places_both_nodes_and_makes_them_the_apps_outputs(self) -> None:
        workflow = yaml_to_workflow(FLOW_WITH_PREVIEW, NODE_CLASS_MAPPINGS)
        by_type = {n["type"]: n for n in workflow["nodes"]}
        preview, selection = by_type[PREVIEW_NODE_CLASS_TYPE], by_type[SELECTION_NODE_CLASS_TYPE]
        tail = next(n for n in workflow["nodes"] if "PrintRecordOp" in n["type"])
        # the preview hangs on the tail's record output, lazily
        link = next(link for link in workflow["links"] if link[3] == preview["id"])
        assert link[1] == tail["id"] and link[2] == 0 and link[5] == RECORD_TYPE
        assert preview["inputs"][0]["link"] == link[0] and link[0] in tail["outputs"][0]["links"]
        # the display nodes ARE the App's outputs; the form is unchanged
        assert workflow["extra"]["linearData"]["outputs"] == [str(preview["id"]), str(selection["id"])]
        assert workflow["extra"]["linearData"]["inputs"] and workflow["extra"]["linearMode"] is True
        # widgets carry the classes' own defaults, in declaration order
        assert preview["widgets_values"] == [DEFAULT_WORKSPACE_URL, 960, 540]
        assert selection["widgets_values"] == [DEFAULT_WORKSPACE_URL, 640, 240]
        assert workflow["last_node_id"] == max(n["id"] for n in workflow["nodes"])

    def test_without_the_flag_nothing_is_placed(self) -> None:
        workflow = yaml_to_workflow(FLOW_WITH_PREVIEW.replace("  preview: true\n", ""), NODE_CLASS_MAPPINGS)
        assert not any(n["type"] in (PREVIEW_NODE_CLASS_TYPE, SELECTION_NODE_CLASS_TYPE) for n in workflow["nodes"])

    def test_a_non_boolean_flag_is_refused(self) -> None:
        with pytest.raises(AppViewError, match="app.preview is true or false"):
            yaml_to_workflow(FLOW_WITH_PREVIEW.replace("preview: true", "preview: yes please"), NODE_CLASS_MAPPINGS)


class TestTheOpenerPointsTheNodesAtTheLaunchingWorkspace:
    def test_the_workspace_param_is_read_stripped_and_applied_after_the_load(self) -> None:
        js = _JS.read_text()
        assert 'url.searchParams.get("taidal_workspace")' in js
        assert 'url.searchParams.delete("taidal_workspace")' in js
        assert js.index("const settled = await settleOpen(") < js.index(
            "if (workspaceUrl) pointPreviewNodesAt(workspaceUrl);"
        )

    def test_only_the_two_workspace_nodes_widget_is_touched(self) -> None:
        body = _JS.read_text().split("function pointPreviewNodesAt")[1].split("\n}\n")[0]
        assert 'w.name === "workspace_url"' in body
        js = _JS.read_text()
        assert '["Taidal_PreviewOnSelection", "Taidal_ShowSelection"]' in js
