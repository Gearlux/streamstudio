"""Unsaved work survives a stop — the user's item 1 (2026-09-29): "streamstudio stop or restart should save the
file to a tmp workspace file (hidden), when opening that same workspace streamstudio should ask to start from the
tmp file or the original".

The workspace is the folder the editor was started on; the recovery file is
``<folder>/.streamstudio-workspace.json``:

* :meth:`Studio.stash` writes it when the editor stops (``streamstudio stop`` / ``restart`` / Ctrl+C — the signal
  path is pinned against a real process in ``test_recovery_process.py``) and ONLY when something is unsaved: per
  graph its path, the text of every file it spans, the text each had when the editor last read or wrote it, and
  the positions / groups held. The undo history is not kept. A graph made with New… is a file from its first
  moment (New writes it), so it is kept only when it holds changes, like any other.
* ``GET /api/workspace`` answers ``{folder, recovery}`` — ``null``, or what a restore would open, each graph
  ``changed`` and whether its file changed on disk since; a file that cannot be read is reported in words.
* ``POST /api/workspace/recovery/restore`` opens those graphs as they were (unsaved, ●) and removes the file;
  ``…/discard`` removes it (a broken one is moved aside to ``.streamstudio-workspace.broken.json``). Both answer
  the graph list.

Everything here runs in-process over temporary folders.
"""

import json
import threading
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Dict, List

import pytest

from streamstudio import app as app_module
from streamstudio.app import BROKEN_RECOVERY_FILE, EXTENSION, RECOVERY_FILE, Studio, create_app, starter_text
from streamstudio.document import CHANGED_ON_DISK

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
AWAY_TEXT = f"flow:\n  away:\n    op: !class:{PROBE} {{label: away}}\noutputs: away\n"
#: What New… wrote for ``fresh`` (a pipeline) — the file on disk until a Save of the kept work.
FRESH_TEXT = starter_text("pipeline", "fresh")
GROUPS = [{"id": "g1", "title": "front", "color": "#111", "nodes": ["probe"]}]


@pytest.fixture
def graph_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "graphs"
    folder.mkdir()
    (folder / "demo.yaml").write_text(FLOW_TEXT)
    return folder


@pytest.fixture
def away(tmp_path: Path) -> Path:
    """A graph in ANOTHER folder, opened with File → Open."""
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    (folder / "away.yaml").write_text(AWAY_TEXT)
    return folder / "away.yaml"


class _Editor:
    """One editor process's app over the folder, and its test client."""

    def __init__(self, graph_dir: Path, tmp_path: Path) -> None:
        self.app = create_app(graph_dir, templates_dir=tmp_path / "templates")
        self.client = self.app.test_client()

    @property
    def studio(self) -> Studio:
        studio: Studio = self.app.extensions[EXTENSION]
        return studio

    def ok(self, method: str, url: str, body: Any = None) -> Any:  # the route's JSON answer
        response = getattr(self.client, method)(url, json=body)
        assert response.status_code == 200, response.get_json()
        return response.get_json()

    def refused(self, method: str, url: str, status: int = 400) -> str:
        response = getattr(self.client, method)(url, json={})
        assert response.status_code == status, response.get_json()
        body = response.get_json()
        assert set(body) == {"error"}, body
        return str(body["error"])


def _edit_everything(editor: _Editor, away: Path) -> None:
    """The session every test stops: ``demo`` changed and moved and grouped, ``fresh`` made with New… (which wrote
    it) and then changed, ``away`` changed."""
    editor.ok("post", "/api/graphs/demo/param", {"node": "mask", "param": "low_level", "value": 0.9})
    editor.ok("put", "/api/graphs/demo/positions", {"positions": {"probe": [11, 22]}})
    editor.ok("put", "/api/graphs/demo/groups", {"groups": GROUPS})
    editor.ok("post", "/api/graphs", {"name": "fresh", "kind": "pipeline"})
    editor.ok("post", "/api/graphs/fresh/param", {"node": "stream.ops[0]", "param": "label", "value": "edited"})
    editor.ok("post", "/api/graphs/open", {"path": str(away)})
    editor.ok("post", "/api/graphs/away/param", {"node": "away", "param": "label", "value": "moved"})


@pytest.fixture
def stopped(graph_dir: Path, away: Path, tmp_path: Path) -> Dict[str, str]:
    """A session with unsaved work, stopped: what each graph's text was when it stopped."""
    editor = _Editor(graph_dir, tmp_path)
    _edit_everything(editor, away)
    texts = {name: editor.ok("get", f"/api/graphs/{name}")["text"] for name in ("demo", "fresh", "away")}
    assert editor.studio.stash() == [str(graph_dir / "demo.yaml"), str(graph_dir / "fresh.yaml"), str(away)]
    return texts


def _recovery(graph_dir: Path) -> Dict[str, Any]:
    data: Dict[str, Any] = json.loads((graph_dir / RECOVERY_FILE).read_text())
    return data


# ------------------------------------------------------------------------------------------------
# what a stop keeps
# ------------------------------------------------------------------------------------------------


class TestWhatAStopKeeps:
    def test_nothing_unsaved_writes_no_file(self, graph_dir: Path, away: Path, tmp_path: Path) -> None:
        editor = _Editor(graph_dir, tmp_path)
        editor.ok("get", "/api/graphs/demo")
        editor.ok("post", "/api/graphs/open", {"path": str(away)})
        assert editor.studio.stash() == []
        assert not (graph_dir / RECOVERY_FILE).exists()

    def test_every_graph_with_unsaved_work_is_kept_with_what_its_files_held(
        self, graph_dir: Path, away: Path, stopped: Dict[str, str]
    ) -> None:
        data = _recovery(graph_dir)
        assert data["folder"] == str(graph_dir)
        assert datetime.fromisoformat(data["saved_at"]).tzinfo is not None
        graphs = {entry["name"]: entry for entry in data["graphs"]}
        assert list(graphs) == ["demo", "fresh", "away"]
        demo_key = str((graph_dir / "demo.yaml").resolve())
        assert graphs["demo"]["path"] == str(graph_dir / "demo.yaml")
        assert graphs["demo"]["texts"] == {demo_key: stopped["demo"]}
        assert "low_level: 0.9" in stopped["demo"]
        assert graphs["demo"]["saved"] == {demo_key: FLOW_TEXT}  # what a later change on disk is compared to
        assert graphs["demo"]["positions"]["probe"] == [11.0, 22.0]
        assert graphs["demo"]["groups"] == GROUPS
        assert graphs["demo"]["layout_dirty"] is True
        fresh_key = str((graph_dir / "fresh.yaml").resolve())
        assert graphs["fresh"]["saved"] == {fresh_key: FRESH_TEXT}  # New wrote it: the starter is on disk
        assert "label: edited" in graphs["fresh"]["texts"][fresh_key]
        assert graphs["away"]["texts"] == {str(away.resolve()): stopped["away"]}

    def test_a_graph_opened_or_made_with_new_but_not_changed_is_not_kept(
        self, graph_dir: Path, away: Path, tmp_path: Path
    ) -> None:
        editor = _Editor(graph_dir, tmp_path)
        editor.ok("get", "/api/graphs/demo")
        editor.ok("post", "/api/graphs/open", {"path": str(away)})
        editor.ok("post", "/api/graphs", {"name": "fresh", "kind": "flow"})
        assert editor.studio.stash() == []
        assert not (graph_dir / RECOVERY_FILE).exists()
        assert (graph_dir / "fresh.yaml").read_text() == starter_text("flow", "fresh")

    def test_the_file_is_never_listed(self, graph_dir: Path, tmp_path: Path, stopped: Dict[str, str]) -> None:
        editor = _Editor(graph_dir, tmp_path)
        assert [entry["name"] for entry in editor.ok("get", "/api/graphs")] == ["demo", "fresh"]
        assert [entry["name"] for entry in editor.ok("get", "/api/files")["entries"]] == ["demo.yaml", "fresh.yaml"]

    def test_the_file_names_are_the_ones_git_ignores(self) -> None:
        """A stop inside a checkout must never leave a committable file: this project's ``.gitignore`` holds the
        pattern (so do the ones of the projects whose folders hold editor graphs — waivefront, annotaide)."""
        assert RECOVERY_FILE == ".streamstudio-workspace.json"
        assert BROKEN_RECOVERY_FILE == ".streamstudio-workspace.broken.json"
        gitignore = Path(__file__).resolve().parent.parent / ".gitignore"
        if not gitignore.is_file():  # pragma: no cover - a test run outside the checkout
            pytest.skip(f"{gitignore} is not there — the tests run outside the checkout")
        patterns = [line.strip() for line in gitignore.read_text().splitlines() if line.strip()[:1] not in ("", "#")]
        written = [
            RECOVERY_FILE,
            BROKEN_RECOVERY_FILE,
            app_module._aside_name(2),
            app_module._writing_name(),
        ]
        assert app_module._aside_name(1) == BROKEN_RECOVERY_FILE
        assert [name for name in written if not any(fnmatch(name, pattern) for pattern in patterns)] == []


# ------------------------------------------------------------------------------------------------
# GET /api/workspace
# ------------------------------------------------------------------------------------------------


class TestTheWorkspaceRoute:
    def test_without_a_file_there_is_nothing_to_recover(self, graph_dir: Path, tmp_path: Path) -> None:
        editor = _Editor(graph_dir, tmp_path)
        assert editor.ok("get", "/api/workspace") == {
            "folder": str(graph_dir),
            "session": editor.studio.session,
            "recovery": None,
        }

    def test_the_next_editor_lists_what_a_restore_would_open(
        self, graph_dir: Path, away: Path, tmp_path: Path, stopped: Dict[str, str]
    ) -> None:
        editor = _Editor(graph_dir, tmp_path)
        answer = editor.ok("get", "/api/workspace")
        saved_at = _recovery(graph_dir)["saved_at"]
        row = {"disk": "unchanged", "reason": None, "saved_at": saved_at, "changed_on_disk": False}
        assert answer == {
            "folder": str(graph_dir),
            "session": editor.studio.session,
            "recovery": {
                "saved_at": saved_at,
                "error": None,
                "aside": None,
                "graphs": [
                    {"name": "demo", "path": str(graph_dir / "demo.yaml"), **row},
                    {"name": "fresh", "path": str(graph_dir / "fresh.yaml"), **row},
                    {"name": "away", "path": str(away), **row},
                ],
            },
        }

    def test_a_file_changed_on_disk_after_the_stop_is_flagged(
        self, graph_dir: Path, tmp_path: Path, stopped: Dict[str, str]
    ) -> None:
        (graph_dir / "demo.yaml").write_text(FLOW_TEXT + "# theirs\n")
        (graph_dir / "fresh.yaml").write_text(FLOW_TEXT)  # someone wrote over the starter New made meanwhile
        graphs = _Editor(graph_dir, tmp_path).ok("get", "/api/workspace")["recovery"]["graphs"]
        assert [(entry["name"], entry["changed_on_disk"], entry["disk"]) for entry in graphs] == [
            ("demo", True, "changed"),
            ("fresh", True, "changed"),
            ("away", False, "unchanged"),
        ]

    def test_a_file_deleted_after_the_stop_is_said_to_be_gone(
        self, graph_dir: Path, tmp_path: Path, stopped: Dict[str, str]
    ) -> None:
        """Measured before (R10): the dialog said a plain ``flow.yaml changed`` and a Save silently wrote the file the
        person had deleted (or moved) back."""
        (graph_dir / "demo.yaml").unlink()
        graphs = _Editor(graph_dir, tmp_path).ok("get", "/api/workspace")["recovery"]["graphs"]
        demo = next(entry for entry in graphs if entry["name"] == "demo")
        assert (demo["disk"], demo["changed_on_disk"], demo["reason"]) == ("gone", False, None)
        fresh = next(entry for entry in graphs if entry["name"] == "fresh")
        assert fresh["disk"] == "unchanged"  # its file is as the stop left it

    def test_a_kept_file_that_cannot_be_read_is_reported_and_never_stops_the_answer(
        self, graph_dir: Path, away: Path, tmp_path: Path, stopped: Dict[str, str]
    ) -> None:
        """Measured before (R2): the start itself died — ``PermissionError: … away.yaml`` from ``workspace()`` — and
        every later start the same way, until someone found and deleted the hidden file by hand."""
        away.chmod(0)
        try:
            graphs = _Editor(graph_dir, tmp_path).ok("get", "/api/workspace")["recovery"]["graphs"]
        finally:
            away.chmod(0o644)
        row = next(entry for entry in graphs if entry["name"] == "away")
        assert (row["disk"], row["changed_on_disk"]) == ("unreadable", False)
        assert row["reason"] == f"{away.resolve()} cannot be read: Permission denied"

    def test_a_kept_file_that_is_no_longer_utf8_is_reported_in_words(
        self, graph_dir: Path, tmp_path: Path, stopped: Dict[str, str]
    ) -> None:
        demo = graph_dir / "demo.yaml"
        demo.write_bytes("# caf\xe9\n".encode("latin-1") + FLOW_TEXT.encode())
        row = _Editor(graph_dir, tmp_path).ok("get", "/api/workspace")["recovery"]["graphs"][0]
        assert row["disk"] == "unreadable"
        assert row["reason"] == f"{demo.resolve()} cannot be read: it is not UTF-8 text (byte 0xe9 at position 5)"

    def test_a_file_that_is_not_json_is_reported_in_words_and_the_editor_still_starts(
        self, graph_dir: Path, tmp_path: Path
    ) -> None:
        (graph_dir / RECOVERY_FILE).write_text("{not json")
        editor = _Editor(graph_dir, tmp_path)
        recovery = editor.ok("get", "/api/workspace")["recovery"]
        assert recovery["saved_at"] is None and recovery["graphs"] == []
        assert recovery["error"].startswith(f"{graph_dir / RECOVERY_FILE} cannot be read: ")
        assert [entry["name"] for entry in editor.ok("get", "/api/graphs")] == ["demo"]

    @pytest.mark.parametrize(
        "content, why",
        [
            ([1, 2], "it holds a list, not the editor's mapping"),
            ({"saved_at": "x", "graphs": 3}, "'graphs' must be a list"),
            ({"saved_at": "x", "graphs": [{"name": "demo"}]}, "graph 1 has no 'path'"),
        ],
    )
    def test_json_the_editor_did_not_write_is_reported_saying_what_is_wrong(
        self, graph_dir: Path, tmp_path: Path, content: Any, why: str
    ) -> None:
        (graph_dir / RECOVERY_FILE).write_text(json.dumps(content))
        recovery = _Editor(graph_dir, tmp_path).ok("get", "/api/workspace")["recovery"]
        assert recovery["error"] == f"{graph_dir / RECOVERY_FILE} is not a recovery file the editor wrote: {why}"


# ------------------------------------------------------------------------------------------------
# restore
# ------------------------------------------------------------------------------------------------


class TestRestore:
    def test_each_graph_opens_as_it_was_unsaved_and_the_file_is_removed(
        self, graph_dir: Path, away: Path, tmp_path: Path, stopped: Dict[str, str]
    ) -> None:
        editor = _Editor(graph_dir, tmp_path)
        listed = editor.ok("post", "/api/workspace/recovery/restore")
        assert [(entry["name"], entry["dirty"]) for entry in listed] == [
            ("away", True),
            ("demo", True),
            ("fresh", True),
        ]
        assert not (graph_dir / RECOVERY_FILE).exists()
        demo = editor.ok("get", "/api/graphs/demo")
        assert demo["text"] == stopped["demo"]
        assert demo["positions"]["probe"] == [11.0, 22.0]
        assert demo["groups"] == GROUPS
        assert editor.ok("get", "/api/graphs/fresh")["text"] == stopped["fresh"]
        assert editor.ok("get", "/api/graphs/away")["path"] == str(away)
        # nothing was written: a restore holds, only Save writes
        assert (graph_dir / "demo.yaml").read_text() == FLOW_TEXT
        assert (graph_dir / "fresh.yaml").read_text() == FRESH_TEXT
        assert away.read_text() == AWAY_TEXT
        assert editor.ok("get", "/api/workspace")["recovery"] is None

    def test_the_undo_history_is_not_kept(self, graph_dir: Path, tmp_path: Path, stopped: Dict[str, str]) -> None:
        editor = _Editor(graph_dir, tmp_path)
        editor.ok("post", "/api/workspace/recovery/restore")
        assert editor.ok("post", "/api/graphs/demo/undo")["changed"] is False

    def test_restore_then_save_then_stop_leaves_no_file(
        self, graph_dir: Path, away: Path, tmp_path: Path, stopped: Dict[str, str]
    ) -> None:
        editor = _Editor(graph_dir, tmp_path)
        editor.ok("post", "/api/workspace/recovery/restore")
        for name in ("demo", "fresh", "away"):
            editor.ok("post", f"/api/graphs/{name}/save")
        assert editor.studio.stash() == []
        assert not (graph_dir / RECOVERY_FILE).exists()
        assert (graph_dir / "demo.yaml").read_text() == stopped["demo"]
        assert (graph_dir / "fresh.yaml").read_text() == stopped["fresh"]

    def test_a_file_changed_on_disk_meanwhile_gets_the_save_refusal(
        self, graph_dir: Path, tmp_path: Path, stopped: Dict[str, str]
    ) -> None:
        (graph_dir / "demo.yaml").write_text(FLOW_TEXT + "# theirs\n")
        editor = _Editor(graph_dir, tmp_path)
        editor.ok("post", "/api/workspace/recovery/restore")
        assert editor.refused("post", "/api/graphs/demo/save") == CHANGED_ON_DISK.format(
            path=(graph_dir / "demo.yaml").resolve()
        )
        assert (graph_dir / "demo.yaml").read_text() == FLOW_TEXT + "# theirs\n"

    def test_a_graph_with_unsaved_edits_of_this_session_keeps_them_and_the_kept_work_opens_beside_it(
        self, graph_dir: Path, tmp_path: Path, stopped: Dict[str, str]
    ) -> None:
        """Measured before (P4): the restore was refused ("Save or Revert them first"), and the page's question offered
        nothing but discarding — the kept work was reachable only by throwing it away. Now neither version is lost: the
        kept one opens beside this session's under a free name, and the second Save gets the changed-on-disk refusal."""
        editor = _Editor(graph_dir, tmp_path)
        mine = editor.ok("post", "/api/graphs/demo/param", {"node": "mask", "param": "low_level", "value": 0.1})
        listed = editor.ok("post", "/api/workspace/recovery/restore")
        assert [(entry["name"], entry["path"], entry["dirty"], entry["restored"]) for entry in listed] == [
            ("away", listed[0]["path"], True, True),
            ("demo", str(graph_dir / "demo.yaml"), True, False),
            ("demo~2", str(graph_dir / "demo.yaml"), True, True),
            ("fresh", str(graph_dir / "fresh.yaml"), True, True),
        ]
        assert editor.ok("get", "/api/graphs/demo")["text"] == mine["text"]
        assert editor.ok("get", "/api/graphs/demo~2")["text"] == stopped["demo"]
        assert not (graph_dir / RECOVERY_FILE).exists()
        editor.ok("post", "/api/graphs/demo/save")
        assert editor.refused("post", "/api/graphs/demo~2/save") == CHANGED_ON_DISK.format(
            path=(graph_dir / "demo.yaml").resolve()
        )

    def test_a_graph_whose_name_is_taken_gets_a_free_one(
        self, graph_dir: Path, away: Path, tmp_path: Path, stopped: Dict[str, str]
    ) -> None:
        (graph_dir / "away.yaml").write_text(AWAY_TEXT)  # the folder now has its own `away`
        editor = _Editor(graph_dir, tmp_path)
        listed = {entry["name"]: entry["path"] for entry in editor.ok("post", "/api/workspace/recovery/restore")}
        assert listed["away"] == str(graph_dir / "away.yaml")
        assert listed["away~2"] == str(away)

    def test_with_no_file_it_is_404(self, graph_dir: Path, tmp_path: Path) -> None:
        assert _Editor(graph_dir, tmp_path).refused("post", "/api/workspace/recovery/restore", 404) == (
            f"no unsaved work of an earlier session in {graph_dir}"
        )

    def test_a_broken_file_is_refused_in_the_words_the_workspace_reports(self, graph_dir: Path, tmp_path: Path) -> None:
        (graph_dir / RECOVERY_FILE).write_text("{not json")
        editor = _Editor(graph_dir, tmp_path)
        error = editor.ok("get", "/api/workspace")["recovery"]["error"]
        assert editor.refused("post", "/api/workspace/recovery/restore") == error
        assert (graph_dir / RECOVERY_FILE).read_text() == "{not json"


# ------------------------------------------------------------------------------------------------
# discard
# ------------------------------------------------------------------------------------------------


class TestDiscard:
    def test_the_file_is_removed_and_the_saved_files_open(
        self, graph_dir: Path, tmp_path: Path, stopped: Dict[str, str]
    ) -> None:
        editor = _Editor(graph_dir, tmp_path)
        listed = editor.ok("post", "/api/workspace/recovery/discard")
        assert [(entry["name"], entry["dirty"]) for entry in listed] == [("demo", False), ("fresh", False)]
        assert not (graph_dir / RECOVERY_FILE).exists()
        assert editor.ok("get", "/api/graphs/demo")["text"] == FLOW_TEXT
        assert editor.ok("get", "/api/graphs/fresh")["text"] == FRESH_TEXT

    def test_a_broken_file_is_moved_aside_not_deleted(self, graph_dir: Path, tmp_path: Path) -> None:
        (graph_dir / RECOVERY_FILE).write_text("{not json")
        editor = _Editor(graph_dir, tmp_path)
        assert editor.ok("get", "/api/workspace")["recovery"]["aside"] == str(graph_dir / BROKEN_RECOVERY_FILE)
        editor.ok("post", "/api/workspace/recovery/discard")
        assert not (graph_dir / RECOVERY_FILE).exists()
        assert (graph_dir / BROKEN_RECOVERY_FILE).read_text() == "{not json"
        assert editor.ok("get", "/api/workspace")["recovery"] is None

    def test_a_second_broken_file_is_moved_aside_under_its_own_name(self, graph_dir: Path, tmp_path: Path) -> None:
        """Measured before (R9): the second set-aside overwrote the first ``.broken.json`` — "kept, never deleted"
        held for one broken file only."""
        (graph_dir / BROKEN_RECOVERY_FILE).write_text("FIRST SESSION WORK")
        (graph_dir / RECOVERY_FILE).write_text("{SECOND SESSION WORK")
        editor = _Editor(graph_dir, tmp_path)
        second = graph_dir / ".streamstudio-workspace.broken-2.json"
        assert editor.ok("get", "/api/workspace")["recovery"]["aside"] == str(second)
        editor.ok("post", "/api/workspace/recovery/discard")
        assert (graph_dir / BROKEN_RECOVERY_FILE).read_text() == "FIRST SESSION WORK"
        assert second.read_text() == "{SECOND SESSION WORK"
        (graph_dir / RECOVERY_FILE).write_text("{THIRD")
        editor.ok("post", "/api/graphs/demo/param", {"node": "mask", "param": "low_level", "value": 0.3})
        editor.studio.stash()  # a stop moves an unreadable file aside too — under the next free name
        assert (graph_dir / ".streamstudio-workspace.broken-3.json").read_text() == "{THIRD"
        assert [entry["name"] for entry in _recovery(graph_dir)["graphs"]] == ["demo"]

    def test_with_no_file_it_is_404(self, graph_dir: Path, tmp_path: Path) -> None:
        assert _Editor(graph_dir, tmp_path).refused("post", "/api/workspace/recovery/discard", 404) == (
            f"no unsaved work of an earlier session in {graph_dir}"
        )


# ------------------------------------------------------------------------------------------------
# a stop before anyone answered the question
# ------------------------------------------------------------------------------------------------


class TestAStopBeforeTheQuestionWasAnswered:
    def test_nothing_unsaved_leaves_the_earlier_file_exactly_as_it_was(
        self, graph_dir: Path, tmp_path: Path, stopped: Dict[str, str]
    ) -> None:
        before = (graph_dir / RECOVERY_FILE).read_bytes()
        editor = _Editor(graph_dir, tmp_path)
        editor.ok("get", "/api/graphs/demo")
        assert editor.studio.stash() == []
        assert (graph_dir / RECOVERY_FILE).read_bytes() == before

    def test_new_unsaved_work_joins_the_earlier_file_and_a_graph_changed_again_keeps_both_versions(
        self, graph_dir: Path, away: Path, tmp_path: Path, stopped: Dict[str, str]
    ) -> None:
        """Measured before (R5, P1): a file this session changed again REPLACED the earlier kept version, although
        nobody had answered for it — two editors on one folder, or a page left open across a restart, lost the first
        version for good while the stop said it was kept."""
        earlier_at = _recovery(graph_dir)["saved_at"]
        editor = _Editor(graph_dir, tmp_path)
        editor.ok("post", "/api/graphs", {"name": "second", "kind": "flow"})
        editor.ok("post", "/api/graphs/second/param", {"node": "probe", "param": "label", "value": "second"})
        editor.ok("post", "/api/graphs/open", {"path": str(away)})
        editor.ok("post", "/api/graphs/away/param", {"node": "away", "param": "label", "value": "again"})
        kept = editor.studio.stash()
        assert kept == [str(graph_dir / "second.yaml"), str(away)]  # what THIS stop kept
        entries = _recovery(graph_dir)["graphs"]
        assert [entry["name"] for entry in entries] == ["second", "away", "demo", "fresh", "away"]
        key = str(away.resolve())
        assert "label: again" in entries[1]["texts"][key]
        assert "label: moved" in entries[4]["texts"][key]  # the earlier version, still there
        assert [entry["saved_at"] for entry in entries[2:]] == [earlier_at] * 3
        assert entries[2]["texts"] == {str((graph_dir / "demo.yaml").resolve()): stopped["demo"]}

    def test_two_versions_of_one_file_are_two_rows_and_restore_opens_both(
        self, graph_dir: Path, away: Path, tmp_path: Path, stopped: Dict[str, str]
    ) -> None:
        first = _Editor(graph_dir, tmp_path)
        first.ok("post", "/api/graphs/demo/param", {"node": "mask", "param": "low_level", "value": 0.11})
        first.studio.stash()
        editor = _Editor(graph_dir, tmp_path)
        rows = editor.ok("get", "/api/workspace")["recovery"]["graphs"]
        assert [(row["name"], Path(row["path"]).name) for row in rows] == [
            ("demo", "demo.yaml"),
            ("demo", "demo.yaml"),
            ("fresh", "fresh.yaml"),
            ("away", "away.yaml"),
        ]
        listed = {entry["name"]: entry for entry in editor.ok("post", "/api/workspace/recovery/restore")}
        assert {name for name, entry in listed.items() if entry["restored"]} == {"demo", "demo~2", "fresh", "away"}
        assert "low_level: 0.11" in editor.ok("get", "/api/graphs/demo")["text"]
        assert "low_level: 0.9" in editor.ok("get", "/api/graphs/demo~2")["text"]
        assert listed["demo~2"]["path"] == str(graph_dir / "demo.yaml")

    def test_an_earlier_file_that_cannot_be_read_is_moved_aside_before_the_new_one_is_written(
        self, graph_dir: Path, tmp_path: Path
    ) -> None:
        (graph_dir / RECOVERY_FILE).write_text("{not json")
        editor = _Editor(graph_dir, tmp_path)
        editor.ok("post", "/api/graphs/demo/param", {"node": "mask", "param": "low_level", "value": 0.3})
        assert editor.studio.stash() == [str(graph_dir / "demo.yaml")]
        assert (graph_dir / BROKEN_RECOVERY_FILE).read_text() == "{not json"
        assert [entry["name"] for entry in _recovery(graph_dir)["graphs"]] == ["demo"]


# ------------------------------------------------------------------------------------------------
# a page left open across a restart
# ------------------------------------------------------------------------------------------------


class TestAPageOfAnEarlierSession:
    """Measured before (R1, P1): a tab left open across ``streamstudio restart`` kept editing the NEW editor's document
    — the one read from disk — so the kept ``0.8`` vanished from the page on the next edit, Save wrote the file without
    it and said clean, and the next stop replaced the kept version. Every editor now has a SESSION; a page sends the one
    it joined (``GET /api/workspace``) with every request, and a request of another session changes nothing."""

    def test_the_workspace_names_the_session_a_page_joins(self, graph_dir: Path, tmp_path: Path) -> None:
        first, second = _Editor(graph_dir, tmp_path), _Editor(graph_dir, tmp_path)
        assert first.ok("get", "/api/workspace")["session"] == first.studio.session
        assert first.studio.session != second.studio.session

    def test_a_request_of_another_session_is_refused_and_changes_nothing(
        self, graph_dir: Path, tmp_path: Path, stopped: Dict[str, str]
    ) -> None:
        editor = _Editor(graph_dir, tmp_path)
        header = {app_module.SESSION_HEADER: "an-earlier-session"}
        edit = {"node": "mask", "param": "low_level", "value": 0.5}
        for method, url, body in [
            ("post", "/api/graphs/demo/param", edit),
            ("post", "/api/graphs/demo/save", None),
            ("get", "/api/graphs", None),
        ]:
            response = getattr(editor.client, method)(url, json=body, headers=header)
            assert response.status_code == 409, (url, response.get_json())
            assert response.get_json() == {"error": app_module.RESTARTED}
        assert editor.studio.dirty("demo") is False
        assert (graph_dir / "demo.yaml").read_text() == FLOW_TEXT
        assert editor.studio.stash() == []
        assert len(_recovery(graph_dir)["graphs"]) == 3  # the kept work waits, whole

    def test_the_workspace_answers_a_page_of_any_session_so_it_can_join_again(
        self, graph_dir: Path, tmp_path: Path, stopped: Dict[str, str]
    ) -> None:
        editor = _Editor(graph_dir, tmp_path)
        response = editor.client.get("/api/workspace", headers={app_module.SESSION_HEADER: "an-earlier-session"})
        assert response.status_code == 200
        assert response.get_json()["session"] == editor.studio.session
        assert response.get_json()["recovery"]["graphs"][0]["name"] == "demo"

    def test_a_request_of_this_session_or_of_no_session_is_served(self, graph_dir: Path, tmp_path: Path) -> None:
        """A tool or a script sends no session: it is served as before."""
        editor = _Editor(graph_dir, tmp_path)
        edit = {"node": "mask", "param": "low_level", "value": 0.2}
        own = {app_module.SESSION_HEADER: editor.studio.session}
        assert editor.client.post("/api/graphs/demo/param", json=edit, headers=own).status_code == 200
        assert editor.ok("post", "/api/graphs/demo/param", {**edit, "value": 0.3})["dirty"] is True


# ------------------------------------------------------------------------------------------------
# the folder moved
# ------------------------------------------------------------------------------------------------


class TestAMovedFolder:
    def test_the_kept_work_lands_on_the_files_of_the_folder_the_editor_serves(
        self, graph_dir: Path, away: Path, tmp_path: Path, stopped: Dict[str, str]
    ) -> None:
        """Measured before (R4): after ``mv C C-moved`` the restore attached the work to the OLD path as ``flow~2`` —
        a second ``flow.yaml`` with ● — and its Save was a bare HTML 500 (``FileNotFoundError``)."""
        moved = graph_dir.rename(tmp_path / "moved")
        editor = _Editor(moved, tmp_path)
        rows = editor.ok("get", "/api/workspace")["recovery"]["graphs"]
        assert [(row["name"], row["path"], row["disk"]) for row in rows] == [
            ("demo", str(moved / "demo.yaml"), "unchanged"),
            ("fresh", str(moved / "fresh.yaml"), "unchanged"),
            ("away", str(away), "unchanged"),  # a file of another folder stays where it is
        ]
        listed = {entry["name"]: entry["path"] for entry in editor.ok("post", "/api/workspace/recovery/restore")}
        assert listed == {
            "away": str(away),
            "demo": str(moved / "demo.yaml"),
            "fresh": str(moved / "fresh.yaml"),
        }
        editor.ok("post", "/api/graphs/demo/save")
        assert (moved / "demo.yaml").read_text() == stopped["demo"]


# ------------------------------------------------------------------------------------------------
# Save of a file the editor cannot read or write: a located 400, never an HTML 500
# ------------------------------------------------------------------------------------------------


class TestASaveThatCannotReadOrWrite:
    def test_a_file_that_cannot_be_read_is_a_located_refusal(
        self, graph_dir: Path, away: Path, tmp_path: Path, stopped: Dict[str, str]
    ) -> None:
        editor = _Editor(graph_dir, tmp_path)
        editor.ok("post", "/api/workspace/recovery/restore")
        away.chmod(0)
        try:
            message = editor.refused("post", "/api/graphs/away/save")
        finally:
            away.chmod(0o644)
        assert message.startswith(f"{away.resolve()} cannot be read: Permission denied — ")
        assert away.read_text() == AWAY_TEXT

    def test_a_folder_that_is_gone_is_a_located_refusal(self, graph_dir: Path, tmp_path: Path) -> None:
        folder = tmp_path / "later"
        folder.mkdir()
        editor = _Editor(graph_dir, tmp_path)
        editor.ok("post", "/api/graphs", {"name": "fresh", "kind": "flow", "dir": str(folder)})
        editor.ok("post", "/api/graphs/fresh/param", {"node": "probe", "param": "label", "value": "edited"})
        (folder / "fresh.yaml").unlink()
        folder.rmdir()
        message = editor.refused("post", "/api/graphs/fresh/save")
        assert message == f"{folder / 'fresh.yaml'} cannot be written: No such file or directory"


# ------------------------------------------------------------------------------------------------
# two editors on one folder
# ------------------------------------------------------------------------------------------------


class TestTwoEditorsStoppingAtOnce:
    def test_each_stop_merges_under_a_lock_and_writes_through_its_own_file(
        self, graph_dir: Path, tmp_path: Path
    ) -> None:
        """Measured before (R3, 4 of 4 trials): two editors stopped at once wrote through ONE shared
        ``.writing.json`` and merged without a lock — one editor's work was lost while its terminal said it was kept,
        and once the interleaved writes left a file that was not JSON. Here many pairs of stops race in threads (the
        lock is on the folder, so two opens of it in one process exclude each other as two processes do)."""
        (graph_dir / "flow.yaml").write_text(AWAY_TEXT)
        for trial in range(20):
            first, second = _Editor(graph_dir, tmp_path), _Editor(graph_dir, tmp_path)
            first.ok("post", "/api/graphs/demo/param", {"node": "mask", "param": "low_level", "value": 0.2})
            second.ok("post", "/api/graphs/flow/param", {"node": "away", "param": "label", "value": "b"})
            barrier = threading.Barrier(2)
            kept: Dict[str, List[str]] = {}

            def stop(name: str, editor: _Editor) -> None:
                barrier.wait()
                kept[name] = editor.studio.stash()

            threads = [threading.Thread(target=stop, args=pair) for pair in (("a", first), ("b", second))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            assert kept == {"a": [str(graph_dir / "demo.yaml")], "b": [str(graph_dir / "flow.yaml")]}, trial
            assert sorted(entry["name"] for entry in _recovery(graph_dir)["graphs"]) == ["demo", "flow"], trial
            assert [path.name for path in graph_dir.iterdir() if path.name.startswith(".")] == [RECOVERY_FILE]
            (graph_dir / RECOVERY_FILE).unlink()


def test_the_studio_reads_nothing_until_asked(graph_dir: Path, tmp_path: Path) -> None:
    """The constructor stores only (workspace rule): a recovery file is read when a route asks, not at start."""
    (graph_dir / RECOVERY_FILE).write_text("{not json")
    studio = app_module.Studio(graph_dir, templates_dir=tmp_path / "templates")
    assert studio.recovery_path == graph_dir / RECOVERY_FILE
    assert studio.workspace()["recovery"]["error"] is not None
