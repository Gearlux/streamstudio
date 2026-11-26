"""The FULL run of a graph — its ``runnable:`` over every record, as one background job of the editor.

Run and Step trace ONE record. A graph that holds a ``runnable:`` (a conversion, a dataset build) is also run whole:
the editor answers its plan first (what ``recordstream run --dry_run`` prints, nothing changed), then starts it as a
job that counts its records and is stopped between two of them. It runs the SAVED file, under the scopes asked for.
"""

import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List

import numpy as np
from confluid import configurable
from recordstream.items import Image, Record

from streamstudio.app import create_app

MOD = __name__

#: What the sinks of this module wrote, by the name each was given — a test reads it back.
WRITTEN: Dict[str, List[float]] = {}
#: Set to let a `Gated` sink's writes through; a test holds a run at its first record with it.
GATE = threading.Event()


@configurable(category="source", group="run-all-test")
class FiveImages:
    """Five records, an image each."""

    def __len__(self) -> int:
        return 5

    def __getitem__(self, index: int) -> Record:
        return {"image": Image(np.full((2, 2), float(index), dtype=np.float32))}

    def __iter__(self) -> Iterator[Record]:
        return (self[index] for index in range(len(self)))


@configurable(category="source", group="run-all-test")
class TwoUncounted:
    """Two records, and no length: a source that is only iterated."""

    def __iter__(self) -> Iterator[Record]:
        return ({"image": Image(np.full((2, 2), float(index), dtype=np.float32))} for index in range(2))


@configurable(category="sink", group="run-all-test")
class Kept:
    """Keeps each record's value under ``name``.

    Args:
        name: The list in ``WRITTEN`` the records go to.
        gated: Whether each write waits for ``GATE``.
        fail_at: The record (counted from 0) whose write raises; -1 never.
    """

    def __init__(self, name: str = "kept", gated: bool = False, fail_at: int = -1) -> None:
        self.name = name
        self.gated = gated
        self.fail_at = fail_at

    def write(self, record: Record) -> None:
        if self.gated:
            assert GATE.wait(timeout=20)
        held = WRITTEN.setdefault(self.name, [])
        if len(held) == self.fail_at:
            raise OSError("the disk is full")
        held.append(float(np.asarray(record["image"]).max()))

    def flush(self) -> None:
        pass


def _graph(name: str, sink: str = "") -> str:
    return f"""\
stop: 5

small: !scope:small
  stop: 2

runnable: !class:recordstream.processing.DatasetProcessor
  stop: !ref:stop
  stream: !class:recordstream.core.stream.Stream
    source: !class:{MOD}.FiveImages {{}}
    ops:
      - !class:recordstream.ops.numpy.Scale {{source_min: 0.0, source_max: 1.0, target_min: 0.0, target_max: 2.0}}
  sink: !class:{MOD}.Kept {{name: {name}{sink}}}
"""


UNCOUNTED = f"""\
runnable: !class:recordstream.processing.DatasetProcessor
  stream: !class:recordstream.core.stream.Stream
    source: !class:{MOD}.TwoUncounted {{}}
  sink: !class:{MOD}.Kept {{name: uncounted}}
"""

NO_RUNNABLE = f"""\
stream: !class:recordstream.core.stream.Stream
  source: !class:{MOD}.FiveImages {{}}
"""


def _client(tmp_path: Path, text: str) -> Any:  # flask.testing.FlaskClient
    (tmp_path / "graph.yaml").write_text(text)
    return create_app(tmp_path).test_client()


def _ended(client: Any, job: str) -> Dict[str, Any]:
    """The job's state once it ended (a run of five small records: well within the wait)."""
    deadline = time.time() + 20
    while time.time() < deadline:
        (state,) = [each for each in client.get("/api/jobs").get_json() if each["id"] == job]
        if not state["running"]:
            return dict(state)
        time.sleep(0.01)
    raise AssertionError(f"job {job} did not end")


class TestThePlan:
    def test_it_says_what_a_run_would_do_and_which_scopes_the_graph_declares(self, tmp_path: Path) -> None:
        response = _client(tmp_path, _graph("plan")).post("/api/graphs/graph/plan", json={})
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["declared"] == ["small"]
        assert body["scopes"] == []
        assert body["would_stop"] is False
        assert body["plan"]["details"] == {"records": 5, "start": 0, "to_build": 5, "sink": "Kept", "stop": 5}
        assert body["lines"][0] == "DatasetProcessor"
        assert "plan" not in WRITTEN  # nothing ran

    def test_a_scope_changes_the_plan(self, tmp_path: Path) -> None:
        body = _client(tmp_path, _graph("plan")).post("/api/graphs/graph/plan", json={"scopes": ["small"]}).get_json()
        assert body["scopes"] == ["small"]
        assert body["plan"]["details"]["to_build"] == 2

    def test_a_scope_the_graph_does_not_declare_is_refused_naming_the_declared_ones(self, tmp_path: Path) -> None:
        response = _client(tmp_path, _graph("plan")).post("/api/graphs/graph/plan", json={"scopes": ["huge"]})
        assert response.status_code == 400
        assert response.get_json()["error"] == "graph.yaml declares no scope 'huge' — its scopes are: small"

    def test_a_graph_without_a_runnable_is_refused_in_words(self, tmp_path: Path) -> None:
        response = _client(tmp_path, NO_RUNNABLE).post("/api/graphs/graph/plan", json={})
        assert response.status_code == 400
        assert response.get_json()["error"] == (
            "graph.yaml has no runnable: — a full run runs the object under the top-level key 'runnable'"
        )

    def test_scopes_that_are_no_list_of_names_are_refused(self, tmp_path: Path) -> None:
        response = _client(tmp_path, _graph("plan")).post("/api/graphs/graph/plan", json={"scopes": "small"})
        assert response.status_code == 400
        assert response.get_json()["error"] == "'scopes' must be a list of names, got 'small'"


class TestTheRun:
    def test_it_runs_every_record_as_a_job_that_counts_them(self, tmp_path: Path) -> None:
        client = _client(tmp_path, _graph("whole"))
        response = client.post("/api/graphs/graph/run-all", json={})
        assert response.status_code == 200, response.get_json()
        started = response.get_json()
        assert started["key"] == "run:graph" and started["unit"] == "records"
        assert started["detail"] == {"graph": "graph", "scopes": []}
        state = _ended(client, started["id"])
        assert (state["status"], state["done"], state["total"]) == ("done", 5, 5)
        assert state["message"] == "graph: 5 records run"
        assert WRITTEN["whole"] == [0.0, 2.0, 4.0, 6.0, 8.0]

    def test_a_source_that_cannot_be_counted_runs_without_a_count(self, tmp_path: Path) -> None:
        text = UNCOUNTED
        client = _client(tmp_path, text)
        state = _ended(client, client.post("/api/graphs/graph/run-all", json={}).get_json()["id"])
        assert (state["status"], state["total"]) == ("done", None)
        assert state["message"] == "graph: run to its end"
        assert WRITTEN["uncounted"] == [0.0, 1.0]

    def test_it_runs_under_the_scopes_asked_for(self, tmp_path: Path) -> None:
        client = _client(tmp_path, _graph("scoped"))
        started = client.post("/api/graphs/graph/run-all", json={"scopes": ["small"]}).get_json()
        state = _ended(client, started["id"])
        assert (state["status"], state["done"], state["total"]) == ("done", 2, 2)
        assert state["detail"] == {"graph": "graph", "scopes": ["small"]}
        assert WRITTEN["scoped"] == [0.0, 2.0]

    def test_a_stop_ends_it_after_the_record_in_hand_and_keeps_what_was_written(self, tmp_path: Path) -> None:
        GATE.clear()
        client = _client(tmp_path, _graph("stopped", ", gated: true"))
        started = client.post("/api/graphs/graph/run-all", json={}).get_json()
        try:
            again = client.post("/api/graphs/graph/run-all", json={}).get_json()
            assert again["id"] == started["id"]  # one run of a graph at a time: the second start follows the first
            stopping = client.post(f"/api/jobs/{started['id']}/stop").get_json()
            assert stopping["stop"] is True and stopping["running"] is True
        finally:
            GATE.set()
        state = _ended(client, started["id"])
        assert (state["status"], state["done"]) == ("stopped", 1)
        assert state["message"] == "graph: stopped after 1 of 5 records — what was written is kept"
        assert WRITTEN["stopped"] == [0.0]

    def test_a_run_that_fails_ends_failed_in_words(self, tmp_path: Path) -> None:
        client = _client(tmp_path, _graph("failing", ", fail_at: 2"))
        started = client.post("/api/graphs/graph/run-all", json={}).get_json()
        state = _ended(client, started["id"])
        assert (state["status"], state["done"]) == ("failed", 2)
        assert state["error"] == "OSError: the disk is full"
        assert WRITTEN["failing"] == [0.0, 2.0]

    def test_unsaved_changes_are_refused_and_nothing_runs(self, tmp_path: Path) -> None:
        client = _client(tmp_path, _graph("unsaved"))
        changed = client.put("/api/graphs/graph", json={"text": _graph("unsaved").replace("stop: 5", "stop: 4")})
        assert changed.status_code == 200, changed.get_json()
        for route in ("plan", "run-all"):
            response = client.post(f"/api/graphs/graph/{route}", json={})
            assert response.status_code == 400
            assert response.get_json()["error"] == (
                "graph.yaml holds unsaved changes — save the graph first: a full run reads the saved file"
            )
        assert client.get("/api/jobs").get_json() == []
        assert "unsaved" not in WRITTEN

    def test_a_graph_without_a_runnable_starts_nothing(self, tmp_path: Path) -> None:
        client = _client(tmp_path, NO_RUNNABLE)
        assert client.post("/api/graphs/graph/run-all", json={}).status_code == 400
        assert client.get("/api/jobs").get_json() == []

    def test_a_stop_of_a_job_nobody_started_is_refused_naming_the_jobs(self, tmp_path: Path) -> None:
        response = _client(tmp_path, _graph("none")).post("/api/jobs/j9/stop")
        assert response.status_code == 404
        assert response.get_json()["error"] == "no job 'j9' — the jobs are: none"
