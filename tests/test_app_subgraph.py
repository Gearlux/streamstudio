"""The subgraph routes — what the canvas's subgraph gestures call (DESIGN §2.4-§2.6).

* ``GET /api/graphs/<name>`` answers ``model.inside`` — the nested model of every subgraph node;
* ``POST …/subgraph {nodes, name}`` — *Convert selection to subgraph*: consecutive steps of ONE level
  (or consecutive members of ONE ``ops`` list) become one subgraph step; ``result`` is the last one, every
  outer reference to the last one names the subgraph, and an outer step reading any OTHER selected step is
  refused, naming it;
* ``POST …/subgraph/<path:node>/extract {path?}`` — *Move to its own file*: the steps go to a
  ``*.steps.yaml`` held in the editor until Save, and ``steps:`` becomes ``{include: <relative path>}``;
* ``POST …/include {path, after, name?}`` — *Insert steps file*: a new subgraph step including it;
* ``POST …/templates/<template>/insert {after}`` — a COPY of a template as a new subgraph step;
* every node route takes a qualified name (``DELETE …/steps/prep/grey``, ``rename``, ``param``,
  ``positions``, ``form``, copy / paste);
* a trace names a subgraph's inner nodes the way the document does (``stream.ops[0]/scaled``, never the
  engine's ``ops[0]/scaled``) both ways — ``until`` and ``rerun`` included.

Every refusal is ``400 {error}`` in the document's words and changes nothing.
"""

from pathlib import Path
from typing import Any, Dict, Iterator, List

import numpy as np
import pytest
from confluid import configurable
from recordstream import Image, Record

from streamstudio.app import create_app

MOD = __name__
SUBGRAPH = "recordstream.flow.subgraph.Subgraph"
PROBE = "recordstream.ops.debug.PrintRecordOp"


@configurable(category="source", group="sb-app-test")
class SbSource:
    """Two records, each a 4x4 float image.

    Args:
        size: The side of the square image.
    """

    def __init__(self, size: int = 4) -> None:
        self.size = size

    def __iter__(self) -> Iterator[Record]:
        for index in range(2):
            yield {"image": Image(np.full((self.size, self.size), 0.25 * (index + 1), dtype=np.float32))}


FLOW_TEXT = f"""\
flow:
  probe:
    op: !class:{PROBE} {{label: probe}}
  grey:
    op: !class:{PROBE} {{label: grey}}   # keeps this comment
    from: probe
  scaled:
    op: !class:{PROBE} {{label: scaled}}
    from: grey
  tail:
    op: !class:{PROBE} {{label: tail}}
    from: scaled
outputs: tail
"""

CONVERTED_TEXT = f"""\
flow:
  probe:
    op: !class:{PROBE} {{label: probe}}
  prep:
    op: !class:{SUBGRAPH}
      steps:
        grey:
          op: !class:{PROBE} {{label: grey}}   # keeps this comment
        scaled:
          op: !class:{PROBE} {{label: scaled}}
          from: grey
      result: scaled
    from: probe
  tail:
    op: !class:{PROBE} {{label: tail}}
    from: prep
outputs: tail
"""

READS_GREY_TEXT = f"""\
flow:
  probe:
    op: !class:{PROBE} {{label: probe}}
  grey:
    op: !class:{PROBE} {{label: grey}}
  scaled:
    op: !class:{PROBE} {{label: scaled}}
  boxes:
    op: !class:{PROBE} {{label: boxes}}
    from: grey
outputs: boxes
"""

SUB_TEXT = f"""\
flow:
  prep:
    op: !class:{SUBGRAPH}
      steps:
        # the inside
        grey:
          op: !class:{PROBE} {{label: grey}}
        scaled:
          op: !class:{PROBE} {{label: scaled}}
      result: scaled
  tail:
    op: !class:{PROBE} {{label: tail}}
outputs: tail
"""

DOUBLE = "!class:recordstream.ops.numpy.Scale {source_min: 0.0, source_max: 1.0, target_min: 0.0, target_max: 2.0}"

PIPE_TEXT = f"""\
stream: !class:recordstream.core.stream.Stream
  source: !class:{MOD}.SbSource {{}}
  ops:
    - {DOUBLE}   # doubled
    - !class:recordstream.ops.numpy.Threshold
      low_level: 0.5
    - !class:recordstream.ops.numpy.ConnectedComponents {{}}
"""

PIPE_CONVERTED_TEXT = f"""\
stream: !class:recordstream.core.stream.Stream
  source: !class:{MOD}.SbSource {{}}
  ops:
    - !class:{SUBGRAPH}
      steps:
        scale:
          op: {DOUBLE}   # doubled
        threshold:
          op: !class:recordstream.ops.numpy.Threshold
            low_level: 0.5
      result: threshold
    - !class:recordstream.ops.numpy.ConnectedComponents {{}}
"""

TRACE_TEXT = f"""\
stream: !class:recordstream.core.stream.Stream
  source: !class:{MOD}.SbSource {{}}
  ops:
    - !class:{SUBGRAPH}
      steps:
        scaled:
          op: !class:recordstream.ops.numpy.Scale {{source_min: 0.0, source_max: 1.0, target_min: 0.0, target_max: 2.0}}
        mask:
          op: !class:recordstream.ops.numpy.Threshold {{low_level: 0.5}}
    - !class:recordstream.ops.numpy.ConnectedComponents {{}}
"""

SHARED_STEPS = f"# shared steps\nmono:\n  op: !class:{PROBE} {{label: mono}}\n"


def _error(response: Any) -> str:
    body = response.get_json()
    assert isinstance(body, dict) and set(body) == {"error"}, body
    return str(body["error"])


@pytest.fixture
def graph_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "graphs"
    folder.mkdir()
    (folder / "demo.yaml").write_text(FLOW_TEXT)
    (folder / "reads.yaml").write_text(READS_GREY_TEXT)
    (folder / "sub.yaml").write_text(SUB_TEXT)
    (folder / "pipe.yaml").write_text(PIPE_TEXT)
    (folder / "trace.yaml").write_text(TRACE_TEXT)
    (folder / "shared.steps.yaml").write_text(SHARED_STEPS)
    return folder


@pytest.fixture
def templates(tmp_path: Path) -> Path:
    return tmp_path / "templates"


@pytest.fixture
def client(graph_dir: Path, templates: Path) -> Any:  # flask.testing.FlaskClient
    return create_app(graph_dir, templates_dir=templates).test_client()


def _ok(response: Any) -> Dict[str, Any]:
    assert response.status_code == 200, response.get_json()
    body: Dict[str, Any] = response.get_json()
    return body


def _convert(client: Any, graph: str, nodes: List[str], name: str) -> Any:
    return client.post(f"/api/graphs/{graph}/subgraph", json={"nodes": nodes, "name": name})


def _names(model: Dict[str, Any]) -> List[str]:
    return [node["name"] for node in model["nodes"]]


class TestTheGraphAnswer:
    def test_the_model_carries_each_subgraphs_inside(self, client: Any) -> None:
        model = _ok(client.get("/api/graphs/sub"))["model"]
        assert [(node["name"], node["kind"]) for node in model["nodes"]] == [("prep", "subgraph"), ("tail", "op")]
        inside = model["inside"]["prep"]
        assert _names(inside) == ["prep/<in>", "prep/grey", "prep/scaled", "prep/<out>"]
        assert inside["outputs"] == ["prep/scaled"] and inside["inside"] == {}

    def test_the_positions_carry_every_level(self, client: Any) -> None:
        positions = _ok(client.get("/api/graphs/sub"))["positions"]
        assert {"prep", "tail", "prep/<in>", "prep/grey", "prep/scaled", "prep/<out>"} == set(positions)

    def test_inner_positions_are_accepted_and_an_unknown_inner_name_is_404(self, client: Any) -> None:
        answer = _ok(client.put("/api/graphs/sub/positions", json={"positions": {"prep/grey": [5, 6]}}))
        assert answer["prep/grey"] == [5.0, 6.0]
        response = client.put("/api/graphs/sub/positions", json={"positions": {"prep/nosuch": [5, 6]}})
        assert response.status_code == 404
        assert "'prep/nosuch'" in _error(response)

    def test_the_form_lists_every_inner_node(self, client: Any) -> None:
        form = _ok(client.get("/api/graphs/sub/form"))
        assert {"prep", "tail", "prep/<in>", "prep/grey", "prep/scaled", "prep/<out>"} == set(form)
        assert "label" in form["prep/grey"] and form["prep/<in>"] == {}


class TestQualifiedNamesThroughTheRoutes:
    def test_set_param_on_an_inner_node(self, client: Any) -> None:
        body = _ok(client.post("/api/graphs/sub/param", json={"node": "prep/grey", "param": "label", "value": "g"}))
        assert "          op: !class:recordstream.ops.debug.PrintRecordOp {label: g}\n" in body["text"]
        assert body["dirty"] is True

    def test_delete_an_inner_step_through_a_path_segment(self, client: Any) -> None:
        body = _ok(client.delete("/api/graphs/sub/steps/prep/grey"))
        assert _names(body["model"]["inside"]["prep"]) == ["prep/<in>", "prep/scaled", "prep/<out>"]

    def test_rename_an_inner_node(self, client: Any) -> None:
        body = _ok(client.post("/api/graphs/sub/rename", json={"node": "prep/grey", "new": "prep/mono"}))
        assert "prep/mono" in _names(body["model"]["inside"]["prep"])

    def test_add_a_step_inside(self, client: Any) -> None:
        body = _ok(
            client.post(
                "/api/graphs/sub/steps",
                json={"name": "mid", "after": "prep/grey", "class_path": PROBE, "params": {"label": "mid"}},
            )
        )
        assert "prep/mid" in _names(body["model"]["inside"]["prep"])

    def test_copy_and_paste_inside(self, client: Any) -> None:
        copied = _ok(client.post("/api/graphs/sub/copy", json={"nodes": ["prep/grey"]}))
        assert copied == {"text": f"grey:\n  op: !class:{PROBE} {{label: grey}}\n", "kind": "steps"}
        body = _ok(client.post("/api/graphs/sub/paste", json={"text": copied["text"], "after": "prep/scaled"}))
        assert body["pasted"] == ["prep/grey_2"]


class TestConvertToSubgraph:
    def test_consecutive_steps_become_one_subgraph_step(self, client: Any, graph_dir: Path) -> None:
        body = _ok(_convert(client, "demo", ["scaled", "grey"], "prep"))
        assert body["text"] == CONVERTED_TEXT
        assert body["subgraph"] == "prep"
        assert body["dirty"] is True and (graph_dir / "demo.yaml").read_text() == FLOW_TEXT
        assert _names(body["model"]) == ["probe", "prep", "tail"]
        assert _names(body["model"]["inside"]["prep"]) == ["prep/<in>", "prep/grey", "prep/scaled", "prep/<out>"]
        assert body["model"]["problems"] == []

    def test_one_undo_walks_the_conversion_back(self, client: Any) -> None:
        _ok(_convert(client, "demo", ["grey", "scaled"], "prep"))
        assert _ok(client.post("/api/graphs/demo/undo"))["text"] == FLOW_TEXT

    def test_the_output_step_converted_moves_the_output_to_the_subgraph(self, client: Any) -> None:
        text = _ok(_convert(client, "demo", ["scaled", "tail"], "finish"))["text"]
        assert text.endswith(
            "  finish:\n"
            f"    op: !class:{SUBGRAPH}\n"
            "      steps:\n"
            "        scaled:\n"
            f"          op: !class:{PROBE} {{label: scaled}}\n"
            "        tail:\n"
            f"          op: !class:{PROBE} {{label: tail}}\n"
            "          from: scaled\n"
            "      result: tail\n"
            "    from: grey\n"
            "outputs: finish\n"
        )

    def test_an_outer_step_reading_a_step_other_than_the_last_is_refused_naming_it(
        self, client: Any, graph_dir: Path
    ) -> None:
        response = _convert(client, "reads", ["grey", "scaled"], "prep")
        assert response.status_code == 400
        assert _error(response) == (
            f"{(graph_dir / 'reads.yaml').resolve()}:10:5: boxes reads grey (from: grey), which would move inside "
            "the subgraph — select boxes too, or leave grey out"
        )
        assert _ok(client.get("/api/graphs/reads"))["dirty"] is False

    def test_a_selected_step_reading_a_step_left_outside_is_refused(self, client: Any, graph_dir: Path) -> None:
        response = _convert(client, "reads", ["scaled", "boxes"], "prep")
        assert response.status_code == 400
        assert _error(response) == (
            f"{(graph_dir / 'reads.yaml').resolve()}:10:5: boxes reads grey (from: grey), which would stay outside "
            "the subgraph — a step inside a subgraph reads only the record the subgraph receives and the steps "
            "before it inside; select grey too, or leave boxes out"
        )

    def test_steps_that_are_not_consecutive_are_refused(self, client: Any) -> None:
        response = _convert(client, "demo", ["probe", "scaled"], "prep")
        assert response.status_code == 400
        assert (
            _error(response) == "probe and scaled are not consecutive steps — select the steps between them too (grey)"
        )

    def test_nodes_of_two_levels_are_refused(self, client: Any) -> None:
        response = _convert(client, "sub", ["prep/grey", "tail"], "x")
        assert response.status_code == 400
        assert "ONE level" in _error(response)

    @pytest.mark.parametrize("name, message", [("tail", "a step named 'tail' is already there"), ("a/b", "'a/b'")])
    def test_a_taken_or_malformed_name_is_refused(self, client: Any, name: str, message: str) -> None:
        response = _convert(client, "demo", ["grey"], name)
        assert response.status_code == 400
        assert message in _error(response)

    def test_steps_inside_a_subgraph_become_a_nested_subgraph(self, client: Any) -> None:
        body = _ok(_convert(client, "sub", ["prep/grey"], "inner"))
        assert body["subgraph"] == "prep/inner"
        assert (
            "      steps:\n"
            "        # the inside\n"
            "        inner:\n"
            f"          op: !class:{SUBGRAPH}\n"
            "            steps:\n"
            "              grey:\n"
            f"                op: !class:{PROBE} {{label: grey}}\n"
            "            result: grey\n"
            "        scaled:\n"
        ) in body["text"]
        inner = body["model"]["inside"]["prep"]["inside"]["prep/inner"]
        assert _names(inner) == ["prep/inner/<in>", "prep/inner/grey", "prep/inner/<out>"]

    def test_consecutive_list_members_become_one_subgraph_member(self, client: Any) -> None:
        body = _ok(_convert(client, "pipe", ["stream.ops[0]", "stream.ops[1]"], "ignored"))
        assert body["text"] == PIPE_CONVERTED_TEXT
        assert body["subgraph"] == "stream.ops[0]"
        assert _names(body["model"]) == ["stream", "stream.source", "stream.ops[0]", "stream.ops[1]"]
        assert _names(body["model"]["inside"]["stream.ops[0]"]) == [
            "stream.ops[0]/<in>",
            "stream.ops[0]/scale",
            "stream.ops[0]/threshold",
            "stream.ops[0]/<out>",
        ]
        assert body["model"]["problems"] == []


class TestMoveToItsOwnFile:
    def test_the_steps_go_to_a_steps_file_held_until_save(self, client: Any, graph_dir: Path) -> None:
        body = _ok(client.post("/api/graphs/sub/subgraph/prep/extract", json={}))
        assert body["text"] == SUB_TEXT.replace(
            "      steps:\n"
            "        # the inside\n"
            "        grey:\n"
            f"          op: !class:{PROBE} {{label: grey}}\n"
            "        scaled:\n"
            f"          op: !class:{PROBE} {{label: scaled}}\n",
            "      steps: {include: prep.steps.yaml}\n",
        )
        steps_file = graph_dir / "prep.steps.yaml"
        assert not steps_file.exists()  # held in the editor, like every other change
        assert body["dirty"] is True
        assert _names(body["model"]["inside"]["prep"]) == ["prep/<in>", "prep/grey", "prep/scaled", "prep/<out>"]
        assert body["model"]["nodes"][0]["name"] == "prep"
        grey = next(n for n in body["model"]["inside"]["prep"]["nodes"] if n["name"] == "prep/grey")
        assert grey["loc"] == f"{steps_file.resolve()}:3:7"
        _ok(client.post("/api/graphs/sub/save"))
        assert steps_file.read_text() == (
            "# the inside\n"
            "grey:\n"
            f"  op: !class:{PROBE} {{label: grey}}\n"
            "scaled:\n"
            f"  op: !class:{PROBE} {{label: scaled}}\n"
        )
        assert (graph_dir / "sub.yaml").read_text() == body["text"]

    def test_undo_takes_the_steps_back_and_nothing_is_written(self, client: Any, graph_dir: Path) -> None:
        _ok(client.post("/api/graphs/sub/subgraph/prep/extract", json={}))
        body = _ok(client.post("/api/graphs/sub/undo"))
        assert body["text"] == SUB_TEXT and body["dirty"] is False
        _ok(client.post("/api/graphs/sub/save"))
        assert not (graph_dir / "prep.steps.yaml").exists()

    def test_a_path_may_be_named_and_is_written_relative_to_the_graph(self, client: Any, graph_dir: Path) -> None:
        (graph_dir / "parts").mkdir()
        body = _ok(client.post("/api/graphs/sub/subgraph/prep/extract", json={"path": "parts/front.steps.yaml"}))
        assert "      steps: {include: parts/front.steps.yaml}\n" in body["text"]

    @pytest.mark.parametrize(
        "node, path, status, message",
        [
            ("tail", None, 400, "'tail' is not a subgraph"),
            ("prep", "shared.steps.yaml", 400, "shared.steps.yaml already exists"),
            ("prep", "front.yaml", 400, "a steps file is named *.steps.yaml"),
            ("nosuch", None, 404, "'nosuch'"),
        ],
    )
    def test_what_cannot_be_moved_is_refused(
        self, client: Any, node: str, path: Any, status: int, message: str
    ) -> None:
        response = client.post(f"/api/graphs/sub/subgraph/{node}/extract", json={} if path is None else {"path": path})
        assert response.status_code == status
        assert message in _error(response)

    def test_steps_already_in_their_own_file_are_refused(self, client: Any) -> None:
        _ok(client.post("/api/graphs/sub/subgraph/prep/extract", json={}))
        response = client.post("/api/graphs/sub/subgraph/prep/extract", json={"path": "again.steps.yaml"})
        assert response.status_code == 400
        assert "prep's steps are already in their own file" in _error(response)


class TestInsertStepsFile:
    def test_a_new_subgraph_step_includes_the_file(self, client: Any) -> None:
        body = _ok(client.post("/api/graphs/sub/include", json={"path": "shared.steps.yaml", "after": "prep"}))
        assert body["text"] == SUB_TEXT.replace(
            "  tail:\n",
            "  shared:\n"
            f"    op: !class:{SUBGRAPH}\n"
            "      steps: {include: shared.steps.yaml}\n"
            "    from: prep\n"
            "  tail:\n",
        )
        assert _names(body["model"]["inside"]["shared"]) == ["shared/<in>", "shared/mono", "shared/<out>"]
        assert body["model"]["problems"] == []

    def test_twice_gives_a_second_name_and_a_given_name_is_used(self, client: Any) -> None:
        _ok(client.post("/api/graphs/sub/include", json={"path": "shared.steps.yaml", "after": "prep"}))
        body = _ok(client.post("/api/graphs/sub/include", json={"path": "shared.steps.yaml", "after": "shared"}))
        assert "shared_2" in _names(body["model"])
        body = _ok(
            client.post("/api/graphs/sub/include", json={"path": "shared.steps.yaml", "after": "tail", "name": "more"})
        )
        assert "more" in _names(body["model"])

    def test_a_missing_file_is_404_and_a_taken_name_400(self, client: Any) -> None:
        response = client.post("/api/graphs/sub/include", json={"path": "nosuch.steps.yaml", "after": "prep"})
        assert response.status_code == 404
        assert "nosuch.steps.yaml" in _error(response)
        response = client.post(
            "/api/graphs/sub/include", json={"path": "shared.steps.yaml", "after": "prep", "name": "tail"}
        )
        assert response.status_code == 400
        assert "a step named 'tail' is already there" in _error(response)


class TestInsertTemplate:
    def test_a_copy_of_the_template_becomes_a_new_subgraph_step(self, client: Any, templates: Path) -> None:
        """The template was saved from flow steps, so its first step still reads ``probe`` — a step of the graph
        it came from. Inside the new subgraph the first step reads the record the subgraph receives, so that
        ``from:`` is dropped (the paste rule); the reference AMONG the template's steps stays."""
        _ok(client.post("/api/templates", json={"name": "front", "graph": "demo", "nodes": ["grey", "scaled"]}))
        template = (templates / "front.steps.yaml").read_text()
        assert "  from: probe\n" in template and "  from: grey\n" in template
        body = _ok(client.post("/api/graphs/sub/templates/front/insert", json={"after": "tail"}))
        copied = template.replace("  from: probe\n", "")
        inserted = "".join(f"        {line}\n" if line else "\n" for line in copied.splitlines())
        assert body["text"] == SUB_TEXT.replace(
            "outputs: tail\n",
            "  front:\n"
            f"    op: !class:{SUBGRAPH}\n"
            "      steps:\n"
            f"{inserted}"
            "      result: scaled\n"
            "    from: tail\n"
            "outputs: front\n",
        )
        assert _names(body["model"]["inside"]["front"]) == [
            "front/<in>",
            "front/grey",
            "front/scaled",
            "front/<out>",
        ]
        assert body["model"]["problems"] == []
        again = _ok(client.post("/api/graphs/sub/templates/front/insert", json={"after": "front"}))
        assert "front_2" in _names(again["model"])

    def test_inside_a_subgraph_it_is_a_nested_subgraph(self, client: Any) -> None:
        _ok(client.post("/api/templates", json={"name": "front", "graph": "demo", "nodes": ["grey"]}))
        body = _ok(client.post("/api/graphs/sub/templates/front/insert", json={"after": "prep/grey"}))
        assert body["model"]["inside"]["prep"]["inside"]["prep/front"]["outputs"] == ["prep/front/grey"]

    def test_an_unknown_template_or_node_is_404(self, client: Any) -> None:
        assert client.post("/api/graphs/sub/templates/nosuch/insert", json={"after": "tail"}).status_code == 404
        _ok(client.post("/api/templates", json={"name": "front", "graph": "demo", "nodes": ["grey"]}))
        assert client.post("/api/graphs/sub/templates/front/insert", json={"after": "nosuch"}).status_code == 404


class TestTraceNames:
    def test_inner_nodes_are_named_as_the_document_names_them(self, client: Any) -> None:
        body = _ok(client.post("/api/graphs/trace/trace", json={"until": "stream.ops[0]/mask"}))
        assert body["paused_at"] == "stream.ops[0]/mask"
        assert body["statuses"] == {
            "stream.ops[0]": "paused",
            "stream.ops[0]/scaled": "ok",
            "stream.ops[0]/mask": "paused",
            "stream.ops[1]": "not reached",
        }
        rows = {row["node"]: row for row in body["trace"]["nodes"]}
        assert rows["stream.ops[0]"]["inner"] == ["stream.ops[0]/scaled", "stream.ops[0]/mask"]
        assert rows["stream.ops[0]/mask"]["parent"] == "stream.ops[0]"
        check = {row["node"]: row for row in body["trace"]["check"]}
        assert check["stream.ops[0]/scaled"]["parent"] == "stream.ops[0]"

    def test_a_rerun_of_an_inner_node_through_the_route(self, client: Any) -> None:
        run = _ok(client.post("/api/graphs/trace/trace", json={}))["run"]
        body = _ok(
            client.post(f"/api/runs/{run}/rerun", json={"node": "stream.ops[0]/mask", "params": {"low_level": 0.9}})
        )
        assert body["generations"] == {
            "stream.ops[0]": 1,
            "stream.ops[0]/scaled": 0,
            "stream.ops[0]/mask": 1,
            "stream.ops[1]": 1,
        }


# ------------------------------------------------------------------------------------------------
# review fixes (2026-09-29): what moves keeps its lines, refusals name their line, re-keying
# ------------------------------------------------------------------------------------------------

READS_TEXT = f"""\
flow:
  a:
    op: !class:{PROBE} {{label: a}}
  b:
    op: !class:{PROBE} {{label: b}}
    from: a      # b reads a
  c:
    op: !class:{PROBE} {{label: c}}
    merge_from:
      - a      # merge a
      # after merge list
outputs: c
"""


def _comment_lines(text: str) -> int:
    return sum("#" in line for line in text.splitlines())


class TestConvertMovesTheReadingLinesAsWritten:
    """DESIGN §2.6: the moved steps keep their own lines and comments — the first step's ``from:`` /
    ``merge_from:`` too, which move to the subgraph step (measured before: re-spelled ``from: a`` without its
    comment, and a block list turned into ``merge_from: [a]`` — 3 comment lines became 1)."""

    @pytest.fixture
    def reads(self, graph_dir: Path, templates: Path) -> Any:
        (graph_dir / "reads2.yaml").write_text(READS_TEXT)
        return create_app(graph_dir, templates_dir=templates).test_client()

    def test_a_from_line_moves_with_its_comment(self, reads: Any) -> None:
        text = _ok(_convert(reads, "reads2", ["b"], "s"))["text"]
        assert _comment_lines(text) == _comment_lines(READS_TEXT)
        assert "      result: b\n    from: a      # b reads a\n  c:\n" in text

    def test_a_block_merge_from_list_moves_as_a_block(self, reads: Any) -> None:
        text = _ok(_convert(reads, "reads2", ["c"], "s"))["text"]
        assert _comment_lines(text) == _comment_lines(READS_TEXT)
        assert (
            "      result: c\n    merge_from:\n      - a      # merge a\n      # after merge list\noutputs: s\n" in text
        )


class TestACommentAboveALineThatStaysStaysThere:
    """A comment written directly above a line that is not moved — ``outputs:``, the subgraph step's own
    ``from:`` — is about THAT line (measured before: Convert took a column-0 ``# before outputs`` into the
    subgraph's steps, and Move to its own file wrote ``# prep reads the probe explicitly`` into the steps file)."""

    def test_convert_leaves_a_comment_above_outputs_in_place(self, graph_dir: Path, templates: Path) -> None:
        (graph_dir / "one.yaml").write_text(
            f"flow:\n  a:\n    op: !class:{PROBE} {{label: a}}\n  d:\n    op: !class:{PROBE} {{label: d}}\n"
            "# before outputs\noutputs: d\n"
        )
        client = create_app(graph_dir, templates_dir=templates).test_client()
        text = _ok(_convert(client, "one", ["d"], "sd"))["text"]
        assert text.endswith("      result: d\n# before outputs\noutputs: sd\n")

    def test_move_to_its_own_file_leaves_a_comment_above_from_in_the_graph(
        self, graph_dir: Path, templates: Path
    ) -> None:
        (graph_dir / "two.yaml").write_text(
            f"flow:\n  probe:\n    op: !class:{PROBE} {{label: probe}}\n  prep:\n    op: !class:{SUBGRAPH}\n"
            f"      steps:\n        grey:\n          op: !class:{PROBE} {{label: grey}}\n"
            "    # prep reads the probe explicitly\n    from: probe\noutputs: prep\n"
        )
        client = create_app(graph_dir, templates_dir=templates).test_client()
        text = _ok(client.post("/api/graphs/two/subgraph/prep/extract", json={}))["text"]
        assert (
            "      steps: {include: prep.steps.yaml}\n    # prep reads the probe explicitly\n    from: probe\n" in text
        )
        _ok(client.post("/api/graphs/two/save"))
        assert (graph_dir / "prep.steps.yaml").read_text() == f"grey:\n  op: !class:{PROBE} {{label: grey}}\n"


ONE_LINE_TEXT = f"""\
flow:
  probe:
    op: !class:{PROBE} {{label: probe}}
  prep:
    op: !class:{SUBGRAPH}
      steps: {{grey: {{op: !class:{PROBE} {{label: grey}}}}, b: {{op: !class:{PROBE} {{}}}}}}
  mask:
    op: !class:{PROBE} {{label: mask}}
    from: probe
outputs: mask
"""


class TestARefusalStartsWithTheLineItIsAbout:
    @pytest.fixture
    def one_line(self, graph_dir: Path, templates: Path) -> Any:
        (graph_dir / "oneline.yaml").write_text(ONE_LINE_TEXT)
        return create_app(graph_dir, templates_dir=templates).test_client()

    @pytest.mark.parametrize("what", ["add", "remove", "paste"])
    def test_steps_written_on_one_line(self, one_line: Any, graph_dir: Path, what: str) -> None:
        if what == "add":
            response = one_line.post(
                "/api/graphs/oneline/steps", json={"name": "m", "after": "prep/grey", "class_path": PROBE, "params": {}}
            )
        elif what == "remove":
            response = one_line.delete("/api/graphs/oneline/steps/prep/b")
        else:
            clip = f"m:\n  op: !class:{PROBE} {{}}\n"
            response = one_line.post("/api/graphs/oneline/paste", json={"text": clip, "after": "prep/grey"})
        assert response.status_code == 400
        assert _error(response).startswith(
            f"{(graph_dir / 'oneline.yaml').resolve()}:6:14: the steps of the subgraph 'prep' are written on one line"
        )

    def test_a_step_reading_a_selected_step_names_its_reading_line(self, graph_dir: Path, templates: Path) -> None:
        (graph_dir / "reads3.yaml").write_text(READS_TEXT)
        client = create_app(graph_dir, templates_dir=templates).test_client()
        response = _convert(client, "reads3", ["a", "b"], "s")
        assert response.status_code == 400
        assert _error(response) == (
            f"{(graph_dir / 'reads3.yaml').resolve()}:9:5: c reads a (merge_from: a), which would move inside the "
            "subgraph — select c too, or leave a out"
        )

    def test_renaming_onto_another_level_names_the_node(self, client: Any, graph_dir: Path) -> None:
        response = client.post("/api/graphs/sub/rename", json={"node": "prep/grey", "new": "other/x"})
        assert response.status_code == 400
        assert _error(response).startswith(f"{graph_dir / 'sub.yaml'}:")
        assert ": prep/grey can only be renamed on its own level" in _error(response)

    def test_a_boundary_is_no_step_and_the_refusal_names_the_subgraph(self, client: Any, graph_dir: Path) -> None:
        response = client.post(
            "/api/graphs/sub/steps", json={"name": "m", "after": "prep/<in>", "class_path": PROBE, "params": {}}
        )
        assert response.status_code == 400
        assert _error(response).startswith(f"{graph_dir / 'sub.yaml'}:3:9: 'prep/<in>' is not a step of the subgraph")


class TestARenameReKeysEveryNameUnderTheNode:
    """A rename moves the positions and the group memberships of every name derived from the node — ``old.…``,
    ``old[…`` and ``old/…`` (measured before: renaming ``stream`` reset ``stream.ops[0]`` and
    ``stream.ops[1]/scaled`` to fresh places and left a group naming ``stream.ops[1]/scaled``)."""

    PIPE = f"""\
stream: !class:recordstream.core.stream.Stream
  ops:
    - !class:{PROBE} {{label: first}}
    - !class:{SUBGRAPH}
      steps:
        scaled:
          op: !class:{PROBE} {{label: scaled}}
      result: scaled
"""

    def test_renaming_the_stream_keeps_its_members_places_and_groups(self, graph_dir: Path, templates: Path) -> None:
        (graph_dir / "rk.yaml").write_text(self.PIPE)
        client = create_app(graph_dir, templates_dir=templates).test_client()
        names = sorted(client.get("/api/graphs/rk").get_json()["positions"])
        placed = {name: [index * 100.0, 7.0] for index, name in enumerate(names)}
        client.put("/api/graphs/rk/positions", json={"positions": placed})
        group = {"id": "g", "title": "t", "color": "#fff", "nodes": ["stream.ops[1]/scaled"], "parent": "stream.ops[1]"}
        client.put("/api/graphs/rk/groups", json={"groups": [group]})
        body = _ok(client.post("/api/graphs/rk/rename", json={"node": "stream", "new": "pipe"}))
        assert body["positions"] == {name.replace("stream", "pipe", 1): xy for name, xy in placed.items()}
        assert body["groups"][0]["nodes"] == ["pipe.ops[1]/scaled"] and body["groups"][0]["parent"] == "pipe.ops[1]"
        undone = client.post("/api/graphs/rk/undo").get_json()
        assert undone["positions"] == placed and undone["groups"][0]["nodes"] == ["stream.ops[1]/scaled"]

    def test_renaming_a_step_holding_a_slot_subgraph(self, graph_dir: Path, templates: Path) -> None:
        (graph_dir / "slot.yaml").write_text(
            f"flow:\n  maybe:\n    op: !class:recordstream.ops.random_apply.RandomApply\n      probability: 1.0\n"
            f"      op: !class:{SUBGRAPH}\n        steps:\n          grey:\n            op: !class:{PROBE} {{}}\n"
            "outputs: maybe\n"
        )
        client = create_app(graph_dir, templates_dir=templates).test_client()
        client.put("/api/graphs/slot/positions", json={"positions": {"maybe.op/grey": [500.0, 9.0]}})
        body = _ok(client.post("/api/graphs/slot/rename", json={"node": "maybe", "new": "perhaps"}))
        assert body["positions"]["perhaps.op/grey"] == [500.0, 9.0]


FOUR_TEXT = f"""\
stream: !class:recordstream.core.stream.Stream
  ops:
    - !class:{PROBE} {{label: a}}
    - !class:{PROBE} {{label: b}}
    - !class:{PROBE} {{label: c}}
    - !class:{PROBE} {{label: d}}
"""


class TestListMembersAfterAChangeAreReKeyed:
    """A list member's name is its position: converting or pasting members shifts the LATER members' names, and
    their positions and group memberships follow (FIXES K6)."""

    @pytest.fixture
    def four(self, graph_dir: Path, templates: Path) -> Any:
        (graph_dir / "four.yaml").write_text(FOUR_TEXT)
        client = create_app(graph_dir, templates_dir=templates).test_client()
        placed = {"stream.ops[2]": [300.0, 7.0], "stream.ops[3]": [400.0, 7.0]}
        client.put("/api/graphs/four/positions", json={"positions": placed})
        group = {"id": "g", "title": "t", "color": "#fff", "nodes": ["stream.ops[3]"]}
        client.put("/api/graphs/four/groups", json={"groups": [group]})
        return client

    def test_convert_moves_the_later_members_down_and_undo_back(self, four: Any) -> None:
        body = _ok(_convert(four, "four", ["stream.ops[0]", "stream.ops[1]"], "x"))
        assert body["positions"]["stream.ops[1]"] == [300.0, 7.0]
        assert body["positions"]["stream.ops[2]"] == [400.0, 7.0]
        assert body["groups"][0]["nodes"] == ["stream.ops[2]"]
        undone = four.post("/api/graphs/four/undo").get_json()
        assert undone["positions"]["stream.ops[2]"] == [300.0, 7.0]
        assert undone["positions"]["stream.ops[3]"] == [400.0, 7.0]
        assert undone["groups"][0]["nodes"] == ["stream.ops[3]"]
        redone = four.post("/api/graphs/four/redo").get_json()
        assert redone["positions"]["stream.ops[2]"] == [400.0, 7.0] and redone["groups"][0]["nodes"] == [
            "stream.ops[2]"
        ]

    def test_paste_moves_the_later_members_up_and_undo_back(self, four: Any) -> None:
        clip = four.post("/api/graphs/four/copy", json={"nodes": ["stream.ops[0]", "stream.ops[1]"]}).get_json()["text"]
        body = _ok(four.post("/api/graphs/four/paste", json={"text": clip, "after": "stream.ops[0]"}))
        assert body["positions"]["stream.ops[4]"] == [300.0, 7.0]
        assert body["positions"]["stream.ops[5]"] == [400.0, 7.0]
        assert body["groups"][0]["nodes"] == ["stream.ops[5]"]
        undone = four.post("/api/graphs/four/undo").get_json()
        assert undone["positions"]["stream.ops[2]"] == [300.0, 7.0]
        assert undone["positions"]["stream.ops[3]"] == [400.0, 7.0]
        assert undone["groups"][0]["nodes"] == ["stream.ops[3]"]
