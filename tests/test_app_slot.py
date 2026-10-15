"""Setting a marker into a slot — ``POST /api/graphs/<name>/slot`` (the canvas's *set source…*).

A consuming workspace's source graph arrives with a ``Stream`` whose ``source`` slot is empty (``ops: []`` and
nothing else); the person picks a source class, fills its form, and the editor writes
``source: !class:<class> {<params>}`` into that slot. The route sets ANY single slot of a node — a source into
``stream.source``, a stream into ``processor.stream`` — replacing whatever the slot held (a marker, a ``!ref:``),
as ONE edit (one undo puts the text back byte for byte) held until Save. Every comment outside the replaced value
stays where it is. Refused, changing nothing, in words: a list slot (``ops`` — its members are added with
``POST …/steps``), a name that is no slot of the node (a typed parameter is set with ``POST …/param``), a class
whose output does not meet the slot's type, a class that does not resolve.
"""

from pathlib import Path
from typing import Any, Dict, Iterator

import numpy as np
import pytest
from confluid import configurable
from recordstream import Image, Record

from streamstudio.app import create_app

MOD = __name__


@configurable(category="source", group="slot-test")
class SlotSource:
    """Two square images.

    Args:
        size: The side of each image.
    """

    def __init__(self, size: int = 2) -> None:
        self.size = int(size)

    def __iter__(self) -> Iterator[Record]:
        for index in range(2):
            yield {"image": Image(np.full((self.size, self.size), 0.25 * (index + 1), dtype=np.float32))}


@configurable(category="op", group="slot-test")
class SlotGain:
    """Scale the image entry.

    Args:
        gain: The multiplier.
    """

    consumes = {"image": "Image"}
    produces = {"image": "Image"}

    def __init__(self, gain: float = 1.0) -> None:
        self.gain = float(gain)

    def __call__(self, record: Record) -> Record:
        return {**record, "image": record["image"] * self.gain}


#: The shape a workspace's source template has: ``_target_:`` spelling, no ``source`` yet.
EMPTY_TEXT = """\
# the records of this workspace come from the stream below
stream:
  _target_: Stream
  ops: []   # nothing to do to them yet
# the end
"""

#: A ``!ref:`` in the slot, naming a top-level entry.
REF_TEXT = f"""\
files: !class:{MOD}.SlotSource {{size: 2}}
stream: !class:recordstream.core.stream.Stream
  source: !ref:files   # the files the host listed
  ops:
    - !class:{MOD}.SlotGain {{gain: 2.0}}
"""

#: A multi-line marker in the slot, with comments above and below it.
BLOCK_TEXT = f"""\
stream: !class:recordstream.core.stream.Stream
  # where the records come from
  source: !class:{MOD}.SlotSource
    size: 4   # four pixels a side
  # what happens to them
  ops:
    - !class:{MOD}.SlotGain {{gain: 2.0}}
"""

#: A one-line flow mapping.
FLOW_MAPPING_TEXT = "stream: !class:recordstream.core.stream.Stream {ops: []}\n"

SOURCE = f"{MOD}.SlotSource"


@pytest.fixture
def graph_dir(tmp_path: Path) -> Path:
    for name, text in [("empty", EMPTY_TEXT), ("ref", REF_TEXT), ("block", BLOCK_TEXT), ("inline", FLOW_MAPPING_TEXT)]:
        (tmp_path / f"{name}.yaml").write_text(text)
    return tmp_path


@pytest.fixture
def client(graph_dir: Path) -> Any:  # flask.testing.FlaskClient
    return create_app(graph_dir).test_client()


def _ok(response: Any) -> Dict[str, Any]:
    body = response.get_json()
    assert response.status_code == 200, body
    assert isinstance(body, dict)
    return body


def _error(response: Any) -> str:
    body = response.get_json()
    assert isinstance(body, dict) and set(body) == {"error"}, body
    return str(body["error"])


def _set(client: Any, graph: str, params: Dict[str, Any], class_path: str = SOURCE, slot: str = "source") -> Any:
    return client.post(
        f"/api/graphs/{graph}/slot", json={"node": "stream", "slot": slot, "class_path": class_path, "params": params}
    )


class TestSetASlot:
    def test_an_empty_slot_gets_the_marker_under_the_streams_head_and_every_comment_stays(
        self, client: Any, graph_dir: Path
    ) -> None:
        body = _ok(_set(client, "empty", {"size": 3}))
        assert body["text"] == (
            "# the records of this workspace come from the stream below\n"
            "stream:\n"
            "  _target_: Stream\n"
            f"  source: !class:{SOURCE} {{size: 3}}\n"
            "  ops: []   # nothing to do to them yet\n"
            "# the end\n"
        )
        assert body["set"] == "stream.source" and body["dirty"] is True
        assert (graph_dir / "empty.yaml").read_text() == EMPTY_TEXT, "an edit waits for Save"
        wires = [(w["source"], w["target"], w["target_socket"]) for w in body["model"]["wires"]]
        assert ("stream.source", "stream", "source") in wires

    def test_the_new_source_is_what_a_run_draws_from(self, client: Any) -> None:
        _ok(_set(client, "empty", {"size": 3}))
        run = _ok(client.post("/api/graphs/empty/trace", json={"record": 1}))
        assert run["record"] == 1 and run["host"] is None

    def test_one_undo_puts_the_text_back_byte_for_byte(self, client: Any) -> None:
        _ok(_set(client, "empty", {"size": 3}))
        undone = _ok(client.post("/api/graphs/empty/undo"))
        assert undone["text"] == EMPTY_TEXT and undone["dirty"] is False
        assert _ok(client.post("/api/graphs/empty/undo"))["changed"] is False

    def test_a_reference_in_the_slot_is_replaced_and_its_comment_kept(self, client: Any) -> None:
        body = _ok(_set(client, "ref", {"size": 5}))
        assert f"  source: !class:{SOURCE} {{size: 5}}   # the files the host listed\n" in body["text"]
        assert body["text"].startswith(f"files: !class:{MOD}.SlotSource {{size: 2}}\n"), "the entry it named stays"

    def test_a_multi_line_marker_is_replaced_and_the_comments_around_it_stay(self, client: Any) -> None:
        body = _ok(_set(client, "block", {"size": 6}))
        assert body["text"] == (
            "stream: !class:recordstream.core.stream.Stream\n"
            "  # where the records come from\n"
            f"  source: !class:{SOURCE} {{size: 6}}\n"
            "  # what happens to them\n"
            "  ops:\n"
            f"    - !class:{MOD}.SlotGain {{gain: 2.0}}\n"
        )

    def test_a_one_line_mapping_gets_the_slot_inside_its_braces(self, client: Any) -> None:
        body = _ok(_set(client, "inline", {}))
        assert (
            body["text"]
            == f"stream: !class:recordstream.core.stream.Stream {{ops: [], source: !class:{SOURCE} {{}}}}\n"
        )


class TestWhatIsRefused:
    def test_a_list_slot_is_refused_naming_the_route_that_adds_a_member(self, client: Any, graph_dir: Path) -> None:
        response = _set(client, "block", {}, class_path=f"{MOD}.SlotGain", slot="ops")
        assert response.status_code == 400
        assert _error(response) == "stream.ops is a list — add a member to it, or remove one; it is never set whole"
        assert _ok(client.get("/api/graphs/block"))["dirty"] is False

    def test_a_typed_parameter_is_not_a_slot(self, client: Any) -> None:
        response = _set(client, "block", {}, slot="chunk_size")
        assert response.status_code == 400
        assert _error(response) == (
            "chunk_size is a parameter of stream, not a slot — set its value instead (the slots of stream are: source)"
        )

    def test_a_name_that_is_neither_is_refused_naming_the_slots(self, client: Any) -> None:
        response = _set(client, "block", {}, slot="nosuch")
        assert response.status_code == 400
        assert _error(response) == "stream has no slot 'nosuch' (its slots are: source)"

    def test_a_class_whose_output_does_not_meet_the_slot_is_refused_and_nothing_changes(self, client: Any) -> None:
        response = _set(client, "empty", {}, class_path=f"{MOD}.SlotGain")
        assert response.status_code == 400
        message = _error(response)
        assert message.startswith("stream.source = ") and "refused" in message
        assert "cannot wire" in message and "the types do not meet" in message
        assert _ok(client.get("/api/graphs/empty"))["text"] == EMPTY_TEXT

    def test_a_class_that_does_not_resolve_is_refused_and_nothing_changes(self, client: Any) -> None:
        response = _set(client, "empty", {}, class_path="no.such.Source")
        assert response.status_code == 400
        assert "no.such.Source" in _error(response)
        assert _ok(client.get("/api/graphs/empty"))["dirty"] is False

    def test_an_unknown_node_is_404(self, client: Any) -> None:
        response = client.post(
            "/api/graphs/empty/slot", json={"node": "nosuch", "slot": "source", "class_path": SOURCE, "params": {}}
        )
        assert response.status_code == 404

    def test_the_request_needs_node_slot_and_class(self, client: Any) -> None:
        response = client.post("/api/graphs/empty/slot", json={"node": "stream", "slot": "source"})
        assert response.status_code == 400
        assert "class_path" in _error(response)
