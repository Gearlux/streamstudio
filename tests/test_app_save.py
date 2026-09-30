"""Explicit Save (the user's decision 1): the editor holds every change until Save.

Before this change every accepted edit was written to the file at once. Now an edit, a move, a group
or a replaced text changes the EDITOR's copy and marks the graph ``dirty``; four routes decide what
reaches the disk:

* ``POST …/save`` writes every file whose text changed and the positions sidecar — unless a file changed
  on disk since the editor read or wrote it, in which case it writes NOTHING and says how to go on;
* ``POST …/save-as {path}`` writes the editor's copy to another file (the list entry follows it, the old
  file is left as it was), and rewrites a relative ``include:`` so it still names the same file;
* ``POST …/revert`` drops what the editor holds and reads the file again;
* ``GET …/check`` answers the problems the editor's copy has.

The documents here use recordstream's own ops only (``PrintRecordOp``, ``Threshold``), so nothing in
this file depends on a class defined in another test module.
"""

import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Tuple, cast

import confluid
import numpy as np
import pytest
from PIL import Image

from streamstudio import document as document_module
from streamstudio import layout
from streamstudio.app import create_app
from streamstudio.document import rebase_includes
from streamstudio.layout import Group

PROBE = "recordstream.ops.debug.PrintRecordOp"
THRESHOLD = "recordstream.ops.numpy.Threshold"

FLOW_TEXT = f"""\
# a two-step flow
flow:
  probe:
    op: !class:{PROBE} {{label: probe}}
  mask:
    op: !class:{THRESHOLD} {{low_level: 0.5}}   # the mask
outputs: mask
"""

MOVED = {"positions": {"probe": [11, 22]}}
GROUPS = [{"id": "g1", "title": "front", "color": "#111", "nodes": ["probe"]}]


def _edit(client: Any, name: str = "demo", value: float = 0.9) -> Dict[str, Any]:
    response = client.post(f"/api/graphs/{name}/param", json={"node": "mask", "param": "low_level", "value": value})
    assert response.status_code == 200, response.get_json()
    body: Dict[str, Any] = response.get_json()
    return body


def _error(response: Any) -> str:
    body = response.get_json()
    assert isinstance(body, dict) and set(body) == {"error"}, body
    return str(body["error"])


@pytest.fixture
def graph_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "graphs"
    folder.mkdir()
    (folder / "demo.yaml").write_text(FLOW_TEXT)
    return folder


@pytest.fixture
def client(graph_dir: Path, tmp_path: Path) -> Any:  # flask.testing.FlaskClient
    return create_app(graph_dir, templates_dir=tmp_path / "templates").test_client()


# ------------------------------------------------------------------------------------------------
# save
# ------------------------------------------------------------------------------------------------


class TestSave:
    def test_save_writes_the_text_the_editor_holds_and_clears_dirty(self, client: Any, graph_dir: Path) -> None:
        edited = _edit(client)
        assert edited["dirty"] is True
        assert (graph_dir / "demo.yaml").read_text() == FLOW_TEXT
        response = client.post("/api/graphs/demo/save")
        assert response.status_code == 200
        body = response.get_json()
        assert body["dirty"] is False and body["text"] == edited["text"]
        assert (graph_dir / "demo.yaml").read_text() == edited["text"]
        assert "{low_level: 0.9}   # the mask" in (graph_dir / "demo.yaml").read_text()

    def test_a_save_with_nothing_held_writes_nothing(self, client: Any, graph_dir: Path) -> None:
        before = (graph_dir / "demo.yaml").stat().st_mtime_ns
        body = client.post("/api/graphs/demo/save").get_json()
        assert body["dirty"] is False
        assert (graph_dir / "demo.yaml").stat().st_mtime_ns == before
        assert not (graph_dir / "demo.layout.yaml").exists()

    def test_the_editor_tracks_what_it_wrote_so_a_second_save_is_not_a_conflict(
        self, client: Any, graph_dir: Path
    ) -> None:
        _edit(client, value=0.9)
        client.post("/api/graphs/demo/save")
        second = _edit(client, value=0.8)
        assert second["dirty"] is True
        assert client.post("/api/graphs/demo/save").status_code == 200
        assert "{low_level: 0.8}" in (graph_dir / "demo.yaml").read_text()

    def test_undoing_back_to_the_written_text_is_clean_again(self, client: Any) -> None:
        _edit(client, value=0.9)
        client.post("/api/graphs/demo/save")
        _edit(client, value=0.8)
        assert client.post("/api/graphs/demo/undo").get_json()["dirty"] is False

    def test_a_file_changed_on_disk_is_never_overwritten(self, client: Any, graph_dir: Path) -> None:
        """Someone else (another editor, a text editor, git) wrote the file after the editor read it: Save
        refuses, writes NOTHING — not the document, not the sidecar — and names the two ways on."""
        _edit(client)
        client.put("/api/graphs/demo/positions", json=MOVED)
        theirs = FLOW_TEXT.replace("label: probe", "label: theirs")
        (graph_dir / "demo.yaml").write_text(theirs)
        response = client.post("/api/graphs/demo/save")
        assert response.status_code == 400
        assert _error(response) == (
            f"{(graph_dir / 'demo.yaml').resolve()} changed on disk since the editor read it — "
            "Revert to load it, or Save As to keep both"
        )
        assert (graph_dir / "demo.yaml").read_text() == theirs
        assert not (graph_dir / "demo.layout.yaml").exists()
        assert client.get("/api/graphs/demo").get_json()["dirty"] is True  # what the editor holds is kept

    def test_a_graph_file_gone_from_disk_is_written_again(self, client: Any, graph_dir: Path) -> None:
        """Revert says "Save writes it again" for a file that is gone, and a gone file overwrites nothing — so
        Save writes it (measured before: Save refused as "changed on disk", Revert refused as "not on disk any
        more", a loop with no way out)."""
        edited = _edit(client)
        (graph_dir / "demo.yaml").unlink()
        response = client.post("/api/graphs/demo/save")
        assert response.status_code == 200, response.get_json()
        assert response.get_json()["dirty"] is False
        assert (graph_dir / "demo.yaml").read_text() == edited["text"]

    def test_positions_written_in_two_parts_both_reach_the_sidecar(self, client: Any, graph_dir: Path) -> None:
        """A caller that sends only the node it moved (an MCP tool, a script) must not undo an earlier unsaved move:
        before Explicit Save every PUT was saved, so a partial map ACCUMULATED in the sidecar — the held copy
        accumulates the same way."""
        first = client.put("/api/graphs/demo/positions", json={"positions": {"probe": [11, 22]}}).get_json()
        assert first["probe"] == [11.0, 22.0]
        second = client.put("/api/graphs/demo/positions", json={"positions": {"mask": [33, 44]}}).get_json()
        assert second["probe"] == [11.0, 22.0] and second["mask"] == [33.0, 44.0]
        client.post("/api/graphs/demo/save")
        sidecar = layout.read_sidecar(graph_dir / "demo.layout.yaml")
        assert sidecar["probe"] == (11.0, 22.0) and sidecar["mask"] == (33.0, 44.0)

    def test_save_of_an_unknown_graph_is_404(self, client: Any) -> None:
        assert client.post("/api/graphs/nosuch/save").status_code == 404


# ------------------------------------------------------------------------------------------------
# revert
# ------------------------------------------------------------------------------------------------


class TestRevert:
    def test_revert_drops_the_edits_the_history_the_positions_and_the_groups(
        self, client: Any, graph_dir: Path
    ) -> None:
        laid_out = client.get("/api/graphs/demo").get_json()["positions"]
        _edit(client)
        client.put("/api/graphs/demo/positions", json=MOVED)
        client.put("/api/graphs/demo/groups", json={"groups": GROUPS})
        response = client.post("/api/graphs/demo/revert")
        assert response.status_code == 200
        body = response.get_json()
        assert body["text"] == FLOW_TEXT and body["dirty"] is False
        assert body["positions"] == laid_out
        assert body["groups"] == []
        assert client.post("/api/graphs/demo/undo").get_json()["changed"] is False
        assert (graph_dir / "demo.yaml").read_text() == FLOW_TEXT
        assert not (graph_dir / "demo.layout.yaml").exists()

    def test_revert_loads_what_someone_else_wrote(self, client: Any, graph_dir: Path) -> None:
        _edit(client)
        theirs = FLOW_TEXT.replace("label: probe", "label: theirs")
        (graph_dir / "demo.yaml").write_text(theirs)
        assert client.post("/api/graphs/demo/revert").get_json()["text"] == theirs
        _edit(client)
        assert client.post("/api/graphs/demo/save").status_code == 200  # no conflict: the editor read it again

    def test_a_file_deleted_meanwhile_has_nothing_to_revert_to_and_save_writes_it_again(
        self, client: Any, graph_dir: Path
    ) -> None:
        client.get("/api/graphs/demo")  # held by the editor
        (graph_dir / "demo.yaml").unlink()
        response = client.post("/api/graphs/demo/revert")
        assert response.status_code == 400
        assert _error(response) == f"{graph_dir / 'demo.yaml'} is not on disk any more — Save writes it again"

    def test_a_graph_made_with_new_reverts_to_the_starter_new_wrote(self, client: Any, graph_dir: Path) -> None:
        starter = client.post("/api/graphs", json={"name": "fresh", "kind": "flow"}).get_json()["text"]
        edit = {"node": "probe", "param": "label", "value": "edited"}
        assert client.post("/api/graphs/fresh/param", json=edit).get_json()["dirty"] is True
        reverted = client.post("/api/graphs/fresh/revert")
        assert reverted.status_code == 200, reverted.get_json()
        assert (reverted.get_json()["text"], reverted.get_json()["dirty"]) == (starter, False)
        assert (graph_dir / "fresh.yaml").read_text() == starter


# ------------------------------------------------------------------------------------------------
# save as
# ------------------------------------------------------------------------------------------------


class TestSaveAs:
    def test_save_as_writes_the_editors_copy_and_the_entry_follows_the_new_file(
        self, client: Any, graph_dir: Path, tmp_path: Path
    ) -> None:
        edited = _edit(client)
        other = tmp_path / "elsewhere"
        other.mkdir()
        response = client.post("/api/graphs/demo/save-as", json={"path": str(other / "copy.yaml")})
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["name"] == "copy" and body["path"] == str(other / "copy.yaml")
        assert body["dirty"] is False and body["text"] == edited["text"]
        assert (other / "copy.yaml").read_text() == edited["text"]
        assert (graph_dir / "demo.yaml").read_text() == FLOW_TEXT  # the old file is left as it was
        listed = {entry["name"]: entry for entry in client.get("/api/graphs").get_json()}
        assert set(listed) == {"copy", "demo"}  # the folder's file is still a graph of the folder
        assert listed["copy"]["path"] == str(other / "copy.yaml")
        assert listed["demo"]["dirty"] is False
        assert client.get("/api/graphs/demo").get_json()["text"] == FLOW_TEXT

    def test_edits_after_save_as_go_to_the_new_file(self, client: Any, graph_dir: Path, tmp_path: Path) -> None:
        client.post("/api/graphs/demo/save-as", json={"path": str(tmp_path / "copy.yaml")})
        _edit(client, name="copy", value=0.3)
        client.post("/api/graphs/copy/save")
        assert "{low_level: 0.3}" in (tmp_path / "copy.yaml").read_text()
        assert (graph_dir / "demo.yaml").read_text() == FLOW_TEXT

    def test_undo_still_walks_the_edits_made_before_save_as(self, client: Any, tmp_path: Path) -> None:
        _edit(client, value=0.9)
        client.post("/api/graphs/demo/save-as", json={"path": str(tmp_path / "copy.yaml")})
        undone = client.post("/api/graphs/copy/undo").get_json()
        assert undone["changed"] is True and undone["text"] == FLOW_TEXT and undone["dirty"] is True

    def test_an_existing_file_is_refused_unless_overwrite_is_asked(self, client: Any, tmp_path: Path) -> None:
        target = tmp_path / "taken.yaml"
        target.write_text("kept: 1\n")
        response = client.post("/api/graphs/demo/save-as", json={"path": str(target)})
        assert response.status_code == 400
        assert _error(response) == f"{target} already exists — pass overwrite: true to replace it"
        assert target.read_text() == "kept: 1\n"
        response = client.post("/api/graphs/demo/save-as", json={"path": str(target), "overwrite": True})
        assert response.status_code == 200
        assert target.read_text() == FLOW_TEXT

    def test_the_same_stem_elsewhere_keeps_the_name_and_the_folders_file_takes_a_free_one(
        self, client: Any, graph_dir: Path, tmp_path: Path
    ) -> None:
        other = tmp_path / "other"
        other.mkdir()
        body = client.post("/api/graphs/demo/save-as", json={"path": str(other / "demo.yaml")}).get_json()
        assert body["name"] == "demo"  # the entry keeps its name: it IS this document, now in a new file
        names = sorted(entry["name"] for entry in client.get("/api/graphs").get_json())
        assert names == ["demo", "demo~2"]  # the folder's file comes back under a free name
        assert client.get("/api/graphs/demo~2").get_json()["path"] == str(graph_dir / "demo.yaml")

    def test_a_stem_another_entry_holds_gets_a_unique_name(self, client: Any, graph_dir: Path, tmp_path: Path) -> None:
        (graph_dir / "other.yaml").write_text(FLOW_TEXT)
        body = client.post("/api/graphs/demo/save-as", json={"path": str(tmp_path / "other.yaml")}).get_json()
        assert body["name"] == "other~2"
        assert sorted(entry["name"] for entry in client.get("/api/graphs").get_json()) == ["demo", "other", "other~2"]

    def test_a_file_another_entry_is_is_refused(self, client: Any, graph_dir: Path) -> None:
        """Saving over a file the list already holds under another name would leave that entry holding a
        copy the disk no longer has — refused, naming the entry, nothing written."""
        (graph_dir / "other.yaml").write_text(FLOW_TEXT)
        client.get("/api/graphs/other")
        _edit(client)
        response = client.post(
            "/api/graphs/demo/save-as", json={"path": str(graph_dir / "other.yaml"), "overwrite": True}
        )
        assert response.status_code == 400
        assert (
            _error(response) == f"{graph_dir / 'other.yaml'} is open as 'other' — save that one, or choose another file"
        )
        assert (graph_dir / "other.yaml").read_text() == FLOW_TEXT

    def test_save_as_onto_its_own_file_is_a_save(self, client: Any, graph_dir: Path) -> None:
        edited = _edit(client)
        body = client.post("/api/graphs/demo/save-as", json={"path": str(graph_dir / "demo.yaml")}).get_json()
        assert body["name"] == "demo" and body["dirty"] is False
        assert (graph_dir / "demo.yaml").read_text() == edited["text"]

    @pytest.mark.parametrize(
        "given, message",
        [
            ("notes.txt", "a graph is saved as a .yaml file, not notes.txt"),
            ("demo.layout.yaml", "demo.layout.yaml is a layout sidecar's name — choose another"),
            ("prep.steps.yaml", "prep.steps.yaml is a steps file's name — choose another"),
        ],
    )
    def test_a_path_that_cannot_hold_a_graph_is_refused(
        self, client: Any, tmp_path: Path, given: str, message: str
    ) -> None:
        response = client.post("/api/graphs/demo/save-as", json={"path": str(tmp_path / given)})
        assert response.status_code == 400
        assert _error(response) == message
        assert not (tmp_path / given).exists()

    def test_a_missing_directory_is_404_naming_it(self, client: Any, tmp_path: Path) -> None:
        response = client.post("/api/graphs/demo/save-as", json={"path": str(tmp_path / "nowhere" / "x.yaml")})
        assert response.status_code == 404
        assert str(tmp_path / "nowhere") in _error(response)

    def test_the_positions_and_groups_the_editor_shows_go_to_the_new_sidecar(
        self, client: Any, graph_dir: Path, tmp_path: Path
    ) -> None:
        layout.write_sidecar(graph_dir / "demo.layout.yaml", {"probe": (1.0, 2.0)})
        client.put("/api/graphs/demo/positions", json=MOVED)
        client.put("/api/graphs/demo/groups", json={"groups": GROUPS})
        client.post("/api/graphs/demo/save-as", json={"path": str(tmp_path / "copy.yaml")})
        assert layout.read_sidecar(tmp_path / "copy.layout.yaml")["probe"] == (11.0, 22.0)
        assert layout.read_groups(tmp_path / "copy.layout.yaml") == GROUPS
        assert layout.read_sidecar(graph_dir / "demo.layout.yaml") == {"probe": (1.0, 2.0)}  # untouched

    def test_an_untouched_sidecar_is_copied_byte_for_byte(self, client: Any, graph_dir: Path, tmp_path: Path) -> None:
        layout.write_sidecar(graph_dir / "demo.layout.yaml", {"probe": (1.0, 2.0)}, groups=cast(List[Group], GROUPS))
        client.post("/api/graphs/demo/save-as", json={"path": str(tmp_path / "copy.yaml")})
        assert (tmp_path / "copy.layout.yaml").read_bytes() == (graph_dir / "demo.layout.yaml").read_bytes()

    def test_a_relative_include_still_names_the_same_file_from_the_new_place(
        self, graph_dir: Path, tmp_path: Path
    ) -> None:
        """Measured 2026-09-29: the same text copied one folder over does not load at all —
        ``does not load: ConfigFileNotFoundError`` — because ``include: parts/pre.steps.yaml`` is read beside
        the document. Save As rewrites it relative to the new place; an absolute or ``$VAR`` entry is left."""
        (graph_dir / "parts").mkdir()
        (graph_dir / "parts" / "pre.steps.yaml").write_text(f"first:\n  op: !class:{PROBE} {{label: first}}\n")
        text = (
            "flow:\n"
            "  include: parts/pre.steps.yaml   # the shared head\n"
            "  last:\n"
            f"    op: !class:{PROBE} {{label: last}}\n"
            "    from: first\n"
            "outputs: last\n"
        )
        (graph_dir / "inc.yaml").write_text(text)
        client = create_app(graph_dir, templates_dir=tmp_path / "templates").test_client()
        assert client.get("/api/graphs/inc/check").get_json() == {"problems": []}
        other = tmp_path / "b"
        other.mkdir()
        body = client.post("/api/graphs/inc/save-as", json={"path": str(other / "inc.yaml")}).get_json()
        assert "  include: ../graphs/parts/pre.steps.yaml   # the shared head\n" in body["text"]
        assert (other / "inc.yaml").read_text() == body["text"]
        assert [node["name"] for node in body["model"]["nodes"]] == ["first", "last"]
        assert body["model"]["problems"] == []
        assert client.get("/api/graphs/inc/check").get_json() == {"problems": []}
        assert (graph_dir / "inc.yaml").read_text() == text

    def test_an_include_the_move_does_not_break_is_left_as_written(self, graph_dir: Path, tmp_path: Path) -> None:
        """CON case: an absolute entry and a ``$VAR`` entry resolve the same from anywhere — untouched."""
        steps = graph_dir / "abs.steps.yaml"
        steps.write_text(f"first:\n  op: !class:{PROBE} {{label: first}}\n")
        text = f"flow:\n  include: {steps}\n  last:\n    op: !class:{PROBE} {{}}\n    from: first\noutputs: last\n"
        (graph_dir / "abs.yaml").write_text(text)
        client = create_app(graph_dir, templates_dir=tmp_path / "templates").test_client()
        body = client.post("/api/graphs/abs/save-as", json={"path": str(tmp_path / "abs.yaml")}).get_json()
        assert body["text"] == text


# ------------------------------------------------------------------------------------------------
# check
# ------------------------------------------------------------------------------------------------


class TestCheck:
    def test_a_sound_graph_has_no_problems(self, client: Any) -> None:
        response = client.get("/api/graphs/demo/check")
        assert response.status_code == 200
        assert response.get_json() == {"problems": []}

    def test_the_problems_are_the_editors_copy_located(self, client: Any, graph_dir: Path, tmp_path: Path) -> None:
        (graph_dir / "broken.yaml").write_text("stream: !class:recordstream.core.stream.Stream\n  ops: [\n")
        problems = client.get("/api/graphs/broken/check").get_json()["problems"]
        assert len(problems) == 1
        assert set(problems[0]) == {"message", "loc"}
        assert "does not load" in problems[0]["message"]
        assert problems[0]["loc"].endswith("broken.yaml:3:1")

    def test_check_of_an_unknown_graph_is_404(self, client: Any) -> None:
        assert client.get("/api/graphs/nosuch/check").status_code == 404


# ------------------------------------------------------------------------------------------------
# rebase_includes on its own
# ------------------------------------------------------------------------------------------------


class TestRebaseIncludes:
    """Every entry shape an ``include:`` takes, measured one by one."""

    @pytest.fixture
    def dirs(self, tmp_path: Path) -> Any:
        old, new = tmp_path / "a", tmp_path / "b"
        (old / "parts").mkdir(parents=True)
        new.mkdir()
        (old / "parts" / "x.yaml").write_text("x: 1\n")
        (old / "y.yaml").write_text("y: 1\n")
        return old, new

    def test_a_list_of_entries_is_rebased_entry_by_entry(self, dirs: Any) -> None:
        old, new = dirs
        text = "include:\n  - parts/x.yaml\n  - y.yaml   # kept\nflow: {}\n"
        assert (
            rebase_includes(text, old, new) == "include:\n  - ../a/parts/x.yaml\n  - ../a/y.yaml   # kept\nflow: {}\n"
        )

    @pytest.mark.parametrize("entry", ["$CONFIGS/x.yaml", "~/x.yaml", "nowhere.yaml", ""])
    def test_an_entry_that_reads_the_same_anywhere_or_names_nothing_is_left(self, dirs: Any, entry: str) -> None:
        old, new = dirs
        text = f"include: '{entry}'\n"
        assert rebase_includes(text, old, new) == text

    def test_the_same_folder_and_a_text_that_is_not_yaml_are_left_as_they_are(self, dirs: Any) -> None:
        old, new = dirs
        assert rebase_includes("include: y.yaml\n", old, old) == "include: y.yaml\n"
        assert rebase_includes("a: [1\n", old, new) == "a: [1\n"


# ------------------------------------------------------------------------------------------------
# a graph that includes a steps file: undo, read-only operations, a teammate's edit, held texts never on disk
# ------------------------------------------------------------------------------------------------

SUBGRAPH = "recordstream.flow.subgraph.Subgraph"
CONVERT = "recordstream.ops.image.ConvertMode"
STEPS_TEXT = f"# the shared prep\ngrey:\n  op: !class:{CONVERT} {{mode: L}}\n"
INCLUDING_TEXT = f"""\
flow:
  prep:
    op: !class:{SUBGRAPH}
      steps: {{include: prep.steps.yaml}}
  mask:
    op: !class:{THRESHOLD} {{low_level: 0.5}}
outputs: mask
"""


@pytest.fixture
def including(graph_dir: Path) -> Path:
    (graph_dir / "prep.steps.yaml").write_text(STEPS_TEXT)
    (graph_dir / "inc.yaml").write_text(INCLUDING_TEXT)
    return graph_dir


def _canvas(body: Dict[str, Any]) -> Tuple[Any, Any]:
    """``(mask.low_level, prep/grey.mode)`` as the canvas shows them."""
    low = next(node for node in body["model"]["nodes"] if node["name"] == "mask")["params"]["low_level"]["value"]
    inside = body["model"]["inside"]["prep"]["nodes"]
    mode = next(node for node in inside if node["name"] == "prep/grey")["params"]["mode"]["value"]
    return low, mode


def _ok(response: Any) -> Dict[str, Any]:
    assert response.status_code == 200, response.get_json()
    body: Dict[str, Any] = response.get_json()
    return body


def _two_edits(client: Any, name: str = "inc") -> None:
    _ok(client.post(f"/api/graphs/{name}/param", json={"node": "mask", "param": "low_level", "value": 0.7}))
    _ok(client.post(f"/api/graphs/{name}/param", json={"node": "prep/grey", "param": "mode", "value": "RGB"}))


class TestUndoRestoresEveryFileTheDocumentHolds:
    """An included file enters the editor on its first EDIT; the history entries older than that edit restore
    it too (measured before: after Save, undoing both edits showed ``mode RGB`` and Save wrote RGB back)."""

    def test_undoing_both_edits_after_save_restores_the_included_file_and_save_writes_it(
        self, client: Any, including: Path
    ) -> None:
        _two_edits(client)
        _ok(client.post("/api/graphs/inc/save"))
        assert "mode: RGB" in (including / "prep.steps.yaml").read_text()
        assert _canvas(_ok(client.post("/api/graphs/inc/undo"))) == (0.7, "L")
        undone = _ok(client.post("/api/graphs/inc/undo"))
        assert _canvas(undone) == (0.5, "L") and undone["dirty"] is True
        saved = _ok(client.post("/api/graphs/inc/save"))
        assert saved["dirty"] is False
        assert (including / "prep.steps.yaml").read_text() == STEPS_TEXT
        assert (including / "inc.yaml").read_text() == INCLUDING_TEXT

    def test_redo_after_that_brings_both_edits_back(self, client: Any, including: Path) -> None:
        _two_edits(client)
        _ok(client.post("/api/graphs/inc/save"))
        client.post("/api/graphs/inc/undo")
        client.post("/api/graphs/inc/undo")
        client.post("/api/graphs/inc/redo")
        assert _canvas(_ok(client.post("/api/graphs/inc/redo"))) == (0.7, "RGB")

    def test_the_same_after_save_as(self, client: Any, including: Path, tmp_path: Path) -> None:
        _two_edits(client)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        _ok(client.post("/api/graphs/inc/save-as", json={"path": str(elsewhere / "copy.yaml")}))
        assert "mode: RGB" in (including / "prep.steps.yaml").read_text()
        client.post("/api/graphs/copy/undo")
        assert _canvas(_ok(client.post("/api/graphs/copy/undo"))) == (0.5, "L")
        _ok(client.post("/api/graphs/copy/save"))
        assert (including / "prep.steps.yaml").read_text() == STEPS_TEXT

    def test_a_removal_of_several_included_steps_is_undone_after_save(self, client: Any, including: Path) -> None:
        """The same for an edit that first holds the file INSIDE one multi-step edit (several steps removed)."""
        three = STEPS_TEXT + f"mono:\n  op: !class:{CONVERT} {{mode: L}}\nlast:\n  op: !class:{CONVERT} {{mode: L}}\n"
        (including / "prep.steps.yaml").write_text(three)
        removed = client.post("/api/graphs/inc/steps/remove", json={"nodes": ["prep/mono", "prep/last"]})
        assert removed.status_code == 200, removed.get_json()
        _ok(client.post("/api/graphs/inc/save"))
        assert (including / "prep.steps.yaml").read_text() == STEPS_TEXT
        assert _ok(client.post("/api/graphs/inc/undo"))["dirty"] is True
        _ok(client.post("/api/graphs/inc/save"))
        assert (including / "prep.steps.yaml").read_text() == three


class TestAReadOnlyOperationHoldsNoFile:
    """Copy and Save as template READ an included file; only an edit makes the editor hold it (measured before:
    after a Copy of ``prep/grey`` the canvas kept ``mode L`` while a teammate's file said RGB, and a trace ran the
    stale L)."""

    @pytest.mark.parametrize("read_only", ["copy", "template"])
    def test_a_teammates_edit_after_a_read_only_operation_is_what_the_canvas_shows(
        self, client: Any, including: Path, read_only: str
    ) -> None:
        if read_only == "copy":
            _ok(client.post("/api/graphs/inc/copy", json={"nodes": ["prep/grey"]}))
        else:
            _ok(client.post("/api/templates", json={"name": "tp", "graph": "inc", "nodes": ["prep"]}))
        (including / "prep.steps.yaml").write_text(STEPS_TEXT.replace("mode: L", "mode: RGB"))
        _ok(client.post("/api/graphs/inc/param", json={"node": "mask", "param": "low_level", "value": 0.6}))
        saved = _ok(client.post("/api/graphs/inc/save"))
        assert saved["dirty"] is False and _canvas(saved) == (0.6, "RGB")
        assert "mode: RGB" in (including / "prep.steps.yaml").read_text()

    def test_an_edit_still_holds_the_file_it_changes(self, client: Any, including: Path) -> None:
        _ok(client.post("/api/graphs/inc/param", json={"node": "prep/grey", "param": "mode", "value": "RGB"}))
        assert (including / "prep.steps.yaml").read_text() == STEPS_TEXT  # held, not written
        assert _canvas(client.get("/api/graphs/inc").get_json()) == (0.5, "RGB")


class TestEveryRemedyARefusalNamesWorks:
    def test_a_changed_included_file_offers_revert_and_names_the_file(self, client: Any, including: Path) -> None:
        """Save As would write a graph that includes the SAME steps file, so it cannot keep both: the refusal
        for an included file does not offer it (measured before: it did, and Save As refused the same way)."""
        _ok(client.post("/api/graphs/inc/param", json={"node": "prep/grey", "param": "mode", "value": "RGB"}))
        (including / "prep.steps.yaml").write_text(STEPS_TEXT + "# someone else\n")
        expected = (
            f"{(including / 'prep.steps.yaml').resolve()} changed on disk since the editor read it — Revert to load "
            "it (Save As cannot keep both: the graph includes this file wherever it is saved)"
        )
        response = client.post("/api/graphs/inc/save")
        assert response.status_code == 400 and _error(response) == expected
        response = client.post("/api/graphs/inc/save-as", json={"path": str(including / "keep_both.yaml")})
        assert response.status_code == 400 and _error(response) == expected
        assert not (including / "keep_both.yaml").exists()
        reverted = _ok(client.post("/api/graphs/inc/revert"))
        assert _canvas(reverted) == (0.5, "L") and reverted["dirty"] is False
        _ok(client.post("/api/graphs/inc/param", json={"node": "prep/grey", "param": "mode", "value": "RGB"}))
        _ok(client.post("/api/graphs/inc/save"))
        assert (including / "prep.steps.yaml").read_text() == STEPS_TEXT.replace("mode: L", "mode: RGB") + (
            "# someone else\n"
        )

    def test_the_graph_file_itself_still_offers_both(self, client: Any, graph_dir: Path) -> None:
        _edit(client)
        (graph_dir / "demo.yaml").write_text(FLOW_TEXT + "# theirs\n")
        assert _error(client.post("/api/graphs/demo/save")).endswith("Revert to load it, or Save As to keep both")


class TestAHeldTextNeverTouchesTheDisk:
    """What the editor holds and has not saved reaches confluid as ``texts=`` — no load writes a file.

    Move to its own file… makes a steps file that exists only in the editor until Save, and an edit of an included
    file holds its new text there too. Measured before (2026-09-30), every check, edit and trace WROTE each held text
    into the graph folder for the length of the load and put the file back after: a Move to its own file… followed
    by 5 checks and 5 edits showed ``prep.steps.yaml`` in the folder during those loads, and with the folder
    read-only every edit was refused (``does not load: PermissionError: [Errno 13] Permission denied:
    '…/prep.steps.yaml'``)."""

    @pytest.fixture
    def traced(self, graph_dir: Path) -> Path:
        _picture(graph_dir)
        (graph_dir / "prep.steps.yaml").write_text(
            f"grey:\n  op: !class:{CONVERT} {{mode: L}}\nscaled:\n  op: !class:{SCALE} "
            "{source_min: 0.0, source_max: 255.0, target_min: 0.0, target_max: 1.0}\n"
        )
        (graph_dir / "run.yaml").write_text(
            "stream: !class:recordstream.core.stream.Stream\n"
            f"  source: !class:recordstream.sources.files.FilesSource\n    files: [{graph_dir / 'demo.png'}]\n"
            "  ops:\n"
            "    - !class:recordstream.ops.image.ReadImage {}\n"
            f"    - !class:{SUBGRAPH}\n      steps: {{include: prep.steps.yaml}}\n"
            f"    - !class:{THRESHOLD} {{low_level: 0.5}}\n"
        )
        return graph_dir

    @pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a user the folder mode binds")
    def test_a_read_only_folder_still_checks_edits_and_traces_and_save_then_writes_the_file(
        self, inline: Path, tmp_path: Path
    ) -> None:
        client = create_app(inline, templates_dir=tmp_path / "templates").test_client()
        _ok(client.post("/api/graphs/run/subgraph/stream.ops[1]/extract", json={"path": "prep.steps.yaml"}))
        before = _listing(inline)
        assert "prep.steps.yaml" not in before
        inline.chmod(0o555)
        try:
            edited = _ok(client.post("/api/graphs/run/param", json=INNER_EDIT))  # an edit of the held file: a load
            assert edited["dirty"] is True
            assert _ok(client.get("/api/graphs/run/check"))["problems"] == []
            traced = _ok(client.post("/api/graphs/run/trace", json={}))  # built from the held texts: a load
            assert set(traced["statuses"].values()) == {"ok"}
            assert _listing(inline) == before
        finally:
            inline.chmod(0o755)
        _ok(client.post("/api/graphs/run/save"))
        assert "target_max: 2.0" in (inline / "prep.steps.yaml").read_text()

    def test_no_file_appears_in_the_folder_during_any_load(
        self, inline: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = create_app(inline, templates_dir=tmp_path / "templates").test_client()
        real_load = document_module.confluid.load
        during: List[List[str]] = []

        def load(*args: Any, **kwargs: Any) -> Any:  # confluid.load's own arguments and answer
            during.append(_listing(inline))
            return real_load(*args, **kwargs)

        monkeypatch.setattr(document_module.confluid, "load", load)
        before = _listing(inline)
        _ok(client.post("/api/graphs/run/subgraph/stream.ops[1]/extract", json={"path": "parts/prep.steps.yaml"}))
        _ok(
            client.post("/api/graphs/run/subgraph/stream.ops[1]/inner/extract", json={"path": "parts/inner.steps.yaml"})
        )
        _ok(client.post("/api/graphs/run/param", json=INNER_EDIT))
        _ok(client.get("/api/graphs/run/check"))
        _ok(client.post("/api/graphs/run/trace", json={}))
        _ok(client.post("/api/graphs/run/undo"))
        _ok(client.post("/api/graphs/run/redo"))
        assert len(during) >= 8
        assert [listing for listing in during if listing != before] == []
        assert _listing(inline) == before
        _ok(client.post("/api/graphs/run/save"))  # the one gesture that writes
        assert _listing(inline) == sorted(before + ["parts/inner.steps.yaml", "parts/prep.steps.yaml"])

    def test_the_nodes_of_a_held_steps_file_are_located_in_that_file(self, inline: Path, tmp_path: Path) -> None:
        client = create_app(inline, templates_dir=tmp_path / "templates").test_client()
        body = _ok(client.post("/api/graphs/run/subgraph/stream.ops[1]/extract", json={"path": "prep.steps.yaml"}))
        steps = (inline / "prep.steps.yaml").resolve()
        inside = body["model"]["inside"]["stream.ops[1]"]
        assert _locs(inside) == {
            "stream.ops[1]/grey": f"{steps}:2:7",
            "stream.ops[1]/inner": f"{steps}:4:7",
        }
        assert _locs(inside["inside"]["stream.ops[1]/inner"]) == {"stream.ops[1]/inner/scaled": f"{steps}:7:13"}
        edited = _ok(client.post("/api/graphs/run/param", json=INNER_EDIT))
        assert _locs(edited["model"]["inside"]["stream.ops[1]"]["inside"]["stream.ops[1]/inner"]) == {
            "stream.ops[1]/inner/scaled": f"{steps}:7:13"
        }
        assert not steps.exists()

    def test_a_nested_include_inside_a_held_steps_file_is_found_beside_that_file(
        self, inline: Path, tmp_path: Path
    ) -> None:
        """Both steps files held, neither on disk: the include ``inner.steps.yaml`` written in
        ``parts/prep.steps.yaml`` names ``parts/inner.steps.yaml`` — never the ``inner.steps.yaml`` beside the graph."""
        (inline / "inner.steps.yaml").write_text(f"decoy:\n  op: !class:{PROBE} {{label: decoy}}\n")
        client = create_app(inline, templates_dir=tmp_path / "templates").test_client()
        _ok(client.post("/api/graphs/run/subgraph/stream.ops[1]/extract", json={"path": "parts/prep.steps.yaml"}))
        body = _ok(
            client.post("/api/graphs/run/subgraph/stream.ops[1]/inner/extract", json={"path": "parts/inner.steps.yaml"})
        )
        nested = body["model"]["inside"]["stream.ops[1]"]["inside"]["stream.ops[1]/inner"]
        assert _locs(nested) == {"stream.ops[1]/inner/scaled": f"{(inline / 'parts/inner.steps.yaml').resolve()}:2:7"}
        assert _ok(client.get("/api/graphs/run/check"))["problems"] == []
        assert set(_ok(client.post("/api/graphs/run/trace", json={}))["statuses"].values()) == {"ok"}
        assert list((inline / "parts").iterdir()) == []
        _ok(client.post("/api/graphs/run/save"))
        assert "steps: {include: inner.steps.yaml}" in (inline / "parts/prep.steps.yaml").read_text()
        assert (inline / "parts/inner.steps.yaml").read_text().startswith("scaled:\n")

    def test_a_held_text_wins_over_the_file_on_disk_which_keeps_its_own_text(
        self, client: Any, including: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The disk says ``mode: L``, the editor holds ``mode: RGB``: every load sees RGB while the file keeps L —
        and each load still runs under ``_DISK_LOCK`` (it reads the includes the editor does NOT hold from disk,
        which a save of another graph may be writing at that moment)."""
        _ok(client.post("/api/graphs/inc/param", json={"node": "prep/grey", "param": "mode", "value": "RGB"}))
        real_load = document_module.confluid.load
        seen: List[Tuple[str, bool]] = []  # (the file's text on disk during the load, another thread got the lock)

        def load(*args: Any, **kwargs: Any) -> Any:  # confluid.load's own arguments and answer
            outside: List[bool] = []

            def take_the_lock() -> None:  # from another thread; given back at once, so a failure cannot hang the suite
                got = document_module._DISK_LOCK.acquire(blocking=False)
                if got:
                    document_module._DISK_LOCK.release()
                outside.append(got)

            other = threading.Thread(target=take_the_lock)
            other.start()
            other.join()
            seen.append(((including / "prep.steps.yaml").read_text(), outside[0]))
            return real_load(*args, **kwargs)

        monkeypatch.setattr(document_module.confluid, "load", load)
        edited = _ok(client.post("/api/graphs/inc/param", json={"node": "mask", "param": "low_level", "value": 0.4}))
        assert _canvas(edited) == (0.4, "RGB")
        assert seen and [disk for disk, _outside in seen if disk != STEPS_TEXT] == []
        assert not any(outside for _disk, outside in seen)
        assert (including / "prep.steps.yaml").read_text() == STEPS_TEXT
        _ok(client.post("/api/graphs/inc/save"))
        assert "mode: RGB" in (including / "prep.steps.yaml").read_text()

    def test_concurrent_traces_leave_the_disk_as_it_was_and_save_is_not_refused(
        self, traced: Path, tmp_path: Path
    ) -> None:
        """4 threads x 6 traces over a graph holding an unsaved edit of an included file (measured when loads still
        wrote held texts to disk: two overlapping loads left the unsaved value ON DISK, and Save then refused it as a
        change someone else made — ``Save: 400 … changed on disk``)."""
        app = create_app(traced, templates_dir=tmp_path / "templates")
        client = app.test_client()
        on_disk = (traced / "prep.steps.yaml").read_text()
        held = {"node": "stream.ops[1]/scaled", "param": "target_max", "value": 9.0}
        _ok(client.post("/api/graphs/run/param", json=held))
        answers: List[Tuple[int, str]] = []

        def traces() -> None:
            mine = app.test_client()
            for _ in range(6):
                response = mine.post("/api/graphs/run/trace", json={})
                answers.append((response.status_code, str((response.get_json() or {}).get("error", ""))))

        threads = [threading.Thread(target=traces) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert [answer for answer in answers if answer[0] != 200] == []
        assert (traced / "prep.steps.yaml").read_text() == on_disk  # the unsaved 9.0 never reached the disk
        _ok(client.post("/api/graphs/run/save"))
        assert "target_max: 9.0" in (traced / "prep.steps.yaml").read_text()


@pytest.fixture
def inline(graph_dir: Path) -> Path:
    """``run.yaml``: a Stream whose ``stream.ops[1]`` is a subgraph written INLINE — ``grey``, then the nested
    subgraph ``inner`` (one ``scaled`` step) — and an empty ``parts/`` folder beside it."""
    _picture(graph_dir)
    (graph_dir / "run.yaml").write_text(INLINE_RUN_TEXT.format(picture=graph_dir / "demo.png"))
    (graph_dir / "parts").mkdir()
    return graph_dir


_MODE_BINDS = pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a user the file mode binds")


class TestASaveTheDiskRefusesWritesNothing:
    """A Save (or Save As) the disk refuses for ONE of its files writes NONE of them, so the graph on disk never names
    a steps file that is not there.

    Measured before (2026-09-30): Move to its own file… into a read-only ``parts/``, an inner edit, Save →
    ``400 …/parts/prep.steps.yaml cannot be written: Permission denied`` — with ``run.yaml`` ALREADY rewritten to
    ``steps: {include: parts/prep.steps.yaml}``, so a fresh ``confluid.load(run.yaml)`` raised
    ``ConfigFileNotFoundError``. Save As wrote ``other/run2.yaml`` the same way before it met the steps file."""

    @staticmethod
    def _moved(inline: Path, tmp_path: Path) -> Any:  # flask.testing.FlaskClient
        """Move to its own file… into ``parts/prep.steps.yaml``, then an edit of the moved steps: two files to write."""
        client = create_app(inline, templates_dir=tmp_path / "templates").test_client()
        _ok(client.post("/api/graphs/run/subgraph/stream.ops[1]/extract", json={"path": "parts/prep.steps.yaml"}))
        _ok(client.post("/api/graphs/run/param", json=INNER_EDIT))
        return client

    @_MODE_BINDS
    def test_save_refused_for_the_steps_file_leaves_the_graph_file_as_it_was(
        self, inline: Path, tmp_path: Path
    ) -> None:
        client = self._moved(inline, tmp_path)
        graph, steps = inline / "run.yaml", inline / "parts" / "prep.steps.yaml"
        before = graph.read_text()
        (inline / "parts").chmod(0o555)
        try:
            refused = _error(client.post("/api/graphs/run/save"))
        finally:
            (inline / "parts").chmod(0o755)
        assert refused == f"{steps.resolve()} cannot be written: Permission denied"
        assert graph.read_text() == before  # the graph still holds its steps inline, and loads
        assert list((inline / "parts").iterdir()) == []
        confluid.load(graph, until="document")
        assert _ok(client.get("/api/graphs/run"))["dirty"] is True  # nothing was saved: the editor still holds both
        _ok(client.post("/api/graphs/run/save"))  # the folder writable again
        assert "steps: {include: parts/prep.steps.yaml}" in graph.read_text()
        assert "target_max: 2.0" in steps.read_text()
        confluid.load(graph, until="document")

    @_MODE_BINDS
    def test_save_refused_for_the_graph_file_writes_no_steps_file(self, inline: Path, tmp_path: Path) -> None:
        """The other way round — ``parts/`` writable, ``run.yaml`` not: the steps file is not written either."""
        client = self._moved(inline, tmp_path)
        graph = inline / "run.yaml"
        graph.chmod(0o444)
        try:
            refused = _error(client.post("/api/graphs/run/save"))
        finally:
            graph.chmod(0o644)
        assert refused == f"{graph.resolve()} cannot be written: Permission denied"
        assert list((inline / "parts").iterdir()) == []
        _ok(client.post("/api/graphs/run/save"))
        assert (inline / "parts" / "prep.steps.yaml").is_file()

    @_MODE_BINDS
    def test_save_as_refused_for_the_steps_file_writes_no_new_graph(self, inline: Path, tmp_path: Path) -> None:
        client = self._moved(inline, tmp_path)
        other = tmp_path / "other"
        other.mkdir()
        (inline / "parts").chmod(0o555)
        try:
            refused = _error(client.post("/api/graphs/run/save-as", json={"path": str(other / "run2.yaml")}))
        finally:
            (inline / "parts").chmod(0o755)
        assert refused == f"{(inline / 'parts' / 'prep.steps.yaml').resolve()} cannot be written: Permission denied"
        assert list(other.iterdir()) == []
        _ok(client.post("/api/graphs/run/save"))  # the entry did not move: Save still writes run.yaml
        assert "steps: {include: parts/prep.steps.yaml}" in (inline / "run.yaml").read_text()

    def test_a_write_the_disk_cuts_short_leaves_the_graph_file_as_it_was(
        self, inline: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure no check can foresee (a full disk) while the steps file is written: the graph's own file is
        written LAST, so it still does not name a steps file that is not there — for Save and for Save As."""
        client = self._moved(inline, tmp_path)
        graph = inline / "run.yaml"
        before = graph.read_text()
        real_write = document_module.Document._write

        def write(key: Path, text: str) -> None:  # Document._write's own arguments
            if key.name == "prep.steps.yaml":
                raise ValueError(f"{key} cannot be written: No space left on device")
            real_write(key, text)

        monkeypatch.setattr(document_module.Document, "_write", staticmethod(write))
        assert _error(client.post("/api/graphs/run/save")).endswith("cannot be written: No space left on device")
        assert graph.read_text() == before
        other = tmp_path / "other"
        other.mkdir()
        assert _error(client.post("/api/graphs/run/save-as", json={"path": str(other / "run2.yaml")})).endswith(
            "cannot be written: No space left on device"
        )
        assert list(other.iterdir()) == []


class TestAConfluidWithoutHeldTexts:
    """The released confluid (0.3.1, what CI installs from PyPI) has no ``load(..., texts=)``.

    Measured 2026-09-30 with that confluid: EVERY graph stopped loading — a plain two-step flow's check answered
    ``does not load: TypeError: load() got an unexpected keyword argument 'texts'`` and its edit ``404 no node 'mask'
    (the nodes are: [])`` — because every load passed ``texts=``, even when the editor held no file but the graph.
    Only a load that holds another file passes it now; that one still needs the confluid carrying ``texts=``."""

    @pytest.fixture
    def released(self, monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
        """``confluid.load`` as the released confluid has it: no ``texts`` keyword. Answers each call's keywords."""
        real_load = document_module.confluid.load
        calls: List[Dict[str, Any]] = []

        def load(*args: Any, **kwargs: Any) -> Any:  # confluid.load's own arguments and answer
            calls.append(kwargs)
            if "texts" in kwargs:
                raise TypeError("load() got an unexpected keyword argument 'texts'")
            return real_load(*args, **kwargs)

        monkeypatch.setattr(document_module.confluid, "load", load)
        return calls

    def test_a_graph_holding_no_other_file_checks_and_edits(self, client: Any, released: List[Dict[str, Any]]) -> None:
        assert _ok(client.get("/api/graphs/demo/check"))["problems"] == []
        assert _edit(client)["dirty"] is True
        assert released and [call for call in released if "texts" in call] == []

    def test_a_graph_including_a_file_it_does_not_hold_checks_and_edits(
        self, client: Any, including: Path, released: List[Dict[str, Any]]
    ) -> None:
        assert _ok(client.get("/api/graphs/inc/check"))["problems"] == []
        edited = _ok(client.post("/api/graphs/inc/param", json={"node": "mask", "param": "low_level", "value": 0.4}))
        assert _canvas(edited) == (0.4, "L")
        assert released and [call for call in released if "texts" in call] == []

    def test_an_edit_of_the_included_file_needs_the_confluid_that_takes_texts(
        self, client: Any, including: Path, released: List[Dict[str, Any]]
    ) -> None:
        """The limit the README states: the edit makes the editor HOLD the steps file; that load needs ``texts=``."""
        refused = _error(
            client.post("/api/graphs/inc/param", json={"node": "prep/grey", "param": "mode", "value": "RGB"})
        )
        assert "TypeError: load() got an unexpected keyword argument 'texts'" in refused
        assert (including / "prep.steps.yaml").read_text() == STEPS_TEXT


def _folds_case(folder: Path) -> bool:
    """Does the filesystem under ``folder`` ignore case (macOS APFS and Windows NTFS do, by default)?"""
    probe = folder / "case_probe.txt"
    probe.write_text("")
    try:
        return (folder / "CASE_PROBE.TXT").exists()
    finally:
        probe.unlink()


class TestAnIncludeSpelledInAnotherCase:
    """On a disk that ignores case, an include and the file it names may be spelled differently (``prep.steps.yaml`` /
    ``Prep.steps.yaml``). The editor holds a file under the spelling its include resolves to, so an edit of it is what
    the canvas shows, what runs and what Save writes. The documented limit (README, "What is not there yet"): an
    include spelling a steps file you have NOT saved in another case finds nothing until Save — measured 2026-09-30,
    the edit that respells it is refused with ``… includes PREP.steps.yaml: Not found``."""

    def test_an_edit_of_a_file_the_disk_spells_in_another_case_runs_and_saves(
        self, graph_dir: Path, tmp_path: Path
    ) -> None:
        if not _folds_case(graph_dir):
            pytest.skip("needs a filesystem that ignores case (macOS APFS, Windows NTFS)")
        _picture(graph_dir)
        (graph_dir / "Prep.steps.yaml").write_text(f"grey:\n  op: !class:{CONVERT} {{mode: L}}\n")
        (graph_dir / "run.yaml").write_text(
            "stream: !class:recordstream.core.stream.Stream\n"
            f"  source: !class:recordstream.sources.files.FilesSource\n    files: [{graph_dir / 'demo.png'}]\n"
            "  ops:\n"
            "    - !class:recordstream.ops.image.ReadImage {}\n"
            f"    - !class:{SUBGRAPH}\n      steps: {{include: prep.steps.yaml}}\n"
        )
        client = create_app(graph_dir, templates_dir=tmp_path / "templates").test_client()
        edited = _ok(
            client.post("/api/graphs/run/param", json={"node": "stream.ops[1]/grey", "param": "mode", "value": "RGB"})
        )
        grey = next(n for n in edited["model"]["inside"]["stream.ops[1]"]["nodes"] if n["name"] == "stream.ops[1]/grey")
        assert grey["params"]["mode"]["value"] == "RGB"
        assert set(_ok(client.post("/api/graphs/run/trace", json={}))["statuses"].values()) == {"ok"}
        _ok(client.post("/api/graphs/run/save"))
        assert "{mode: RGB}" in (graph_dir / "Prep.steps.yaml").read_text()

    def test_an_include_respelling_a_steps_file_not_saved_yet_is_refused_until_save(
        self, inline: Path, tmp_path: Path
    ) -> None:
        if not _folds_case(inline):
            pytest.skip("needs a filesystem that ignores case (macOS APFS, Windows NTFS)")
        client = create_app(inline, templates_dir=tmp_path / "templates").test_client()
        _ok(client.post("/api/graphs/run/subgraph/stream.ops[1]/extract", json={"path": "prep.steps.yaml"}))
        text = _ok(client.get("/api/graphs/run"))["text"]
        respelled = text.replace("include: prep.steps.yaml", "include: PREP.steps.yaml")
        assert respelled != text
        refused = _error(client.put("/api/graphs/run", json={"text": respelled}))
        assert "includes PREP.steps.yaml: Not found: PREP.steps.yaml" in refused
        _ok(client.post("/api/graphs/run/save"))  # on disk now: the disk finds it whatever the case
        assert _ok(client.put("/api/graphs/run", json={"text": respelled}))["dirty"] is True


SCALE = "recordstream.ops.numpy.Scale"
INNER_EDIT = {"node": "stream.ops[1]/inner/scaled", "param": "target_max", "value": 2.0}
INLINE_RUN_TEXT = f"""\
stream: !class:recordstream.core.stream.Stream
  source: !class:recordstream.sources.files.FilesSource
    files: [{{picture}}]
  ops:
    - !class:recordstream.ops.image.ReadImage {{{{}}}}
    - !class:{SUBGRAPH}
      steps:
        grey:
          op: !class:{CONVERT} {{{{mode: L}}}}
        inner:
          op: !class:{SUBGRAPH}
            steps:
              scaled:
                op: !class:{SCALE} {{{{source_min: 0.0, source_max: 255.0, target_min: 0.0, target_max: 1.0}}}}
    - !class:{THRESHOLD} {{{{low_level: 0.5}}}}
"""


def _picture(folder: Path) -> None:
    """``demo.png``: a 20x20 RGB picture with one bright square — what the traced streams read."""
    pixels = np.zeros((20, 20, 3), np.uint8)
    pixels[5:12, 5:12] = 230
    Image.fromarray(pixels).save(folder / "demo.png")


def _listing(folder: Path) -> List[str]:
    """Every file and folder under ``folder``, relative to it, sorted."""
    return sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*"))


def _locs(level: Dict[str, Any]) -> Dict[str, str]:
    """``{node name: file:line:col}`` of one level's nodes — its boundary nodes have no line and are left out."""
    return {node["name"]: node["loc"] for node in level["nodes"] if node["kind"] != "boundary"}
