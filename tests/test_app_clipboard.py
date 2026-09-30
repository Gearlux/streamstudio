"""Copy and paste as TEXT — the clipboard holds the document's own lines, so a paste is a copy of them.

``POST …/copy {nodes}`` answers the YAML of the selected nodes: flow steps as a steps mapping (their own
lines, every comment kept, dedented to the left edge) or ``ops`` list members as a YAML sequence. ``POST
…/paste {text, after}`` inserts that text after ``after`` as ONE edit (one undo walks it back):

* a pasted step whose name is taken is renamed (``grey`` → ``grey_2``) and every reference AMONG the
  pasted steps follows the rename (``from:``, ``merge_from:``, ``bind:``);
* a pasted step reading a step that was NOT pasted reads the step before it in the paste instead — the
  first one reads ``after`` — so the block lands as a chain AFTER ``after``, whatever it read where it
  was copied from;
* the step that read ``after`` now reads the last pasted one, and the flow's output moves to it when
  ``after`` was the output tail — the ``add_step`` rule.

Because the clipboard is text, a copy pastes into ANOTHER graph as well. The documents use
recordstream's own ``PrintRecordOp`` (it reads and writes no entry), so every insertion derives clean.
"""

from pathlib import Path
from typing import Any, Dict, List

import pytest

from streamstudio.app import create_app

PROBE = "recordstream.ops.debug.PrintRecordOp"

FLOW_TEXT = f"""\
flow:
  # the head of the chain
  probe:
    op: !class:{PROBE} {{label: probe}}
  grey:
    op: !class:{PROBE} {{label: grey}}   # keeps this comment
    # a comment below grey travels with it
  scaled:
    op: !class:{PROBE} {{label: scaled}}
    from: grey
  tail:
    op: !class:{PROBE} {{label: tail}}
    from: scaled
outputs: tail
"""

GREY_AND_SCALED = f"""\
grey:
  op: !class:{PROBE} {{label: grey}}   # keeps this comment
  # a comment below grey travels with it
scaled:
  op: !class:{PROBE} {{label: scaled}}
  from: grey
"""

PIPE_TEXT = f"""\
stream: !class:recordstream.core.stream.Stream
  ops:
    - !class:{PROBE} {{label: a}}   # first
    - !class:{PROBE}
      label: b
    - !class:{PROBE} {{label: c}}
"""

SUBGRAPH_TEXT = f"""\
flow:
  prep:
    op: !class:recordstream.flow.subgraph.Subgraph
      steps:
        grey:
          op: !class:{PROBE} {{label: grey}}   # inside
        scaled:
          op: !class:{PROBE} {{label: scaled}}
      result: scaled
  tail:
    op: !class:{PROBE} {{label: tail}}
outputs: tail
"""


def _error(response: Any) -> str:
    body = response.get_json()
    assert isinstance(body, dict) and set(body) == {"error"}, body
    return str(body["error"])


@pytest.fixture
def graph_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "graphs"
    folder.mkdir()
    (folder / "demo.yaml").write_text(FLOW_TEXT)
    (folder / "pipe.yaml").write_text(PIPE_TEXT)
    (folder / "other.yaml").write_text(f"flow:\n  first:\n    op: !class:{PROBE} {{label: first}}\noutputs: first\n")
    return folder


@pytest.fixture
def client(graph_dir: Path, tmp_path: Path) -> Any:  # flask.testing.FlaskClient
    return create_app(graph_dir, templates_dir=tmp_path / "templates").test_client()


def _copy(client: Any, graph: str, nodes: List[str]) -> Dict[str, Any]:
    response = client.post(f"/api/graphs/{graph}/copy", json={"nodes": nodes})
    assert response.status_code == 200, response.get_json()
    body: Dict[str, Any] = response.get_json()
    return body


def _paste(client: Any, graph: str, text: str, after: str) -> Any:
    return client.post(f"/api/graphs/{graph}/paste", json={"text": text, "after": after})


# ------------------------------------------------------------------------------------------------
# copy
# ------------------------------------------------------------------------------------------------


class TestCopy:
    def test_flow_steps_copy_as_their_own_lines_dedented_with_every_comment(self, client: Any) -> None:
        assert _copy(client, "demo", ["grey", "scaled"]) == {"text": GREY_AND_SCALED, "kind": "steps"}

    def test_the_copy_follows_the_document_order_not_the_selection_order(self, client: Any) -> None:
        assert _copy(client, "demo", ["scaled", "grey"])["text"] == GREY_AND_SCALED

    def test_list_members_copy_as_a_yaml_sequence(self, client: Any) -> None:
        body = _copy(client, "pipe", ["stream.ops[2]", "stream.ops[1]"])
        assert body == {"text": f"- !class:{PROBE}\n  label: b\n- !class:{PROBE} {{label: c}}\n", "kind": "ops"}
        assert _copy(client, "pipe", ["stream.ops[0]"])["text"] == f"- !class:{PROBE} {{label: a}}   # first\n"

    def test_a_subgraph_step_copies_its_whole_block(self, graph_dir: Path, tmp_path: Path) -> None:
        (graph_dir / "sub.yaml").write_text(SUBGRAPH_TEXT)
        client = create_app(graph_dir, templates_dir=tmp_path / "templates").test_client()
        text = _copy(client, "sub", ["prep"])["text"]
        assert text == "".join(line[2:] + "\n" for line in SUBGRAPH_TEXT.splitlines()[1:9])
        assert "# inside" in text and "result: scaled" in text

    def test_copy_does_not_change_the_graph(self, client: Any, graph_dir: Path) -> None:
        _copy(client, "demo", ["grey"])
        body = client.get("/api/graphs/demo").get_json()
        assert body["dirty"] is False and body["text"] == FLOW_TEXT

    def test_steps_and_list_members_together_are_refused(self, graph_dir: Path, tmp_path: Path) -> None:
        (graph_dir / "both.yaml").write_text(FLOW_TEXT + PIPE_TEXT)
        client = create_app(graph_dir, templates_dir=tmp_path / "templates").test_client()
        response = client.post("/api/graphs/both/copy", json={"nodes": ["grey", "stream.ops[0]"]})
        assert response.status_code == 400
        assert _error(response) == "copy takes flow steps or list members, not both"

    def test_a_node_that_is_neither_is_refused_naming_it(self, client: Any) -> None:
        response = client.post("/api/graphs/pipe/copy", json={"nodes": ["stream"]})
        assert response.status_code == 400
        assert _error(response) == "'stream' is not a flow step or a list member — only those can be copied"

    def test_an_unknown_node_is_404_and_an_empty_selection_400(self, client: Any) -> None:
        response = client.post("/api/graphs/demo/copy", json={"nodes": ["nosuch"]})
        assert response.status_code == 404
        assert "nosuch" in _error(response) and "grey" in _error(response)
        response = client.post("/api/graphs/demo/copy", json={"nodes": []})
        assert response.status_code == 400
        assert _error(response) == "copy needs at least one node"


# ------------------------------------------------------------------------------------------------
# paste steps
# ------------------------------------------------------------------------------------------------


class TestPasteSteps:
    def test_a_taken_name_is_renamed_and_the_references_among_the_pasted_follow(self, client: Any) -> None:
        response = _paste(client, "demo", GREY_AND_SCALED, after="tail")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["pasted"] == ["grey_2", "scaled_2"]
        assert body["dirty"] is True
        assert body["text"].endswith(
            "    from: scaled\n"
            "  grey_2:\n"
            f"    op: !class:{PROBE} {{label: grey}}   # keeps this comment\n"
            "    # a comment below grey travels with it\n"
            "  scaled_2:\n"
            f"    op: !class:{PROBE} {{label: scaled}}\n"
            "    from: grey_2\n"
            "outputs: scaled_2\n"  # tail was the output: the output moves to the last pasted step
        )
        assert body["model"]["problems"] == []

    def test_the_step_that_read_after_now_reads_the_last_pasted_one(self, client: Any) -> None:
        body = _paste(client, "demo", GREY_AND_SCALED, after="grey").get_json()
        text = body["text"]
        assert "  grey_2:\n" in text and "  scaled_2:\n" in text
        # scaled read grey explicitly: it now reads the last pasted step
        assert f"  scaled:\n    op: !class:{PROBE} {{label: scaled}}\n    from: scaled_2\n" in text
        assert "\noutputs: tail\n" in text  # grey was not the tail: the output stays
        records = {(w["source"], w["target"]) for w in body["model"]["wires"] if w["kind"] == "record"}
        assert {("grey", "grey_2"), ("grey_2", "scaled_2"), ("scaled_2", "scaled")} <= records

    def test_a_step_reading_something_not_pasted_reads_the_chain_before_it(self, client: Any) -> None:
        """``scaled`` read ``grey``; pasted alone after ``tail`` it reads ``tail`` — the block lands AFTER the
        anchor the user chose, not beside the step it was copied beside."""
        text = _copy(client, "demo", ["scaled"])["text"]
        body = _paste(client, "demo", text, after="tail").get_json()
        assert f"  scaled_2:\n    op: !class:{PROBE} {{label: scaled}}\n    from: tail\n" in body["text"]

    def test_a_paste_is_one_edit_for_undo(self, client: Any) -> None:
        _paste(client, "demo", GREY_AND_SCALED, after="tail")
        undone = client.post("/api/graphs/demo/undo").get_json()
        assert undone["changed"] is True and undone["text"] == FLOW_TEXT and undone["dirty"] is False

    def test_a_copy_pastes_into_another_graph_keeping_free_names(self, client: Any) -> None:
        text = _copy(client, "demo", ["grey", "scaled"])["text"]
        body = _paste(client, "other", text, after="first").get_json()
        assert body["pasted"] == ["grey", "scaled"]
        assert "\noutputs: scaled\n" in body["text"]
        assert body["model"]["problems"] == []

    def test_a_name_suffix_skips_names_the_paste_itself_brings(self, client: Any) -> None:
        clip = f"grey:\n  op: !class:{PROBE} {{}}\ngrey_2:\n  op: !class:{PROBE} {{}}\n"
        body = _paste(client, "demo", clip, after="tail").get_json()
        assert body["pasted"] == ["grey_3", "grey_2"]

    def test_a_subgraph_block_pastes_whole(self, graph_dir: Path, tmp_path: Path) -> None:
        pytest.importorskip("recordstream.flow.subgraph")
        (graph_dir / "sub.yaml").write_text(SUBGRAPH_TEXT)
        client = create_app(graph_dir, templates_dir=tmp_path / "templates").test_client()
        text = _copy(client, "sub", ["prep"])["text"]
        body = _paste(client, "sub", text, after="tail").get_json()
        assert body["pasted"] == ["prep_2"]
        assert "  prep_2:\n    op: !class:recordstream.flow.subgraph.Subgraph\n" in body["text"]
        assert body["text"].count("# inside") == 2

    def test_a_paste_that_breaks_the_graph_is_refused_and_changes_nothing(self, client: Any) -> None:
        clip = "ghost:\n  op: !class:no.such.Class {}\n"
        response = _paste(client, "demo", clip, after="tail")
        assert response.status_code == 400
        assert "cannot resolve the class 'no.such.Class'" in _error(response)
        body = client.get("/api/graphs/demo").get_json()
        assert body["text"] == FLOW_TEXT and body["dirty"] is False

    @pytest.mark.parametrize(
        "clip, message",
        [
            ("just a sentence", "the pasted text is not flow steps or a list — it is a single value"),
            ("", "the pasted text is not flow steps or a list — it is nothing"),
            ("{}", "the pasted text holds no steps"),
            ("a: [1\n", "the pasted text is not YAML"),
            (f"include: x.steps.yaml\ngrey:\n  op: !class:{PROBE} {{}}\n", "the pasted text holds an include:"),
        ],
    )
    def test_a_text_that_is_not_steps_is_refused(self, client: Any, clip: str, message: str) -> None:
        response = _paste(client, "demo", clip, after="tail")
        assert response.status_code == 400
        assert _error(response).startswith(message)

    def test_steps_pasted_after_a_list_member_are_refused(self, client: Any) -> None:
        response = _paste(client, "pipe", GREY_AND_SCALED, after="stream.ops[0]")
        assert response.status_code == 400
        assert (
            _error(response) == "the clipboard holds flow steps; 'stream.ops[0]' is not a flow step (the steps are: [])"
        )

    def test_pasting_after_an_unknown_node_is_404(self, client: Any) -> None:
        assert _paste(client, "demo", GREY_AND_SCALED, after="nosuch").status_code == 404


# ------------------------------------------------------------------------------------------------
# paste list members
# ------------------------------------------------------------------------------------------------


class TestPasteListMembers:
    def test_members_paste_after_the_anchor_member_as_one_edit(self, client: Any) -> None:
        clip = _copy(client, "pipe", ["stream.ops[1]", "stream.ops[2]"])["text"]
        response = _paste(client, "pipe", clip, after="stream.ops[0]")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["pasted"] == ["stream.ops[1]", "stream.ops[2]"]
        assert body["text"] == (
            "stream: !class:recordstream.core.stream.Stream\n"
            "  ops:\n"
            f"    - !class:{PROBE} {{label: a}}   # first\n"
            f"    - !class:{PROBE}\n"
            "      label: b\n"
            f"    - !class:{PROBE} {{label: c}}\n"
            f"    - !class:{PROBE}\n"
            "      label: b\n"
            f"    - !class:{PROBE} {{label: c}}\n"
        )
        assert [n["name"] for n in body["model"]["nodes"] if n["name"].startswith("stream.ops")] == [
            f"stream.ops[{i}]" for i in range(5)
        ]
        assert client.post("/api/graphs/pipe/undo").get_json()["text"] == PIPE_TEXT

    def test_a_member_pastes_after_the_last_one(self, client: Any) -> None:
        body = _paste(client, "pipe", f"- !class:{PROBE} {{label: d}}\n", after="stream.ops[2]").get_json()
        assert body["pasted"] == ["stream.ops[3]"]
        assert body["text"] == PIPE_TEXT + f"    - !class:{PROBE} {{label: d}}\n"

    def test_a_top_level_list_is_a_list_too(self, graph_dir: Path, tmp_path: Path) -> None:
        """``ops:`` as a TOP-LEVEL key (a list of markers the document holds on its own) names its members
        ``ops[i]``; copy and paste reach it the same way."""
        (graph_dir / "top.yaml").write_text(f"ops:\n  - !class:{PROBE} {{label: a}}\n  - !class:{PROBE} {{label: b}}\n")
        client = create_app(graph_dir, templates_dir=tmp_path / "templates").test_client()
        clip = _copy(client, "top", ["ops[1]"])
        assert clip == {"text": f"- !class:{PROBE} {{label: b}}\n", "kind": "ops"}
        body = _paste(client, "top", clip["text"], after="ops[0]").get_json()
        assert body["pasted"] == ["ops[1]"]
        assert body["text"].count("label: b") == 2

    def test_a_list_pasted_after_a_flow_step_is_refused(self, client: Any) -> None:
        response = _paste(client, "demo", f"- !class:{PROBE} {{}}\n", after="grey")
        assert response.status_code == 400
        assert (
            _error(response) == "the clipboard holds list members; 'grey' is not a list member — paste them after one"
        )

    def test_a_list_in_flow_style_is_refused(self, graph_dir: Path, tmp_path: Path) -> None:
        (graph_dir / "inline.yaml").write_text(
            f"stream: !class:recordstream.core.stream.Stream\n  ops: [!class:{PROBE} {{label: a}}]\n"
        )
        client = create_app(graph_dir, templates_dir=tmp_path / "templates").test_client()
        response = _paste(client, "inline", f"- !class:{PROBE} {{}}\n", after="stream.ops[0]")
        assert response.status_code == 400
        assert "is written on one line" in _error(response)


# ------------------------------------------------------------------------------------------------
# a copy is a COPY: an included subgraph's steps come along inline
# ------------------------------------------------------------------------------------------------

SUBGRAPH_CLASS = "recordstream.flow.subgraph.Subgraph"
CONVERT = "recordstream.ops.image.ConvertMode"
STEPS_FILE = f"# the shared prep\ngrey:\n  op: !class:{CONVERT} {{mode: %s}}   # to grey\n"
INCLUDES = f"""\
flow:
  prep:
    op: !class:{SUBGRAPH_CLASS}
      steps: {{include: prep.steps.yaml}}   # shared
  tail:
    op: !class:{PROBE} {{label: tail}}
outputs: tail
"""
INLINED = f"""\
prep:
  op: !class:{SUBGRAPH_CLASS}
    steps:   # shared
      # the shared prep
      grey:
        op: !class:{CONVERT} {{mode: L}}   # to grey
"""


def _mode(body: Dict[str, Any], subgraph: str) -> str:
    inside = body["model"]["inside"][subgraph]["nodes"]
    return str(next(node for node in inside if node["name"] == f"{subgraph}/grey")["params"]["mode"]["value"])


class TestCopyingAnIncludedSubgraph:
    """DESIGN §0.3.1: Copy/Paste pastes a full, independent copy of the block — sharing one steps file is what
    *Insert steps file…* is for (measured before: the clipboard held ``steps: {include: prep.steps.yaml}``, so
    in another folder it bound to THAT folder's file, and in the same graph editing the copy edited the original)."""

    @pytest.fixture
    def folders(self, tmp_path: Path) -> Any:
        mine, other = tmp_path / "g", tmp_path / "other"
        mine.mkdir()
        other.mkdir()
        (mine / "prep.steps.yaml").write_text(STEPS_FILE % "L")
        (mine / "a.yaml").write_text(INCLUDES)
        (other / "prep.steps.yaml").write_text(STEPS_FILE % "RGB")
        (other / "b.yaml").write_text(f"flow:\n  first:\n    op: !class:{PROBE} {{label: first}}\noutputs: first\n")
        client = create_app(mine, templates_dir=tmp_path / "templates").test_client()
        assert client.post("/api/graphs/open", json={"path": str(other / "b.yaml")}).status_code == 200
        return client, mine, other

    def test_the_clipboard_holds_the_included_steps_inline_with_their_comments(self, folders: Any) -> None:
        client, _mine, _other = folders
        assert _copy(client, "a", ["prep"]) == {"text": INLINED, "kind": "steps"}

    def test_pasted_into_a_graph_in_another_folder_it_keeps_its_own_steps(self, folders: Any) -> None:
        client, _mine, _other = folders
        pasted = client.post(
            "/api/graphs/b/paste", json={"text": _copy(client, "a", ["prep"])["text"], "after": "first"}
        )
        assert pasted.status_code == 200, pasted.get_json()
        assert _mode(pasted.get_json(), "prep") == "L" and pasted.get_json()["model"]["problems"] == []

    def test_editing_the_copy_never_changes_the_original_or_its_file(self, folders: Any) -> None:
        client, mine, _other = folders
        assert _paste(client, "a", _copy(client, "a", ["prep"])["text"], "prep").status_code == 200
        edited = client.post("/api/graphs/a/param", json={"node": "prep_2/grey", "param": "mode", "value": "RGB"})
        assert edited.status_code == 200, edited.get_json()
        assert _mode(edited.get_json(), "prep") == "L" and _mode(edited.get_json(), "prep_2") == "RGB"
        assert client.post("/api/graphs/a/save").status_code == 200
        assert (mine / "prep.steps.yaml").read_text() == STEPS_FILE % "L"

    def test_an_include_inside_the_included_steps_is_read_from_that_files_folder(self, tmp_path: Path) -> None:
        folder, parts = tmp_path / "g", tmp_path / "g" / "parts"
        parts.mkdir(parents=True)
        (parts / "inner.steps.yaml").write_text(f"mono:\n  op: !class:{PROBE} {{label: mono}}\n")
        (parts / "outer.steps.yaml").write_text(
            f"inner:\n  op: !class:{SUBGRAPH_CLASS}\n    steps:\n      include: inner.steps.yaml\n"
        )
        (folder / "a.yaml").write_text(INCLUDES.replace("prep.steps.yaml", "parts/outer.steps.yaml"))
        client = create_app(folder, templates_dir=tmp_path / "templates").test_client()
        text = _copy(client, "a", ["prep"])["text"]
        assert "include" not in text
        assert f"\n            mono:\n              op: !class:{PROBE} {{label: mono}}\n" in text

    def test_steps_written_inline_copy_as_they_are(self, graph_dir: Path, tmp_path: Path) -> None:
        (graph_dir / "sub.yaml").write_text(SUBGRAPH_TEXT)
        client = create_app(graph_dir, templates_dir=tmp_path / "templates").test_client()
        assert "    steps:\n      grey:" in _copy(client, "sub", ["prep"])["text"]


# ------------------------------------------------------------------------------------------------
# several steps removed as ONE edit (Cut, Delete of a selection)
# ------------------------------------------------------------------------------------------------


def _remove(client: Any, graph: str, nodes: Any) -> Any:
    return client.post(f"/api/graphs/{graph}/steps/remove", json={"nodes": nodes})


class TestRemovingSeveralSteps:
    """``POST …/steps/remove {nodes}`` answers the graph; every step goes in ONE edit — one undo entry — and a
    refusal of any of them removes none (measured before: the page removed one step per request, so a
    Select-all + Delete inside a subgraph removed ``scaled`` and then refused ``grey``, half applied)."""

    def test_the_steps_go_and_one_undo_brings_them_back(self, client: Any) -> None:
        response = _remove(client, "demo", ["grey", "scaled"])
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert [node["name"] for node in body["model"]["nodes"]] == ["probe", "tail"]
        assert "    from: probe\n" in body["text"] and body["dirty"] is True
        undone = client.post("/api/graphs/demo/undo").get_json()
        assert undone["changed"] is True and undone["text"] == FLOW_TEXT
        assert client.post("/api/graphs/demo/undo").get_json()["changed"] is False

    def test_the_order_of_the_selection_does_not_matter(self, client: Any) -> None:
        first = _remove(client, "demo", ["scaled", "grey"]).get_json()["text"]
        client.post("/api/graphs/demo/undo")
        assert _remove(client, "demo", ["grey", "scaled"]).get_json()["text"] == first

    def test_every_step_of_a_subgraph_is_refused_before_anything_is_removed(
        self, graph_dir: Path, tmp_path: Path
    ) -> None:
        (graph_dir / "sub.yaml").write_text(SUBGRAPH_TEXT)
        client = create_app(graph_dir, templates_dir=tmp_path / "templates").test_client()
        response = _remove(client, "sub", ["prep/grey", "prep/scaled"])
        assert response.status_code == 400
        assert _error(response) == (
            "'prep/grey' is the only step of the subgraph 'prep' — nothing would be left for it to return; "
            "remove 'prep' instead"
        )
        body = client.get("/api/graphs/sub").get_json()
        assert body["text"] == SUBGRAPH_TEXT and body["dirty"] is False
        assert client.post("/api/graphs/sub/undo").get_json()["changed"] is False

    def test_a_subgraph_and_a_step_inside_it_remove_the_subgraph(self, graph_dir: Path, tmp_path: Path) -> None:
        (graph_dir / "sub.yaml").write_text(SUBGRAPH_TEXT)
        client = create_app(graph_dir, templates_dir=tmp_path / "templates").test_client()
        response = _remove(client, "sub", ["prep/grey", "prep"])
        assert response.status_code == 200, response.get_json()
        assert [node["name"] for node in response.get_json()["model"]["nodes"]] == ["tail"]

    def test_an_unknown_node_is_404_and_nothing_is_removed(self, client: Any) -> None:
        response = _remove(client, "demo", ["grey", "nosuch"])
        assert response.status_code == 404 and "nosuch" in _error(response)
        assert client.get("/api/graphs/demo").get_json()["text"] == FLOW_TEXT

    @pytest.mark.parametrize("nodes", [[], "grey", [1]])
    def test_the_nodes_must_be_a_non_empty_list_of_names(self, client: Any, nodes: Any) -> None:
        assert _remove(client, "demo", nodes).status_code == 400
