"""A ``!ref:`` to a top-level PLAIN value — a knob the graph keeps at its top (``limit: 3``) and hands to a card's
parameter (``stop: !ref:limit``) — is read as that value: the parameter is a field showing the value and the key it
comes from, the card is built with it, and nothing is a problem.

Measured before, on a graph of nine such references: nine problems (``source.stop: !ref:limit names no node``), each
parameter an empty input socket instead of a field, and no card of them built.

The field is not set from the form or its route (the key feeds every parameter that names it): refused in one line
naming the key, nothing changed. A reference to a key that is not there stays a located problem, and a reference to a
card stays a wire.
"""

from pathlib import Path
from typing import Any, List, Tuple

from streamstudio.app import create_app
from streamstudio.document import Document

KNOB_TEXT = """\
# the knobs
level: 0.25
missing: null

mask: !class:recordstream.ops.numpy.Threshold
  low_level: !ref:level      # the knob above
  high_level: !ref:missing
"""

FLOW_TEXT = """\
level: 0.25
flow:
  mask:
    op: !class:recordstream.ops.numpy.Threshold
      low_level: !ref:level
outputs: mask
"""

CARD_TEXT = """\
limit: 3
inner: !class:recordstream.sources.range.RangeSource
  source: null
  stop: 5
source: !class:recordstream.sources.range.RangeSource
  source: !ref:inner
  stop: !ref:limit
"""


def _open(tmp_path: Path, text: str) -> Document:
    path = tmp_path / "graph.yaml"
    path.write_text(text)
    return Document.open(path)


def _wires(doc: Document) -> List[Tuple[str, str, str]]:
    return [(wire.source, wire.target, wire.target_socket) for wire in doc.model.wires]


class TestAReferenceToAPlainValue:
    def test_the_parameter_is_a_field_holding_the_value_and_naming_its_key(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, KNOB_TEXT)
        mask = doc.model.node("mask")
        low = mask.params["low_level"]
        assert (low.value, low.ref, low.spelled) == (0.25, "level", True)
        assert "low_level" not in [socket.name for socket in mask.inputs]  # a field, never an open socket
        assert doc.check() == []

    def test_a_key_holding_null_is_a_value_too(self, tmp_path: Path) -> None:
        high = _open(tmp_path, KNOB_TEXT).model.node("mask").params["high_level"]
        assert (high.value, high.ref, high.spelled) == (None, "missing", True)

    def test_a_parameter_the_document_spells_itself_names_no_key(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, CARD_TEXT)
        assert doc.model.node("inner").params["stop"].ref == ""

    def test_a_flow_steps_parameter_reads_the_value_too(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, FLOW_TEXT)
        low = doc.model.node("mask").params["low_level"]
        assert (low.value, low.ref) == (0.25, "level")
        assert doc.check() == []

    def test_a_reference_to_a_card_is_still_a_wire_beside_one_to_a_value(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, CARD_TEXT)
        assert _wires(doc) == [("inner", "source", "source")]
        stop = doc.model.node("source").params["stop"]
        assert (stop.value, stop.ref) == (3, "limit")
        assert doc.check() == []

    def test_a_key_that_is_not_there_is_a_located_problem(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, KNOB_TEXT.replace("!ref:missing", "!ref:nowhere"))
        (problem,) = doc.model.problems
        assert "mask.high_level: !ref:nowhere names no node and no top-level value" in problem.message
        assert problem.loc.endswith("graph.yaml:5:7")

    def test_a_value_named_where_a_card_is_wired_is_a_located_problem(self, tmp_path: Path) -> None:
        """A slot takes a card: the value of a key is no source."""
        doc = _open(tmp_path, CARD_TEXT.replace("source: !ref:inner", "source: !ref:limit"))
        (problem,) = doc.model.problems
        assert "source.source: !ref:limit names the top-level value limit, and a card is wired here" in problem.message


class TestSettingIt:
    def test_the_field_is_refused_naming_its_key_and_nothing_changes(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, KNOB_TEXT)
        before = doc.text
        try:
            doc.set_param("mask", "low_level", 0.5)
        except ValueError as exc:
            said = str(exc)
        else:  # pragma: no cover - the failure this test is about
            raise AssertionError("the field was set")
        assert said == (
            "mask.low_level is the value of the top-level key level (!ref:level) — change level in the text tab"
        )
        assert doc.text == before and not doc.dirty

    def test_the_route_answers_400_and_the_file_is_left_byte_for_byte(self, tmp_path: Path) -> None:
        (tmp_path / "graph.yaml").write_text(KNOB_TEXT)
        client: Any = create_app(tmp_path).test_client()
        node = next(n for n in client.get("/api/graphs/graph").get_json()["model"]["nodes"] if n["name"] == "mask")
        assert node["params"]["low_level"]["ref"] == "level" and node["params"]["low_level"]["value"] == 0.25
        response = client.post("/api/graphs/graph/param", json={"node": "mask", "param": "low_level", "value": 0.5})
        assert response.status_code == 400
        assert "the top-level key level" in response.get_json()["error"]
        assert client.get("/api/graphs/graph").get_json()["dirty"] is False
        assert (tmp_path / "graph.yaml").read_text() == KNOB_TEXT
