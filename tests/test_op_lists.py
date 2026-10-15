"""Op lists are editable: a class is added to a ``Stream``'s ``ops`` (a ``Pipeline``'s ``transforms``) as a MEMBER,
and members are removed — the user's answer "yes" to "Pipeline graphs: adding and removing work on ``ops`` list
members" (2026-09-29).

Measured before this change, on a ``stream: Stream {ops: [...]}`` graph (New ▸ Pipeline, ``demo.yaml``)::

    add_step("threshold", after="stream.ops[3]", …)
        -> ValueError: 'stream.ops[3]' is not a flow step (the steps are: [])
    remove_step("stream.ops[3]")                      -> the same refusal
    remove_steps(["stream.ops[2]", "stream.ops[3]"])  -> the same refusal

Now:

* ``add_step(…, after="stream.ops[1]")`` inserts the new member right after that member, and
  ``add_step(…, after=None, into="stream.ops")`` appends it — written in the list's own style: a ``- !class:X {…}``
  line in a block list, ``, !class:X {…}`` inside a one-line ``[…]`` list, and an empty one-line ``ops: []`` under
  a block marker becomes a block list with one member. The new member's name is answered (``stream.ops[2]``).
* ``remove_step`` / ``remove_steps`` take members out; several at once are ONE edit (one undo); removing every
  member leaves ``ops: []`` — the identity stream, which is valid.
* The positions and group members of LATER members follow them (``stream.ops[3]`` → ``stream.ops[2]``), as a
  Convert or a Paste of members already did, and an undo moves them back; a member added between two others is
  laid out, never drawn on the place the sidecar kept for the member that used to have its name.
* Every line the edit does not touch — comments included — stays byte-identical.
"""

import difflib
from pathlib import Path
from typing import Any, Dict, List

import pytest
from confluid import configurable
from recordstream import Record

from streamstudio import layout
from streamstudio.app import create_app
from streamstudio.document import Document

MOD = __name__
PROBE = "recordstream.ops.debug.PrintRecordOp"
STREAM = "recordstream.core.stream.Stream"
PIPELINE = "recordstream.transform.Pipeline"
FILES = "recordstream.sources.files.FilesSource"

PIPE_TEXT = f"""\
# a pipeline: each op reads the record the op above it returns
stream: !class:{STREAM}
  ops:
    # first
    - !class:{PROBE} {{label: a}}
    - !class:{PROBE} {{label: b}}   # the middle one
    - !class:{PROBE}
      label: c
    # below the last one
  # about the source, written left of the list
  source: !class:{FILES} {{files: []}}
"""

EMPTY_TEXT = f"""\
stream: !class:{STREAM}
  ops: []   # nothing yet
"""

ONE_LINE_TEXT = f"""\
stream: !class:{STREAM}
  ops: [!class:{PROBE} {{label: a}}, !class:{PROBE} {{label: b}}]   # two
"""

NO_OPS_TEXT = f"""\
stream: !class:{STREAM}
  source: !class:{FILES} {{files: []}}   # nothing runs yet
"""

PIPELINE_TEXT = f"""\
pipe: !class:{PIPELINE}
  transforms:
    - !class:{PROBE} {{label: a}}
"""


@configurable(category="op", group="op-lists-test")
class MakesFloor:
    """Write a ``floor`` entry — what :class:`UsesFloor` reads, by NAME."""

    produces = {"floor": "*"}

    def __call__(self, record: Record) -> Record:
        return {**record, "floor": 0.0}


@configurable(category="op", group="op-lists-test")
class UsesFloor:
    """Read the ``floor`` entry — without :class:`MakesFloor` before it, the chain check refuses it."""

    consumes = {"floor": "*"}

    def __call__(self, record: Record) -> Record:
        return record


FLOOR_TEXT = f"""\
stream: !class:{STREAM}
  ops:
    - !class:{PROBE} {{label: head}}
    - !class:{MOD}.MakesFloor {{}}
    - !class:{MOD}.UsesFloor {{}}
"""


def _doc(tmp_path: Path, text: str, name: str = "demo") -> Document:
    path = tmp_path / f"{name}.yaml"
    path.write_text(text)
    return Document.open(path)


def _changed_lines(before: str, after: str) -> List[str]:
    """The lines an edit added or removed (``+x`` / ``-x``) — everything else is byte-identical."""
    return [
        line
        for line in difflib.ndiff(before.splitlines(), after.splitlines())
        if line.startswith(("+ ", "- ")) and not line.startswith(("+++", "---"))
    ]


# ------------------------------------------------------------------------------------------------
# adding a member
# ------------------------------------------------------------------------------------------------


class TestAddAMember:
    def test_after_a_member_the_new_member_is_the_next_line_of_the_block_list(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, PIPE_TEXT)
        added = document.add_step("ignored", "stream.ops[1]", PROBE, {"label": "added"})
        assert added == "stream.ops[2]"
        assert _changed_lines(PIPE_TEXT, document.text) == [f"+     - !class:{PROBE} {{label: added}}"]
        assert f"# the middle one\n    - !class:{PROBE} {{label: added}}\n    - !class:{PROBE}\n      label: c" in (
            document.text
        )
        assert document.model.node("stream.ops[2]").params["label"].value == "added"
        assert document.model.node("stream.ops[3]").params["label"].value == "c"
        assert document.check() == []
        assert (tmp_path / "demo.yaml").read_text() == PIPE_TEXT  # held by the editor until Save

    def test_with_no_member_named_it_goes_to_the_end_of_the_list_its_comments_staying_where_they_are(
        self, tmp_path: Path
    ) -> None:
        document = _doc(tmp_path, PIPE_TEXT)
        assert document.add_step("", None, PROBE, {"label": "last"}, into="stream.ops") == "stream.ops[3]"
        assert _changed_lines(PIPE_TEXT, document.text) == [f"+     - !class:{PROBE} {{label: last}}"]
        # the comment below the last member travels with it; the one written left of the list is about `source:`
        assert (
            f"      label: c\n    # below the last one\n    - !class:{PROBE} {{label: last}}\n"
            "  # about the source, written left of the list\n"
        ) in document.text
        assert document.check() == []

    def test_an_empty_one_line_list_becomes_a_block_list_with_one_member(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, EMPTY_TEXT)
        assert document.add_step("", None, PROBE, {"label": "first"}, into="stream.ops") == "stream.ops[0]"
        assert document.text == (
            f"stream: !class:{STREAM}\n  ops:   # nothing yet\n    - !class:{PROBE} {{label: first}}\n"
        )
        assert document.check() == []

    def test_a_one_line_list_keeps_its_style(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, ONE_LINE_TEXT)
        assert document.add_step("", "stream.ops[0]", PROBE, {"label": "mid"}) == "stream.ops[1]"
        assert document.text == (
            f"stream: !class:{STREAM}\n  ops: [!class:{PROBE} {{label: a}}, !class:{PROBE} {{label: mid}}, "
            f"!class:{PROBE} {{label: b}}]   # two\n"
        )
        assert document.add_step("", None, PROBE, {}, into="stream.ops") == "stream.ops[3]"
        assert document.text.endswith(f"!class:{PROBE} {{label: b}}, !class:{PROBE} {{}}]   # two\n")
        assert document.check() == []

    def test_an_empty_list_inside_a_one_line_marker_is_filled_in_place(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, f"stream: !class:{STREAM} {{ops: []}}   # one line\n")
        assert document.add_step("", None, PROBE, {"label": "a"}, into="stream.ops") == "stream.ops[0]"
        assert document.text == f"stream: !class:{STREAM} {{ops: [!class:{PROBE} {{label: a}}]}}   # one line\n"

    def test_a_stream_that_spells_no_ops_gets_its_list(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, NO_OPS_TEXT)
        assert document.add_step("", None, PROBE, {"label": "a"}, into="stream.ops") == "stream.ops[0]"
        assert document.text == (
            f"stream: !class:{STREAM}\n  ops:\n    - !class:{PROBE} {{label: a}}\n"
            f"  source: !class:{FILES} {{files: []}}   # nothing runs yet\n"
        )
        assert document.check() == []

    def test_a_pipelines_transforms_is_an_op_list_too(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, PIPELINE_TEXT)
        assert document.add_step("", "pipe.transforms[0]", PROBE, {"label": "b"}) == "pipe.transforms[1]"
        assert document.text == PIPELINE_TEXT + f"    - !class:{PROBE} {{label: b}}\n"
        assert document.add_step("", None, PROBE, {}, into="pipe.transforms") == "pipe.transforms[2]"
        assert document.text.endswith(f"    - !class:{PROBE} {{}}\n")

    def test_one_undo_takes_the_member_out_again_and_redo_brings_it_back(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, PIPE_TEXT)
        document.add_step("", "stream.ops[0]", PROBE, {"label": "x"})
        added = document.text
        assert document.undo() is True
        assert document.text == PIPE_TEXT
        assert document.redo() is True
        assert document.text == added


class TestAddRefusals:
    def test_neither_a_member_nor_a_list_is_refused_naming_both(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="needs 'after' .* or 'list'"):
            _doc(tmp_path, PIPE_TEXT).add_step("", None, PROBE, {})

    def test_a_slot_that_is_not_an_op_list_is_refused_naming_the_op_lists(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, PIPE_TEXT)
        with pytest.raises(ValueError) as refused:
            document.add_step("", None, PROBE, {}, into="stream.source")
        assert str(refused.value) == (
            "'stream.source' is not an op list — a class is added to the ops of a Stream or the transforms of a "
            "Pipeline ('<node>.ops', '<node>.transforms')"
        )
        assert document.text == PIPE_TEXT

    def test_a_list_of_an_unknown_node_is_a_key_error_naming_the_nodes(self, tmp_path: Path) -> None:
        with pytest.raises(KeyError, match="no node 'nowhere'"):
            _doc(tmp_path, PIPE_TEXT).add_step("", None, PROBE, {}, into="nowhere.ops")

    def test_a_member_named_with_another_list_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match=r"'stream.ops\[0\]' is a member of stream.ops, not of pipe.transforms"):
            _doc(tmp_path, PIPE_TEXT).add_step("", "stream.ops[0]", PROBE, {}, into="pipe.transforms")

    def test_a_node_that_takes_no_such_list_is_refused(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, PIPE_TEXT)
        with pytest.raises(ValueError) as refused:
            document.add_step("", None, PROBE, {}, into="stream.source.ops")
        assert str(refused.value) == f"stream.source ({FILES}) takes no ops list"

    def test_a_list_over_several_lines_in_brackets_is_refused_located(self, tmp_path: Path) -> None:
        text = f"stream: !class:{STREAM}\n  ops: [!class:{PROBE} {{label: a}},\n        !class:{PROBE} {{label: b}}]\n"
        document = _doc(tmp_path, text)
        with pytest.raises(ValueError) as refused:
            document.add_step("", "stream.ops[0]", PROBE, {})
        assert str(refused.value) == (
            f"{(tmp_path / 'demo.yaml').resolve()}:2:8: stream.ops is written in brackets over several lines — "
            "spell it as a block list (one '- ' item per line) to add to it or remove from it"
        )

    def test_a_list_from_an_included_file_is_edited_there(self, tmp_path: Path) -> None:
        (tmp_path / "parts.yaml").write_text(f"stream: !class:{STREAM}\n  ops:\n    - !class:{PROBE} {{label: a}}\n")
        document = _doc(tmp_path, "include: parts.yaml\n")
        with pytest.raises(ValueError, match=r"stream.ops comes from .*parts.yaml; edit that list there"):
            document.add_step("", "stream.ops[0]", PROBE, {})

    def test_an_unknown_class_is_refused_and_nothing_changes(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, PIPE_TEXT)
        with pytest.raises(ValueError, match="cannot resolve the class 'no.such.Op'"):
            document.add_step("", "stream.ops[0]", "no.such.Op", {})
        assert document.text == PIPE_TEXT
        assert document.undo() is False

    def test_an_add_the_chain_check_refuses_changes_nothing(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, FLOOR_TEXT)
        with pytest.raises(ValueError) as refused:
            document.add_step("", "stream.ops[0]", f"{MOD}.UsesFloor", {})
        assert str(refused.value).startswith(f"add {MOD}.UsesFloor to stream.ops at [1] refused at ")
        assert "floor" in str(refused.value)
        assert document.text == FLOOR_TEXT

    def test_a_flow_step_after_which_a_list_is_named_is_refused(self, tmp_path: Path) -> None:
        text = f"flow:\n  a:\n    op: !class:{PROBE} {{label: a}}\noutputs: a\n"
        with pytest.raises(ValueError, match="'a' is not a member of stream.ops"):
            _doc(tmp_path, text).add_step("b", "a", PROBE, {}, into="stream.ops")

    def test_a_flow_step_still_needs_a_name(self, tmp_path: Path) -> None:
        text = f"flow:\n  a:\n    op: !class:{PROBE} {{label: a}}\noutputs: a\n"
        with pytest.raises(ValueError, match="a new flow step needs a name"):
            _doc(tmp_path, text).add_step("", "a", PROBE, {})


# ------------------------------------------------------------------------------------------------
# removing members
# ------------------------------------------------------------------------------------------------


class TestRemoveMembers:
    def test_a_middle_member_goes_with_its_own_line_and_nothing_else_changes(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, PIPE_TEXT)
        document.remove_step("stream.ops[1]")
        assert _changed_lines(PIPE_TEXT, document.text) == [f"-     - !class:{PROBE} {{label: b}}   # the middle one"]
        assert [document.model.node(f"stream.ops[{i}]").params["label"].value for i in range(2)] == ["a", "c"]
        assert document.check() == []

    def test_the_last_member_goes_with_the_comment_below_it_not_the_one_left_of_the_list(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, PIPE_TEXT)
        document.remove_step("stream.ops[2]")
        assert _changed_lines(PIPE_TEXT, document.text) == [
            f"-     - !class:{PROBE}",
            "-       label: c",
            "-     # below the last one",
        ]

    def test_the_first_member_leaves_the_comment_written_above_the_list(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, PIPE_TEXT)
        document.remove_step("stream.ops[0]")
        assert _changed_lines(PIPE_TEXT, document.text) == [f"-     - !class:{PROBE} {{label: a}}"]

    def test_several_members_are_one_edit_and_one_undo(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, PIPE_TEXT)
        document.remove_steps(["stream.ops[0]", "stream.ops[2]"])
        assert [node.name for node in document.model.nodes if node.name.startswith("stream.ops")] == ["stream.ops[0]"]
        assert document.model.node("stream.ops[0]").params["label"].value == "b"
        assert document.undo() is True
        assert document.text == PIPE_TEXT
        assert document.undo() is False

    def test_removing_every_member_leaves_the_identity_stream(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, PIPE_TEXT)
        document.remove_steps(["stream.ops[0]", "stream.ops[1]", "stream.ops[2]"])
        assert document.text == (
            "# a pipeline: each op reads the record the op above it returns\n"
            f"stream: !class:{STREAM}\n"
            "  ops: []\n"
            "    # first\n"
            "  # about the source, written left of the list\n"
            f"  source: !class:{FILES} {{files: []}}\n"
        )
        assert document.check() == []
        assert [name for name in document.model.names() if name.startswith("stream.ops")] == []

    def test_a_one_line_list_loses_the_member_and_one_comma(self, tmp_path: Path) -> None:
        three = ONE_LINE_TEXT.replace("{label: b}]", f"{{label: b}}, !class:{PROBE} {{label: c}}]")
        document = _doc(tmp_path, three)
        document.remove_step("stream.ops[1]")
        assert f"[!class:{PROBE} {{label: a}}, !class:{PROBE} {{label: c}}]   # two" in document.text
        document.remove_step("stream.ops[0]")
        assert f"  ops: [!class:{PROBE} {{label: c}}]   # two\n" in document.text
        document.remove_step("stream.ops[0]")
        assert document.text == f"stream: !class:{STREAM}\n  ops: []   # two\n"

    def test_a_removal_the_chain_check_refuses_changes_nothing(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, FLOOR_TEXT)
        assert document.check() == []
        with pytest.raises(ValueError) as refused:
            document.remove_step("stream.ops[1]")
        message = str(refused.value)
        assert message.startswith(f"remove 'stream.ops[1]' refused at {(tmp_path / 'demo.yaml').resolve()}:4:7: ")
        assert "floor" in message
        assert document.text == FLOOR_TEXT
        assert document.undo() is False

    def test_a_member_of_a_list_that_is_not_an_op_list_is_still_refused(self, tmp_path: Path) -> None:
        text = f"joint: !class:recordstream.core.stream.JointStream\n  streams:\n    - !class:{STREAM} {{}}\n"
        with pytest.raises(ValueError, match=r"'joint.streams\[0\]' is not a flow step"):
            _doc(tmp_path, text).remove_step("joint.streams[0]")


#: The finding's own document (P2): a comment on its own line above a member is about THAT member.
COMMENTED_TEXT = """\
stream: !class:recordstream.core.stream.Stream
  ops:
    # read first
    - !class:recordstream.ops.image.ReadImage {}
    - !class:recordstream.ops.image.ConvertMode {mode: L}   # one channel
    # then the mask
    - !class:recordstream.ops.numpy.Threshold {low_level: 0.5}
  # after the list
"""


class TestTheCommentAboveAMemberIsThatMembers:
    """Measured before (P2): a member ran up to the next member's ``-`` line, so removing ``ConvertMode`` also took
    ``# then the mask``, a member added after ``ConvertMode`` landed between that comment and ``Threshold``, and a cut
    of ``Threshold`` left its comment behind."""

    def test_removing_a_member_takes_only_its_own_line(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, COMMENTED_TEXT)
        document.remove_step("stream.ops[1]")
        assert _changed_lines(COMMENTED_TEXT, document.text) == [
            "-     - !class:recordstream.ops.image.ConvertMode {mode: L}   # one channel"
        ]

    def test_a_member_added_after_one_goes_above_the_comment_of_the_next(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, COMMENTED_TEXT)
        document.add_step("", "stream.ops[1]", PROBE, {})
        assert (
            "    - !class:recordstream.ops.image.ConvertMode {mode: L}   # one channel\n"
            f"    - !class:{PROBE} {{}}\n"
            "    # then the mask\n"
            "    - !class:recordstream.ops.numpy.Threshold {low_level: 0.5}\n"
        ) in document.text

    def test_a_member_with_a_comment_above_it_goes_with_it(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, COMMENTED_TEXT)
        document.remove_step("stream.ops[2]")
        assert _changed_lines(COMMENTED_TEXT, document.text) == [
            "-     # then the mask",
            "-     - !class:recordstream.ops.numpy.Threshold {low_level: 0.5}",
        ]
        assert document.text.endswith("  # after the list\n")

    def test_cut_and_paste_carries_the_comment_with_its_member(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, COMMENTED_TEXT)
        clip, kind = document.copy_text(["stream.ops[2]"])
        assert (clip, kind) == ("# then the mask\n- !class:recordstream.ops.numpy.Threshold {low_level: 0.5}\n", "ops")
        document.remove_step("stream.ops[2]")
        document.paste_text(clip, "stream.ops[0]")
        assert document.text == (
            "stream: !class:recordstream.core.stream.Stream\n"
            "  ops:\n"
            "    # read first\n"
            "    - !class:recordstream.ops.image.ReadImage {}\n"
            "    # then the mask\n"
            "    - !class:recordstream.ops.numpy.Threshold {low_level: 0.5}\n"
            "    - !class:recordstream.ops.image.ConvertMode {mode: L}   # one channel\n"
            "  # after the list\n"
        )

    def test_the_comment_above_the_first_member_stays_with_the_list(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, COMMENTED_TEXT)
        document.remove_step("stream.ops[0]")
        assert _changed_lines(COMMENTED_TEXT, document.text) == ["-     - !class:recordstream.ops.image.ReadImage {}"]

    def test_a_comment_deeper_than_the_dash_stays_with_the_member_above_it(self, tmp_path: Path) -> None:
        text = (
            "stream: !class:recordstream.core.stream.Stream\n  ops:\n"
            f"    - !class:{PROBE}\n      label: a\n      # about a's label\n"
            f"    - !class:{PROBE} {{label: b}}\n"
        )
        document = _doc(tmp_path, text)
        document.remove_step("stream.ops[1]")
        assert _changed_lines(text, document.text) == [f"-     - !class:{PROBE} {{label: b}}"]


# ------------------------------------------------------------------------------------------------
# positions and groups follow the members
# ------------------------------------------------------------------------------------------------

SAVED_POSITIONS = {
    "stream": (0.0, 0.0),
    "stream.source": (0.0, 300.0),
    "stream.ops[0]": (100.0, 0.0),
    "stream.ops[1]": (200.0, 0.0),
    "stream.ops[2]": (300.0, 0.0),
}
GROUP = {"id": "g1", "title": "tail", "color": "#111", "nodes": ["stream.ops[1]", "stream.ops[2]"]}


@pytest.fixture
def arranged(tmp_path: Path) -> Document:
    """``demo.yaml`` with a sidecar the user arranged: the three members in a row, the last two in a group."""
    document = _doc(tmp_path, PIPE_TEXT)
    layout.write_sidecar(document.sidecar_path, SAVED_POSITIONS, groups=[GROUP])  # type: ignore[list-item]
    return Document.open(document.path)


class TestPositionsFollowTheMembers:
    def test_a_member_added_between_two_is_laid_out_and_the_later_ones_keep_their_places(
        self, arranged: Document
    ) -> None:
        arranged.add_step("", "stream.ops[0]", PROBE, {"label": "new"})
        positions = arranged.positions
        assert positions["stream.ops[0]"] == (100.0, 0.0)
        assert positions["stream.ops[2]"] == (200.0, 0.0)  # was stream.ops[1]
        assert positions["stream.ops[3]"] == (300.0, 0.0)  # was stream.ops[2]
        # the new member is laid out — never drawn on the place the sidecar kept for the old stream.ops[1]
        assert positions["stream.ops[1]"] not in SAVED_POSITIONS.values()
        assert arranged.groups[0]["nodes"] == ["stream.ops[2]", "stream.ops[3]"]

    def test_the_undo_of_an_add_moves_them_back(self, arranged: Document) -> None:
        arranged.add_step("", "stream.ops[0]", PROBE, {"label": "new"})
        arranged.undo()
        assert {name: arranged.positions[name] for name in SAVED_POSITIONS} == SAVED_POSITIONS
        assert arranged.groups[0]["nodes"] == ["stream.ops[1]", "stream.ops[2]"]

    def test_a_removed_member_takes_its_place_with_it_and_the_later_ones_follow(self, arranged: Document) -> None:
        arranged.remove_step("stream.ops[1]")
        positions = arranged.positions
        assert positions["stream.ops[0]"] == (100.0, 0.0)
        assert positions["stream.ops[1]"] == (300.0, 0.0)  # was stream.ops[2]
        assert "stream.ops[2]" not in positions
        assert arranged.groups[0]["nodes"] == ["stream.ops[1]"]

    def test_several_removed_as_one_edit_and_undone_as_one(self, arranged: Document) -> None:
        arranged.remove_steps(["stream.ops[0]", "stream.ops[1]"])
        assert arranged.positions["stream.ops[0]"] == (300.0, 0.0)  # was stream.ops[2]
        assert arranged.groups[0]["nodes"] == ["stream.ops[0]"]
        arranged.undo()
        assert {name: arranged.positions[name] for name in SAVED_POSITIONS} == SAVED_POSITIONS
        # the removed members come back to their places AND into their group (P6: stream.ops[1] was dropped from it)
        assert arranged.groups[0]["nodes"] == ["stream.ops[1]", "stream.ops[2]"]

    def test_the_undo_of_a_removal_puts_the_member_back_into_its_group(self, arranged: Document) -> None:
        """Measured before (P6): Delete of ``stream.ops[1]`` then Ctrl+Z left the group holding only
        ``stream.ops[2]`` and the member away from its place — a flow step keeps both."""
        arranged.remove_step("stream.ops[1]")
        arranged.undo()
        assert arranged.groups[0]["nodes"] == ["stream.ops[1]", "stream.ops[2]"]
        assert arranged.positions["stream.ops[1]"] == (200.0, 0.0)
        arranged.redo()
        assert arranged.groups[0]["nodes"] == ["stream.ops[1]"]
        arranged.undo()
        assert arranged.groups[0]["nodes"] == ["stream.ops[1]", "stream.ops[2]"]

    def test_the_redo_of_an_add_puts_the_member_back_at_its_place_and_in_its_group(self, arranged: Document) -> None:
        arranged.add_step("", "stream.ops[0]", PROBE, {"label": "new"})
        arranged.set_positions({"stream.ops[1]": (400.0, 540.0)})
        arranged.set_groups(
            [
                {
                    "id": "g1",
                    "title": "tail",
                    "color": "#111",
                    "nodes": ["stream.ops[1]", "stream.ops[2]", "stream.ops[3]"],
                }
            ]
        )
        arranged.undo()
        assert "stream.ops[1]" in arranged.groups[0]["nodes"]  # the OLD stream.ops[1], back under its name
        assert arranged.positions["stream.ops[1]"] == (200.0, 0.0)
        arranged.redo()
        assert arranged.positions["stream.ops[1]"] == (400.0, 540.0)
        assert arranged.groups[0]["nodes"] == ["stream.ops[1]", "stream.ops[2]", "stream.ops[3]"]

    def test_an_undo_says_how_the_member_names_moved(self, arranged: Document) -> None:
        """P7: the page follows the level it shows through these (a subgraph member renamed by the undo)."""
        arranged.remove_step("stream.ops[0]")
        assert arranged.stepped_shifts == ()
        arranged.undo()
        assert arranged.stepped_shifts == (("stream.ops", 0, 1),)
        arranged.redo()
        assert arranged.stepped_shifts == (("stream.ops", 1, -1),)
        arranged.set_param("stream.ops[0]", "label", "x")
        arranged.undo()
        assert arranged.stepped_shifts == ()

    def test_a_move_the_editor_holds_is_shifted_with_the_sidecars_places(self, arranged: Document) -> None:
        """A partial move held in the editor (the page sends only what moved) must not stop the sidecar's
        entries of the later members from following them."""
        arranged.set_positions({"stream.ops[0]": (111.0, 5.0)})
        arranged.add_step("", "stream.ops[0]", PROBE, {})
        positions = arranged.positions
        assert positions["stream.ops[0]"] == (111.0, 5.0)
        assert positions["stream.ops[2]"] == (200.0, 0.0)
        assert positions["stream.ops[3]"] == (300.0, 0.0)

    def test_save_writes_the_shifted_places(self, arranged: Document) -> None:
        arranged.remove_step("stream.ops[0]")
        arranged.save()
        saved = layout.read_sidecar(arranged.sidecar_path)
        assert saved["stream.ops[0]"] == (200.0, 0.0)
        assert saved["stream.ops[1]"] == (300.0, 0.0)
        assert "stream.ops[2]" not in saved


class TestAPastedMemberIsLaidOutToo:
    def test_a_pasted_member_does_not_sit_on_the_member_that_moved_down(self, arranged: Document) -> None:
        """Measured before (round 1's paste re-key): with a sidecar, the pasted ``stream.ops[1]`` was drawn at
        (200, 0) — the sidecar's place of the OLD ``stream.ops[1]``, which had moved to ``stream.ops[2]``, also
        at (200, 0)."""
        clip, _kind = arranged.copy_text(["stream.ops[0]"])
        assert arranged.paste_text(clip, "stream.ops[0]") == ["stream.ops[1]"]
        positions = arranged.positions
        assert positions["stream.ops[2]"] == (200.0, 0.0)
        assert positions["stream.ops[1]"] != positions["stream.ops[2]"]


# ------------------------------------------------------------------------------------------------
# the routes
# ------------------------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> Any:  # flask.testing.FlaskClient
    folder = tmp_path / "graphs"
    folder.mkdir()
    (folder / "demo.yaml").write_text(PIPE_TEXT)
    (folder / "empty.yaml").write_text(EMPTY_TEXT)
    return create_app(folder, templates_dir=tmp_path / "templates").test_client()


def _post(client: Any, url: str, body: Dict[str, Any], status: int = 200) -> Dict[str, Any]:
    response = client.post(url, json=body)
    assert response.status_code == status, response.get_json()
    answer: Dict[str, Any] = response.get_json()
    return answer


class TestTheRoutes:
    def test_steps_after_a_member_answers_the_graph_and_the_new_members_name(self, client: Any, tmp_path: Path) -> None:
        answer = _post(
            client,
            "/api/graphs/demo/steps",
            {"name": "threshold", "after": "stream.ops[1]", "class_path": PROBE, "params": {"label": "t"}},
        )
        assert answer["added"] == "stream.ops[2]"
        assert answer["dirty"] is True
        assert f"    - !class:{PROBE} {{label: t}}\n" in answer["text"]
        assert "stream.ops[3]" in [node["name"] for node in answer["model"]["nodes"]]
        assert (tmp_path / "graphs" / "demo.yaml").read_text() == PIPE_TEXT

    def test_steps_with_a_list_and_no_member_appends_and_needs_no_name(self, client: Any) -> None:
        answer = _post(client, "/api/graphs/empty/steps", {"list": "stream.ops", "class_path": PROBE, "params": {}})
        assert answer["added"] == "stream.ops[0]"
        assert f"  ops:   # nothing yet\n    - !class:{PROBE} {{}}\n" in answer["text"]

    def test_steps_with_neither_is_a_400_naming_both(self, client: Any) -> None:
        answer = _post(client, "/api/graphs/demo/steps", {"class_path": PROBE}, status=400)
        assert "'after'" in answer["error"] and "'list'" in answer["error"]

    def test_a_flow_step_answers_its_name_too(self, client: Any, tmp_path: Path) -> None:
        (tmp_path / "graphs" / "flow.yaml").write_text(f"flow:\n  a:\n    op: !class:{PROBE} {{}}\noutputs: a\n")
        answer = _post(client, "/api/graphs/flow/steps", {"name": "b", "after": "a", "class_path": PROBE})
        assert answer["added"] == "b"

    def test_steps_remove_takes_several_members_as_one_undo(self, client: Any) -> None:
        answer = _post(client, "/api/graphs/demo/steps/remove", {"nodes": ["stream.ops[0]", "stream.ops[1]"]})
        assert [n["name"] for n in answer["model"]["nodes"] if n["name"].startswith("stream.ops")] == ["stream.ops[0]"]
        undone = _post(client, "/api/graphs/demo/undo", {})
        assert undone["changed"] is True and undone["text"] == PIPE_TEXT

    def test_undo_and_redo_answer_the_member_shifts_they_made(self, client: Any) -> None:
        _post(client, "/api/graphs/demo/steps/remove", {"nodes": ["stream.ops[0]"]})
        undone = _post(client, "/api/graphs/demo/undo", {})
        assert undone["shifts"] == [{"list": "stream.ops", "first": 0, "by": 1}]
        redone = _post(client, "/api/graphs/demo/redo", {})
        assert redone["shifts"] == [{"list": "stream.ops", "first": 1, "by": -1}]
        _post(client, "/api/graphs/demo/param", {"node": "stream.ops[0]", "param": "label", "value": "x"})
        assert _post(client, "/api/graphs/demo/undo", {})["shifts"] == []

    def test_delete_takes_one_member(self, client: Any) -> None:
        response = client.delete("/api/graphs/demo/steps/stream.ops[2]")
        assert response.status_code == 200, response.get_json()
        assert "label: c" not in response.get_json()["text"]
