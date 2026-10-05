"""A wire drawn on the canvas is SAVED, in the document's own spelling — and a wire is removed the same way.

Before this, a hand-drawn wire stayed a dashed proposal the editor never wrote (``docs/rewrite.md`` §5). Now
``Document.add_wire`` writes it as ONE edit (held until Save, one undo entry), and ``Document.remove_wires`` takes
one or several wires out as ONE edit. Three spellings are written, each a text patch at the place it belongs —
never a re-dump:

(a) into an OUTPUT slot of a ``GraphContract`` root — ``delivered: {<slot>: !ref:<producer>}`` (``delivered:`` is
    made when the root has none);
(b) into a single slot or parameter of any other card — ``<slot>: !ref:<producer>`` on that card's marker;
(c) from a source's (or a step's) RECORD ENTRY into a record entry of the root — ``delivered: {<entry>: <key>}``,
    the plain word the root reads the entry from (``input: image``), never a ``!ref:``.

A ``!ref:`` names a TOP-LEVEL key only (confluid refuses ``!ref:stream.source``), so a producer written inside
another card's slot (``stream.source``) is first MOVED OUT to a top-level key named after its CLASS, in snake case —
``hugging_face_source`` for a ``HuggingFaceSource``, or ``hugging_face_source_2`` when that name is taken — and its
slot then reads ``!ref:hugging_face_source``. The card keeps its place: its sidecar position and groups follow the
new name. A name is "taken" when it is a top-level key, a node, or a constructor parameter of ANY card of the
document, the slot's owner included: confluid hands a top-level key to every constructor taking a parameter of that
name (measured 2026-10-04: a top-level ``source`` reached a second Stream's ``source``, and overrode one written in a
card above it; 2026-10-05: ``second (no source written) got: FilesSource ['a.png']``).

A source's record-entry outputs are its ``produces`` (a HuggingFace source answers it from its settings and the
dataset's description), an op's its own (``ReadImage.produces`` follows its ``output``, read off the built op); no test
asks the Hub — ``tests/conftest.py`` answers every description with nothing.

A root's ``optional`` slots are inputs WITHOUT the required mark, after its record entries: wired and removed like an
output slot, never a problem while unwired. A rooted graph that declares ``inputs`` draws them on the HOST card
(``<host>``, never written), and where the host puts one when it runs the graph — the parameter of that name on
exactly one card of the delivered stream's source chain (a ``FilesSource``'s ``files``) — is a dashed ``host`` wire the
editor derives, never saves, and refuses to remove. The files graph a host workspace seeds is pinned whole here.

Everything else is refused before anything is written, with one plain line. The user's case — the source graph a
classification workspace seeds — is pinned end to end at the bottom: three wires, Save, and the saved text loads,
passes ``GraphContract.check()`` and reads ``input`` / ``target`` from ``image`` / ``class``.
"""

from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import confluid
import pytest
from confluid import configurable, output
from recordstream import Image, Record
from recordstream.ops.contract import GraphContract
from recordstream.sources import huggingface
from recordstream.sources.huggingface import HuggingFaceSource

from streamstudio.document import DELIVERED_LITERALLY, Document, Model, _snake
from streamstudio.layout import Group, read_groups, read_sidecar, write_sidecar

MOD = __name__


# ------------------------------------------------------------------------------------------------
# synthetic classes — the documents below spell them as !class:<MOD>.<Class>
# ------------------------------------------------------------------------------------------------


@configurable(category="source", group="wire-test")
class WireSource:
    """A source that declares the entries every record carries, and a class vocabulary.

    Args:
        root: Where the files are.
    """

    produces = {"image": "Image", "class": "Label"}

    def __init__(self, root: str = "") -> None:
        self.root = root

    def __iter__(self) -> Iterator[Record]:
        return iter(())

    @property
    @output
    def class_names(self) -> List[str]:
        """The class vocabulary."""
        return ["cat", "dog"]


@configurable(category="source", group="wire-test")
class WireWrap:
    """A source wrapping another one — a card with a card in its own slot.

    Args:
        inner: The wrapped source.
    """

    def __init__(self, inner: Any = None) -> None:  # any source object
        self.inner = inner

    def __iter__(self) -> Iterator[Record]:
        return iter(())


@configurable(category="value", group="wire-test")
class WireHolder:
    """A card with an untyped parameter a wire can fill.

    Args:
        thing: Anything.
        size: A number.
    """

    def __init__(self, thing: Any = None, size: int = 0) -> None:  # thing: any object a wire brings
        self.thing = thing
        self.size = size


@configurable(category="value", group="wire-test")
class WireNeeds:
    """A card whose constructor REFUSES to be built without its ``thing``.

    Args:
        thing: What it needs.
    """

    def __init__(self, thing: Any) -> None:  # any object a wire brings
        self.thing = thing


@configurable(category="value", group="wire-test")
class WirePreview:
    """A card with an ``Image`` output that is NOT a record entry — an object, not an entry of every record."""

    @property
    @output
    def picture(self) -> Image:
        """One picture."""
        return Image([[0]])


@configurable(category="op", group="wire-test")
class WireRename:
    """Rewrite the image entry — a step that writes ``image`` after the source does."""

    consumes = {"image": "Image"}
    produces = {"image": "Image"}

    def __call__(self, record: Record) -> Record:
        return record


@configurable(category="op", group="wire-test")
class WireDecode:
    """Write an image entry — the head of a flow."""

    produces = {"image": "Image"}

    def __call__(self, record: Record) -> Record:
        return record


@configurable(category="source", group="wire-test")
class WireLimit:
    """A source passing on the first records of the source it wraps — a card BETWEEN a Stream and the source the host
    fills.

    Args:
        source: The wrapped source.
        count: How many records it passes on.
    """

    def __init__(self, source: Any = None, count: int = 1) -> None:  # any source object
        self.source = source
        self.count = count

    def __iter__(self) -> Iterator[Record]:
        return iter(())


@configurable(category="source", group="wire-test")
class WireListed:
    """A source wrapping another one that takes ``files`` ITSELF — two cards of one chain then take the host's files.

    Args:
        source: The wrapped source.
        files: The paths it lists.
    """

    def __init__(self, source: Any = None, files: Optional[List[str]] = None) -> None:  # any source object
        self.source = source
        self.files = list(files or [])

    def __iter__(self) -> Iterator[Record]:
        return iter(())


class Thing:
    """A source whose class name in snake case, ``thing``, is a parameter of :class:`WireHolder` (a plain class: the
    editor resolves it by its dotted path)."""

    def __iter__(self) -> Iterator[Record]:
        return iter(())


class Source:
    """A source whose class name in snake case, ``source``, is a parameter of the ``Stream`` holding it."""

    def __iter__(self) -> Iterator[Record]:
        return iter(())


# ------------------------------------------------------------------------------------------------
# documents
# ------------------------------------------------------------------------------------------------

#: The user's graph: the source graph a classification workspace seeds (its contract root, its comments).
USER_SEED = """\
stream: !class:recordstream.core.stream.Stream
  source: !class:recordstream.sources.huggingface.HuggingFaceSource {path: ylecun/mnist}
  ops: []
# The graph's ROOT — what this graph must deliver, declared by the workspace (its `graphs:`).
# Wire what each output needs; the workspace applies the graph through this root's own check.
contract: !class:recordstream.ops.contract.GraphContract
  name: "classification source"
  outputs: {"stream": "Stream", "classes": "List[str]"}
  records: {"input": "Image", "target": "Label"}
  delivered:
    stream: !ref:stream
"""

#: After ``stream.source.class_names → contract.classes``: the source moved out above ``stream``, to a key named after
#: its class — ``HuggingFaceSource`` → ``hugging_face_source``.
USER_ONE = """\
hugging_face_source: !class:recordstream.sources.huggingface.HuggingFaceSource {path: ylecun/mnist}
stream: !class:recordstream.core.stream.Stream
  source: !ref:hugging_face_source
  ops: []
# The graph's ROOT — what this graph must deliver, declared by the workspace (its `graphs:`).
# Wire what each output needs; the workspace applies the graph through this root's own check.
contract: !class:recordstream.ops.contract.GraphContract
  name: "classification source"
  outputs: {"stream": "Stream", "classes": "List[str]"}
  records: {"input": "Image", "target": "Label"}
  delivered:
    stream: !ref:stream
    classes: !ref:hugging_face_source
"""

#: After ``source.image → contract.input`` too.
USER_TWO = USER_ONE + "    input: image\n"

#: After ``source.class → contract.target`` too — what Save writes.
USER_SAVED = USER_TWO + "    target: class\n"

#: A source graph of synthetic classes in the same shape, a holder card beside it.
WRAPPED = f"""\
stream: !class:recordstream.core.stream.Stream
  source: !class:{MOD}.WireWrap
    inner: !class:{MOD}.WireSource {{root: /a}}
  ops: []
holder: !class:{MOD}.WireHolder {{size: 1}}
"""

#: A top-level ``wire_source`` exists already: the moved-out ``WireSource`` takes ``wire_source_2``.
TAKEN = f"""\
wire_source: !class:{MOD}.WireHolder {{size: 1}}
stream: !class:recordstream.core.stream.Stream
  source: !class:{MOD}.WireSource {{root: /a}}
  ops: []
contract: !class:recordstream.ops.contract.GraphContract
  name: taken
  outputs: {{stream: Stream, classes: "List[str]"}}
  delivered:
    stream: !ref:stream
"""

#: ``thing`` — the moved-out card's class name in snake case — is a parameter of ANOTHER card (``holder``): a top-level
#: ``thing`` would be pushed into it, so the card takes ``thing_2`` — inserted above ``stream`` AND the comment written
#: for it.
BROADCAST = f"""\
holder: !class:{MOD}.WireHolder {{size: 1}}
# the records
stream: !class:recordstream.core.stream.Stream
  source: !class:{MOD}.Thing {{}}
  ops: []
user: !class:{MOD}.WireHolder {{size: 2}}
"""

#: ``source`` — the moved-out card's class name in snake case — is a parameter of the very Stream whose slot held it.
OWNER = f"""\
stream: !class:recordstream.core.stream.Stream
  source: !class:{MOD}.Source {{}}
  ops: []
holder: !class:{MOD}.WireHolder {{size: 1}}
"""

#: A FilesSource written in the Stream's slot — the files graph before its source was moved out.
NESTED_FILES = f"""\
stream: !class:recordstream.core.stream.Stream
  source: !class:recordstream.sources.files.FilesSource {{}}
  ops: []
holder: !class:{MOD}.WireHolder {{size: 1}}
"""

#: The files graph a host workspace seeds (2026-10-05): a FilesSource the host fills with the files a person drops, a
#: Stream reading each file as an image, and its root — the host's ``files`` as its input, an optional class list, the
#: record entry ``input`` read from ``image``.
FILES = """\
file_source: !class:recordstream.sources.files.FilesSource {}
stream: !class:recordstream.core.stream.Stream
  source: !ref:file_source
  ops:
    - !class:recordstream.ops.image.ReadImage {field: file, output: image}
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

#: The files graph with a class list beside it, for the optional ``classes`` slot.
FILES_NAMES = FILES.replace(
    "contract: ", "names: !class:recordstream.ops.contract.ClassNamesOutput {names: [cat, dog]}\ncontract: ", 1
)

#: A top-level source, a holder in block form, a contract with no ``delivered:`` yet.
TOP = f"""\
source: !class:{MOD}.WireSource {{root: /a}}
stream: !class:recordstream.core.stream.Stream
  source: !ref:source
  ops: []
holder: !class:{MOD}.WireHolder
  size: 1
contract: !class:recordstream.ops.contract.GraphContract
  name: fresh
  outputs: {{stream: Stream, classes: "List[str]"}}
  records: {{input: Image}}
"""

#: The vocabulary typed as a list, which a drawn wire replaces.
LITERAL = f"""\
source: !class:{MOD}.WireSource {{root: /a}}
stream: !class:recordstream.core.stream.Stream
  source: !ref:source
  ops: []
contract: !class:recordstream.ops.contract.GraphContract
  name: literal
  outputs: {{stream: Stream, classes: "List[str]"}}
  delivered:
    stream: !ref:stream
    classes: [cat, dog]
"""

#: A Stream whose step rewrites ``image`` after the source, a spare source, a preview card and a spare op.
STEPS = f"""\
source: !class:{MOD}.WireSource {{root: /a}}
spare: !class:{MOD}.WireSource {{root: /b}}
preview: !class:{MOD}.WirePreview {{}}
extra: !class:{MOD}.WireRename {{}}
holder: !class:{MOD}.WireHolder {{size: 1}}
stream: !class:recordstream.core.stream.Stream
  source: !ref:source
  ops:
    - !class:{MOD}.WireRename {{}}
    - !class:recordstream.ops.random_apply.RandomApply
      op: !class:{MOD}.WireRename {{}}
contract: !class:recordstream.ops.contract.GraphContract
  name: steps
  outputs: {{stream: Stream}}
  records: {{input: Image, target: Label}}
  delivered:
    stream: !ref:stream
"""

#: A flow: its wires are ``from:`` / ``bind:`` / gates.
FLOW = f"""\
holder: !class:{MOD}.WireHolder {{size: 1}}
flow:
  decode:
    op: !class:{MOD}.WireDecode {{}}
  rename:
    op: !class:{MOD}.WireRename {{}}
    from: decode
  prep:
    op: !class:recordstream.flow.subgraph.Subgraph
      steps:
        first:
          op: !class:{MOD}.WireRename {{}}
        second:
          op: !class:{MOD}.WireRename {{}}
          from: first
      result: second
    from: rename
outputs: prep
"""

#: A document that is ONE object: no top-level name a ``!ref:`` could point at.
ONE_OBJECT = f"""\
!class:recordstream.core.stream.Stream
source: !class:{MOD}.WireSource {{root: /a}}
ops: []
"""

#: A card whose constructor needs what a wire brings it.
NEEDS = f"""\
source: !class:{MOD}.WireSource {{root: /a}}
needs: !class:{MOD}.WireNeeds
  thing: !ref:source
"""


def _write(tmp_path: Path, text: str, name: str = "graph.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def _open(tmp_path: Path, text: str, name: str = "graph.yaml") -> Document:
    return Document.open(_write(tmp_path, text, name))


def _wires(model: Model, kind: Optional[str] = None) -> List[Tuple[str, str, str, str]]:
    return [(w.source, w.source_socket, w.target, w.target_socket) for w in model.wires if kind in (None, w.kind)]


def _refused(document: Document, wire: Tuple[str, str, str, str]) -> str:
    """Add ``wire`` and answer the refusal's message — and that nothing was written."""
    before = document.text
    with pytest.raises(ValueError) as refused:
        document.add_wire(*wire)
    assert document.text == before
    assert document.dirty is False
    return str(refused.value)


# ------------------------------------------------------------------------------------------------
# the sockets a wire is drawn between
# ------------------------------------------------------------------------------------------------


class TestTheSockets:
    def test_the_root_has_an_input_per_output_slot_then_one_per_record_entry(self, tmp_path: Path) -> None:
        contract = _open(tmp_path, USER_SEED).model.node("contract")
        assert [(s.name, s.type, s.required, s.entry) for s in contract.inputs] == [
            ("stream", "Stream", True, ""),
            ("classes", "List[str]", True, ""),
            ("input", "Image", False, "input"),
            ("target", "Label", False, "target"),
        ]

    def test_a_source_has_an_output_per_entry_it_declares_after_its_stream(self, tmp_path: Path) -> None:
        source = _open(tmp_path, USER_SEED).model.node("stream.source")
        assert [(s.name, s.type, s.entry) for s in source.outputs] == [
            ("stream", "Source", ""),
            ("image", "Image", "image"),
            ("class", "Label", "class"),
            ("class_names", "List[str]", ""),
        ]

    def test_a_sources_entry_outputs_follow_its_settings(self, tmp_path: Path) -> None:
        """``HuggingFaceSource.produces`` is answered by the BUILT source from its settings: a metadata column it is
        set to keep is one more entry output, after a setting edit like any other."""
        document = _open(tmp_path, USER_SEED)
        document.set_param("stream.source", "metadata_features", ["coarse_label"])
        assert [(s.name, s.type, s.entry) for s in document.model.node("stream.source").outputs] == [
            ("stream", "Source", ""),
            ("image", "Image", "image"),
            ("class", "Label", "class"),
            ("coarse_label", "Label", "coarse_label"),
            ("class_names", "List[str]", ""),
        ]

    def test_a_sources_entry_outputs_follow_the_dataset_it_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With every column kept (``"*"``, the default) the dataset's own description decides — CIFAR-100's."""
        monkeypatch.setattr(
            huggingface,
            "described_columns",
            lambda path, name=None, revision=None: ("img", "fine_label", "coarse_label"),
        )
        text = USER_SEED.replace(
            "{path: ylecun/mnist}", "{path: uoft-cs/cifar100, input_feature: img, target_feature: fine_label}"
        )
        outputs = _open(tmp_path, text).model.node("stream.source").outputs
        assert [s.name for s in outputs if s.entry] == ["image", "class", "coarse_label"]

    def test_a_source_that_declares_no_entries_has_its_stream_only(self, tmp_path: Path) -> None:
        wrap = _open(tmp_path, WRAPPED).model.node("stream.source")
        assert [s.name for s in wrap.outputs] == ["stream"]

    def test_a_record_entry_read_from_a_declared_key_is_drawn_from_the_card_that_writes_it(
        self, tmp_path: Path
    ) -> None:
        model = _open(tmp_path, USER_SAVED).model
        assert _wires(model, "delivered") == [
            ("stream", "stream", "contract", "stream"),
            ("hugging_face_source", "class_names", "contract", "classes"),
            ("hugging_face_source", "image", "contract", "input"),
            ("hugging_face_source", "class", "contract", "target"),
        ]
        assert [w.type for w in model.wires if w.target == "contract"] == ["Stream", "List[str]", "Image", "Label"]

    def test_the_last_step_writing_the_key_is_the_card_it_is_read_from(self, tmp_path: Path) -> None:
        text = STEPS.replace("    stream: !ref:stream\n", "    stream: !ref:stream\n    input: image\n")
        model = _open(tmp_path, text).model
        assert ("stream.ops[0]", "image", "contract", "input") in _wires(model, "delivered")
        assert not [w for w in model.wires if w.target_socket == "input" and w.source == "source"]

    def test_a_key_no_card_declares_draws_no_wire_and_fills_the_socket(self, tmp_path: Path) -> None:
        document = _open(
            tmp_path, USER_SEED.replace("    stream: !ref:stream\n", "    stream: !ref:stream\n    input: pixels\n")
        )
        model = document.model
        assert not [w for w in model.wires if w.target_socket == "input"]
        assert model.node("contract").params["input"].value == "pixels"
        # the one problem is the vocabulary nobody wired yet — the entry is filled, not open
        assert [p.message.split(" at ")[0] for p in document.check()] == [
            "contract.classes (List[str]) is required and nothing is wired into it"
        ]


# ------------------------------------------------------------------------------------------------
# (a) into a root's output slot; the move-out
# ------------------------------------------------------------------------------------------------


class TestIntoARootsOutputSlot:
    def test_a_nested_producer_moves_out_above_its_card_and_its_slot_reads_the_reference(self, tmp_path: Path) -> None:
        document = _open(tmp_path, USER_SEED)
        assert document.add_wire("stream.source", "class_names", "contract", "classes") == "hugging_face_source"
        assert document.text == USER_ONE
        assert ("hugging_face_source", "class_names", "contract", "classes") in _wires(document.model, "delivered")
        assert ("hugging_face_source", "stream", "stream", "source") in _wires(document.model)
        assert "stream.source" not in document.model.names()
        assert document.dirty is True
        assert (tmp_path / "graph.yaml").read_text() == USER_SEED  # held until Save

    def test_delivered_is_made_when_the_root_has_none(self, tmp_path: Path) -> None:
        document = _open(tmp_path, TOP)
        assert document.add_wire("stream", "stream", "contract", "stream") == "stream"
        assert document.text.endswith("  records: {input: Image}\n  delivered:\n    stream: !ref:stream\n")

    def test_a_slot_that_held_a_typed_list_is_replaced(self, tmp_path: Path) -> None:
        document = _open(tmp_path, LITERAL)
        document.add_wire("source", "class_names", "contract", "classes")
        assert document.text.endswith("    stream: !ref:stream\n    classes: !ref:source\n")


# ------------------------------------------------------------------------------------------------
# the moved-out card is named after its class
# ------------------------------------------------------------------------------------------------


class TestTheMovedOutCardIsNamedAfterItsClass:
    """The key a card moved out of a slot takes: its class name in snake case — ``_`` before an uppercase letter that
    follows a lowercase letter or a digit, and before the last uppercase of an uppercase run that a lowercase follows —
    then ``_2``, ``_3`` … while it is TAKEN: a top-level key, a node, or a constructor parameter of any card, the
    slot's owner included."""

    @pytest.mark.parametrize(
        "class_path, key",
        [
            ("recordstream.sources.huggingface.HuggingFaceSource", "hugging_face_source"),
            ("recordstream.sources.files.FilesSource", "files_source"),
            ("somewhere.HTTPSource", "http_source"),
            ("somewhere.Mp3Source", "mp3_source"),
        ],
    )
    def test_the_key_is_the_class_name_in_snake_case(self, class_path: str, key: str) -> None:
        assert _snake(class_path) == key

    def test_a_files_source_moves_out_as_files_source(self, tmp_path: Path) -> None:
        document = _open(tmp_path, NESTED_FILES)
        assert document.add_wire("stream.source", "stream", "holder", "thing") == "files_source"
        assert document.text == (
            "files_source: !class:recordstream.sources.files.FilesSource {}\n"
            "stream: !class:recordstream.core.stream.Stream\n"
            "  source: !ref:files_source\n"
            "  ops: []\n"
            f"holder: !class:{MOD}.WireHolder {{size: 1, thing: !ref:files_source }}\n"
        )

    def test_a_taken_name_gets_the_next_free_suffix(self, tmp_path: Path) -> None:
        document = _open(tmp_path, TAKEN)
        assert document.add_wire("stream.source", "class_names", "contract", "classes") == "wire_source_2"
        assert document.text == (
            f"wire_source: !class:{MOD}.WireHolder {{size: 1}}\n"
            f"wire_source_2: !class:{MOD}.WireSource {{root: /a}}\n"
            "stream: !class:recordstream.core.stream.Stream\n"
            "  source: !ref:wire_source_2\n"
            "  ops: []\n"
            "contract: !class:recordstream.ops.contract.GraphContract\n"
            "  name: taken\n"
            '  outputs: {stream: Stream, classes: "List[str]"}\n'
            "  delivered:\n"
            "    stream: !ref:stream\n"
            "    classes: !ref:wire_source_2\n"
        )

    def test_a_class_named_like_a_parameter_of_another_card_is_taken(self, tmp_path: Path) -> None:
        """The CON case of the plain class name: ``holder`` takes a ``thing``, so a top-level ``thing`` would be handed
        to it too — the moved card takes ``thing_2``, and ``holder`` stays without one."""
        document = _open(tmp_path, BROADCAST)
        assert document.add_wire("stream.source", "stream", "user", "thing") == "thing_2"
        assert document.text == (
            f"holder: !class:{MOD}.WireHolder {{size: 1}}\n"
            f"thing_2: !class:{MOD}.Thing {{}}\n"
            "# the records\n"
            "stream: !class:recordstream.core.stream.Stream\n"
            "  source: !ref:thing_2\n"
            "  ops: []\n"
            f"user: !class:{MOD}.WireHolder {{size: 2, thing: !ref:thing_2 }}\n"
        )
        built = confluid.load(document.text)
        assert built["holder"].thing is None
        assert isinstance(built["user"].thing, Thing)
        assert built["stream"].source is built["user"].thing

    def test_a_class_named_like_a_parameter_of_the_slots_owner_is_taken(self, tmp_path: Path) -> None:
        """The owner is no exception: ``Source`` is ``source``, a parameter of the Stream that held it. Named
        ``source``, the card would stay in that Stream through confluid after the person removed the wire; as
        ``source_2`` the removal leaves the Stream without a source."""
        document = _open(tmp_path, OWNER)
        assert document.add_wire("stream.source", "stream", "holder", "thing") == "source_2"
        assert document.text.startswith(f"source_2: !class:{MOD}.Source {{}}\n")
        assert "  source: !ref:source_2\n" in document.text
        document.remove_wires([("source_2", "stream", "stream", "source")])
        built = confluid.load(document.text)
        assert built["stream"].source is None
        assert isinstance(built["holder"].thing, Source)


class TestTheMovedCardKeepsItsPlace:
    def test_its_sidecar_position_and_group_and_those_under_it_follow_the_new_name(self, tmp_path: Path) -> None:
        document = _open(tmp_path, WRAPPED)
        group: Group = {"id": "g1", "title": "inputs", "color": "#335", "nodes": ["stream.source.inner"]}
        write_sidecar(
            document.sidecar_path,
            {"stream.source": (400.0, 200.0), "stream.source.inner": (400.0, 400.0)},
            groups=[group],
        )
        assert document.add_wire("stream.source", "stream", "holder", "thing") == "wire_wrap"
        assert document.positions["wire_wrap"] == (400.0, 200.0)
        assert document.positions["wire_wrap.inner"] == (400.0, 400.0)
        assert document.groups[0].get("nodes") == ["wire_wrap.inner"]
        document.save()
        assert read_sidecar(document.sidecar_path)["wire_wrap.inner"] == (400.0, 400.0)
        assert read_groups(document.sidecar_path)[0].get("nodes") == ["wire_wrap.inner"]

    def test_without_a_sidecar_it_stays_where_it_was_drawn(self, tmp_path: Path) -> None:
        document = _open(tmp_path, USER_SEED)
        drawn = document.positions["stream.source"]
        document.add_wire("stream.source", "class_names", "contract", "classes")
        assert document.positions["hugging_face_source"] == drawn

    def test_the_wrapped_text_moves_whole_and_dedented(self, tmp_path: Path) -> None:
        document = _open(tmp_path, WRAPPED)
        document.add_wire("stream.source", "stream", "holder", "thing")
        assert document.text == (
            f"wire_wrap: !class:{MOD}.WireWrap\n"
            f"  inner: !class:{MOD}.WireSource {{root: /a}}\n"
            "stream: !class:recordstream.core.stream.Stream\n"
            "  source: !ref:wire_wrap\n"
            "  ops: []\n"
            f"holder: !class:{MOD}.WireHolder {{size: 1, thing: !ref:wire_wrap }}\n"
        )
        assert [node.name for node in document.model.nodes] == ["wire_wrap", "wire_wrap.inner", "stream", "holder"]


# ------------------------------------------------------------------------------------------------
# (b) into a slot or a parameter of any other card
# ------------------------------------------------------------------------------------------------


class TestIntoACardsSlot:
    def test_a_parameter_the_card_does_not_spell_is_inserted_as_a_reference(self, tmp_path: Path) -> None:
        document = _open(tmp_path, TOP)
        assert document.add_wire("source", "stream", "holder", "thing") == "source"
        assert f"holder: !class:{MOD}.WireHolder\n  thing: !ref:source\n  size: 1\n" in document.text
        assert ("source", "stream", "holder", "thing") in _wires(document.model, "ref")

    def test_a_value_the_card_spelled_is_replaced(self, tmp_path: Path) -> None:
        document = _open(tmp_path, WRAPPED.replace("{size: 1}", "{size: 1, thing: 5}"))
        document.add_wire("stream.source", "stream", "holder", "thing")
        assert f"holder: !class:{MOD}.WireHolder {{size: 1, thing: !ref:wire_wrap }}\n" in document.text

    def test_a_streams_source_slot_takes_a_top_level_source(self, tmp_path: Path) -> None:
        document = _open(tmp_path, STEPS)
        document.add_wire("spare", "stream", "stream", "source")
        assert "  source: !ref:spare\n" in document.text
        assert ("spare", "stream", "stream", "source") in _wires(document.model, "ref")

    def test_an_output_that_is_not_the_first_to_fit_is_refused(self, tmp_path: Path) -> None:
        document = _open(tmp_path, TOP)
        assert _refused(document, ("source", "class_names", "holder", "thing")) == (
            "source has more than one output that fits holder.thing; only stream can be wired into it."
        )

    def test_types_that_do_not_meet_are_refused(self, tmp_path: Path) -> None:
        document = _open(tmp_path, TOP)
        assert _refused(document, ("source", "class_names", "contract", "stream")) == (
            "source.class_names (List[str]) cannot be wired into contract.stream (Stream): the types do not meet."
        )


# ------------------------------------------------------------------------------------------------
# (c) a record entry into a record entry of the root
# ------------------------------------------------------------------------------------------------


class TestARecordEntry:
    def test_it_is_saved_as_the_key_the_entry_is_read_from(self, tmp_path: Path) -> None:
        document = _open(tmp_path, USER_ONE)
        assert document.add_wire("hugging_face_source", "image", "contract", "input") == "hugging_face_source"
        assert document.text == USER_TWO

    def test_a_nested_source_is_not_moved_out_for_it(self, tmp_path: Path) -> None:
        document = _open(tmp_path, USER_SEED)
        assert document.add_wire("stream.source", "image", "contract", "input") == "stream.source"
        assert document.text == USER_SEED + "    input: image\n"
        assert ("stream.source", "image", "contract", "input") in _wires(document.model, "delivered")

    def test_from_the_last_step_that_writes_it(self, tmp_path: Path) -> None:
        document = _open(tmp_path, STEPS)
        document.add_wire("stream.ops[0]", "image", "contract", "input")
        assert document.text.endswith("    stream: !ref:stream\n    input: image\n")

    def test_an_output_that_is_not_a_record_entry_is_refused(self, tmp_path: Path) -> None:
        assert _refused(_open(tmp_path, STEPS), ("preview", "picture", "contract", "input")) == (
            "Only a record entry — like a source's image — can be wired into input."
        )

    def test_an_entry_a_later_step_writes_again_is_refused_naming_that_step(self, tmp_path: Path) -> None:
        assert _refused(_open(tmp_path, STEPS), ("source", "image", "contract", "input")) == (
            "contract.input would be read from stream.ops[0].image, which writes 'image' after source does."
        )

    def test_a_card_outside_the_delivered_stream_is_refused(self, tmp_path: Path) -> None:
        assert _refused(_open(tmp_path, STEPS), ("spare", "image", "contract", "input")) == (
            "spare does not feed the stream contract delivers (stream) — contract.input is read from that stream's "
            "records."
        )

    def test_with_no_stream_delivered_yet_it_is_refused(self, tmp_path: Path) -> None:
        assert _refused(_open(tmp_path, TOP), ("source", "image", "contract", "input")) == (
            "Wire a stream into contract.stream first — contract.input is read from the records it delivers."
        )

    def test_an_entry_into_a_card_that_is_not_the_root_is_refused(self, tmp_path: Path) -> None:
        assert _refused(_open(tmp_path, STEPS), ("source", "image", "holder", "thing")) == (
            "The editor can't save this wire yet: source.image is an entry of every record, and only a record "
            "entry of the graph's root can take one."
        )

    def test_a_wire_into_what_a_step_reads_from_its_record_is_refused(self, tmp_path: Path) -> None:
        assert _refused(_open(tmp_path, STEPS), ("preview", "picture", "stream.ops[0]", "image")) == (
            "The editor can't save this wire yet: stream.ops[0].image reads the record entry 'image' from the "
            "record it is handed."
        )


# ------------------------------------------------------------------------------------------------
# refused: what the editor cannot save yet
# ------------------------------------------------------------------------------------------------


class TestRefused:
    def test_a_record_wire_would_change_the_order_of_the_steps(self, tmp_path: Path) -> None:
        assert _refused(_open(tmp_path, STEPS), ("stream.ops[1]", "record", "stream.ops[0]", "record")) == (
            "This wire would change the order of the steps — the editor can't save that yet."
        )

    def test_a_wire_that_is_there_already_is_refused(self, tmp_path: Path) -> None:
        assert _refused(_open(tmp_path, STEPS), ("source", "stream", "stream", "source")) == (
            "source.stream is already wired into stream.source."
        )

    def test_a_wire_into_a_member_of_an_op_list_would_change_the_order_of_the_steps(self, tmp_path: Path) -> None:
        assert _refused(_open(tmp_path, STEPS), ("extra", "op", "stream", "ops[1]")) == (
            "This wire would change the order of the steps — the editor can't save that yet."
        )

    def test_any_wire_of_a_flow_is_refused(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FLOW)
        message = "The editor can't save wires in this kind of graph yet."
        assert _refused(document, ("decode", "record", "rename", "record")) == message
        assert _refused(document, ("decode", "image", "rename", "image")) == message

    def test_inside_a_subgraph_it_is_refused(self, tmp_path: Path) -> None:
        assert _refused(_open(tmp_path, FLOW), ("prep/first", "record", "prep/second", "record")) == (
            "The editor can't save wires inside a subgraph yet."
        )

    def test_a_document_that_is_one_object_is_refused(self, tmp_path: Path) -> None:
        document = _open(tmp_path, ONE_OBJECT, name="one.yaml")
        assert _refused(document, ("one.source", "stream", "one", "source")) == (
            "The editor can't save wires in this graph yet: the whole file is one object, with no top-level name a "
            "wire can point at."
        )

    def test_a_card_inside_a_list_member_is_refused(self, tmp_path: Path) -> None:
        document = _open(tmp_path, STEPS)
        assert _refused(document, ("stream.ops[1].op", "op", "holder", "thing")) == (
            "The editor can't save a wire from stream.ops[1].op yet: it sits inside a list (stream.ops[1])."
        )
        assert _refused(document, ("stream.ops[0]", "op", "holder", "thing")) == (
            "The editor can't save a wire from stream.ops[0] yet: it sits inside a list (stream.ops[0])."
        )

    def test_an_unknown_node_names_the_nodes_there_are(self, tmp_path: Path) -> None:
        with pytest.raises(KeyError, match="no node 'nosuch'"):
            _open(tmp_path, TOP).add_wire("nosuch", "stream", "holder", "thing")

    def test_an_unknown_socket_names_the_sockets_there_are(self, tmp_path: Path) -> None:
        assert _refused(_open(tmp_path, TOP), ("source", "nosuch", "holder", "thing")) == (
            "source has no output 'nosuch' (its outputs are: stream, image, class, class_names)"
        )
        assert _refused(_open(tmp_path, TOP), ("source", "stream", "holder", "nosuch")) == (
            "holder has no input 'nosuch' (its inputs are: thing, size)"
        )


# ------------------------------------------------------------------------------------------------
# removing
# ------------------------------------------------------------------------------------------------


class TestRemoving:
    def test_a_delivered_wire_drops_its_pair(self, tmp_path: Path) -> None:
        document = _open(tmp_path, USER_SAVED)
        document.remove_wires([("hugging_face_source", "image", "contract", "input")])
        assert document.text == USER_SAVED.replace("    input: image\n", "")

    def test_a_wire_into_a_required_slot_is_removed_although_the_slot_is_then_open(self, tmp_path: Path) -> None:
        document = _open(tmp_path, USER_SAVED)
        document.remove_wires([("hugging_face_source", "class_names", "contract", "classes")])
        assert "classes: !ref:hugging_face_source" not in document.text
        assert [p.message.split(" at ")[0] for p in document.check()] == [
            "contract.classes (List[str]) is required and nothing is wired into it"
        ]

    def test_the_last_delivered_pair_takes_delivered_with_it(self, tmp_path: Path) -> None:
        document = _open(tmp_path, USER_SAVED)
        document.remove_wires(
            [
                ("stream", "stream", "contract", "stream"),
                ("hugging_face_source", "class_names", "contract", "classes"),
                ("hugging_face_source", "image", "contract", "input"),
                ("hugging_face_source", "class", "contract", "target"),
            ]
        )
        assert document.text == USER_ONE.replace(
            "  delivered:\n    stream: !ref:stream\n    classes: !ref:hugging_face_source\n", ""
        )
        assert document.undo() and document.text == USER_SAVED  # ONE edit
        assert document.undo() is False

    def test_a_reference_drops_its_pair_and_the_parameter_returns_to_its_default(self, tmp_path: Path) -> None:
        document = _open(tmp_path, TOP.replace("  size: 1\n", "  thing: !ref:source\n  size: 1\n"))
        document.remove_wires([("source", "stream", "holder", "thing")])
        assert document.text == TOP
        assert document.model.node("holder").params["thing"].spelled is False

    def test_a_card_in_the_slot_moves_out_named_after_its_class_and_both_cards_stay(self, tmp_path: Path) -> None:
        """The slot is dropped and the card moves out under its class name, ``wire_wrap`` — a parameter of no card, so
        confluid hands it to nobody: the Stream is left without a source, as the person asked."""
        document = _open(tmp_path, WRAPPED)
        drawn = document.positions["stream.source"]
        document.remove_wires([("stream.source", "stream", "stream", "source")])
        assert document.text == (
            f"wire_wrap: !class:{MOD}.WireWrap\n"
            f"  inner: !class:{MOD}.WireSource {{root: /a}}\n"
            "stream: !class:recordstream.core.stream.Stream\n"
            "  ops: []\n"
            f"holder: !class:{MOD}.WireHolder {{size: 1}}\n"
        )
        assert {"wire_wrap", "wire_wrap.inner", "stream"} <= set(document.model.names())
        assert not [w for w in document.model.wires if w.target == "stream" and w.target_socket == "source"]
        assert document.positions["wire_wrap"] == drawn
        assert confluid.load(document.text)["stream"].source is None

    def test_a_card_whose_class_is_named_like_the_slot_moves_out_with_a_suffix(self, tmp_path: Path) -> None:
        """The CON case of the removal: ``Source`` is ``source``, the very parameter the removal empties — named
        ``source``, confluid would hand the card straight back into the Stream and keep the wire the person removed."""
        document = _open(tmp_path, OWNER)
        document.remove_wires([("stream.source", "stream", "stream", "source")])
        assert document.text.startswith(f"source_2: !class:{MOD}.Source {{}}\nstream: ")
        assert confluid.load(document.text)["stream"].source is None

    def test_a_wire_the_document_does_not_hold_is_refused(self, tmp_path: Path) -> None:
        document = _open(tmp_path, USER_SEED)
        with pytest.raises(ValueError) as refused:
            document.remove_wires([("stream", "stream", "contract", "classes")])
        assert str(refused.value) == "stream.stream → contract.classes is not a wire of this graph"
        assert document.dirty is False

    def test_a_record_wire_is_refused(self, tmp_path: Path) -> None:
        document = _open(tmp_path, STEPS)
        with pytest.raises(ValueError) as refused:
            document.remove_wires([("stream.ops[0]", "record", "stream.ops[1]", "record")])
        assert str(refused.value) == "This wire would change the order of the steps — the editor can't save that yet."

    def test_a_flow_wire_is_refused(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FLOW)
        with pytest.raises(ValueError) as refused:
            document.remove_wires([("decode", "record", "rename", "record")])
        assert str(refused.value) == "The editor can't save wires in this kind of graph yet."

    def test_a_removal_that_makes_another_problem_is_refused(self, tmp_path: Path) -> None:
        """The CON case of the allowance: only the open slot itself may appear; a constructor that then refuses
        to be built is a problem the removal GAINS, and nothing changes."""
        document = _open(tmp_path, NEEDS)
        with pytest.raises(ValueError, match="refused"):
            document.remove_wires([("source", "stream", "needs", "thing")])
        assert document.text == NEEDS and document.dirty is False

    def test_a_refusal_among_several_removes_none(self, tmp_path: Path) -> None:
        document = _open(
            tmp_path, STEPS.replace("    stream: !ref:stream\n", "    stream: !ref:stream\n    input: image\n")
        )
        before = document.text
        with pytest.raises(ValueError):
            document.remove_wires(
                [
                    ("stream.ops[0]", "image", "contract", "input"),
                    ("stream.ops[0]", "record", "stream.ops[1]", "record"),
                ]
            )
        assert document.text == before and document.dirty is False


# ------------------------------------------------------------------------------------------------
# one edit, one undo
# ------------------------------------------------------------------------------------------------


class TestOneEditOneUndo:
    def test_each_wire_is_one_undo_and_one_redo(self, tmp_path: Path) -> None:
        document = _open(tmp_path, USER_SEED)
        document.add_wire("stream.source", "class_names", "contract", "classes")
        document.add_wire("hugging_face_source", "image", "contract", "input")
        document.add_wire("hugging_face_source", "class", "contract", "target")
        assert document.text == USER_SAVED
        assert document.undo() and document.text == USER_TWO
        assert document.undo() and document.text == USER_ONE
        assert document.undo() and document.text == USER_SEED
        assert document.undo() is False
        assert document.redo() and document.redo() and document.redo()
        assert document.text == USER_SAVED

    def test_undoing_a_move_out_puts_the_place_and_the_group_back_under_the_old_name(self, tmp_path: Path) -> None:
        document = _open(tmp_path, WRAPPED)
        group: Group = {"id": "g1", "title": "inputs", "color": "#335", "nodes": ["stream.source"]}
        write_sidecar(document.sidecar_path, {"stream.source": (400.0, 200.0)}, groups=[group])
        document.add_wire("stream.source", "stream", "holder", "thing")
        assert document.undo()
        assert document.text == WRAPPED
        assert document.positions["stream.source"] == (400.0, 200.0)
        assert document.groups[0].get("nodes") == ["stream.source"]
        assert document.redo()
        assert document.positions["wire_wrap"] == (400.0, 200.0)
        assert document.groups[0].get("nodes") == ["wire_wrap"]

    def test_removing_a_contained_card_is_one_undo(self, tmp_path: Path) -> None:
        document = _open(tmp_path, WRAPPED)
        write_sidecar(document.sidecar_path, {"stream.source": (400.0, 200.0)})
        document.remove_wires([("stream.source", "stream", "stream", "source")])
        assert document.positions["wire_wrap"] == (400.0, 200.0)
        assert document.undo() and document.text == WRAPPED
        assert document.positions["stream.source"] == (400.0, 200.0)
        assert document.redo() and document.positions["wire_wrap"] == (400.0, 200.0)


# ------------------------------------------------------------------------------------------------
# a root's optional slots
# ------------------------------------------------------------------------------------------------


class TestAnOptionalSlot:
    """``optional: {classes: "List[str]"}`` — a slot the graph MAY deliver (the host has its own answer when it is left
    unwired): an input of the root without the required mark, after its record entries, saved and removed like an
    output slot."""

    def test_it_is_an_input_without_the_required_mark_after_the_record_entries(self, tmp_path: Path) -> None:
        contract = _open(tmp_path, FILES).model.node("contract")
        assert [(s.name, s.type, s.required, s.entry) for s in contract.inputs] == [
            ("stream", "Stream", True, ""),
            ("input", "Image", False, "input"),
            ("classes", "List[str]", False, ""),
        ]

    def test_left_unwired_it_is_no_problem(self, tmp_path: Path) -> None:
        """The CON case beside a required slot: ``stream`` unwired IS a problem, ``classes`` unwired is not."""
        assert _open(tmp_path, FILES).check() == []
        open_stream = _open(tmp_path, FILES.replace("    stream: !ref:stream\n", ""), "open.yaml")
        assert [p.message.split(" at ")[0] for p in open_stream.check()] == [
            "contract.stream (Stream) is required and nothing is wired into it"
        ]

    def test_a_wire_into_it_is_saved_under_delivered_like_an_output(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FILES_NAMES)
        assert document.add_wire("names", "class_names", "contract", "classes") == "names"
        assert document.text == FILES_NAMES + "    classes: !ref:names\n"
        assert ("names", "class_names", "contract", "classes") in _wires(document.model, "delivered")
        assert document.check() == []

    def test_removing_it_drops_its_pair_and_leaves_no_problem(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FILES_NAMES + "    classes: !ref:names\n")
        document.remove_wires([("names", "class_names", "contract", "classes")])
        assert document.text == FILES_NAMES
        assert document.check() == []

    def test_a_typed_list_fills_it(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FILES + "    classes: [cat, dog]\n")
        classes = document.model.node("contract").params["classes"]
        assert (classes.type, classes.value, classes.doc) == ("List[str]", ["cat", "dog"], DELIVERED_LITERALLY)
        assert document.check() == []


# ------------------------------------------------------------------------------------------------
# the host card and its dashed wire — the files graph a host workspace seeds
# ------------------------------------------------------------------------------------------------

#: The line every removal of a host wire is refused with.
HOST_WIRE_REFUSED = "The host fills this when it runs the graph — it isn't a wire you can remove."


class TestTheFilesGraph:
    """The files graph a host workspace seeds, opened in the editor: the host card ``files`` —dashed→
    ``file_source.files``; ``file_source.stream → stream.source``; the read-image card's ``op → stream.ops[0]``;
    ``stream.stream → contract.stream``; ``stream.ops[0].image → contract.input``; ``contract.classes`` optional and
    unwired — and no problem."""

    def test_it_draws_the_host_card_the_dashed_wire_and_the_delivered_wires(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FILES)
        model = document.model
        assert [(node.name, node.kind) for node in model.nodes] == [
            ("file_source", "source"),
            ("stream", "engine"),
            ("stream.ops[0]", "op"),
            ("contract", "contract"),
            ("<host>", "host"),
        ]
        assert [(w.kind, w.source, w.source_socket, w.target, w.target_socket) for w in model.wires] == [
            ("list", "stream.ops[0]", "op", "stream", "ops[0]"),
            ("ref", "file_source", "stream", "stream", "source"),
            ("delivered", "stream", "stream", "contract", "stream"),
            ("delivered", "stream.ops[0]", "image", "contract", "input"),
            ("host", "<host>", "files", "file_source", "files"),
        ]
        assert document.check() == []

    def test_the_host_card_carries_the_contracts_inputs_and_the_root_none(self, tmp_path: Path) -> None:
        model = _open(tmp_path, FILES).model
        host = model.node("<host>")
        assert (host.class_path, host.loc, host.params, host.inputs) == ("", "", {}, [])
        assert [(s.name, s.type, s.entry) for s in host.outputs] == [("files", "List[str]", "")]
        assert model.node("contract").outputs == []

    def test_without_inputs_there_is_no_host_card(self, tmp_path: Path) -> None:
        """The CON case: a root that declares no ``inputs`` takes nothing from its host — no card, no host wire."""
        model = _open(tmp_path, FILES.replace('  inputs: {files: "List[str]"}\n', "")).model
        assert "<host>" not in model.names()
        assert _wires(model, "host") == []

    def test_read_images_image_is_read_off_the_built_op(self, tmp_path: Path) -> None:
        """``ReadImage.produces`` is a property that follows ``output`` — readable on the built op only, never on the
        class: the card shows ``image · Image``, and with ``output: picture`` it shows ``picture`` instead."""
        model = _open(tmp_path, FILES).model
        assert [(s.name, s.type, s.entry) for s in model.node("stream.ops[0]").outputs] == [
            ("record", "Record", ""),
            ("op", "Op", ""),
            ("image", "Image", "image"),
        ]
        renamed = _open(tmp_path, FILES.replace("output: image}", "output: picture}"), "renamed.yaml").model
        assert [s.name for s in renamed.node("stream.ops[0]").outputs if s.entry] == ["picture"]

    def test_drawing_read_images_image_into_input_saves_input_image(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FILES.replace("    input: image\n", ""))
        assert document.add_wire("stream.ops[0]", "image", "contract", "input") == "stream.ops[0]"
        assert document.text == FILES
        assert ("stream.ops[0]", "image", "contract", "input") in _wires(document.model, "delivered")
        assert document.check() == []


class TestTheHostWire:
    """Where the host puts what it hands in: the parameter of that name on EXACTLY ONE card of the delivered stream's
    source chain — the stream, the card in its ``source`` slot, that card's own ``source`` … — the rule a host fills a
    graph's ``files`` by. Derived from the document's shape, never written, never removable."""

    def test_one_card_of_the_chain_takes_it_one_wire(self, tmp_path: Path) -> None:
        model = _open(tmp_path, FILES).model
        assert _wires(model, "host") == [("<host>", "files", "file_source", "files")]
        assert [w.type for w in model.wires if w.kind == "host"] == ["List[str]"]

    def test_the_chain_is_followed_through_a_card_between(self, tmp_path: Path) -> None:
        text = FILES.replace(
            "file_source: !class:recordstream.sources.files.FilesSource {}\n",
            f"file_source: !class:{MOD}.WireLimit\n  source: !class:recordstream.sources.files.FilesSource {{}}\n",
        )
        model = _open(tmp_path, text).model
        assert _wires(model, "host") == [("<host>", "files", "file_source.source", "files")]

    def test_two_cards_of_the_chain_take_it_no_wire(self, tmp_path: Path) -> None:
        text = FILES.replace(
            "file_source: !class:recordstream.sources.files.FilesSource {}\n",
            f"file_source: !class:{MOD}.WireListed\n  source: !class:recordstream.sources.files.FilesSource {{}}\n",
        )
        document = _open(tmp_path, text)
        assert _wires(document.model, "host") == []
        assert [s.name for s in document.model.node("<host>").outputs] == ["files"]  # the card is still drawn

    def test_no_card_of_the_chain_takes_it_no_wire(self, tmp_path: Path) -> None:
        text = FILES.replace("recordstream.sources.files.FilesSource {}", f"{MOD}.WireSource {{root: /a}}")
        document = _open(tmp_path, text)
        assert _wires(document.model, "host") == []
        assert document.check() == []

    def test_removing_it_is_refused_in_one_plain_line(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FILES)
        with pytest.raises(ValueError) as refused:
            document.remove_wires([("<host>", "files", "file_source", "files")])
        assert str(refused.value) == HOST_WIRE_REFUSED
        assert document.text == FILES and document.dirty is False

    def test_a_host_wire_among_several_removes_none(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FILES)
        with pytest.raises(ValueError) as refused:
            document.remove_wires(
                [("stream.ops[0]", "image", "contract", "input"), ("<host>", "files", "file_source", "files")]
            )
        assert str(refused.value) == HOST_WIRE_REFUSED
        assert document.text == FILES and document.dirty is False

    def test_a_wire_drawn_from_the_host_card_is_refused(self, tmp_path: Path) -> None:
        assert _refused(_open(tmp_path, FILES), ("<host>", "files", "stream", "class_names")) == (
            "The editor can't save this wire yet."
        )

    def test_the_host_card_is_no_node_of_the_document(self, tmp_path: Path) -> None:
        document = _open(tmp_path, FILES)
        for gesture in (lambda: document.rename_node("<host>", "files"), lambda: document.remove_steps(["<host>"])):
            with pytest.raises(ValueError) as refused:
                gesture()
            assert str(refused.value) == "<host> is the host's side of the graph, not a node of the document"
        assert document.text == FILES and document.dirty is False


# ------------------------------------------------------------------------------------------------
# the user's case, end to end
# ------------------------------------------------------------------------------------------------


class TestTheUsersSourceGraph:
    def test_three_wires_and_save_write_what_the_workspace_reads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write(tmp_path, USER_SEED, "source.yaml")
        document = Document.open(path)
        assert document.add_wire("stream.source", "class_names", "contract", "classes") == "hugging_face_source"
        assert document.add_wire("hugging_face_source", "image", "contract", "input") == "hugging_face_source"
        assert document.add_wire("hugging_face_source", "class", "contract", "target") == "hugging_face_source"
        assert document.check() == []
        assert document.save() == [path.resolve()]
        assert path.read_text() == USER_SAVED
        assert path.read_text() == (
            "hugging_face_source: !class:recordstream.sources.huggingface.HuggingFaceSource {path: ylecun/mnist}\n"
            "stream: !class:recordstream.core.stream.Stream\n"
            "  source: !ref:hugging_face_source\n"
            "  ops: []\n"
            "# The graph's ROOT — what this graph must deliver, declared by the workspace (its `graphs:`).\n"
            "# Wire what each output needs; the workspace applies the graph through this root's own check.\n"
            "contract: !class:recordstream.ops.contract.GraphContract\n"
            '  name: "classification source"\n'
            '  outputs: {"stream": "Stream", "classes": "List[str]"}\n'
            '  records: {"input": "Image", "target": "Label"}\n'
            "  delivered:\n"
            "    stream: !ref:stream\n"
            "    classes: !ref:hugging_face_source\n"
            "    input: image\n"
            "    target: class\n"
        )

        reopened = Document.open(path)
        assert _wires(reopened.model, "delivered") == [
            ("stream", "stream", "contract", "stream"),
            ("hugging_face_source", "class_names", "contract", "classes"),
            ("hugging_face_source", "image", "contract", "input"),
            ("hugging_face_source", "class", "contract", "target"),
        ]
        # MNIST's ten class names, answered without the network: the HuggingFace lookup is replaced for the load
        names = [str(digit) for digit in range(10)]
        monkeypatch.setattr(HuggingFaceSource, "class_names", property(lambda self: names))
        contract = confluid.load(path)["contract"]
        assert isinstance(contract, GraphContract)
        contract.check()
        assert contract.wired_entries() == {"input": "image", "target": "class"}
        assert contract.class_names == names

    def test_the_saved_text_names_every_wire_in_the_documents_own_spelling(self) -> None:
        """What a reader of the file sees — the three wires as ``!ref:`` and plain words, nothing else."""
        delivered: Dict[str, str] = {
            line.strip().split(": ")[0]: line.strip().split(": ")[1]
            for line in USER_SAVED.split("  delivered:\n")[1].splitlines()
        }
        assert delivered == {
            "stream": "!ref:stream",
            "classes": "!ref:hugging_face_source",
            "input": "image",
            "target": "class",
        }
