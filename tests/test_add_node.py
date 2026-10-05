"""A class that is not an op is a CARD of its own (``Document.add_node``) — the user's fix of 2026-10-05.

Before it the editor knew two places for an added class: a member of an op list (``Document.add_step`` →
``_add_member``) or a flow step. A class that is neither — a value like ``ClassNamesScan``, a source, an engine, a
sink — had no place in a Stream document, and the host card was taken as an anchor. Measured on the files graph a host
workspace seeds, right-click ▸ Add Node ▸ ClassNamesScan:

    nothing selected        add recordstream.ops.contract.ClassNamesScan to stream.ops at [1] refused at
                            …/files.yaml:12:7: cannot wire stream.ops[1].object (object) into stream.ops[1] (Op) at
                            …/files.yaml:9:9: the types do not meet
    the host card selected  a new flow step needs a name — after '<host>', it has none

Now such a class is written as a NEW top-level key — ``class_names_scan: !class:…ClassNamesScan {}`` — named by the
move-out rule (``Document._moved_key``: the class name in snake case, ``_2`` … when that name is a top-level key, a node
or a constructor parameter of any card), directly ABOVE the graph's root and the comment lines written for it (the root
stays last), at the end of the file when there is no root. ONE edit, one undo. ``add_step`` hands such a class over, so
every add the page sends lands where the DOCUMENT's kind says — the catalogue lists 35 classes as values that the
document derives as ops (``FilterOp``, ``WrappedOp``, …), and those keep going into the list. A flow graph and the
inside of a subgraph refuse a card in one plain line; the host card and the root are never an anchor. A wire dragged
out of a socket and dropped on empty canvas is saved to the new card in the same edit, or nothing is added.
"""

from pathlib import Path
from typing import Any, List, Optional, Tuple

import pytest
from confluid import configurable
from recordstream import Record

from streamstudio.document import Document, Model

MOD = __name__

SCAN = "recordstream.ops.contract.ClassNamesScan"
OUTPUT = "recordstream.ops.contract.ClassNamesOutput"
PROBE = "recordstream.ops.debug.PrintRecordOp"
STREAM = "recordstream.core.stream.Stream"


# ------------------------------------------------------------------------------------------------
# synthetic classes — the documents below spell them as !class:<MOD>.<Class>
# ------------------------------------------------------------------------------------------------


@configurable(category="value", group="card-test")
class CardHolder:
    """A card with a parameter named like another class in snake case (``thing``).

    Args:
        thing: Anything a wire brings.
        size: A number.
    """

    def __init__(self, thing: Any = None, size: int = 0) -> None:  # thing: any object a wire brings
        self.thing = thing
        self.size = size


@configurable(category="value", group="card-test")
class Thing:
    """A value whose class name in snake case, ``thing``, is a parameter of :class:`CardHolder`: confluid would hand a
    top-level ``thing`` to that card, so a new ``Thing`` card is called ``thing_2``."""


class Untagged:
    """No ``@configurable`` category and a ``__call__`` that takes the record: the catalogue lists it as a value
    (``palette.kind_of``), the document derives an OP (``document._kind_of``) — and the document decides."""

    def __call__(self, record: Record) -> Record:
        return record


# ------------------------------------------------------------------------------------------------
# documents
# ------------------------------------------------------------------------------------------------

#: The files graph a host workspace seeds, spelled as its template is: a header, then the root's own comment line.
FILES = """\
# ------------------------------------------------------------------------------
# A TEMPLATE graph: the files graph a workspace of this type is seeded with.
# ------------------------------------------------------------------------------
file_source: !class:recordstream.sources.files.FilesSource {}
stream: !class:recordstream.core.stream.Stream
  source: !ref:file_source
  ops:
    - !class:recordstream.ops.image.ReadImage {field: file, output: image}
# The graph's ROOT — what this graph must deliver; the host applies a drop through its check.
contract: !class:recordstream.ops.contract.GraphContract
  name: classification files
  inputs: {files: "List[str]"}
  outputs: {stream: Stream}
  optional: {classes: "List[str]"}
  records: {input: Image}
  delivered:
    stream: !ref:stream
    input: image
"""

#: The first line of the root's own comment in :data:`FILES`.
ROOT_COMMENT = "# The graph's ROOT"


def _above_the_root(text: str, *lines: str) -> str:
    """``text`` with ``lines`` written directly above the root's own comment line."""
    return text.replace(ROOT_COMMENT, "".join(f"{line}\n" for line in lines) + ROOT_COMMENT, 1)


#: The user's case, done: the card above the root's comment, every other line as it was.
SCANNED = _above_the_root(FILES, f"class_names_scan: !class:{SCAN} {{}}")

#: Two comment lines written for the root, and a blank line above them that is neither the root's nor the stream's.
COMMENTED = f"""\
stream: !class:{STREAM}
  source: !class:recordstream.sources.files.FilesSource {{}}
  ops: []

# the root: what the graph delivers
# (a host reads it)
contract: !class:recordstream.ops.contract.GraphContract
  name: commented
  outputs: {{stream: Stream}}
  delivered:
    stream: !ref:stream
"""

#: The root written FIRST, under the file's own comment.
ROOT_FIRST = f"""\
# the file's own comment
contract: !class:recordstream.ops.contract.GraphContract
  name: first
  outputs: {{stream: Stream}}
  delivered:
    stream: !ref:stream
stream: !class:{STREAM}
  source: !class:recordstream.sources.files.FilesSource {{}}
  ops: []
"""

#: A Pipeline with no root — and a comment at the very end.
PIPE = f"""\
# a stream over one folder
stream: !class:{STREAM}
  source: !class:recordstream.sources.files.FilesSource {{}}
  ops:
    - !class:{PROBE} {{label: probe}}
# the end
"""

#: A card holding a parameter named ``thing``.
HELD = f"""\
holder: !class:{MOD}.CardHolder {{size: 1}}
stream: !class:{STREAM}
  source: !class:recordstream.sources.files.FilesSource {{}}
  ops: []
"""

#: A flow graph: its top level is steps.
FLOW = f"""\
flow:
  probe:
    op: !class:{PROBE} {{label: probe}}
outputs: probe
"""

#: A Stream whose first member is a subgraph: its inside is a flow of its own.
SUB = f"""\
stream: !class:{STREAM}
  source: !class:recordstream.sources.files.FilesSource {{}}
  ops:
    - !class:recordstream.flow.subgraph.Subgraph
      steps:
        first:
          op: !class:{PROBE} {{label: first}}
"""

#: A document that is ONE object: no top-level name a card could take.
ONE_OBJECT = f"""\
!class:{STREAM}
source: !class:recordstream.sources.files.FilesSource {{}}
ops: []
"""

#: A text that does not parse.
BROKEN = f"stream: !class:{STREAM}\n  ops: [\n"

#: A graph that offers its settings only.
LOCKED = f"""\
flow:
  probe:
    op: !class:{PROBE} {{label: probe}}
outputs: probe
app:
  graph: settings-only
"""

#: The one plain line a card asked of a flow graph is refused with — the user's words, ``{kind}`` the class's node kind.
IN_A_FLOW = "The editor can't add a value card to a flow graph yet — pick an op."


def _open(tmp_path: Path, text: str, name: str = "files.yaml") -> Document:
    path = tmp_path / name
    path.write_text(text)
    return Document.open(path)


def _wires(model: Model) -> List[Tuple[str, str, str, str]]:
    return [(w.source, w.source_socket, w.target, w.target_socket) for w in model.wires]


def _refused(document: Document, add: Any, error: type = ValueError) -> str:  # add: the call to make, of any shape
    """Make ``add`` and answer its refusal — and that nothing was written or held."""
    before = document.text
    with pytest.raises(error) as refused:
        add()
    assert document.text == before
    assert document.dirty is False
    assert document.undo() is False
    return str(refused.value)


# ------------------------------------------------------------------------------------------------
# the user's case
# ------------------------------------------------------------------------------------------------


class TestTheUsersCase:
    def test_the_card_goes_above_the_roots_comment_and_check_finds_nothing(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FILES)
        assert document.add_node(SCAN, {}) == "class_names_scan"
        assert document.text == SCANNED
        assert (
            "    - !class:recordstream.ops.image.ReadImage {field: file, output: image}\n"
            "class_names_scan: !class:recordstream.ops.contract.ClassNamesScan {}\n"
            "# The graph's ROOT — what this graph must deliver; the host applies a drop through its check.\n"
            "contract: !class:recordstream.ops.contract.GraphContract\n"
        ) in document.text
        card = document.model.node("class_names_scan")
        assert (card.class_path, card.kind) == (SCAN, "value")
        assert document.check() == []
        assert document.dirty is True
        assert (tmp_path / "files.yaml").read_text() == FILES  # held until Save

    def test_the_measured_request_with_the_host_card_selected_adds_the_card(self, tmp_path: Path) -> None:
        """``POST …/steps {"after": "<host>", "class_path": …ClassNamesScan, "params": {}}`` answered ``a new flow step
        needs a name — after '<host>', it has none``; the host card is no anchor, and the class is no op."""
        document = _open(tmp_path, FILES)
        assert document.add_step("", "<host>", SCAN, {}) == "class_names_scan"
        assert document.text == SCANNED

    def test_what_the_page_sent_with_nothing_selected_adds_the_card(self, tmp_path: Path) -> None:
        """The page put the class after the last member, ``stream.ops[0]``, and the type check refused the member."""
        document = _open(tmp_path, FILES)
        assert document.add_step("classnamesscan", "stream.ops[0]", SCAN, {}) == "class_names_scan"
        assert document.text == SCANNED
        assert document.check() == []

    def test_the_root_is_no_anchor_either(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FILES)
        assert document.add_step("x", "contract", SCAN, {}) == "class_names_scan"
        assert document.text == SCANNED


# ------------------------------------------------------------------------------------------------
# where the card goes
# ------------------------------------------------------------------------------------------------


class TestWhereTheCardGoes:
    def test_above_every_comment_line_written_over_the_root_below_the_blank_line(self, tmp_path: Path) -> None:
        document = _open(tmp_path, COMMENTED)
        document.add_node(SCAN, {})
        assert document.text == COMMENTED.replace(
            "\n# the root", f"\nclass_names_scan: !class:{SCAN} {{}}\n# the root", 1
        )
        assert document.check() == []

    def test_a_root_written_first_gets_the_card_above_it_under_the_files_own_comment(self, tmp_path: Path) -> None:
        document = _open(tmp_path, ROOT_FIRST)
        assert document.check() == []
        document.add_node(SCAN, {})
        assert document.text == ROOT_FIRST.replace("contract: ", f"class_names_scan: !class:{SCAN} {{}}\ncontract: ", 1)
        assert document.text.startswith("# the file's own comment\nclass_names_scan: ")
        assert document.check() == []

    def test_without_a_root_the_card_goes_at_the_end(self, tmp_path: Path) -> None:
        document = _open(tmp_path, PIPE)
        assert document.add_node(SCAN, {}) == "class_names_scan"
        assert document.text == PIPE + f"class_names_scan: !class:{SCAN} {{}}\n"
        assert document.check() == []

    def test_a_last_line_without_its_line_end_gets_one_first(self, tmp_path: Path) -> None:
        document = _open(tmp_path, PIPE.rstrip("\n"))
        document.add_node(SCAN, {})
        assert document.text == PIPE + f"class_names_scan: !class:{SCAN} {{}}\n"


# ------------------------------------------------------------------------------------------------
# the name — the move-out rule
# ------------------------------------------------------------------------------------------------


class TestTheName:
    def test_a_second_and_a_third_card_of_one_class_are_numbered(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FILES)
        assert [document.add_node(SCAN, {}) for _ in range(3)] == [
            "class_names_scan",
            "class_names_scan_2",
            "class_names_scan_3",
        ]
        assert document.text == _above_the_root(
            FILES,
            f"class_names_scan: !class:{SCAN} {{}}",
            f"class_names_scan_2: !class:{SCAN} {{}}",
            f"class_names_scan_3: !class:{SCAN} {{}}",
        )
        assert document.check() == []

    def test_a_name_another_card_takes_as_a_parameter_is_skipped(self, tmp_path: Path) -> None:
        """confluid hands a top-level key to every constructor taking a parameter of that name: a top-level ``thing``
        would be pushed into ``holder``'s ``thing`` — a wire nobody drew."""
        document = _open(tmp_path, HELD)
        assert document.add_node(f"{MOD}.Thing", {}) == "thing_2"
        assert document.text == HELD + f"thing_2: !class:{MOD}.Thing {{}}\n"
        assert document.check() == []

    def test_the_CON_case_a_free_name_is_the_class_name_itself(self, tmp_path: Path) -> None:
        document = _open(tmp_path, PIPE)
        assert document.add_node(f"{MOD}.Thing", {}) == "thing"


class TestTheValues:
    def test_they_are_spelled_on_the_cards_own_line(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FILES)
        document.add_node(OUTPUT, {"names": ["cat", "dog"]})
        assert document.text == _above_the_root(FILES, f"class_names_output: !class:{OUTPUT} {{names: [cat, dog]}}")
        assert document.model.node("class_names_output").params["names"].value == ["cat", "dog"]


class TestOneEditOneUndo:
    def test_one_undo_takes_the_card_out_and_one_redo_brings_it_back(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FILES)
        document.add_node(SCAN, {})
        assert document.undo() is True
        assert document.text == FILES
        assert document.undo() is False
        assert document.redo() is True
        assert document.text == SCANNED


# ------------------------------------------------------------------------------------------------
# refused, in one plain line — nothing written
# ------------------------------------------------------------------------------------------------


class TestRefused:
    def test_a_flow_graph_refuses_a_card_in_the_users_words(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FLOW)
        assert _refused(document, lambda: document.add_node(SCAN, {})) == IN_A_FLOW

    def test_the_kind_is_named_with_its_article(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FLOW)
        assert _refused(document, lambda: document.add_node(STREAM, {})) == (
            "The editor can't add an engine card to a flow graph yet — pick an op."
        )

    def test_a_flow_step_after_which_a_card_is_asked_is_refused_the_same(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FLOW)
        assert _refused(document, lambda: document.add_step("scan", "probe", SCAN, {})) == IN_A_FLOW

    def test_inside_a_subgraph_a_card_is_refused(self, tmp_path: Path) -> None:
        document = _open(tmp_path, SUB)
        assert document.check() == []
        assert _refused(document, lambda: document.add_step("scan", "stream.ops[0]/first", SCAN, {})) == (
            "The editor can't add a value card inside a subgraph yet — pick an op."
        )

    def test_an_op_is_never_a_card(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FILES)
        assert _refused(document, lambda: document.add_node(PROBE, {})) == (
            "PrintRecordOp is an op — the editor adds it after a step or to an op list, never as a card of its own."
        )

    def test_a_document_that_is_one_object_has_no_place_for_a_card(self, tmp_path: Path) -> None:
        document = _open(tmp_path, ONE_OBJECT)
        assert _refused(document, lambda: document.add_node(SCAN, {})) == (
            "The editor can't add a card to this graph yet: the whole file is one object, with no top-level name a "
            "card can take."
        )

    def test_a_text_that_does_not_parse_is_refused_where_it_breaks(self, tmp_path: Path) -> None:
        document = _open(tmp_path, BROKEN)
        message = _refused(document, lambda: document.add_node(SCAN, {}))
        assert (
            message == f"{(tmp_path / 'files.yaml').resolve()}:3:1: the text does not parse — fix it, then add the card"
        )

    def test_a_character_yaml_refuses_to_read_is_refused_naming_the_file(self, tmp_path: Path) -> None:
        """PyYAML's reader stops at a control character before any line is parsed: it names no line, so the file."""
        document = _open(tmp_path, "stream: \x07\n")
        message = _refused(document, lambda: document.add_node(SCAN, {}))
        assert message == f"{(tmp_path / 'files.yaml').resolve()}: the text does not parse — fix it, then add the card"

    def test_a_class_that_does_not_resolve_is_refused(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FILES)
        assert _refused(document, lambda: document.add_node("no.such.Class", {})) == (
            "cannot resolve the class 'no.such.Class'"
        )

    def test_an_anchor_that_names_no_node_is_a_key_error_even_for_a_card(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FILES)
        assert "no node 'nosuch'" in _refused(document, lambda: document.add_step("x", "nosuch", SCAN, {}), KeyError)

    def test_a_graph_that_offers_its_settings_only_refuses_it_first(self, tmp_path: Path) -> None:
        document = _open(tmp_path, LOCKED, "locked.yaml")
        assert _refused(document, lambda: document.add_node(SCAN, {})) == (
            "locked offers its settings only — its graph is fixed where it is shipped"
        )


# ------------------------------------------------------------------------------------------------
# the wire dropped on empty canvas: saved to the new card in the same edit
# ------------------------------------------------------------------------------------------------

#: A wire dragged out of the root's optional ``classes`` input: the new card feeds it.
INTO_CLASSES = ("", "", "contract", "classes")

#: The files graph after ``classes`` ⟵ a new ClassNamesOutput card.
OUTPUT_WIRED = (
    _above_the_root(FILES, f"class_names_output: !class:{OUTPUT} {{}}") + "    classes: !ref:class_names_output\n"
)


class TestTheWireDroppedOnEmptyCanvas:
    def test_from_the_roots_classes_the_card_is_written_and_wired_as_one_edit(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FILES)
        assert document.add_node(OUTPUT, {}, wire=INTO_CLASSES) == "class_names_output"
        assert document.text == OUTPUT_WIRED
        assert ("class_names_output", "class_names", "contract", "classes") in _wires(document.model)
        assert document.check() == []
        assert document.undo() is True
        assert document.text == FILES  # ONE undo takes the card and its wire
        assert document.redo() is True
        assert document.text == OUTPUT_WIRED

    def test_out_of_an_output_it_goes_into_the_new_cards_first_input_that_takes_it(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FILES)
        assert document.add_node(SCAN, {}, wire=("file_source", "stream", "", "")) == "class_names_scan"
        assert document.text == _above_the_root(FILES, f"class_names_scan: !class:{SCAN} {{source: !ref:file_source }}")
        assert ("file_source", "stream", "class_names_scan", "source") in _wires(document.model)
        assert document.check() == []

    def test_through_add_step_the_same(self, tmp_path: Path) -> None:
        """What the page sends for the pick: ``POST …/steps`` with the dragged end and no place (nothing feeds the
        root)."""
        document = _open(tmp_path, FILES)
        assert document.add_step("classnamesoutput", None, OUTPUT, {}, wire=INTO_CLASSES) == "class_names_output"
        assert document.text == OUTPUT_WIRED

    def test_an_input_the_new_card_has_none_for_adds_nothing(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FILES)
        assert _refused(document, lambda: document.add_node(SCAN, {}, wire=("stream.ops[0]", "image", "", ""))) == (
            "class_names_scan has no input that takes stream.ops[0].image (Image)."
        )

    def test_a_wire_the_editor_cannot_save_adds_nothing(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FILES)
        assert _refused(document, lambda: document.add_node(OUTPUT, {}, wire=("", "", "stream", "ops[0]"))) == (
            "This wire would change the order of the steps — the editor can't save that yet."
        )

    @pytest.mark.parametrize("wire", [("a", "b", "c", "d"), ("", "", "", ""), ("file_source", "", "", "")])
    def test_a_wire_must_name_exactly_the_end_it_was_dragged_out_of(
        self, tmp_path: Path, wire: Tuple[str, str, str, str]
    ) -> None:
        document = _open(tmp_path, FILES)
        assert _refused(document, lambda: document.add_node(SCAN, {}, wire=wire)) == (
            "a wire dropped on empty canvas names the end it was dragged out of — {source, source_socket} or "
            "{target, target_socket}; the new card is its other end"
        )

    def test_an_ops_wire_is_its_place(self, tmp_path: Path) -> None:
        """The CON case: an op picked from a dropped wire goes after the node the wire came from — that order IS the
        wire, and nothing else is written."""
        document = _open(tmp_path, FILES)
        added = document.add_step("probe", "stream.ops[0]", PROBE, {}, wire=("stream.ops[0]", "record", "", ""))
        assert added == "stream.ops[1]"
        assert document.text == FILES.replace("output: image}\n", f"output: image}}\n    - !class:{PROBE} {{}}\n", 1)


# ------------------------------------------------------------------------------------------------
# the host card and the root are never an anchor — and the document's kind decides
# ------------------------------------------------------------------------------------------------


class TestNeverAnAnchor:
    @pytest.mark.parametrize("anchor", ["<host>", "contract"])
    def test_an_op_after_the_host_card_or_the_root_goes_where_no_selection_puts_it(
        self, tmp_path: Path, anchor: str
    ) -> None:
        document = _open(tmp_path, FILES)
        assert document.add_step("probe", anchor, PROBE, {}, into="stream.ops") == "stream.ops[1]"
        assert document.text == FILES.replace("output: image}\n", f"output: image}}\n    - !class:{PROBE} {{}}\n", 1)

    @pytest.mark.parametrize("anchor", ["<host>", "contract"])
    def test_with_no_list_named_it_asks_for_a_place(self, tmp_path: Path, anchor: str) -> None:
        """The CON case: the old ``a new flow step needs a name — after '<host>', it has none`` never comes back."""
        document = _open(tmp_path, FILES)
        assert _refused(document, lambda: document.add_step("", anchor, PROBE, {})) == (
            "adding a class needs 'after' (the node to add it after) or 'list' (the op list to add it to the end of, "
            "e.g. stream.ops)"
        )


class TestTheDocumentsKindDecides:
    def test_a_class_the_catalogue_lists_as_a_value_but_the_document_derives_as_an_op_stays_an_op(
        self, tmp_path: Path
    ) -> None:
        document = _open(tmp_path, FILES)
        assert document.add_step("", "stream.ops[0]", f"{MOD}.Untagged", {}) == "stream.ops[1]"
        assert document.model.node("stream.ops[1]").kind == "op"
        assert "untagged" not in document.text


def test_the_synthetic_classes_resolve_by_their_dotted_path() -> None:
    """What every document above spells — a class that did not resolve would make each test above a refusal test."""
    from streamstudio.document import _resolve

    for name in ("CardHolder", "Thing", "Untagged"):
        resolved: Optional[type] = _resolve(f"{MOD}.{name}")
        assert resolved is not None and resolved.__name__ == name
