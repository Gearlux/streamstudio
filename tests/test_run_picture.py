"""``GET /api/runs/<run>/picture`` — a traced node's entry as a PNG.

A run answers summaries (type, shape, range); the page shows an entry that is a raster as a picture beside them. The
picture is recordstream's (``Tracer.picture``): the editor names the node as the document does and answers the bytes.
An entry with no picture, a node the run never reached and a name that is not there are refused in words.
"""

import io
from pathlib import Path
from typing import Any, Dict, Iterator

import numpy as np
import pytest
from confluid import configurable
from PIL import Image as PILImage
from recordstream.items import Image, Label, Record

from streamstudio.app import create_app

MOD = __name__


@configurable(category="source", group="run-picture-test")
class WideImages:
    """Two records: a 16 x 64 float image (a ramp along its width) and a label."""

    def __iter__(self) -> Iterator[Record]:
        for index in range(2):
            ramp = np.tile(np.linspace(0.0, 1.0, 64, dtype=np.float32), (16, 1)) * (index + 1)
            yield {"image": Image(ramp), "class": Label("cat")}


TEXT = f"""\
stream: !class:recordstream.core.stream.Stream
  source: !class:{MOD}.WideImages {{}}
  ops:
    - !class:recordstream.ops.numpy.Scale {{source_min: 0.0, source_max: 1.0, target_min: 0.0, target_max: 2.0}}
    - !class:recordstream.ops.numpy.Threshold {{low_level: 0.5}}
"""

FIRST, SECOND = "stream.ops[0]", "stream.ops[1]"


@pytest.fixture
def client(tmp_path: Path) -> Any:  # flask.testing.FlaskClient
    (tmp_path / "graph.yaml").write_text(TEXT)
    return create_app(tmp_path).test_client()


def _run(client: Any, **body: Any) -> Dict[str, Any]:
    response = client.post("/api/graphs/graph/trace", json=body)
    assert response.status_code == 200, response.get_json()
    answer: Dict[str, Any] = response.get_json()
    return answer


def _picture(client: Any, run: str, **query: Any) -> Any:
    return client.get(f"/api/runs/{run}/picture", query_string=query)


class TestTheSummarySaysWhichEntriesHaveAPicture:
    def test_an_image_does_and_a_label_does_not(self, client: Any) -> None:
        node = _run(client)["trace"]["nodes"][0]
        assert node["output"]["image"]["picture"] is True
        assert "picture" not in node["output"]["class"]


class TestThePicture:
    def test_an_entry_is_answered_as_a_png_of_its_own_size(self, client: Any) -> None:
        run = _run(client)["run"]
        response = _picture(client, run, node=FIRST, entry="image")
        assert response.status_code == 200 and response.mimetype == "image/png"
        drawn = PILImage.open(io.BytesIO(response.data))
        assert drawn.size == (64, 16)
        grey = np.asarray(drawn.convert("L"))
        assert grey[0, 0] == 0 and grey[0, 63] == 255  # the ramp, from its lowest to its highest value

    def test_a_width_scales_it_down_and_the_input_side_is_the_node_s_input(self, client: Any) -> None:
        run = _run(client)["run"]
        response = _picture(client, run, node=SECOND, entry="image", side="input", width=32)
        assert response.status_code == 200
        assert PILImage.open(io.BytesIO(response.data)).size == (32, 8)

    def test_the_answer_is_never_kept_by_the_browser(self, client: Any) -> None:
        """A rerun changes what the same address answers."""
        run = _run(client)["run"]
        assert _picture(client, run, node=FIRST, entry="image").headers["Cache-Control"] == "no-store"


class TestWhatIsRefused:
    def test_an_entry_without_a_picture_is_400_naming_it(self, client: Any) -> None:
        response = _picture(client, _run(client)["run"], node=FIRST, entry="class")
        assert response.status_code == 400
        assert "the output entry 'class' (Label) has no picture" in response.get_json()["error"]

    def test_a_node_the_run_did_not_reach_is_400_saying_so(self, client: Any) -> None:
        run = _run(client, until=SECOND)["run"]
        response = _picture(client, run, node=SECOND, entry="image")
        assert response.status_code == 400
        assert "has no output yet" in response.get_json()["error"]

    @pytest.mark.parametrize(
        "query, said",
        [
            ({"node": "nosuch", "entry": "image"}, "no node 'nosuch'"),
            ({"node": FIRST, "entry": "nosuch"}, "has no output entry 'nosuch'"),
        ],
    )
    def test_a_node_or_an_entry_that_is_not_there_is_404(self, client: Any, query: Dict[str, str], said: str) -> None:
        response = _picture(client, _run(client)["run"], **query)
        assert response.status_code == 404
        assert said in response.get_json()["error"]

    def test_a_run_that_is_not_there_is_404(self, client: Any) -> None:
        response = _picture(client, "run_nosuch", node=FIRST, entry="image")
        assert response.status_code == 404 and "no run 'run_nosuch'" in response.get_json()["error"]

    @pytest.mark.parametrize(
        "query, said",
        [
            ({"entry": "image"}, "'node' is required"),
            ({"node": FIRST}, "'entry' is required"),
            ({"node": FIRST, "entry": "image", "side": "middle"}, "'side' must be input or output"),
            ({"node": FIRST, "entry": "image", "width": "wide"}, "'width' must be a whole number of pixels"),
            ({"node": FIRST, "entry": "image", "width": "0"}, "width must be 1 or more"),
        ],
    )
    def test_a_malformed_question_is_400_in_words(self, client: Any, query: Dict[str, str], said: str) -> None:
        response = _picture(client, _run(client)["run"], **query)
        assert response.status_code == 400
        assert said in response.get_json()["error"]
