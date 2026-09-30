"""Templates, like ComfyUI's: a saved selection of steps any graph can insert a COPY of.

A template is a STEPS FILE — a bare steps mapping, the grammar of a ``flow:`` mapping, comments allowed —
named ``<template>.steps.yaml`` in the templates folder (``Studio(templates_dir=…)``; the CLI's
``--templates-dir``; default ``~/.streamstudio/templates``, created on the first save). It belongs to no
graph, so saving one writes the file at once: no Save, and the graph it came from stays clean.

* ``GET /api/templates`` → ``[{name, path, steps, doc}]`` (``doc`` = the file's first comment line);
* ``POST /api/templates {name, graph, nodes, overwrite?}`` → one SUBGRAPH step saves its inner steps,
  flow steps save themselves (their own lines, comments kept);
* ``DELETE /api/templates/<name>``.

Inserting a template into a graph is a separate route (it builds a subgraph step): ``test_app_subgraph.py``.
"""

from pathlib import Path
from typing import Any, List

import pytest

from streamstudio.app import create_app
from streamstudio.document import Document

PROBE = "recordstream.ops.debug.PrintRecordOp"

FLOW_TEXT = f"""\
flow:
  probe:
    op: !class:{PROBE} {{label: probe}}
  grey:
    op: !class:{PROBE} {{label: grey}}   # keeps this comment
  scaled:
    op: !class:{PROBE} {{label: scaled}}
    from: grey
outputs: scaled
"""

SUBGRAPH_TEXT = f"""\
flow:
  prep:
    op: !class:recordstream.flow.subgraph.Subgraph
      steps:
        # the inside
        grey:
          op: !class:{PROBE} {{label: grey}}
        scaled:
          op: !class:{PROBE} {{label: scaled}}
      result: scaled
  shared:
    op: !class:recordstream.flow.subgraph.Subgraph
      steps: {{include: parts/shared.steps.yaml}}
  tail:
    op: !class:{PROBE} {{label: tail}}
outputs: tail
"""

SHARED_STEPS = f"# shared steps\nmono:\n  op: !class:{PROBE} {{label: mono}}\n"

PIPE_TEXT = f"stream: !class:recordstream.core.stream.Stream\n  ops:\n    - !class:{PROBE} {{}}\n"


def _error(response: Any) -> str:
    body = response.get_json()
    assert isinstance(body, dict) and set(body) == {"error"}, body
    return str(body["error"])


@pytest.fixture
def graph_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "graphs"
    (folder / "parts").mkdir(parents=True)
    (folder / "demo.yaml").write_text(FLOW_TEXT)
    (folder / "sub.yaml").write_text(SUBGRAPH_TEXT)
    (folder / "pipe.yaml").write_text(PIPE_TEXT)
    (folder / "parts" / "shared.steps.yaml").write_text(SHARED_STEPS)
    return folder


@pytest.fixture
def templates(tmp_path: Path) -> Path:
    return tmp_path / "home" / "templates"


@pytest.fixture
def client(graph_dir: Path, templates: Path) -> Any:  # flask.testing.FlaskClient
    return create_app(graph_dir, templates_dir=templates).test_client()


def _save(client: Any, name: str, graph: str, nodes: List[str], **extra: Any) -> Any:  # a Flask test response
    return client.post("/api/templates", json={"name": name, "graph": graph, "nodes": nodes, **extra})


class TestTheStore:
    def test_no_folder_yet_means_no_templates(self, client: Any, templates: Path) -> None:
        assert not templates.exists()
        response = client.get("/api/templates")
        assert response.status_code == 200
        assert response.get_json() == []

    def test_flow_steps_save_at_once_into_a_folder_made_on_the_first_save(
        self, client: Any, graph_dir: Path, templates: Path
    ) -> None:
        response = _save(client, "front", "demo", ["scaled", "grey"])
        assert response.status_code == 200, response.get_json()
        path = templates / "front.steps.yaml"
        doc = f"front: grey, scaled — saved from {graph_dir / 'demo.yaml'}"
        assert response.get_json() == {"name": "front", "path": str(path), "steps": ["grey", "scaled"], "doc": doc}
        assert path.read_text() == (
            f"# {doc}\n"
            "grey:\n"
            f"  op: !class:{PROBE} {{label: grey}}   # keeps this comment\n"
            "scaled:\n"
            f"  op: !class:{PROBE} {{label: scaled}}\n"
            "  from: grey\n"
        )
        assert client.get("/api/templates").get_json() == [response.get_json()]
        assert client.get("/api/graphs/demo").get_json()["dirty"] is False  # no graph was edited

    def test_a_subgraph_step_saves_its_inner_steps(self, client: Any, templates: Path) -> None:
        body = _save(client, "prep", "sub", ["prep"]).get_json()
        assert body["steps"] == ["grey", "scaled"]
        text = (templates / "prep.steps.yaml").read_text()
        assert text.splitlines()[1:] == [
            "# the inside",
            "grey:",
            f"  op: !class:{PROBE} {{label: grey}}",
            "scaled:",
            f"  op: !class:{PROBE} {{label: scaled}}",
        ]

    def test_a_subgraph_that_includes_its_steps_saves_the_included_file(self, client: Any, templates: Path) -> None:
        body = _save(client, "shared", "sub", ["shared"]).get_json()
        assert body["steps"] == ["mono"]
        assert (templates / "shared.steps.yaml").read_text().split("\n", 1)[1] == SHARED_STEPS

    def test_a_subgraph_whose_steps_are_written_on_one_line_saves_that_mapping(
        self, graph_dir: Path, templates: Path
    ) -> None:
        (graph_dir / "inline.yaml").write_text(
            "flow:\n"
            "  prep:\n"
            "    op: !class:recordstream.flow.subgraph.Subgraph\n"
            f"      steps: {{grey: {{op: !class:{PROBE} {{label: grey}}}}}}\n"
            "  tail:\n"
            f"    op: !class:{PROBE} {{}}\n"
            "outputs: tail\n"
        )
        client = create_app(graph_dir, templates_dir=templates).test_client()
        body = _save(client, "inline", "inline", ["prep"]).get_json()
        assert body["steps"] == ["grey"]
        assert (templates / "inline.steps.yaml").read_text().split("\n", 1)[1] == (
            f"{{grey: {{op: !class:{PROBE} {{label: grey}}}}}}\n"
        )

    def test_a_subgraph_including_a_missing_steps_file_is_refused_naming_it(
        self, graph_dir: Path, templates: Path
    ) -> None:
        (graph_dir / "parts" / "shared.steps.yaml").unlink()
        client = create_app(graph_dir, templates_dir=templates).test_client()
        response = _save(client, "shared", "sub", ["shared"])
        assert response.status_code == 400
        assert "the steps file 'parts/shared.steps.yaml' does not exist" in _error(response)

    def test_a_template_is_a_steps_file_a_flow_can_include(self, client: Any, templates: Path, tmp_path: Path) -> None:
        _save(client, "front", "demo", ["grey", "scaled"])
        user = tmp_path / "user.yaml"
        user.write_text(f"flow:\n  include: {templates / 'front.steps.yaml'}\noutputs: scaled\n")
        document = Document.open(user)
        assert [node.name for node in document.model.nodes] == ["grey", "scaled"]
        assert document.check() == []

    def test_the_first_comment_line_is_the_doc_and_the_list_is_sorted(self, client: Any, templates: Path) -> None:
        templates.mkdir(parents=True)
        (templates / "zeta.steps.yaml").write_text(f"\n# does zeta things\n# more\nz:\n  op: !class:{PROBE} {{}}\n")
        (templates / "alpha.steps.yaml").write_text(f"a:\n  op: !class:{PROBE} {{}}\n")
        (templates / "notes.yaml").write_text("not: a template\n")
        listed = client.get("/api/templates").get_json()
        assert [(entry["name"], entry["steps"], entry["doc"]) for entry in listed] == [
            ("alpha", ["a"], ""),
            ("zeta", ["z"], "does zeta things"),
        ]

    def test_a_template_that_exists_is_kept_unless_overwrite_is_asked(self, client: Any, templates: Path) -> None:
        _save(client, "front", "demo", ["grey"])
        first = (templates / "front.steps.yaml").read_text()
        response = _save(client, "front", "demo", ["scaled"])
        assert response.status_code == 400
        assert _error(response) == "a template named 'front' exists — pass overwrite: true to replace it"
        assert (templates / "front.steps.yaml").read_text() == first
        assert _save(client, "front", "demo", ["scaled"], overwrite=True).get_json()["steps"] == ["scaled"]

    def test_list_members_are_not_a_template(self, client: Any) -> None:
        response = _save(client, "x", "pipe", ["stream.ops[0]"])
        assert response.status_code == 400
        assert _error(response) == "a template holds flow steps — 'stream.ops[0]' is a list member"

    @pytest.mark.parametrize("name", ["", "a/b", ".hidden", "a b/c"])
    def test_a_name_that_cannot_be_a_file_stem_is_refused(self, client: Any, name: str) -> None:
        response = _save(client, name, "demo", ["grey"])
        assert response.status_code == 400
        assert repr(name) in _error(response)

    def test_an_unknown_graph_or_node_is_404(self, client: Any) -> None:
        assert _save(client, "x", "nosuch", ["grey"]).status_code == 404
        assert _save(client, "x", "demo", ["nosuch"]).status_code == 404

    def test_delete_removes_the_file_and_answers_what_is_left(self, client: Any, templates: Path) -> None:
        _save(client, "front", "demo", ["grey"])
        _save(client, "back", "demo", ["scaled"])
        response = client.delete("/api/templates/front")
        assert response.status_code == 200
        assert [entry["name"] for entry in response.get_json()] == ["back"]
        assert not (templates / "front.steps.yaml").exists()

    def test_deleting_an_unknown_template_is_404_naming_the_templates(self, client: Any) -> None:
        _save(client, "back", "demo", ["scaled"])
        response = client.delete("/api/templates/nosuch")
        assert response.status_code == 404
        assert "'nosuch'" in _error(response) and "back" in _error(response)


class TestMoreSubgraphsSaveTheirSteps:
    def test_a_subgraph_in_a_slot_saves_its_inner_steps(self, graph_dir: Path, templates: Path) -> None:
        """``maybe.op`` is a subgraph node like any other (measured before: 400 "'maybe.op' is not a flow step or a
        list member — only those can be copied", while every edit inside it worked)."""
        (graph_dir / "slot.yaml").write_text(
            "flow:\n"
            "  maybe:\n"
            "    op: !class:recordstream.ops.random_apply.RandomApply\n"
            "      probability: 1.0\n"
            "      op: !class:recordstream.flow.subgraph.Subgraph\n"
            "        steps:\n"
            "          grey:\n"
            f"            op: !class:{PROBE} {{label: grey}}   # to grey\n"
            "          scaled:\n"
            f"            op: !class:{PROBE} {{label: scaled}}\n"
            "        result: scaled\n"
            "outputs: maybe\n"
        )
        client = create_app(graph_dir, templates_dir=templates).test_client()
        response = _save(client, "slot", "slot", ["maybe.op"])
        assert response.status_code == 200, response.get_json()
        assert response.get_json()["steps"] == ["grey", "scaled"]
        assert (templates / "slot.steps.yaml").read_text().split("\n", 1)[1] == (
            f"grey:\n  op: !class:{PROBE} {{label: grey}}   # to grey\n"
            f"scaled:\n  op: !class:{PROBE} {{label: scaled}}\n"
        )

    def test_an_include_inside_the_saved_steps_is_saved_inline(self, graph_dir: Path, templates: Path) -> None:
        """A template lives in the templates folder, where a relative include written beside the graph names
        nothing — so the steps it includes are saved inline, as a copy is."""
        (graph_dir / "parts" / "outer.steps.yaml").write_text(
            "inner:\n  op: !class:recordstream.flow.subgraph.Subgraph\n    steps: {include: shared.steps.yaml}\n"
        )
        (graph_dir / "nest.yaml").write_text(SUBGRAPH_TEXT.replace("parts/shared.steps.yaml", "parts/outer.steps.yaml"))
        client = create_app(graph_dir, templates_dir=templates).test_client()
        assert _save(client, "nest", "nest", ["shared"]).status_code == 200
        saved = (templates / "nest.steps.yaml").read_text()
        assert "include:" not in saved
        assert f"      mono:\n        op: !class:{PROBE} {{label: mono}}\n" in saved
