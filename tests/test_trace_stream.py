"""Which stream Run traces when a graph holds several.

A pipeline may hold a HELPER stream — one that only makes a source indexable, read by a split or a range that the
stream which runs then draws from. Run traces the stream no other node draws its records from. Measured before, on a
conversion graph of that shape: ``several streams draw records (['indexable', 'runnable.stream']) — tracing one of
several is not supported``, so the graph could not be run or stepped at all.

Two streams that are both an end of the graph are still refused, naming them: nothing says which the person means.
"""

from pathlib import Path
from typing import Any, Iterator

import numpy as np
from confluid import configurable
from recordstream.items import Image, Record

from streamstudio.app import create_app

MOD = __name__


@configurable(category="source", group="trace-stream-test")
class TwoImages:
    """Two records, an image each — indexable, as a range reads them."""

    def __len__(self) -> int:
        return 2

    def __getitem__(self, index: int) -> Record:
        return {"image": Image(np.full((4, 4), 0.25 * (index + 1), dtype=np.float32))}

    def __iter__(self) -> Iterator[Record]:
        return (self[index] for index in range(len(self)))


#: `indexable` only feeds the range the run stream draws from — the shape of a conversion pipeline.
HELPER_TEXT = f"""\
records: !class:{MOD}.TwoImages {{}}

indexable: !class:recordstream.core.stream.Stream
  source: !ref:records

window: !class:recordstream.sources.range.RangeSource
  source: !ref:indexable
  stop: 2

runnable: !class:recordstream.processing.DatasetProcessor
  stream: !class:recordstream.core.stream.Stream
    source: !ref:window
    ops:
      - !class:recordstream.ops.numpy.Scale {{source_min: 0.0, source_max: 1.0, target_min: 0.0, target_max: 2.0}}
      - !class:recordstream.ops.numpy.Threshold {{low_level: 0.5}}
"""

#: Two streams, each an end of the graph: no node draws from either.
TWO_ENDS_TEXT = f"""\
first: !class:recordstream.core.stream.Stream
  source: !class:{MOD}.TwoImages {{}}
  ops:
    - !class:recordstream.ops.numpy.Threshold {{low_level: 0.5}}

second: !class:recordstream.core.stream.Stream
  source: !class:{MOD}.TwoImages {{}}
  ops:
    - !class:recordstream.ops.numpy.Threshold {{low_level: 0.5}}
"""

RUN_STEPS = ["runnable.stream.ops[0]", "runnable.stream.ops[1]"]


def _client(tmp_path: Path, text: str) -> Any:  # flask.testing.FlaskClient
    (tmp_path / "graph.yaml").write_text(text)
    return create_app(tmp_path).test_client()


class TestAHelperStreamFeedsTheOneThatRuns:
    def test_run_traces_the_stream_no_other_node_draws_from(self, tmp_path: Path) -> None:
        response = _client(tmp_path, HELPER_TEXT).post("/api/graphs/graph/trace", json={"record": 1})
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["statuses"] == {step: "ok" for step in RUN_STEPS}
        assert [node["node"] for node in body["trace"]["nodes"]] == RUN_STEPS
        assert body["trace"]["nodes"][0]["output"]["image"]["max"] == 1.0  # record #1 (0.5), scaled by two
        assert body["trace"]["where"].endswith("graph.yaml:runnable.stream")

    def test_it_pauses_before_a_step_of_that_stream(self, tmp_path: Path) -> None:
        response = _client(tmp_path, HELPER_TEXT).post("/api/graphs/graph/trace", json={"until": RUN_STEPS[1]})
        assert response.status_code == 200, response.get_json()
        assert response.get_json()["statuses"] == {RUN_STEPS[0]: "ok", RUN_STEPS[1]: "paused"}


class TestTwoStreamsThatAreBothAnEnd:
    def test_they_are_refused_by_name(self, tmp_path: Path) -> None:
        response = _client(tmp_path, TWO_ENDS_TEXT).post("/api/graphs/graph/trace", json={})
        assert response.status_code == 400
        assert response.get_json()["error"] == (
            "graph.yaml: several streams draw records (['first', 'second']) and no node draws from either — "
            "tracing one of several is not supported"
        )
