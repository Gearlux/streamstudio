"""New, Open, the file browser and the graph list — the editor is no longer bound to one folder's files.

The graph list is a NAME → FILE registry: every ``*.yaml`` of the folder the editor was started on
(rescanned on every list, so a file that appears later shows up), every file opened with File → Open,
and every file File → New… wrote. A ``*.layout.yaml`` (positions sidecar) and a ``*.steps.yaml`` (a
subgraph's steps file) are never graphs. A name is the file's stem; a stem already taken gets ``~2``,
``~3`` … so two ``demo.yaml`` from two folders can be open side by side.

New… WRITES its starter file at once (the user, 2026-09-29: "New ... by mistake should not happen. It should
ask to save") and answers it with nothing unsaved: there is never a graph that exists only in the editor. A
refusal — the file exists, the folder does not exist / is not a folder / cannot be written, a name the name
rule refuses — writes NOTHING. The starter text is one per kind, and each is pinned to derive with NO problem
— a New graph that opens with a red node would be a broken first impression.
"""

import errno
import os
from pathlib import Path
from typing import Any, List, get_args, get_type_hints

import pytest

from streamstudio.app import NEW_GRAPH_KINDS, FileEntryKind, NewGraphKind, Studio, create_app, starter_text
from streamstudio.document import ALREADY_EXISTS, Document

PROBE = "recordstream.ops.debug.PrintRecordOp"
FLOW_TEXT = f"flow:\n  probe:\n    op: !class:{PROBE} {{label: probe}}\noutputs: probe\n"


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


def _names(client: Any) -> List[str]:
    return [entry["name"] for entry in client.get("/api/graphs").get_json()]


# ------------------------------------------------------------------------------------------------
# New
# ------------------------------------------------------------------------------------------------


class TestNew:
    def test_the_kinds_are_a_closed_set_with_one_starter_each(self) -> None:
        assert len({starter_text(kind, "x") for kind in NEW_GRAPH_KINDS}) == 3
        assert NEW_GRAPH_KINDS == ("flow", "pipeline", "source")
        with pytest.raises(ValueError, match="kind must be one of flow, pipeline, source — got 'sink'"):
            starter_text("sink", "x")  # type: ignore[arg-type]

    @pytest.mark.parametrize("kind", ["flow", "pipeline", "source"])
    def test_every_starter_derives_with_no_problem(self, tmp_path: Path, kind: NewGraphKind) -> None:
        path = tmp_path / "fresh.yaml"
        document = Document.create(path, starter_text(kind, "fresh"))
        assert document.model.nodes, kind
        assert document.check() == [], kind
        assert document.dirty is False, kind
        assert path.read_text() == document.text == starter_text(kind, "fresh")

    @pytest.mark.parametrize(
        "kind, listed_kind, nodes",
        [
            ("flow", "flow", ["probe"]),
            ("pipeline", "graph", ["stream", "stream.ops[0]"]),
            ("source", "contract", ["probe", "contract"]),
        ],
    )
    def test_new_writes_the_starter_at_once_and_answers_it_with_nothing_unsaved(
        self, client: Any, graph_dir: Path, kind: NewGraphKind, listed_kind: str, nodes: List[str]
    ) -> None:
        response = client.post("/api/graphs", json={"name": "fresh", "kind": kind})
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["name"] == "fresh" and body["path"] == str(graph_dir / "fresh.yaml")
        assert body["dirty"] is False and body["kind"] == listed_kind
        assert sorted(node["name"] for node in body["model"]["nodes"]) == sorted(nodes)
        assert body["model"]["problems"] == []
        assert (graph_dir / "fresh.yaml").read_text() == body["text"] == starter_text(kind, "fresh")
        assert not (graph_dir / "fresh.layout.yaml").exists()  # the starter file only: positions are laid out
        listed = {entry["name"]: entry for entry in client.get("/api/graphs").get_json()}
        assert (listed["fresh"]["path"], listed["fresh"]["dirty"]) == (str(graph_dir / "fresh.yaml"), False)
        assert client.post("/api/graphs/fresh/save").get_json()["dirty"] is False  # nothing to write
        assert (graph_dir / "fresh.yaml").read_text() == body["text"]

    def test_an_edit_of_a_new_graph_is_an_ordinary_unsaved_change(self, client: Any, graph_dir: Path) -> None:
        body = client.post("/api/graphs", json={"name": "fresh", "kind": "flow"}).get_json()
        edit = {"node": "probe", "param": "label", "value": "edited"}
        assert client.post("/api/graphs/fresh/param", json=edit).get_json()["dirty"] is True
        assert (graph_dir / "fresh.yaml").read_text() == body["text"]  # the edit waits for Save
        saved = client.post("/api/graphs/fresh/save").get_json()
        assert saved["dirty"] is False and "label: edited" in (graph_dir / "fresh.yaml").read_text()

    def test_the_source_starter_appends_its_contract_after_the_flow(self, client: Any) -> None:
        """Measured: a contract written BEFORE ``flow:`` receives the file's ``outputs:`` line and does not build."""
        text = client.post("/api/graphs", json={"name": "src", "kind": "source"}).get_json()["text"]
        assert text.index("flow:") < text.index("outputs:") < text.index("contract: !class:")
        assert "  name: src\n" in text

    def test_new_in_another_directory_writes_there_and_joins_the_list(self, client: Any, tmp_path: Path) -> None:
        other = tmp_path / "other"
        other.mkdir()
        body = client.post("/api/graphs", json={"name": "fresh", "kind": "flow", "dir": str(other)}).get_json()
        assert (body["name"], body["path"], body["dirty"]) == ("fresh", str(other / "fresh.yaml"), False)
        assert (other / "fresh.yaml").read_text() == body["text"]
        assert _names(client) == ["demo", "fresh"]

    def test_a_stem_the_list_already_holds_joins_under_a_free_name_like_open(
        self, client: Any, graph_dir: Path, tmp_path: Path
    ) -> None:
        """A folder other than the editor's joins like a file opened with File ▸ Open: ``demo`` is the folder's own
        ``demo.yaml``, so the new ``other/demo.yaml`` is listed as ``demo~2`` — both files side by side."""
        other = tmp_path / "other"
        other.mkdir()
        response = client.post("/api/graphs", json={"name": "demo", "kind": "flow", "dir": str(other)})
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert (body["name"], body["path"], body["dirty"]) == ("demo~2", str(other / "demo.yaml"), False)
        assert (other / "demo.yaml").read_text() == starter_text("flow", "demo")
        assert (graph_dir / "demo.yaml").read_text() == FLOW_TEXT
        assert _names(client) == ["demo", "demo~2"]

    @pytest.mark.parametrize("where", ["the graph folder", "another folder"])
    def test_a_file_that_exists_is_refused_and_left_byte_for_byte(
        self, client: Any, graph_dir: Path, tmp_path: Path, where: str
    ) -> None:
        folder = graph_dir if where == "the graph folder" else tmp_path / "other"
        folder.mkdir(exist_ok=True)
        taken = folder / "demo.yaml"
        taken.write_bytes(b"kept: 1   # theirs\r\n")
        asked = {"name": "demo", "kind": "flow", **({} if folder == graph_dir else {"dir": str(folder)})}
        response = client.post("/api/graphs", json=asked)
        assert response.status_code == 400
        assert _error(response) == f"{taken} already exists — pick another name, or open it with File ▸ Open"
        assert taken.read_bytes() == b"kept: 1   # theirs\r\n"
        assert sorted(path.name for path in folder.iterdir()) == ["demo.yaml"]

    def test_a_file_that_appears_between_the_check_and_the_write_is_never_overwritten(self, tmp_path: Path) -> None:
        """The write itself refuses an existing file (it creates the file or fails), so a file someone made after the
        route looked is left as it is — the same refusal, in the same words."""
        taken = tmp_path / "fresh.yaml"
        taken.write_bytes(b"theirs\n")
        with pytest.raises(ValueError) as refused:
            Document.create(taken, starter_text("flow", "fresh"))
        assert str(refused.value) == ALREADY_EXISTS.format(path=taken)
        assert taken.read_bytes() == b"theirs\n"

    def test_a_graph_open_on_a_file_gone_from_disk_is_not_written_over(self, client: Any, graph_dir: Path) -> None:
        """``demo`` is held by the editor (unsaved work, maybe) while its file was deleted: a New ``demo`` would make a
        second entry on the SAME file, whose first Save then finds a file it never read. Refused, nothing written."""
        client.post("/api/graphs/demo/param", json={"node": "probe", "param": "label", "value": "mine"})
        (graph_dir / "demo.yaml").unlink()
        response = client.post("/api/graphs", json={"name": "demo", "kind": "flow"})
        assert response.status_code == 400
        assert _error(response) == (
            f"{graph_dir / 'demo.yaml'} is open as 'demo' — its file is gone from disk; Save that graph, or pick "
            "another name"
        )
        assert not (graph_dir / "demo.yaml").exists()
        assert "label: mine" in client.get("/api/graphs/demo").get_json()["text"]

    def test_an_unknown_kind_is_refused_naming_the_kinds(self, client: Any, graph_dir: Path) -> None:
        response = client.post("/api/graphs", json={"name": "x", "kind": "sink"})
        assert response.status_code == 400
        assert _error(response) == "kind must be one of flow, pipeline, source — got 'sink'"
        assert sorted(path.name for path in graph_dir.iterdir()) == ["demo.yaml"]

    @pytest.mark.parametrize("name", ["", "a/b", ".hidden", "x.layout", "x.steps"])
    def test_a_name_that_cannot_be_a_file_stem_is_refused(self, client: Any, graph_dir: Path, name: str) -> None:
        response = client.post("/api/graphs", json={"name": name, "kind": "flow"})
        assert response.status_code == 400
        assert repr(name) in _error(response)
        assert sorted(path.name for path in graph_dir.iterdir()) == ["demo.yaml"]

    def test_a_missing_folder_is_404_naming_it(self, client: Any, tmp_path: Path) -> None:
        nowhere = tmp_path / "nowhere"
        response = client.post("/api/graphs", json={"name": "x", "kind": "flow", "dir": str(nowhere)})
        assert response.status_code == 404
        assert _error(response) == f"{nowhere} does not exist — pick another folder"
        assert not nowhere.exists()

    def test_a_folder_that_is_a_file_is_refused(self, client: Any, tmp_path: Path) -> None:
        plain = tmp_path / "notes.txt"
        plain.write_text("a file\n")
        response = client.post("/api/graphs", json={"name": "x", "kind": "flow", "dir": str(plain)})
        assert response.status_code == 400
        assert _error(response) == f"{plain} is not a folder — pick another folder"
        assert plain.read_text() == "a file\n"

    @pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a user the folder mode binds")
    def test_a_folder_that_cannot_be_written_is_refused(self, client: Any, tmp_path: Path) -> None:
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o555)
        try:
            response = client.post("/api/graphs", json={"name": "fresh", "kind": "flow", "dir": str(locked)})
            assert response.status_code == 400
            assert (
                _error(response)
                == f"{locked / 'fresh.yaml'} cannot be written: Permission denied — pick another folder"
            )
            assert list(locked.iterdir()) == []
        finally:
            locked.chmod(0o755)

    def test_a_write_cut_short_leaves_no_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A disk that fills up while the starter is written: the file the write began is removed again — a refusal
        writes nothing, not half a graph."""
        path = tmp_path / "fresh.yaml"
        opened = Path.open

        class _FullDisk:
            def __init__(self, handle: Any) -> None:  # the real file handle, created by the write
                self.handle = handle

            def __enter__(self) -> "_FullDisk":
                return self

            def __exit__(self, *_exc: Any) -> None:
                self.handle.close()

            def write(self, _text: str) -> int:
                raise OSError(errno.ENOSPC, "No space left on device")

        def full_disk(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:  # a file handle, or the fake
            handle = opened(self, mode, *args, **kwargs)
            return _FullDisk(handle) if self == path else handle

        monkeypatch.setattr(Path, "open", full_disk)
        with pytest.raises(ValueError) as refused:
            Document.create(path, starter_text("flow", "fresh"))
        assert str(refused.value) == f"{path} cannot be written: No space left on device — pick another folder"
        assert not path.exists()


# ------------------------------------------------------------------------------------------------
# Open
# ------------------------------------------------------------------------------------------------


class TestOpen:
    def test_open_adds_a_file_from_anywhere_and_answers_it(self, client: Any, tmp_path: Path) -> None:
        other = tmp_path / "other"
        other.mkdir()
        (other / "away.yaml").write_text(FLOW_TEXT)
        response = client.post("/api/graphs/open", json={"path": str(other / "away.yaml")})
        assert response.status_code == 200
        body = response.get_json()
        assert body["name"] == "away" and body["text"] == FLOW_TEXT and body["dirty"] is False
        assert _names(client) == ["away", "demo"]

    def test_a_stem_the_list_holds_gets_the_next_free_name(self, client: Any, tmp_path: Path) -> None:
        for index in (1, 2):
            folder = tmp_path / f"o{index}"
            folder.mkdir()
            (folder / "demo.yaml").write_text(FLOW_TEXT)
        first = client.post("/api/graphs/open", json={"path": str(tmp_path / "o1" / "demo.yaml")}).get_json()
        second = client.post("/api/graphs/open", json={"path": str(tmp_path / "o2" / "demo.yaml")}).get_json()
        assert (first["name"], second["name"]) == ("demo~2", "demo~3")
        assert _names(client) == ["demo", "demo~2", "demo~3"]

    def test_opening_a_file_already_listed_answers_that_entry(self, client: Any, graph_dir: Path) -> None:
        client.post("/api/graphs/demo/param", json={"node": "probe", "param": "label", "value": "edited"})
        body = client.post("/api/graphs/open", json={"path": str(graph_dir / "demo.yaml")}).get_json()
        assert body["name"] == "demo" and body["dirty"] is True  # the SAME entry, with what the editor holds
        assert _names(client) == ["demo"]

    def test_a_relative_path_is_read_against_the_graph_folder(self, client: Any, graph_dir: Path) -> None:
        (graph_dir / "sub").mkdir()
        (graph_dir / "sub" / "deep.yaml").write_text(FLOW_TEXT)
        body = client.post("/api/graphs/open", json={"path": "sub/deep.yaml"}).get_json()
        assert body["name"] == "deep" and body["path"] == str(graph_dir / "sub" / "deep.yaml")

    def test_a_missing_file_is_404(self, client: Any, tmp_path: Path) -> None:
        response = client.post("/api/graphs/open", json={"path": str(tmp_path / "nope.yaml")})
        assert response.status_code == 404
        assert str(tmp_path / "nope.yaml") in _error(response)

    def test_a_file_that_is_not_yaml_or_is_a_sidecar_is_refused(self, client: Any, graph_dir: Path) -> None:
        (graph_dir / "notes.txt").write_text("x")
        (graph_dir / "demo.layout.yaml").write_text("positions: {}\n")
        response = client.post("/api/graphs/open", json={"path": str(graph_dir / "notes.txt")})
        assert response.status_code == 400
        assert _error(response) == f"{graph_dir / 'notes.txt'} is not a .yaml file"
        response = client.post("/api/graphs/open", json={"path": str(graph_dir / "demo.layout.yaml")})
        assert response.status_code == 400
        assert _error(response) == f"{graph_dir / 'demo.layout.yaml'} is a layout sidecar, not a graph"


# ------------------------------------------------------------------------------------------------
# the list
# ------------------------------------------------------------------------------------------------


class TestTheList:
    def test_a_file_that_appears_in_the_folder_later_shows_up(self, client: Any, graph_dir: Path) -> None:
        assert _names(client) == ["demo"]
        (graph_dir / "later.yaml").write_text(FLOW_TEXT)
        assert _names(client) == ["demo", "later"]
        assert client.get("/api/graphs/later").status_code == 200

    def test_a_file_deleted_from_the_folder_leaves_the_list_unless_the_editor_holds_it(
        self, client: Any, graph_dir: Path
    ) -> None:
        (graph_dir / "gone.yaml").write_text(FLOW_TEXT)
        (graph_dir / "held.yaml").write_text(FLOW_TEXT)
        client.get("/api/graphs/held")  # opened: the editor holds it now
        (graph_dir / "gone.yaml").unlink()
        (graph_dir / "held.yaml").unlink()
        assert _names(client) == ["demo", "held"]
        assert client.get("/api/graphs/gone").status_code == 404

    def test_hidden_files_sidecars_and_steps_files_are_not_graphs(self, client: Any, graph_dir: Path) -> None:
        (graph_dir / ".hidden.yaml").write_text(FLOW_TEXT)
        (graph_dir / "demo.layout.yaml").write_text("positions: {}\n")
        (graph_dir / "prep.steps.yaml").write_text(f"grey:\n  op: !class:{PROBE} {{}}\n")
        assert _names(client) == ["demo"]

    def test_an_unknown_name_is_404_naming_the_graphs(self, client: Any) -> None:
        response = client.get("/api/graphs/nosuch")
        assert response.status_code == 404
        assert "'nosuch'" in _error(response) and "demo" in _error(response)


# ------------------------------------------------------------------------------------------------
# the file browser
# ------------------------------------------------------------------------------------------------


class TestFiles:
    def test_without_a_dir_it_lists_the_graph_folder_dirs_first(self, client: Any, graph_dir: Path) -> None:
        (graph_dir / "zeta").mkdir()
        (graph_dir / "alpha").mkdir()
        (graph_dir / "b.yaml").write_text(FLOW_TEXT)
        (graph_dir / "prep.steps.yaml").write_text("{}\n")
        (graph_dir / "demo.layout.yaml").write_text("positions: {}\n")
        (graph_dir / ".secret.yaml").write_text("{}\n")
        (graph_dir / ".git").mkdir()
        (graph_dir / "notes.txt").write_text("x")
        response = client.get("/api/files")
        assert response.status_code == 200
        body = response.get_json()
        assert body["dir"] == str(graph_dir.resolve())
        assert body["parent"] == str(graph_dir.resolve().parent)
        assert [(entry["name"], entry["kind"]) for entry in body["entries"]] == [
            ("alpha", "dir"),
            ("zeta", "dir"),
            ("b.yaml", "yaml"),
            ("demo.yaml", "yaml"),
            ("prep.steps.yaml", "yaml"),
        ]
        assert body["entries"][2]["path"] == str(graph_dir.resolve() / "b.yaml")

    def test_a_named_directory_is_listed(self, client: Any, tmp_path: Path) -> None:
        other = tmp_path / "other"
        other.mkdir()
        (other / "x.yaml").write_text(FLOW_TEXT)
        body = client.get("/api/files", query_string={"dir": str(other)}).get_json()
        assert body["dir"] == str(other.resolve())
        assert [entry["name"] for entry in body["entries"]] == ["x.yaml"]

    def test_the_root_has_no_parent(self, client: Any) -> None:
        body = client.get("/api/files", query_string={"dir": "/"}).get_json()
        assert body["dir"] == "/" and body["parent"] is None

    def test_a_missing_directory_is_404_naming_it(self, client: Any, tmp_path: Path) -> None:
        response = client.get("/api/files", query_string={"dir": str(tmp_path / "nowhere")})
        assert response.status_code == 404
        assert _error(response) == f"no directory {tmp_path / 'nowhere'}"

    def test_a_directory_that_cannot_be_read_is_400_naming_it(self, client: Any, tmp_path: Path) -> None:
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0)
        try:
            if os.access(locked, os.R_OK):
                pytest.skip("this user reads a mode-000 directory anyway (root)")
            response = client.get("/api/files", query_string={"dir": str(locked)})
            assert response.status_code == 400
            assert _error(response).startswith(f"{locked.resolve()} cannot be read: ")
        finally:
            locked.chmod(0o755)

    def test_a_file_is_not_a_directory(self, client: Any, graph_dir: Path) -> None:
        response = client.get("/api/files", query_string={"dir": str(graph_dir / "demo.yaml")})
        assert response.status_code == 404
        assert _error(response) == f"no directory {graph_dir / 'demo.yaml'}"


# ------------------------------------------------------------------------------------------------
# review fixes (2026-09-29)
# ------------------------------------------------------------------------------------------------


class TestARelativeGraphFolder:
    """The folder the editor was started on is made absolute ONCE, so every path it answers is absolute and can
    be sent back (measured before, ``streamstudio graphs``: the list said ``graphs/chain.yaml``, the file browser
    looked in ``graphs/graphs`` and opening the listed path answered ``no file graphs/graphs/chain.yaml``)."""

    @pytest.fixture
    def relative(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
        (tmp_path / "graphs").mkdir()
        (tmp_path / "graphs" / "chain.yaml").write_text(FLOW_TEXT)
        monkeypatch.chdir(tmp_path)
        return create_app(Path("graphs"), templates_dir=Path("tpl")).test_client()

    def test_the_listed_path_is_absolute_and_opens(self, relative: Any, tmp_path: Path) -> None:
        listed = relative.get("/api/graphs").get_json()
        assert [entry["path"] for entry in listed] == [str(tmp_path / "graphs" / "chain.yaml")]
        response = relative.post("/api/graphs/open", json={"path": listed[0]["path"]})
        assert response.status_code == 200, response.get_json()
        assert response.get_json()["name"] == "chain" and [entry["name"] for entry in _listed(relative)] == ["chain"]

    def test_the_file_browser_opens_in_the_graph_folder(self, relative: Any, tmp_path: Path) -> None:
        body = relative.get("/api/files").get_json()
        assert body["dir"] == str(tmp_path / "graphs")
        assert [entry["path"] for entry in body["entries"]] == [str(tmp_path / "graphs" / "chain.yaml")]

    def test_the_current_directory_is_a_folder_too(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "chain.yaml").write_text(FLOW_TEXT)
        monkeypatch.chdir(tmp_path)
        client = create_app(Path("."), templates_dir=tmp_path / "tpl").test_client()
        assert client.get("/api/files").get_json()["dir"] == str(tmp_path)
        assert client.get("/api/graphs").get_json()[0]["path"] == str(tmp_path / "chain.yaml")

    def test_the_templates_folder_is_absolute_too(self, relative: Any, tmp_path: Path) -> None:
        relative.post("/api/templates", json={"name": "t", "graph": "chain", "nodes": ["probe"]})
        assert relative.get("/api/templates").get_json()[0]["path"] == str(tmp_path / "tpl" / "t.steps.yaml")


def _listed(client: Any) -> List[Any]:
    return list(client.get("/api/graphs").get_json())


def _unreadable(path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip(f"root reads {path} whatever its mode")


class TestOneUnreadableFileBreaksNothingElse:
    def test_opening_an_unreadable_file_is_400_naming_it_and_the_list_is_unchanged(
        self, client: Any, tmp_path: Path
    ) -> None:
        """Measured before: 500, and ``GET /api/graphs`` stayed 500 for the rest of the session (the entry was
        registered before its file was read)."""
        secret = tmp_path / "secret.yaml"
        secret.write_text(FLOW_TEXT)
        secret.chmod(0)
        try:
            _unreadable(secret)
            response = client.post("/api/graphs/open", json={"path": str(secret)})
            assert response.status_code == 400
            assert _error(response).startswith(f"{secret} cannot be read: ")
            listed = client.get("/api/graphs")
            assert listed.status_code == 200 and [entry["name"] for entry in listed.get_json()] == ["demo"]
        finally:
            secret.chmod(0o644)

    def test_a_directory_listed_but_not_searchable_is_400_naming_it(self, client: Any, tmp_path: Path) -> None:
        """Read permission without search (``x``) lists the names but cannot tell a file from a directory."""
        folder = tmp_path / "noexec"
        folder.mkdir()
        (folder / "f.yaml").write_text(FLOW_TEXT)
        folder.chmod(0o600)
        try:
            _unreadable(folder)
            response = client.get("/api/files", query_string={"dir": str(folder)})
            assert response.status_code == 400
            assert _error(response).startswith(f"{folder.resolve()} cannot be read: ")
        finally:
            folder.chmod(0o755)


class TestClosedSets:
    def test_new_graph_takes_the_closed_kind(self) -> None:
        assert get_type_hints(Studio.new_graph)["kind"] == NewGraphKind

    def test_a_file_entry_kind_is_a_closed_set_the_answers_keep_to(self, client: Any, graph_dir: Path) -> None:
        (graph_dir / "sub").mkdir()
        assert get_args(FileEntryKind) == ("dir", "yaml", "file")
        kinds = {entry["kind"] for entry in client.get("/api/files").get_json()["entries"]}
        assert kinds == {"dir", "yaml"}, "the graph browser lists folders and graphs"
        every = {entry["kind"] for entry in client.get("/api/files?show=all").get_json()["entries"]}
        assert every == {"dir", "file"}, "a file picker lists folders and every file (tests/test_pickers.py)"
