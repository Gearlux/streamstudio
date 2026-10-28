"""A graph and its SETTINGS: two files, ``<graph>.yaml`` and ``<graph>.settings.yaml`` beside it (user design
2026-10-05).

The graph holds the nodes and their wiring; the settings file holds the values a person changed of the fields the
graph's ``app:`` block names. The two are loaded as ONE document by confluid's own ``include:`` rule — the graph
first, the settings below it, so a setting wins over what the graph spells — and nothing else. A setting names its
node:

* a flow step by its NAME — ``flow.<step>.op.<param>: value`` (two nodes of one class are set apart);
* a top-level object by its KEY — ``<key>.<param>: value``;
* a node with no name (a member of an ``ops:`` list) by its CLASS — ``<Class>.<param>: value`` — only while that
  class is used once in the graph.

A setting that is none of the graph's settings is REFUSED where it is written (``file:line:col``), never confluid's
warn-and-ignore: measured before, a misspelt ``line_dbb`` loaded with a warning and the pane ran the graph's value.

The editor holds both files: a settings field changed on the canvas or the settings tab goes to the settings file, any
other change to the graph. A host may SHIP a graph (``EditorHost.shipped()``): it is listed and opened while the
folder holds no file of its name, a settings change writes only the settings file, and the first save of a change to
the graph itself writes the graph into the folder — the copy. A graph whose ``app:`` block says ``graph:
settings-only`` takes its settings and nothing else.

Every test runs in-process over temporary folders.
"""

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pytest
from confluid import configurable
from flask import Flask
from recordstream import Image, Record

from streamstudio.app import EditorHost, HostRecord, create_app, editor_blueprint
from streamstudio.settings import SETTINGS_SUFFIX, SettingField, SettingsError, load_graph, set_setting, settings_path

MOD = __name__
PREFIX = "/studio"


# ------------------------------------------------------------------------------------------------
# synthetic ops — spelled !class:<MOD>.<Class> by the documents below
# ------------------------------------------------------------------------------------------------


@configurable(category="op", group="settings-test")
class SetKnobs:
    """A level, a range and a parameter no ``app:`` block names.

    Args:
        level: A threshold — a setting.
        spread: A (min, max) range — a setting.
        other: A parameter that is not a setting.
    """

    consumes = {"image": "Image"}
    produces = {"image": "Image"}

    def __init__(self, level: float = 0.5, spread: Tuple[float, float] = (0.1, 0.9), other: int = 1) -> None:
        self.level = float(level)
        self.spread = spread
        self.other = int(other)

    def __call__(self, record: Record) -> Record:
        return record


@configurable(category="op", group="settings-test")
class SetFloor:
    """A second class.

    Args:
        floor_db: A floor — a setting.
    """

    consumes = {"image": "Image"}
    produces = {"image": "Image"}

    def __init__(self, floor_db: float = -90.0) -> None:
        self.floor_db = float(floor_db)

    def __call__(self, record: Record) -> Record:
        return record


@configurable(category="op", group="settings-test")
class SetGain:
    """A third class — the one step a graph adds after the file it includes.

    Args:
        gain_db: A gain — a setting.
    """

    consumes = {"image": "Image"}
    produces = {"image": "Image"}

    def __init__(self, gain_db: float = 0.0) -> None:
        self.gain_db = float(gain_db)

    def __call__(self, record: Record) -> Record:
        return record


# ------------------------------------------------------------------------------------------------
# documents
# ------------------------------------------------------------------------------------------------

#: A flow: every node has a NAME, and SetKnobs is the class of two of them.
PANE_TEXT = f"""\
# a pane: two knob nodes and a floor between them
flow:
  first:
    op: !class:{MOD}.SetKnobs {{level: 0.25}}
  second:
    op: !class:{MOD}.SetFloor {{}}
    from: first
  third:
    op: !class:{MOD}.SetKnobs {{}}
    from: second
outputs: third
app:
  inputs:
    - [SetKnobs, level]
    - [SetKnobs, spread__lo]
    - [SetKnobs, spread__hi]
    - [SetFloor, floor_db]
"""

#: The settings ``PANE_TEXT`` has, by the key that names each.
PANE_SETTINGS = [
    "flow.first.op.level",
    "flow.first.op.spread",
    "flow.second.op.floor_db",
    "flow.third.op.level",
    "flow.third.op.spread",
]

#: A Stream's ``ops:`` list: no node has a name. SetFloor is used once, SetKnobs twice.
CHAIN_TEXT = f"""\
stream: !class:recordstream.core.stream.Stream
  ops:
    - !class:{MOD}.SetFloor {{}}
    - !class:{MOD}.SetKnobs {{level: 0.25}}
    - !class:{MOD}.SetKnobs {{}}
app:
  inputs:
    - [SetFloor, floor_db]
    - [SetKnobs, level]
"""

#: A top-level object, named by its key, that the stream reaches by reference.
TOP_TEXT = f"""\
knobs: !class:{MOD}.SetKnobs {{level: 0.25}}
stream: !class:recordstream.core.stream.Stream
  ops:
    - !ref:knobs
app:
  inputs:
    - [SetKnobs, level]
"""

#: A graph that offers its settings and nothing else.
LOCKED_TEXT = f"""\
flow:
  first:
    op: !class:{MOD}.SetKnobs {{level: 0.25}}
outputs: first
app:
  graph: settings-only
  inputs:
    - [SetKnobs, level]
"""


def _write(folder: Path, name: str, text: str) -> Path:
    path = folder / name
    path.write_text(text)
    return path


def _ok(response: Any) -> Dict[str, Any]:
    body = response.get_json()
    assert response.status_code == 200, body
    assert isinstance(body, dict)
    return body


def _error(response: Any) -> str:
    body = response.get_json()
    assert response.status_code == 400, body
    assert isinstance(body, dict) and set(body) == {"error"}, body
    return str(body["error"])


def _value(graph: Mapping[str, Any], node: str, param: str) -> Any:  # a parameter value of any JSON type
    """The value the editor shows for ``node.param`` in a ``GET /api/graphs/<name>`` answer."""
    found = [entry for entry in graph["model"]["nodes"] if entry["name"] == node]
    assert found, f"no node {node!r} in {[entry['name'] for entry in graph['model']['nodes']]}"
    return found[0]["params"][param]["value"]


def _yaml_files(folder: Path) -> List[str]:
    return sorted(path.name for path in folder.glob("*.yaml"))


# ================================================================================================
# the settings file
# ================================================================================================


class TestTheSettingsFile:
    def test_a_graphs_settings_sit_beside_it_under_its_name(self) -> None:
        assert SETTINGS_SUFFIX == ".settings.yaml"
        assert settings_path(Path("/w/view_bte.yaml")) == Path("/w/view_bte.settings.yaml")

    def test_a_settings_file_is_not_listed_as_a_graph(self, tmp_path: Path) -> None:
        _write(tmp_path, "pane.yaml", PANE_TEXT)
        _write(tmp_path, "pane.settings.yaml", "flow.first.op.level: 0.9\n")
        client = create_app(tmp_path).test_client()
        names = [entry["name"] for entry in client.get("/api/graphs").get_json()]
        assert names == ["pane"]


# ================================================================================================
# loading a graph with its settings — what a host runs
# ================================================================================================


def test_the_editor_sets_a_bare_line_in_place() -> None:
    """A settings file a person wrote with a bare name keeps that line when the editor sets the same field."""
    field = SettingField("second", "floor_db", "flow.second.op.floor_db", "pane.yaml:12:5")
    assert set_setting("# mine\nfloor_db: -60.0\n", field, -70.0) == "# mine\nfloor_db: -70.0\n"
    assert set_setting("", field, -70.0) == "flow.second.op.floor_db: -70.0\n"
    assert set_setting("flow.second.op.floor_db: -60.0\n", field, -70.0) == "flow.second.op.floor_db: -70.0\n"


class TestLoadingAGraphWithItsSettings:
    def test_a_setting_names_a_flow_step_so_two_nodes_of_one_class_are_set_apart(self, tmp_path: Path) -> None:
        graph = _write(tmp_path, "pane.yaml", PANE_TEXT)
        settings = _write(tmp_path, "pane.settings.yaml", "flow.third.op.level: 0.9\n")
        built = load_graph(graph, settings)
        assert built["flow"]["first"]["op"].level == 0.25, "what the graph spells, where no setting names the node"
        assert built["flow"]["third"]["op"].level == 0.9, "the setting wins over the graph"

    def test_a_setting_names_a_top_level_object_by_its_key(self, tmp_path: Path) -> None:
        graph = _write(tmp_path, "top.yaml", TOP_TEXT)
        settings = _write(tmp_path, "top.settings.yaml", "knobs.level: 0.9\n")
        built = load_graph(graph, settings)
        assert built["knobs"].level == 0.9
        assert built["stream"].ops[0] is built["knobs"], "the reference still names the one object"

    def test_a_node_with_no_name_is_set_through_its_class_while_the_class_is_used_once(self, tmp_path: Path) -> None:
        graph = _write(tmp_path, "chain.yaml", CHAIN_TEXT)
        for spelled in ("SetFloor.floor_db: -60.0\n", "SetFloor:\n  floor_db: -60.0\n"):
            settings = _write(tmp_path, "chain.settings.yaml", spelled)
            assert load_graph(graph, settings)["stream"].ops[0].floor_db == -60.0, spelled

    def test_a_class_two_nodes_with_no_name_share_is_refused_naming_both(self, tmp_path: Path) -> None:
        graph = _write(tmp_path, "chain.yaml", CHAIN_TEXT)
        settings = _write(tmp_path, "chain.settings.yaml", "SetKnobs.level: 0.9\n")
        with pytest.raises(SettingsError) as refused:
            load_graph(graph, settings)
        message = str(refused.value)
        assert message.startswith(f"{settings}:1:1: "), message
        assert "SetKnobs" in message and "stream.ops[1]" in message and "stream.ops[2]" in message, message

    def test_a_parameter_one_node_alone_has_may_be_written_bare(self, tmp_path: Path) -> None:
        """PRO: ``floor_db`` is SetFloor's alone in the pane and in the chain — ``floor_db: -60.0`` is the setting
        ``flow.second.op.floor_db`` (confluid's own bare-key broadcast reaches that one node; measured 2026-10-14 on
        a review graph: ``overrides: {…}`` built the package step with it in both graphs). CON: ``level`` is a
        parameter of two nodes — a bare line would set both — refused naming the full keys to write."""
        graph = _write(tmp_path, "pane.yaml", PANE_TEXT)
        settings = _write(tmp_path, "pane.settings.yaml", "floor_db: -60.0\n")
        assert load_graph(graph, settings)["flow"]["second"]["op"].floor_db == -60.0
        settings = _write(tmp_path, "pane.settings.yaml", "level: 0.9\n")
        with pytest.raises(SettingsError) as refused:
            load_graph(graph, settings)
        assert str(refused.value) == (
            f"{settings}:1:1: level is a parameter of 2 nodes (first, third) — a bare line would set them all; "
            "write flow.first.op.level or flow.third.op.level"
        )
        chain = _write(tmp_path, "chain.yaml", CHAIN_TEXT)
        settings = _write(tmp_path, "chain.settings.yaml", "floor_db: -60.0\n")
        assert load_graph(chain, settings)["stream"].ops[0].floor_db == -60.0
        settings = _write(tmp_path, "chain.settings.yaml", "level: 0.9\n")
        with pytest.raises(SettingsError, match="level is a parameter of 2 nodes"):
            load_graph(chain, settings)

    def test_an_unknown_bare_name_is_refused_and_the_message_names_the_bare_spellings(self, tmp_path: Path) -> None:
        graph = _write(tmp_path, "pane.yaml", PANE_TEXT)
        settings = _write(tmp_path, "pane.settings.yaml", "other: 3\n")  # SetKnobs has it; no app: entry names it
        with pytest.raises(SettingsError) as refused:
            load_graph(graph, settings)
        message = str(refused.value)
        assert message.startswith(f"{settings}:1:1: other is not a setting of pane — its settings are: "), message
        assert message.endswith(" (a parameter one node alone has may be written bare: floor_db)"), message

    def test_a_setting_that_is_none_of_the_graphs_settings_is_refused_where_it_is_written(self, tmp_path: Path) -> None:
        """PRO: ``other`` is a parameter of SetKnobs, but no ``app:`` entry names it; ``fourth`` names no node.
        CON: the line above them is a setting and alone would load."""
        graph = _write(tmp_path, "pane.yaml", PANE_TEXT)
        for line in ("flow.first.op.other: 3", "flow.fourth.op.level: 0.9", "flow.first.op.line_dbb: 0.9"):
            settings = _write(tmp_path, "pane.settings.yaml", f"flow.first.op.level: 0.9\n{line}\n")
            with pytest.raises(SettingsError) as refused:
                load_graph(graph, settings)
            message = str(refused.value)
            key = line.split(":")[0]
            assert message.startswith(f"{settings}:2:1: {key} is not a setting of pane — its settings are: "), message
            assert all(setting in message for setting in PANE_SETTINGS), message
        settings = _write(tmp_path, "pane.settings.yaml", "flow.first.op.level: 0.9\n")
        assert load_graph(graph, settings)["flow"]["first"]["op"].level == 0.9

    def test_a_flow_block_is_refused_naming_the_one_line_spelling(self, tmp_path: Path) -> None:
        """Measured: confluid merges a ``flow:`` block by moving the step to the end, which breaks the schedule."""
        graph = _write(tmp_path, "pane.yaml", PANE_TEXT)
        settings = _write(tmp_path, "pane.settings.yaml", "flow:\n  first:\n    op:\n      level: 0.9\n")
        with pytest.raises(SettingsError) as refused:
            load_graph(graph, settings)
        message = str(refused.value)
        assert message.startswith(f"{settings}:1:1: "), message
        assert "flow.first.op.level: 0.9" in message, message

    def test_a_yaml_mistake_in_the_settings_file_names_the_settings_file(self, tmp_path: Path) -> None:
        graph = _write(tmp_path, "pane.yaml", PANE_TEXT)
        settings = _write(tmp_path, "pane.settings.yaml", "flow.first.op.level: [0.9\n")
        with pytest.raises(Exception) as refused:
            load_graph(graph, settings)
        assert str(settings) in str(refused.value)

    def test_no_settings_file_loads_the_graph_as_it_is(self, tmp_path: Path) -> None:
        graph = _write(tmp_path, "pane.yaml", PANE_TEXT)
        for settings in (None, tmp_path / "pane.settings.yaml"):
            assert load_graph(graph, settings)["flow"]["first"]["op"].level == 0.25

    def test_nothing_is_written_to_load_them_together(self, tmp_path: Path) -> None:
        shipped = tmp_path / "package"
        shipped.mkdir()
        graph = _write(shipped, "pane_view.yaml", PANE_TEXT)
        folder = tmp_path / "state"
        folder.mkdir()
        settings = _write(folder, "view_pane.settings.yaml", "flow.first.op.level: 0.9\n")
        before = (sorted(p.name for p in shipped.iterdir()), sorted(p.name for p in folder.iterdir()))
        assert load_graph(graph, settings)["flow"]["first"]["op"].level == 0.9
        assert (sorted(p.name for p in shipped.iterdir()), sorted(p.name for p in folder.iterdir())) == before


# ================================================================================================
# the editor holds both files
# ================================================================================================


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    folder = tmp_path / "state"
    folder.mkdir()
    for name, text in [("pane", PANE_TEXT), ("chain", CHAIN_TEXT), ("top", TOP_TEXT), ("locked", LOCKED_TEXT)]:
        _write(folder, f"{name}.yaml", text)
    return folder


@pytest.fixture
def client(folder: Path) -> Any:  # flask.testing.FlaskClient
    return create_app(folder).test_client()


class TestTheEditorHoldsTheSettings:
    def test_the_canvas_shows_the_settings_value_not_the_graphs(self, client: Any, folder: Path) -> None:
        _write(folder, "pane.settings.yaml", "flow.first.op.level: 0.9\n")
        graph = _ok(client.get("/api/graphs/pane"))
        assert _value(graph, "first", "level") == 0.9
        assert _value(graph, "third", "level") == 0.5, "a node no setting names shows what the graph gives it"
        assert graph["text"] == PANE_TEXT, "the text tab shows the graph itself"

    def test_a_settings_field_goes_to_the_settings_file_and_leaves_the_graph_alone(
        self, client: Any, folder: Path
    ) -> None:
        body = _ok(client.post("/api/graphs/pane/param", json={"node": "first", "param": "level", "value": 0.75}))
        assert body["dirty"] is True and body["text"] == PANE_TEXT
        assert _value(body, "first", "level") == 0.75
        _ok(client.post("/api/graphs/pane/save"))
        assert (folder / "pane.yaml").read_text() == PANE_TEXT
        assert (folder / "pane.settings.yaml").read_text() == "flow.first.op.level: 0.75\n"

    def test_a_range_is_one_line(self, client: Any, folder: Path) -> None:
        _ok(client.post("/api/graphs/pane/param", json={"node": "third", "param": "spread", "value": [0.2, 0.8]}))
        _ok(client.post("/api/graphs/pane/save"))
        assert (folder / "pane.settings.yaml").read_text() == "flow.third.op.spread: [0.2, 0.8]\n"

    def test_a_setting_set_again_replaces_its_line_and_the_rest_of_the_file_stays(
        self, client: Any, folder: Path
    ) -> None:
        _write(folder, "pane.settings.yaml", "# mine\nflow.second.op.floor_db: -60.0\n")
        for value in (0.75, 0.6):
            _ok(client.post("/api/graphs/pane/param", json={"node": "first", "param": "level", "value": value}))
        _ok(client.post("/api/graphs/pane/save"))
        assert (folder / "pane.settings.yaml").read_text() == (
            "# mine\nflow.second.op.floor_db: -60.0\nflow.first.op.level: 0.6\n"
        )

    def test_a_parameter_that_is_not_a_setting_is_an_edit_of_the_graph(self, client: Any, folder: Path) -> None:
        body = _ok(client.post("/api/graphs/pane/param", json={"node": "first", "param": "other", "value": 3}))
        assert "other: 3" in body["text"]
        _ok(client.post("/api/graphs/pane/save"))
        assert "other: 3" in (folder / "pane.yaml").read_text()
        assert not (folder / "pane.settings.yaml").exists()

    def test_undo_takes_a_setting_back(self, client: Any) -> None:
        _ok(client.post("/api/graphs/pane/param", json={"node": "first", "param": "level", "value": 0.75}))
        body = _ok(client.post("/api/graphs/pane/undo"))
        assert _value(body, "first", "level") == 0.25
        assert body["dirty"] is False

    def test_a_top_level_object_is_set_by_its_key(self, client: Any, folder: Path) -> None:
        _ok(client.post("/api/graphs/top/param", json={"node": "knobs", "param": "level", "value": 0.9}))
        _ok(client.post("/api/graphs/top/save"))
        assert (folder / "top.settings.yaml").read_text() == "knobs.level: 0.9\n"

    def test_a_node_with_no_name_is_set_through_its_class(self, client: Any, folder: Path) -> None:
        _ok(client.post("/api/graphs/chain/param", json={"node": "stream.ops[0]", "param": "floor_db", "value": -60.0}))
        _ok(client.post("/api/graphs/chain/save"))
        assert (folder / "chain.settings.yaml").read_text() == "SetFloor.floor_db: -60.0\n"

    def test_a_setting_on_a_class_two_unnamed_nodes_share_is_refused_naming_both(
        self, client: Any, folder: Path
    ) -> None:
        message = _error(
            client.post("/api/graphs/chain/param", json={"node": "stream.ops[1]", "param": "level", "value": 0.9})
        )
        assert "SetKnobs" in message and "stream.ops[1]" in message and "stream.ops[2]" in message, message
        assert _ok(client.get("/api/graphs/chain"))["dirty"] is False
        assert not (folder / "chain.settings.yaml").exists()

    def test_the_app_block_says_which_of_its_entries_two_unnamed_nodes_share(self, client: Any, folder: Path) -> None:
        app = _ok(client.get("/api/graphs/chain/app"))
        entry = f"{folder / 'chain.yaml'}:9:7"  # - [SetKnobs, level]
        found = [problem for problem in app["problems"] if problem["loc"] == entry]
        assert found and "stream.ops[1]" in found[0]["message"] and "stream.ops[2]" in found[0]["message"], app
        assert {"node": "stream.ops[0]", "param": "floor_db", "loc": f"{folder / 'chain.yaml'}:8:7"} in app["fields"]

    def test_the_settings_stay_in_force_after_the_graph_is_edited(self, client: Any, folder: Path) -> None:
        _write(folder, "pane.settings.yaml", "flow.first.op.level: 0.9\n")
        added = {"name": "extra", "after": "third", "class_path": f"{MOD}.SetFloor", "params": {}}
        _ok(client.post("/api/graphs/pane/steps", json=added))
        _ok(client.post("/api/graphs/pane/save"))
        assert "extra:" in (folder / "pane.yaml").read_text()
        assert (folder / "pane.settings.yaml").read_text() == "flow.first.op.level: 0.9\n"
        assert _value(_ok(client.get("/api/graphs/pane")), "first", "level") == 0.9

    def test_save_as_takes_the_settings_along_and_leaves_the_old_ones(self, client: Any, folder: Path) -> None:
        """PRO: the held settings are written beside the new graph and are its own from then on. CON: the old graph's
        settings file stays as it was, and a settings file already at the new place is never overwritten unasked."""
        _write(folder, "pane.settings.yaml", "flow.first.op.level: 0.9\n")
        _ok(client.post("/api/graphs/pane/param", json={"node": "third", "param": "level", "value": 0.7}))
        _write(folder, "taken.settings.yaml", "# someone's\n")
        refused = _error(client.post("/api/graphs/pane/save-as", json={"path": str(folder / "taken.yaml")}))
        assert (
            refused
            == f"{(folder / 'taken.settings.yaml').resolve()} already exists — pass overwrite: true to replace it"
        )
        assert not (folder / "taken.yaml").exists()
        saved = _ok(client.post("/api/graphs/pane/save-as", json={"path": str(folder / "copy.yaml")}))
        assert (folder / "copy.settings.yaml").read_text() == "flow.first.op.level: 0.9\nflow.third.op.level: 0.7\n"
        assert (folder / "pane.settings.yaml").read_text() == "flow.first.op.level: 0.9\n"
        assert saved["dirty"] is False and _value(saved, "third", "level") == 0.7
        _ok(client.post(f"/api/graphs/{saved['name']}/param", json={"node": "first", "param": "level", "value": 0.1}))
        _ok(client.post(f"/api/graphs/{saved['name']}/save"))
        assert (folder / "copy.settings.yaml").read_text() == "flow.first.op.level: 0.1\nflow.third.op.level: 0.7\n"
        assert (folder / "pane.settings.yaml").read_text() == "flow.first.op.level: 0.9\n"

    def test_a_save_of_the_settings_alone_leaves_a_change_to_the_graph_held(self, client: Any, folder: Path) -> None:
        """The settings sheet (``?only=settings``) saves each change at once, and ONLY the settings file: a change
        of the graph itself the editor tab holds stays unsaved (user pick 2026-10-05). Measured before: the sheet's
        save wrote the editor tab's unsaved change too (``pane.yaml now holds the editor tab's unsaved edit: True``).
        """
        _ok(client.post("/api/graphs/pane/param", json={"node": "first", "param": "other", "value": 3}))  # the tab
        _ok(client.post("/api/graphs/pane/param", json={"node": "third", "param": "level", "value": 0.7}))  # the sheet
        saved = _ok(client.post("/api/graphs/pane/save", json={"only": "settings"}))
        assert (folder / "pane.settings.yaml").read_text() == "flow.third.op.level: 0.7\n"
        assert (folder / "pane.yaml").read_text() == PANE_TEXT
        assert saved["dirty"] is True and "other: 3" in saved["text"]
        _ok(client.post("/api/graphs/pane/save"))  # the editor tab's own Save writes what it holds
        assert "other: 3" in (folder / "pane.yaml").read_text()

    def test_a_save_of_anything_else_alone_is_refused(self, client: Any) -> None:
        refused = _error(client.post("/api/graphs/pane/save", json={"only": "graph"}))
        assert refused == "only must be one of settings — got 'graph'"

    def test_a_node_a_setting_names_is_not_removed_or_renamed_from_under_it(self, client: Any, folder: Path) -> None:
        """PRO: removing or renaming ``first`` would leave ``flow.first.op.level`` naming nothing — refused at the
        setting's line. CON: a node no setting names is removed as before."""
        settings = _write(folder, "pane.settings.yaml", "flow.first.op.level: 0.9\n")
        for refused in (
            client.delete("/api/graphs/pane/steps/first"),
            client.post("/api/graphs/pane/rename", json={"node": "first", "new": "head"}),
        ):
            message = _error(refused)
            assert f"{settings.resolve()}:1:1: flow.first.op.level is not a setting of pane" in message, message
        assert _ok(client.get("/api/graphs/pane"))["dirty"] is False
        assert _ok(client.delete("/api/graphs/pane/steps/third"))["dirty"] is True

    def test_a_setting_that_is_not_one_is_a_problem_of_the_graph(self, client: Any, folder: Path) -> None:
        settings = _write(folder, "pane.settings.yaml", "flow.first.op.other: 3\n")
        problems = _ok(client.get("/api/graphs/pane/check"))["problems"]
        located = [problem for problem in problems if problem["loc"] == f"{settings}:1:1"]
        assert located and "flow.first.op.other is not a setting of pane" in located[0]["message"], problems


# ================================================================================================
# a graph the host ships
# ================================================================================================


class ShippingHost:
    """A host that ships ``view_pane`` (its file named for what it means, ``pane_view.yaml``) outside its folder."""

    name = "the test host"

    def __init__(self, folder: Path, shipped: Mapping[str, Path]) -> None:
        self.where = folder
        self.graphs = dict(shipped)
        self.saves: List[List[Path]] = []

    def folder(self) -> Path:
        return self.where

    def record(self, graph: str, path: Path) -> HostRecord:
        return HostRecord(record={"image": Image(np.full((2, 2), 0.5, dtype=np.float32))}, fills=(), label="rec_1")

    def saved(self, paths: Sequence[Path]) -> None:
        self.saves.append([Path(path).resolve() for path in paths])

    def shipped(self) -> Mapping[str, Path]:
        return self.graphs


@pytest.fixture
def package(tmp_path: Path) -> Path:
    package = tmp_path / "package"
    package.mkdir()
    _write(package, "pane_view.yaml", PANE_TEXT)
    return package


@pytest.fixture
def empty(tmp_path: Path) -> Path:
    folder = tmp_path / "workspace"
    folder.mkdir()
    return folder


@pytest.fixture
def shipping(empty: Path, package: Path) -> ShippingHost:
    return ShippingHost(empty, {"view_pane": package / "pane_view.yaml"})


@pytest.fixture
def hosted(shipping: ShippingHost) -> Any:  # flask.testing.FlaskClient
    app = Flask("host")
    app.register_blueprint(editor_blueprint(host=shipping), url_prefix=PREFIX)
    return app.test_client()


class TestAShippedGraph:
    def test_a_host_names_the_graphs_it_ships(self, shipping: ShippingHost) -> None:
        assert isinstance(shipping, EditorHost)

    def test_it_is_listed_though_the_folder_holds_no_file_of_it(self, hosted: Any, empty: Path) -> None:
        listed = {entry["name"]: entry for entry in _ok_list(hosted.get(f"{PREFIX}/api/graphs"))}
        assert set(listed) == {"view_pane"}
        assert listed["view_pane"]["shipped"] is True
        assert _yaml_files(empty) == []

    def test_it_opens_on_the_shipped_text_holding_nothing_unsaved(self, hosted: Any, empty: Path) -> None:
        graph = _ok(hosted.get(f"{PREFIX}/api/graphs/view_pane"))
        assert graph["text"] == PANE_TEXT and graph["dirty"] is False and graph["shipped"] is True
        assert Path(graph["path"]).resolve() == (empty / "view_pane.yaml").resolve(), "where its copy would go"

    def test_a_settings_change_writes_only_the_settings_file(
        self, hosted: Any, shipping: ShippingHost, empty: Path, package: Path
    ) -> None:
        _ok(hosted.post(f"{PREFIX}/api/graphs/view_pane/param", json={"node": "first", "param": "level", "value": 0.9}))
        _ok(hosted.post(f"{PREFIX}/api/graphs/view_pane/save"))
        assert _yaml_files(empty) == ["view_pane.settings.yaml"]
        assert (empty / "view_pane.settings.yaml").read_text() == "flow.first.op.level: 0.9\n"
        assert (package / "pane_view.yaml").read_text() == PANE_TEXT
        assert shipping.saves == [[(empty / "view_pane.settings.yaml").resolve()]]
        assert _ok(hosted.get(f"{PREFIX}/api/graphs/view_pane"))["shipped"] is True

    def test_the_settings_in_the_folder_apply_to_the_shipped_graph(self, hosted: Any, empty: Path) -> None:
        _write(empty, "view_pane.settings.yaml", "flow.first.op.level: 0.9\n")
        assert _value(_ok(hosted.get(f"{PREFIX}/api/graphs/view_pane")), "first", "level") == 0.9
        body = _ok(hosted.post(f"{PREFIX}/api/graphs/view_pane/trace", json={}))
        rows = {row["node"]: row for row in body["trace"]["nodes"]}
        assert rows["first"]["params"]["level"] == 0.9, "a Run runs the settings too"

    def test_the_first_save_of_a_change_to_the_graph_writes_the_copy(
        self, hosted: Any, shipping: ShippingHost, empty: Path, package: Path
    ) -> None:
        _write(empty, "view_pane.settings.yaml", "flow.first.op.level: 0.9\n")
        added = {"name": "extra", "after": "third", "class_path": f"{MOD}.SetFloor", "params": {}}
        _ok(hosted.post(f"{PREFIX}/api/graphs/view_pane/steps", json=added))
        saved = _ok(hosted.post(f"{PREFIX}/api/graphs/view_pane/save"))
        copy = empty / "view_pane.yaml"
        assert copy.read_text() == saved["text"] and "extra:" in copy.read_text()
        assert (package / "pane_view.yaml").read_text() == PANE_TEXT, "the shipped graph is never written"
        assert shipping.saves == [[copy.resolve()]]
        assert saved["shipped"] is False
        assert _value(saved, "first", "level") == 0.9, "the settings stay in force on the copy"

    def test_a_save_of_the_settings_alone_never_writes_the_copy(
        self, hosted: Any, shipping: ShippingHost, empty: Path
    ) -> None:
        added = {"name": "extra", "after": "third", "class_path": f"{MOD}.SetFloor", "params": {}}
        _ok(hosted.post(f"{PREFIX}/api/graphs/view_pane/steps", json=added))  # the editor tab, unsaved
        _ok(hosted.post(f"{PREFIX}/api/graphs/view_pane/param", json={"node": "first", "param": "level", "value": 0.9}))
        _ok(hosted.post(f"{PREFIX}/api/graphs/view_pane/save", json={"only": "settings"}))
        assert _yaml_files(empty) == ["view_pane.settings.yaml"]
        assert shipping.saves == [[(empty / "view_pane.settings.yaml").resolve()]]
        assert _ok(hosted.get(f"{PREFIX}/api/graphs/view_pane"))["shipped"] is True

    def test_a_copy_in_the_folder_wins_over_the_shipped_graph(self, hosted: Any, empty: Path) -> None:
        edited = PANE_TEXT.replace("{level: 0.25}", "{level: 0.3}")
        _write(empty, "view_pane.yaml", edited)
        graph = _ok(hosted.get(f"{PREFIX}/api/graphs/view_pane"))
        assert graph["text"] == edited and graph["shipped"] is False

    def test_revert_reads_the_shipped_graph_again(self, hosted: Any) -> None:
        added = {"name": "extra", "after": "third", "class_path": f"{MOD}.SetFloor", "params": {}}
        _ok(hosted.post(f"{PREFIX}/api/graphs/view_pane/steps", json=added))
        reverted = _ok(hosted.post(f"{PREFIX}/api/graphs/view_pane/revert"))
        assert reverted["text"] == PANE_TEXT and reverted["dirty"] is False


def _ok_list(response: Any) -> List[Dict[str, Any]]:
    body = response.get_json()
    assert response.status_code == 200, body
    assert isinstance(body, list)
    return body


# ================================================================================================
# a graph that offers its settings only
# ================================================================================================

#: Every edit of a settings-only graph that is not one of its settings — each refused, nothing held.
_STRUCTURAL: List[Tuple[str, str, Optional[Dict[str, Any]]]] = [
    ("post", "/api/graphs/locked/param", {"node": "first", "param": "other", "value": 3}),
    ("post", "/api/graphs/locked/steps", {"name": "extra", "after": "first", "class_path": f"{MOD}.SetFloor"}),
    ("post", "/api/graphs/locked/nodes", {"class_path": "recordstream.ops.contract.ClassNamesScan"}),
    ("delete", "/api/graphs/locked/steps/first", None),
    ("post", "/api/graphs/locked/rename", {"node": "first", "new": "renamed"}),
    ("put", "/api/graphs/locked", {"text": LOCKED_TEXT.replace("0.25", "0.3")}),
]


class TestASettingsOnlyGraph:
    def test_the_app_answer_says_whether_the_graph_itself_may_be_edited(self, client: Any) -> None:
        assert _ok(client.get("/api/graphs/locked/app"))["graph"] == "settings-only"
        assert _ok(client.get("/api/graphs/pane/app"))["graph"] == "editable"
        assert _ok(client.get("/api/graphs/chain/app"))["graph"] == "editable"

    def test_its_settings_are_set_as_any_graphs(self, client: Any, folder: Path) -> None:
        _ok(client.post("/api/graphs/locked/param", json={"node": "first", "param": "level", "value": 0.9}))
        _ok(client.post("/api/graphs/locked/save"))
        assert (folder / "locked.settings.yaml").read_text() == "flow.first.op.level: 0.9\n"
        assert (folder / "locked.yaml").read_text() == LOCKED_TEXT

    @pytest.mark.parametrize(("method", "url", "body"), _STRUCTURAL, ids=[url for _m, url, _b in _STRUCTURAL])
    def test_every_other_edit_is_refused_and_nothing_is_held(
        self, client: Any, method: str, url: str, body: Optional[Dict[str, Any]]
    ) -> None:
        response = getattr(client, method)(url, json=body) if body is not None else getattr(client, method)(url)
        message = _error(response)
        assert message == "locked offers its settings only — its graph is fixed where it is shipped", message
        graph = _ok(client.get("/api/graphs/locked"))
        assert graph["text"] == LOCKED_TEXT and graph["dirty"] is False

    def test_a_word_the_graph_line_does_not_know_is_a_located_problem(self, client: Any, folder: Path) -> None:
        _write(folder, "odd.yaml", LOCKED_TEXT.replace("graph: settings-only", "graph: frozen"))
        app = _ok(client.get("/api/graphs/odd/app"))
        assert app["graph"] == "editable"
        located = [problem for problem in app["problems"] if problem["loc"] == f"{folder / 'odd.yaml'}:6:10"]
        assert located and "editable, settings-only" in located[0]["message"], app["problems"]


#: A shipped graph that INCLUDES another file it ships beside it, the way a host ships a graph extending a pane.
WRAP_TEXT = f"""\
include: pane_view.yaml
flow:
  fourth:
    op: !class:{MOD}.SetGain {{}}
    from: third
outputs: fourth
app:
  inputs:
    - [SetGain, gain_db]
"""


class TestAShippedGraphThatIncludesAFileShippedBesideIt:
    """Measured before 2026-10-27: a shipped graph opened where its copy would go looked for its included file in the
    host's folder, where it is not — `does not load: ConfigFileNotFoundError … includes pane_view.yaml: Not found`,
    no node, and every `app:` entry `names no node of this graph`."""

    @pytest.fixture
    def wrapping(self, empty: Path, package: Path) -> Any:  # flask.testing.FlaskClient
        _write(package, "wrap_view.yaml", WRAP_TEXT)
        host = ShippingHost(empty, {"view_wrap": package / "wrap_view.yaml"})
        app = Flask("host")
        app.register_blueprint(editor_blueprint(host=host), url_prefix=PREFIX)
        return app.test_client()

    def test_it_opens_with_the_included_files_nodes_and_no_problem(self, wrapping: Any, empty: Path) -> None:
        graph = _ok(wrapping.get(f"{PREFIX}/api/graphs/view_wrap"))
        assert {"first", "second", "third", "fourth"} <= {node["name"] for node in graph["model"]["nodes"]}
        assert _ok(wrapping.get(f"{PREFIX}/api/graphs/view_wrap/check"))["problems"] == []
        assert graph["dirty"] is False and graph["shipped"] is True
        assert _yaml_files(empty) == []

    def test_its_settings_form_names_the_included_nodes(self, wrapping: Any) -> None:
        app = _ok(wrapping.get(f"{PREFIX}/api/graphs/view_wrap/app"))
        assert app["problems"] == []
        assert app["fields"], "the app: entries name nodes of the graph"

    def test_a_setting_saves_the_settings_file_alone(self, wrapping: Any, empty: Path, package: Path) -> None:
        _ok(
            wrapping.post(
                f"{PREFIX}/api/graphs/view_wrap/param", json={"node": "fourth", "param": "gain_db", "value": 3.0}
            )
        )
        _ok(wrapping.post(f"{PREFIX}/api/graphs/view_wrap/save", json={"only": "settings"}))
        assert _yaml_files(empty) == ["view_wrap.settings.yaml"]
        assert (package / "wrap_view.yaml").read_text() == WRAP_TEXT, "the shipped graph is never written"

    def test_the_copy_a_change_writes_includes_the_shipped_file(
        self, wrapping: Any, empty: Path, package: Path
    ) -> None:
        added = {"name": "fifth", "after": "fourth", "class_path": f"{MOD}.SetFloor", "params": {}}
        _ok(wrapping.post(f"{PREFIX}/api/graphs/view_wrap/steps", json=added))
        _ok(wrapping.post(f"{PREFIX}/api/graphs/view_wrap/save"))
        copy = empty / "view_wrap.yaml"
        assert copy.is_file() and (package / "wrap_view.yaml").read_text() == WRAP_TEXT
        reopened = _ok(wrapping.get(f"{PREFIX}/api/graphs/view_wrap"))
        assert reopened["shipped"] is False
        assert {"first", "fifth"} <= {node["name"] for node in reopened["model"]["nodes"]}

    def test_revert_reads_it_again_with_its_include(self, wrapping: Any) -> None:
        added = {"name": "fifth", "after": "fourth", "class_path": f"{MOD}.SetFloor", "params": {}}
        _ok(wrapping.post(f"{PREFIX}/api/graphs/view_wrap/steps", json=added))
        reverted = _ok(wrapping.post(f"{PREFIX}/api/graphs/view_wrap/revert"))
        assert reverted["dirty"] is False
        assert "first" in {node["name"] for node in reverted["model"]["nodes"]}


# ================================================================================================
# every host names what it ships
# ================================================================================================


def test_a_host_without_shipped_is_not_a_host(empty: Path) -> None:
    class Old:
        name = "an old host"

        def folder(self) -> Path:
            return empty

        def record(self, graph: str, path: Path) -> HostRecord:  # pragma: no cover - never called
            raise ValueError("none")

        def saved(self, paths: Sequence[Path]) -> None:  # pragma: no cover - never called
            pass

    assert not isinstance(Old(), EditorHost)
