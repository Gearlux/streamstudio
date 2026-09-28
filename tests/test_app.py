"""The editor's HTTP surface (``streamstudio.app``) and the ``streamstudio editor`` command.

Every route is driven once through Flask's test client over a temporary graph directory holding
four synthetic documents — a ``Stream`` with a two-record source (the one that can be TRACED), a
plain ``flow:`` (no source: tracing it is refused), a contract-rooted flow (the add-step refusal
comes from recordstream's own chain check there — a contract-less flow lets the host provide
whatever nobody produces) and a contract-rooted source graph (the twins' shape). The op classes
are recordstream's own numpy ops plus a few tiny classes defined HERE and spelled by the module
name pytest imported this file under, the way ``test_document.py`` does it.

What is pinned, and why it is pinned at the HTTP layer rather than only on ``Document``:

* a refusal reaches the caller as ``400 {error}`` carrying the LOCATED message (``file:line:col``),
  an unknown graph / node / run as ``404 {error}`` naming what there is — a page and an LLM tool
  read these, so the status and the text are the contract;
* a refused ``PUT`` writes nothing, a refused ``add_step`` leaves the file byte-identical, a refused
  rerun leaves the trace identical — measured on the files and the JSON, not inferred;
* a trace names nodes the way the document does (``stream.ops[1]``, never the engine's ``ops[1]``),
  pauses where asked, and a rerun recomputes only what is downstream;
* the blueprint answers under a prefix in a second Flask app, with its own state — the mount a
  consuming workspace needs.

The command is pinned through the liquifai app object (its registration, shorts, positionals and
the ``Args:`` block liquifai renders as help), never by running ``--help`` — the workspace rule.
"""

import socket
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from confluid import configurable, parse_param_docs
from flask import Flask
from recordstream import Algorithm, Image, Input, Label, Output, Record
from recordstream.ops.contract import ClassNamesOutput

from streamstudio import cli, layout
from streamstudio.app import EXTENSION, GRAPH_KINDS, create_app, editor_blueprint
from streamstudio.document import Document

MOD = __name__


# ------------------------------------------------------------------------------------------------
# synthetic classes — the documents below spell them as !class:<MOD>.<Class>
# ------------------------------------------------------------------------------------------------


@configurable(category="source", group="app-test")
class TinySource:
    """Two records: a square float image and a label.

    Args:
        size: The side of the square image.
    """

    def __init__(self, size: int = 4) -> None:
        self.size = size

    def __iter__(self) -> Iterator[Record]:
        for index in range(2):
            image = Image(np.full((self.size, self.size), 0.25 * (index + 1), dtype=np.float32))
            yield {"image": image, "class": Label("cat" if index == 0 else "dog")}


@configurable(category="op", group="app-test")
class TinyGain:
    """Scale the image entry — declares its interface by NAME.

    Args:
        gain: The multiplier.
    """

    consumes = {"image": "Image"}
    produces = {"image": "Image"}

    def __init__(self, gain: float = 1.0) -> None:
        self.gain = gain

    def __call__(self, record: Record) -> Record:
        return {**record, "image": record["image"] * self.gain}


@configurable(category="op", group="app-test")
class NeedsSpectrogram:
    """Consumes an entry no synthetic op produces — the add_step refusal.

    Args:
        percentile: The percentile rank.
    """

    consumes = {"spectrogram": "*"}
    produces = {"floor": "*"}

    def __init__(self, percentile: float = 25.0) -> None:
        self.percentile = percentile

    def __call__(self, record: Record) -> Record:
        return record


@configurable(category="op", group="app-test")
class TinyDecode(Algorithm):
    """Produce an image (a stand-in decoder)."""

    image: Image = Output(doc="The decoded image.")

    def compute(self) -> Dict[str, Any]:
        return {"image": Image([[0]])}


@configurable(category="op", group="app-test")
class TinyCount(Algorithm):
    """Consume a label (a stand-in counter)."""

    label: Label = Input(doc="The label to count.")
    count: int = Output(doc="How many.")

    def compute(self) -> Dict[str, Any]:
        return {"count": 1}


# ------------------------------------------------------------------------------------------------
# the documents
# ------------------------------------------------------------------------------------------------

PETS_TEXT = f"""\
# a two-record stream
stream: !class:recordstream.core.stream.Stream
  source: !class:{MOD}.TinySource {{size: 4}}
  ops:
    - !class:recordstream.ops.numpy.Scale {{source_min: 0.0, source_max: 1.0, target_min: 0.0, target_max: 2.0}}
    - !class:recordstream.ops.numpy.Threshold {{low_level: 0.5}}   # the mask
    - !class:recordstream.ops.numpy.ConnectedComponents {{}}
"""

FLOW_TEXT = f"""\
flow:
  gain:
    op: !class:{MOD}.TinyGain {{gain: 2.0}}
  mask:
    op: !class:recordstream.ops.numpy.Threshold {{low_level: 0.5}}
    from: gain
outputs: mask
"""

STUDY_TEXT = f"""\
contract: !class:recordstream.ops.contract.GraphContract
  name: app study
  inputs: {{image: Image}}
  outputs: {{}}
flow:
  gain:
    op: !class:{MOD}.TinyGain {{gain: 2.0}}
  mask:
    op: !class:recordstream.ops.numpy.Threshold {{low_level: 0.5}}
    from: gain
outputs: mask
"""

SOURCE_TEXT = f"""\
stream: !class:recordstream.core.stream.Stream
  source: !class:{MOD}.TinySource {{}}
  ops:
    - !class:recordstream.ops.numpy.Threshold {{low_level: 0.5}}
names: !class:{ClassNamesOutput.__module__}.ClassNamesOutput {{names: [cat, dog]}}
contract: !class:recordstream.ops.contract.GraphContract
  name: app source
  outputs: {{stream: Stream, classes: "List[str]"}}
  records: {{input: Image}}
  delivered:
    stream: !ref:stream
    classes: !ref:names
"""

MISMATCH_TEXT = f"""\
flow:
  decode:
    op: !class:{MOD}.TinyDecode {{}}
  count:
    op: !class:{MOD}.TinyCount {{keys: {{label: image}}}}
    from: decode
"""

BROKEN_TEXT = "stream: !class:recordstream.core.stream.Stream\n  ops: [\n"

NO_SOURCE_MESSAGE = (
    "flow.yaml: this graph has no source to draw a record from — "
    "a host supplies the record (mount the editor in one) or wire a source"
)


@pytest.fixture
def graph_dir(tmp_path: Path) -> Path:
    for name, text in [("pets", PETS_TEXT), ("flow", FLOW_TEXT), ("study", STUDY_TEXT), ("source", SOURCE_TEXT)]:
        (tmp_path / f"{name}.yaml").write_text(text)
    return tmp_path


@pytest.fixture
def client(graph_dir: Path) -> Any:  # flask.testing.FlaskClient — Flask's own alias is not exported for annotation
    return create_app(graph_dir).test_client()


def _error(response: Any) -> str:
    body = response.get_json()
    assert isinstance(body, dict) and set(body) == {"error"}, body
    return str(body["error"])


# ------------------------------------------------------------------------------------------------
# graphs
# ------------------------------------------------------------------------------------------------


class TestGraphs:
    def test_every_document_is_listed_with_its_kind_and_a_sidecar_is_not(self, client: Any, graph_dir: Path) -> None:
        layout.write_sidecar(graph_dir / "pets.layout.yaml", {"stream": (1.0, 2.0)})
        response = client.get("/api/graphs")
        assert response.status_code == 200
        listed = response.get_json()
        assert [entry["name"] for entry in listed] == ["flow", "pets", "source", "study"]
        assert {entry["name"]: entry["kind"] for entry in listed} == {
            "flow": "flow",
            "pets": "graph",
            "source": "contract",
            "study": "contract",
        }
        assert all(entry["kind"] in GRAPH_KINDS for entry in listed)
        assert listed[1]["path"] == str(graph_dir / "pets.yaml")

    def test_a_graph_answers_its_text_model_positions_and_history_depth(self, client: Any) -> None:
        response = client.get("/api/graphs/pets")
        assert response.status_code == 200
        body = response.get_json()
        assert body["name"] == "pets" and body["kind"] == "graph"
        assert body["text"] == PETS_TEXT
        assert body["history_depth"] == 50
        names = [node["name"] for node in body["model"]["nodes"]]
        assert names == ["stream", "stream.source", "stream.ops[0]", "stream.ops[1]", "stream.ops[2]"]
        assert body["model"]["problems"] == []
        assert [w["kind"] for w in body["model"]["wires"]].count("record") == 2
        assert set(body["positions"]) == set(names)
        assert all(len(xy) == 2 for xy in body["positions"].values())
        threshold = next(node for node in body["model"]["nodes"] if node["name"] == "stream.ops[1]")
        assert threshold["params"]["low_level"]["value"] == 0.5
        assert threshold["params"]["low_level"]["spelled"] is True
        assert threshold["loc"].endswith("pets.yaml:6:7")

    def test_an_unknown_graph_is_404_naming_the_graphs_there_are(self, client: Any) -> None:
        response = client.get("/api/graphs/nosuch")
        assert response.status_code == 404
        message = _error(response)
        assert "nosuch" in message and "pets" in message and "flow" in message

    def test_a_graph_whose_text_does_not_parse_is_still_listed_with_its_problem(
        self, client: Any, graph_dir: Path
    ) -> None:
        (graph_dir / "broken.yaml").write_text(BROKEN_TEXT)
        listed = {entry["name"]: entry["kind"] for entry in client.get("/api/graphs").get_json()}
        assert listed["broken"] == "graph"
        body = client.get("/api/graphs/broken").get_json()
        assert body["model"]["nodes"] == []
        assert "does not load" in body["model"]["problems"][0]["message"]
        assert body["model"]["problems"][0]["loc"].endswith("broken.yaml:3:1")


# ------------------------------------------------------------------------------------------------
# PUT text
# ------------------------------------------------------------------------------------------------


class TestPutText:
    def test_a_sound_text_is_written_and_its_model_returned(self, client: Any, graph_dir: Path) -> None:
        text = PETS_TEXT.replace("low_level: 0.5", "low_level: 0.7")
        response = client.put("/api/graphs/pets", json={"text": text})
        assert response.status_code == 200
        body = response.get_json()
        assert body["text"] == text
        assert (graph_dir / "pets.yaml").read_text() == text
        threshold = next(node for node in body["model"]["nodes"] if node["name"] == "stream.ops[1]")
        assert threshold["params"]["low_level"]["value"] == 0.7

    def test_a_text_that_does_not_load_is_refused_with_the_located_message_and_not_written(
        self, client: Any, graph_dir: Path
    ) -> None:
        response = client.put("/api/graphs/pets", json={"text": BROKEN_TEXT})
        assert response.status_code == 400
        message = _error(response)
        assert "does not load" in message
        assert f"{graph_dir / 'pets.yaml'}" in message and "line 3, column 1" in message
        assert (graph_dir / "pets.yaml").read_text() == PETS_TEXT

    def test_a_wire_whose_types_do_not_meet_is_refused_and_not_written(self, client: Any, graph_dir: Path) -> None:
        response = client.put("/api/graphs/flow", json={"text": MISMATCH_TEXT})
        assert response.status_code == 400
        message = _error(response)
        assert "cannot wire decode.image (Image) into count.label (Label) at" in message
        assert f"{graph_dir / 'flow.yaml'}:5:9: the types do not meet" in message
        assert (graph_dir / "flow.yaml").read_text() == FLOW_TEXT

    def test_a_body_without_text_is_400(self, client: Any) -> None:
        response = client.put("/api/graphs/pets", json={"nope": 1})
        assert response.status_code == 400
        assert "'text'" in _error(response)

    def test_a_body_that_is_not_a_json_object_is_400(self, client: Any) -> None:
        response = client.put("/api/graphs/pets", json=[1, 2])
        assert response.status_code == 400
        assert "JSON object" in _error(response)


# ------------------------------------------------------------------------------------------------
# edits
# ------------------------------------------------------------------------------------------------


class TestEdits:
    def test_set_param_patches_the_located_line_keeps_the_comment_and_saves(self, client: Any, graph_dir: Path) -> None:
        response = client.post(
            "/api/graphs/pets/param", json={"node": "stream.ops[1]", "param": "low_level", "value": 0.9}
        )
        assert response.status_code == 200
        text = (graph_dir / "pets.yaml").read_text()
        assert "    - !class:recordstream.ops.numpy.Threshold {low_level: 0.9}   # the mask" in text
        assert text.count("\n") == PETS_TEXT.count("\n")
        assert response.get_json()["text"] == text

    def test_set_param_on_an_unknown_node_is_404(self, client: Any) -> None:
        response = client.post("/api/graphs/pets/param", json={"node": "nosuch", "param": "x", "value": 1})
        assert response.status_code == 404
        assert "stream.ops[1]" in _error(response)

    def test_set_param_of_an_unknown_parameter_is_400(self, client: Any, graph_dir: Path) -> None:
        response = client.post("/api/graphs/pets/param", json={"node": "stream.ops[1]", "param": "nosuch", "value": 1})
        assert response.status_code == 400
        assert "has no parameter 'nosuch'" in _error(response)
        assert (graph_dir / "pets.yaml").read_text() == PETS_TEXT

    def test_add_step_inserts_after_the_anchor_and_moves_the_output(self, client: Any, graph_dir: Path) -> None:
        response = client.post(
            "/api/graphs/study/steps",
            json={
                "name": "mask2",
                "after": "mask",
                "class_path": "recordstream.ops.numpy.Threshold",
                "params": {"low_level": 0.7},
            },
        )
        assert response.status_code == 200
        body = response.get_json()
        assert "mask2" in [node["name"] for node in body["model"]["nodes"]]
        assert body["model"]["outputs"] == ["mask2"]
        text = (graph_dir / "study.yaml").read_text()
        assert "  mask2:\n    op: !class:recordstream.ops.numpy.Threshold {low_level: 0.7}\n    from: mask\n" in text
        assert text.endswith("outputs: mask2\n")

    def test_add_step_whose_input_nothing_produces_is_refused_and_the_file_unchanged(
        self, client: Any, graph_dir: Path
    ) -> None:
        response = client.post(
            "/api/graphs/study/steps",
            json={
                "name": "floor",
                "after": "gain",
                "class_path": f"{MOD}.NeedsSpectrogram",
                "params": {"percentile": 30},
            },
        )
        assert response.status_code == 400
        message = _error(response)
        assert "NeedsSpectrogram needs the record entry 'spectrogram', which nothing before it produces" in message
        assert (graph_dir / "study.yaml").read_text() == STUDY_TEXT

    def test_add_step_after_an_unknown_node_is_404(self, client: Any) -> None:
        response = client.post(
            "/api/graphs/study/steps",
            json={"name": "x", "after": "nosuch", "class_path": "recordstream.ops.numpy.Threshold", "params": {}},
        )
        assert response.status_code == 404

    def test_remove_step_rewires_what_read_it(self, client: Any, graph_dir: Path) -> None:
        response = client.delete("/api/graphs/study/steps/mask")
        assert response.status_code == 200
        body = response.get_json()
        assert "mask" not in [node["name"] for node in body["model"]["nodes"]]
        assert body["model"]["outputs"] == ["gain"]
        assert "mask" not in (graph_dir / "study.yaml").read_text()

    def test_remove_step_of_an_unknown_node_is_404(self, client: Any) -> None:
        assert client.delete("/api/graphs/study/steps/nosuch").status_code == 404

    def test_rename_node_renames_the_key_and_every_reference_at_once(self, client: Any, graph_dir: Path) -> None:
        response = client.post("/api/graphs/study/rename", json={"node": "mask", "new": "masked"})
        assert response.status_code == 200
        text = (graph_dir / "study.yaml").read_text()
        assert "  masked:\n" in text and "outputs: masked\n" in text and "mask:" not in text
        assert response.get_json()["model"]["outputs"] == ["masked"]

    def test_rename_to_an_existing_name_is_400(self, client: Any) -> None:
        response = client.post("/api/graphs/study/rename", json={"node": "mask", "new": "gain"})
        assert response.status_code == 400
        assert "'gain' is already a node" in _error(response)

    def test_undo_after_set_param_restores_the_text_on_disk_and_redo_reapplies_it(
        self, client: Any, graph_dir: Path
    ) -> None:
        client.post("/api/graphs/pets/param", json={"node": "stream.ops[1]", "param": "low_level", "value": 0.9})
        assert "low_level: 0.9" in (graph_dir / "pets.yaml").read_text()
        undone = client.post("/api/graphs/pets/undo")
        assert undone.status_code == 200
        assert undone.get_json()["changed"] is True
        assert undone.get_json()["text"] == PETS_TEXT
        assert (graph_dir / "pets.yaml").read_text() == PETS_TEXT
        redone = client.post("/api/graphs/pets/redo")
        assert redone.get_json()["changed"] is True
        assert "low_level: 0.9" in (graph_dir / "pets.yaml").read_text()

    def test_undo_with_nothing_to_undo_changes_nothing(self, client: Any, graph_dir: Path) -> None:
        response = client.post("/api/graphs/pets/undo")
        assert response.status_code == 200
        assert response.get_json()["changed"] is False
        assert (graph_dir / "pets.yaml").read_text() == PETS_TEXT


# ------------------------------------------------------------------------------------------------
# positions and layout
# ------------------------------------------------------------------------------------------------


class TestPositions:
    def test_put_then_get_round_trips_through_the_sidecar(self, client: Any, graph_dir: Path) -> None:
        response = client.put("/api/graphs/pets/positions", json={"positions": {"stream.ops[0]": [10, 20]}})
        assert response.status_code == 200
        assert response.get_json()["stream.ops[0]"] == [10.0, 20.0]
        assert client.get("/api/graphs/pets").get_json()["positions"]["stream.ops[0]"] == [10.0, 20.0]
        sidecar = graph_dir / "pets.layout.yaml"
        assert sidecar.exists()
        assert layout.read_sidecar(sidecar)["stream.ops[0]"] == (10.0, 20.0)

    def test_positions_of_an_unknown_node_are_404(self, client: Any) -> None:
        response = client.put("/api/graphs/pets/positions", json={"positions": {"nosuch": [1, 2]}})
        assert response.status_code == 404
        assert "nosuch" in _error(response)

    def test_a_malformed_position_is_400(self, client: Any) -> None:
        response = client.put("/api/graphs/pets/positions", json={"positions": {"stream": [1]}})
        assert response.status_code == 400
        assert "stream" in _error(response)

    def test_layout_lays_the_graph_out_again_and_saves(self, client: Any, graph_dir: Path) -> None:
        client.put("/api/graphs/pets/positions", json={"positions": {"stream.ops[0]": [999, 999]}})
        response = client.post("/api/graphs/pets/layout")
        assert response.status_code == 200
        expected = layout.layered_layout(Document.open(graph_dir / "pets.yaml").model)
        assert response.get_json() == {name: [x, y] for name, (x, y) in expected.items()}
        assert layout.read_sidecar(graph_dir / "pets.layout.yaml") == expected


# ------------------------------------------------------------------------------------------------
# palette and form
# ------------------------------------------------------------------------------------------------


class TestPaletteAndForm:
    def test_the_palette_lists_at_least_recordstreams_own_classes(self, client: Any) -> None:
        from confluid import get_registry

        response = client.get("/api/palette")
        assert response.status_code == 200
        specs = response.get_json()
        registry = get_registry()
        core = [
            key
            for key in registry.list_classes()
            if (registry.get_class(key) or type).__module__.startswith("recordstream.")
        ]
        assert len(specs) >= len(core) > 0
        by_path = {spec["class_path"]: spec for spec in specs}
        assert by_path["recordstream.ops.numpy.Threshold"]["kind"] == "op"
        assert "low_level" in by_path["recordstream.ops.numpy.Threshold"]["params_schema"]
        assert client.get("/api/palette?refresh=1").status_code == 200

    def test_the_form_gives_every_node_its_parameter_schema(self, client: Any) -> None:
        response = client.get("/api/graphs/pets/form")
        assert response.status_code == 200
        form = response.get_json()
        # a JSON object's key order is not a contract (Flask sorts keys); the SET of nodes is
        assert set(form) == {"stream", "stream.source", "stream.ops[0]", "stream.ops[1]", "stream.ops[2]"}
        assert "low_level" in form["stream.ops[1]"]
        assert form["stream.source"]["size"]["type"] == "integer"
        assert "source" in form["stream"]

    def test_the_form_of_a_graph_with_a_host_node_gives_it_no_fields(self, client: Any) -> None:
        form = client.get("/api/graphs/flow/form").get_json()
        assert form["<host>"] == {}
        assert "low_level" in form["mask"]


# ------------------------------------------------------------------------------------------------
# trace and rerun
# ------------------------------------------------------------------------------------------------

DOCUMENT_NAMES = ["stream.ops[0]", "stream.ops[1]", "stream.ops[2]"]


class TestTrace:
    def test_the_streams_own_record_runs_and_the_run_pauses_before_the_named_node(self, client: Any) -> None:
        response = client.post("/api/graphs/pets/trace", json={"record": 0, "until": "stream.ops[1]"})
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["run"].startswith("run_") and body["graph"] == "pets" and body["record"] == 0
        assert body["paused_at"] == "stream.ops[1]"
        assert body["statuses"] == {"stream.ops[0]": "ok", "stream.ops[1]": "paused", "stream.ops[2]": "not reached"}
        assert body["generations"] == {"stream.ops[0]": 0, "stream.ops[1]": 0, "stream.ops[2]": None}
        trace = body["trace"]
        assert [node["node"] for node in trace["nodes"]] == DOCUMENT_NAMES
        assert trace["paused_at"] == "stream.ops[1]"
        assert [row["node"] for row in trace["check"]] == DOCUMENT_NAMES
        assert trace["nodes"][0]["output"]["image"]["max"] == pytest.approx(0.5)
        assert trace["nodes"][1]["params"]["low_level"] == 0.5 and "output" not in trace["nodes"][1]
        assert client.get(f"/api/runs/{body['run']}").get_json() == body

    def test_a_rerun_recomputes_only_the_node_and_what_is_downstream(self, client: Any) -> None:
        run = client.post("/api/graphs/pets/trace", json={"until": "stream.ops[1]"}).get_json()["run"]
        response = client.post(f"/api/runs/{run}/rerun", json={"node": "stream.ops[1]", "params": {"low_level": 0.9}})
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["run"] == run
        assert body["statuses"] == {"stream.ops[0]": "ok", "stream.ops[1]": "ok", "stream.ops[2]": "ok"}
        assert body["generations"] == {"stream.ops[0]": 0, "stream.ops[1]": 1, "stream.ops[2]": 1}
        assert body["paused_at"] is None
        assert body["trace"]["nodes"][1]["params"]["low_level"] == 0.9
        assert body["trace"]["nodes"][1]["output"]["mask"]["type"] == "Mask"

    def test_a_refused_parameter_is_400_with_the_constructors_message_and_the_trace_unchanged(
        self, client: Any
    ) -> None:
        before = client.post("/api/graphs/pets/trace", json={}).get_json()
        response = client.post(
            f"/api/runs/{before['run']}/rerun", json={"node": "stream.ops[1]", "params": {"low_op": "~"}}
        )
        assert response.status_code == 400
        message = _error(response)
        assert "low_op" in message and "Input should be '>' or '>='" in message
        assert client.get(f"/api/runs/{before['run']}").get_json() == before

    def test_a_rerun_of_an_unknown_node_is_404_naming_the_documents_nodes(self, client: Any) -> None:
        run = client.post("/api/graphs/pets/trace", json={}).get_json()["run"]
        response = client.post(f"/api/runs/{run}/rerun", json={"node": "ops[1]", "params": {}})
        assert response.status_code == 404
        message = _error(response)
        assert "'ops[1]'" in message and "stream.ops[1]" in message

    def test_an_unknown_run_is_404(self, client: Any) -> None:
        assert client.get("/api/runs/run_00000000").status_code == 404
        response = client.post("/api/runs/run_00000000/rerun", json={"node": "stream.ops[1]", "params": {}})
        assert response.status_code == 404
        assert "run_00000000" in _error(response)

    def test_a_graph_without_a_source_is_refused_with_the_exact_message(self, client: Any) -> None:
        response = client.post("/api/graphs/flow/trace", json={})
        assert response.status_code == 400
        assert _error(response) == NO_SOURCE_MESSAGE

    def test_a_record_the_source_does_not_yield_is_refused(self, client: Any) -> None:
        response = client.post("/api/graphs/pets/trace", json={"record": 5})
        assert response.status_code == 400
        assert _error(response) == "pets.yaml: the source yields only 2 records — there is no record #5"
        assert client.post("/api/graphs/pets/trace", json={"record": -1}).status_code == 400

    def test_pausing_before_an_unknown_node_is_404(self, client: Any) -> None:
        response = client.post("/api/graphs/pets/trace", json={"until": "nosuch"})
        assert response.status_code == 404
        assert "stream.ops[0]" in _error(response)

    def test_the_contract_rooted_source_graph_traces_its_own_stream(self, client: Any) -> None:
        response = client.post("/api/graphs/source/trace", json={"record": 1})
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["statuses"] == {"stream.ops[0]": "ok"}
        assert body["trace"]["nodes"][0]["input"]["class"] == {"type": "Label", "value": "dog"}

    def test_every_run_is_kept_on_the_app_never_on_the_module(self, graph_dir: Path) -> None:
        first, second = create_app(graph_dir), create_app(graph_dir)
        run = first.test_client().post("/api/graphs/pets/trace", json={}).get_json()["run"]
        assert second.test_client().get(f"/api/runs/{run}").status_code == 404
        assert first.extensions[EXTENSION] is not second.extensions[EXTENSION]


# ------------------------------------------------------------------------------------------------
# mounting
# ------------------------------------------------------------------------------------------------


class TestMounting:
    def test_the_blueprint_answers_under_a_prefix_in_a_consuming_app(self, graph_dir: Path) -> None:
        host = Flask("host")

        @host.route("/")
        def home() -> str:
            return "the host's own page"

        host.register_blueprint(editor_blueprint(graph_dir), url_prefix="/editor")
        client = host.test_client()
        assert client.get("/").data == b"the host's own page"
        listed = client.get("/editor/api/graphs")
        assert listed.status_code == 200
        assert [entry["name"] for entry in listed.get_json()] == ["flow", "pets", "source", "study"]
        assert client.get("/editor/api/graphs/nosuch").status_code == 404
        assert client.get("/api/graphs").status_code == 404

    def test_the_placeholder_page_says_the_editor_arrives_in_phase_3_and_lists_the_routes(self, client: Any) -> None:
        response = client.get("/")
        assert response.status_code == 200
        page = response.get_data(as_text=True)
        assert "phase 3" in page
        for route in ("/api/graphs", "/api/palette", "/api/runs/<run>/rerun", "/api/graphs/<name>/trace"):
            assert route in page


# ------------------------------------------------------------------------------------------------
# the CLI command
# ------------------------------------------------------------------------------------------------


@pytest.fixture
def busy_port() -> Iterator[int]:
    """A really-listening socket on a free port — the probe is exercised, never mocked."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind((cli.STUDIO_HOST, 0))
        held.listen(1)
        yield int(held.getsockname()[1])


@pytest.fixture
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((cli.STUDIO_HOST, 0))
        return int(probe.getsockname()[1])


class TestEditorCommand:
    def test_editor_is_registered_and_its_help_names_every_parameter(self) -> None:
        assert cli.app._commands["editor"] is cli.editor
        documented = parse_param_docs(cli.editor)
        import inspect

        assert list(documented) == list(inspect.signature(cli.editor).parameters) == ["graph_dir", "port"]
        assert all(documented.values())
        assert cli.editor.__liquifai_short__ == {"p": "port"}  # type: ignore[attr-defined]
        assert cli.editor.__liquifai_positionals__ == ["graph_dir"]  # type: ignore[attr-defined]
        assert cli.EDITOR_PORT != cli.DEFAULT_PORT

    def test_editor_serves_the_directory_on_the_port_as_an_int(
        self, graph_dir: Path, free_port: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["streamstudio", "editor", str(graph_dir), "-p", str(free_port)])
        with patch("streamstudio.app.create_app") as create_app_mock:
            cli.main()
        create_app_mock.assert_called_once_with(graph_dir)
        create_app_mock.return_value.run.assert_called_once_with(host=cli.STUDIO_HOST, port=free_port)

    def test_editor_defaults_to_the_current_directory(
        self, graph_dir: Path, free_port: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(graph_dir)
        monkeypatch.setattr(sys, "argv", ["streamstudio", "editor", "--port", str(free_port)])
        with patch("streamstudio.app.create_app") as create_app_mock:
            cli.main()
        create_app_mock.assert_called_once_with(Path.cwd())

    def test_editor_refuses_a_missing_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        missing = tmp_path / "nowhere"
        monkeypatch.setattr(sys, "argv", ["streamstudio", "editor", str(missing)])
        with patch("streamstudio.app.create_app") as create_app_mock:
            with pytest.raises(SystemExit) as exit_info:
                cli.main()
        assert str(missing) in str(exit_info.value)
        create_app_mock.assert_not_called()

    def test_editor_on_a_busy_port_refuses_before_serving_and_offers_a_command_the_cli_accepts(
        self, graph_dir: Path, busy_port: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["streamstudio", "editor", str(graph_dir), "--port", str(busy_port)])
        with patch("streamstudio.app.create_app") as create_app_mock:
            with pytest.raises(SystemExit) as exit_info:
                cli.main()
        assert exit_info.value.code == 1
        create_app_mock.assert_not_called()
        out = capsys.readouterr().out
        assert f"http://{cli.STUDIO_HOST}:{busy_port}" in out
        suggested = [line for line in out.splitlines() if "streamstudio editor --port" in line]
        assert len(suggested) == 1, out
        argv: List[str] = suggested[0].split("streamstudio", 1)[1].split()
        assert argv[0] == "editor"
        # the positional goes right after the command: liquifai binds positionals up to the first flag
        monkeypatch.setattr(sys, "argv", ["streamstudio", "editor", str(graph_dir), *argv[1:]])
        monkeypatch.setattr(cli, "_port_in_use", lambda port, host=cli.STUDIO_HOST: False)
        with patch("streamstudio.app.create_app") as create_app_mock:
            cli.main()
        create_app_mock.return_value.run.assert_called_once_with(host=cli.STUDIO_HOST, port=busy_port + 1)

    def test_run_still_offers_its_own_command_on_a_busy_port(self, capsys: pytest.CaptureFixture[str]) -> None:
        cli._report_port_in_use(8188)
        assert "streamstudio --port 8189" in capsys.readouterr().out
        cli._report_port_in_use(8190, command="editor")
        assert "streamstudio editor --port 8191" in capsys.readouterr().out


def test_magic_mock_is_not_left_installed() -> None:
    """The ``patch`` contexts above are scoped — the real factory is what the module exports afterwards."""
    from streamstudio import app as app_module

    assert not isinstance(app_module.create_app, MagicMock)
