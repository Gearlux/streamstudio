"""The settings form: every parameter (``GET …/form``) or the graph's ``app:`` fields (``GET …/app``).

A graph may declare which of its parameters are its SETTINGS — an ``app:`` block whose ``inputs`` list
``[Class, param]`` pairs (written for the ComfyUI editor's App view, which showed only those). The settings tab
opens on that short form and offers "all parameters"; ``GET …/app`` answers it:

* one field per (node, parameter) in the block's ORDER — a class used by two nodes gives a field on each;
* ``<param>__lo`` / ``<param>__hi`` (the ComfyUI form's two halves of one range field) name ONE field, ``<param>``;
* an entry that names no node of the graph, or a parameter the class does not take, is a located problem in words
  (``file:line:col`` of the entry), never a silent omission;
* the gate a branch sets (``requires``) is never a field — in the narrowed form or in ``GET …/form``.

Measured on the shipped Bluetooth pane: 247 fields on 30 nodes in the full form, 30 in its ``app:`` form (32
entries, two ranges written as ``__lo`` / ``__hi`` pairs).
"""

from pathlib import Path
from typing import Any, Dict, Tuple

import pytest
import yaml
from confluid import configurable
from recordstream import Record

from streamstudio._yamltext import YamlText
from streamstudio.app import create_app

MOD = __name__


@configurable(category="op", group="form-test")
class FormKnobs:
    """Two knobs, one range and a gate — what a pane's form is made of.

    Args:
        level: A threshold.
        width_hz: A width.
        spread: A (min, max) range.
        requires: The flag this step runs on (the gate a branch sets).
    """

    consumes = {"image": "Image"}
    produces = {"image": "Image"}

    def __init__(
        self,
        level: float = 0.5,
        width_hz: float = 1.0e6,
        spread: Tuple[float, float] = (0.1, 0.9),
        requires: str = "",
    ) -> None:
        self.level = float(level)
        self.width_hz = float(width_hz)
        self.spread = spread
        self.requires = requires

    def __call__(self, record: Record) -> Record:
        return record


@configurable(category="op", group="form-test")
class FormOther:
    """A second class.

    Args:
        floor_db: A floor.
    """

    consumes = {"image": "Image"}
    produces = {"image": "Image"}

    def __init__(self, floor_db: float = -90.0) -> None:
        self.floor_db = float(floor_db)

    def __call__(self, record: Record) -> Record:
        return record


PANE_TEXT = f"""\
flow:
  first:
    op: !class:{MOD}.FormKnobs {{level: 0.25}}
  second:
    op: !class:{MOD}.FormOther {{}}
    from: first
  third:
    op: !class:{MOD}.FormKnobs {{}}
    from: second
outputs: third
app:
  inputs:
    - [FormOther, floor_db]
    - [FormKnobs, spread__lo]
    - [FormKnobs, spread__hi]
    - [{MOD}.FormKnobs, level]
    - [FormKnobs, requires]
    - [FormKnobs, nosuch]
    - [Missing, level]
  outputs: [FormKnobs]
"""

PLAIN_TEXT = f"""\
flow:
  only:
    op: !class:{MOD}.FormKnobs {{}}
outputs: only
"""


@pytest.fixture
def graph_dir(tmp_path: Path) -> Path:
    (tmp_path / "pane.yaml").write_text(PANE_TEXT)
    (tmp_path / "plain.yaml").write_text(PLAIN_TEXT)
    return tmp_path


@pytest.fixture
def client(graph_dir: Path) -> Any:  # flask.testing.FlaskClient
    return create_app(graph_dir).test_client()


def _ok(response: Any) -> Dict[str, Any]:
    body = response.get_json()
    assert response.status_code == 200, body
    assert isinstance(body, dict)
    return body


class TestTheFullForm:
    def test_the_gate_is_never_a_field(self, client: Any) -> None:
        form = _ok(client.get("/api/graphs/pane/form"))
        assert set(form["first"]) == {"level", "width_hz", "spread"}
        assert set(form["second"]) == {"floor_db"}

    def test_the_gate_stays_on_the_model_where_the_gate_wire_is_drawn_from(self, client: Any) -> None:
        graph = _ok(client.get("/api/graphs/pane"))
        first = next(node for node in graph["model"]["nodes"] if node["name"] == "first")
        assert "requires" in first["params"]


class TestTheAppForm:
    def test_the_fields_follow_the_blocks_order_one_per_node(self, client: Any, graph_dir: Path) -> None:
        body = _ok(client.get("/api/graphs/pane/app"))
        assert body["declared"] is True
        pane = graph_dir / "pane.yaml"
        assert body["fields"] == [
            {"node": "second", "param": "floor_db", "loc": f"{pane}:13:7"},
            {"node": "first", "param": "spread", "loc": f"{pane}:14:7"},
            {"node": "third", "param": "spread", "loc": f"{pane}:14:7"},
            {"node": "first", "param": "level", "loc": f"{pane}:16:7"},
            {"node": "third", "param": "level", "loc": f"{pane}:16:7"},
        ]

    def test_the_form_holds_those_fields_in_the_full_forms_shape(self, client: Any) -> None:
        body = _ok(client.get("/api/graphs/pane/app"))
        full = _ok(client.get("/api/graphs/pane/form"))
        assert body["form"] == {
            "first": {"spread": full["first"]["spread"], "level": full["first"]["level"]},
            "second": {"floor_db": full["second"]["floor_db"]},
            "third": {"spread": full["third"]["spread"], "level": full["third"]["level"]},
        }

    def test_an_entry_that_names_nothing_is_a_located_problem_in_words(self, client: Any, graph_dir: Path) -> None:
        pane = graph_dir / "pane.yaml"
        problems = _ok(client.get("/api/graphs/pane/app"))["problems"]
        assert problems == [
            {
                "message": f"{pane}:17:7: the app input [FormKnobs, requires] is the gate a branch sets, "
                "not a setting — it is never a field",
                "loc": f"{pane}:17:7",
            },
            {
                "message": f"{pane}:18:7: the app input [FormKnobs, nosuch] names no parameter of FormKnobs "
                "(its parameters are: level, spread, width_hz)",
                "loc": f"{pane}:18:7",
            },
            {
                "message": f"{pane}:19:7: the app input [Missing, level] names no node of this graph "
                "(its classes are: FormKnobs, FormOther)",
                "loc": f"{pane}:19:7",
            },
        ]

    def test_a_graph_without_an_app_block_declares_nothing(self, client: Any) -> None:
        assert _ok(client.get("/api/graphs/plain/app")) == {
            "declared": False,
            "graph": "editable",
            "fields": [],
            "form": {},
            "problems": [],
        }

    def test_the_form_follows_what_the_editor_holds_not_the_file(self, client: Any, graph_dir: Path) -> None:
        text = PANE_TEXT.replace("    - [Missing, level]\n", "    - [FormOther, floor_db]\n")
        _ok(client.put("/api/graphs/pane", json={"text": text}))
        assert (graph_dir / "pane.yaml").read_text() == PANE_TEXT
        body = _ok(client.get("/api/graphs/pane/app"))
        assert [problem["message"].split(": ", 1)[1] for problem in body["problems"]] == [
            "the app input [FormKnobs, requires] is the gate a branch sets, not a setting — it is never a field",
            "the app input [FormKnobs, nosuch] names no parameter of FormKnobs (its parameters are: level, spread, "
            "width_hz)",
        ]

    def test_an_unknown_graph_is_404(self, client: Any) -> None:
        assert client.get("/api/graphs/nosuch/app").status_code == 404


class TestTheShippedPanes:
    """The ``app:`` blocks shipped with the signal domain's panes, opened as they are."""

    @pytest.mark.parametrize(
        "package, pane, entries, fields, all_fields",
        [
            ("waivefront.analysis.annotation", "bte_view.yaml", 32, 30, 248),
            ("waivefront.analysis.annotation", "clock_view.yaml", 12, 9, 50),
            ("waivefront.analysis.annotation", "cell_view.yaml", 6, 6, 67),
            ("waivefront.annotation", "write_annotations.yaml", 1, 1, 14),
        ],
    )
    def test_each_pane_narrows_to_its_app_inputs_with_no_problem(
        self, tmp_path: Path, package: str, pane: str, entries: int, fields: int, all_fields: int
    ) -> None:
        annotation = pytest.importorskip(package)

        source = Path(annotation.__file__).with_name(pane)
        (tmp_path / pane).write_text(source.read_text())
        client = create_app(tmp_path).test_client()
        name = pane[: -len(".yaml")]
        body = _ok(client.get(f"/api/graphs/{name}/app"))
        assert body["problems"] == []
        declared = YamlText(source.read_text()).path("app", "inputs")
        assert isinstance(declared, yaml.SequenceNode) and len(declared.value) == entries
        assert len(body["fields"]) == fields
        full = _ok(client.get(f"/api/graphs/{name}/form"))
        gates = sum(
            1 for node in _ok(client.get(f"/api/graphs/{name}"))["model"]["nodes"] if "requires" in node["params"]
        )
        assert sum(len(params) for params in full.values()) == all_fields - gates
