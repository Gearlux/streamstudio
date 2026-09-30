"""The confluid document is the only truth: a canvas MODEL is derived from it, and every edit
patches the line a marker came from — never a re-dump.

The scope (``docs/rewrite.md`` §2.1, §5) measured what a re-dump costs: the shipped Bluetooth
pane, 275 lines with 148 of them comments, comes back as 179 lines with 0 comments and its
``$DATA_ROOT`` baked in. So ``Document`` reads text, derives nodes/wires/problems from
``confluid.load(text, until="document")``, and writes back by patching the located line.

Every test on a shipped waivefront document is gated on ``pytest.importorskip("waivefront")``;
everything else runs on synthetic documents built from recordstream's own ops and the small
classes defined in this module (spelled ``!class:<this module>.<Class>``).
"""

import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import pytest
import yaml
from confluid import configurable
from recordstream import Algorithm, Image, Input, Label, Mask, Output, Param, Record, register_item

import streamstudio
from streamstudio.document import Document, Model, Problem, _gained, _load_problem
from streamstudio.layout import Group, read_groups, read_sidecar, write_sidecar

MOD = __name__


# ------------------------------------------------------------------------------------------------
# synthetic ops — the documents below spell them as !class:<MOD>.<Class>
# ------------------------------------------------------------------------------------------------


@configurable(category="op", group="doc-test")
class DocGain:
    """Scale the image entry — a plain op declaring its interface by NAME.

    Args:
        gain: The multiplier.
        requires: The flag that gates this step (blank = always runs).
        offset: Added after the multiplication.
    """

    consumes = {"image": "Image"}
    produces = {"image": "Image"}

    def __init__(self, gain: float = 1.0, requires: str = "", offset: float = 0.0) -> None:
        self.gain = gain
        self.requires = requires
        self.offset = offset

    def __call__(self, record: Record) -> Record:
        return {**record, "image": record["image"] * self.gain + self.offset}


@configurable(category="op", group="doc-test")
class DocDecide:
    """A TEST node: raises one of two outcomes and reports what it found.

    Args:
        level: The mean above which the image counts as bright.
        requires: The flag that gates this step.
        margin: A tolerance around the level.
    """

    consumes = {"image": "Image"}
    produces: Dict[str, str] = {}
    flags = ("bright", "dark")
    reports = "decide"

    def __init__(self, level: float = 0.5, requires: str = "", margin: float = 0.0) -> None:
        self.level = level
        self.requires = requires
        self.margin = margin

    def __call__(self, record: Record) -> Record:
        return record


@configurable(category="op", group="doc-test")
class DocMode:
    """An op with a closed Literal — the refused-value case.

    Args:
        mode: fast or slow.
        requires: The flag that gates this step.
    """

    consumes = {"image": "Image"}
    produces = {"image": "Image"}

    def __init__(self, mode: Literal["fast", "slow"] = "fast", requires: str = "") -> None:
        self.mode = mode
        self.requires = requires

    def __call__(self, record: Record) -> Record:
        return record


@configurable(category="op", group="doc-test")
class DocNeedsSpectrogram:
    """Consumes an entry no synthetic op produces — the add_step refusal.

    Args:
        percentile: The percentile rank.
    """

    consumes = {"spectrogram": "*"}
    produces = {"floor": "*"}

    def __init__(self, percentile: float = 25.0) -> None:
        self.percentile = percentile

    def __call__(self, record: Record) -> Record:
        return record


@configurable(category="op", group="doc-test")
class DocBackground(Algorithm):
    """The background level of the image."""

    percentile: float = Param(default=25.0, doc="Percentile rank across the per-row medians.")
    image: Image = Input(doc="The image to read.")
    background: float = Output(doc="The background level.")

    def compute(self) -> Dict[str, Any]:
        return {"background": 1.0}


@configurable(category="op", group="doc-test")
class DocMaskFrom(Algorithm):
    """Cells above a threshold, as a mask."""

    threshold: float = Param(default=0.5, doc="Cells above it are set.")
    image: Image = Input(doc="The image to threshold.")
    mask: Mask = Output(doc="True where the cell is above the threshold.")

    def compute(self) -> Dict[str, Any]:
        return {"mask": Mask(self.image > self.threshold)}


@configurable(category="op", group="doc-test")
class DocDecodeImage(Algorithm):
    """Produce an image (a stand-in decoder)."""

    image: Image = Output(doc="The decoded image.")

    def compute(self) -> Dict[str, Any]:
        return {"image": Image([[0]])}


@configurable(category="op", group="doc-test")
class DocCountLabel(Algorithm):
    """Consume a label (a stand-in counter)."""

    label: Label = Input(doc="The label to count.")
    count: int = Output(doc="How many.")

    def compute(self) -> Dict[str, Any]:
        return {"count": 1}


@register_item
class DocBigImage(Image):
    """A registered SUBCLASS of Image — an output of this type meets an Image input."""


@configurable(category="op", group="doc-test")
class DocDecodeBig(Algorithm):
    """Produce a DocBigImage under the entry ``image``."""

    image: DocBigImage = Output(doc="The decoded big image.")

    def compute(self) -> Dict[str, Any]:
        return {"image": DocBigImage([[0]])}


@configurable(category="source", group="doc-test")
class DocSource:
    """A source that yields nothing — its ``root`` keeps a ``$VAR``.

    Args:
        root: Where the files are.
    """

    def __init__(self, root: str = "") -> None:
        self.root = root

    def __iter__(self) -> Any:
        return iter(())


class DocRefuse:
    """V1 of the CON case: three parameters, no ``note``.

    Args:
        max_band_hz: The widest band.
        max_duration_s: The longest window.
        hint: What the signal occupies, in words.
    """

    def __init__(self, max_band_hz: float = 0.0, max_duration_s: float = 0.0, hint: str = "") -> None:
        self.max_band_hz = max_band_hz
        self.max_duration_s = max_duration_s
        self.hint = hint

    def __call__(self, record: Record) -> Record:
        return record


class DocRefuseV2:
    """V2 of the CON case: ``note`` added as the FIRST parameter.

    Args:
        note: Free text shown in the refusal.
        max_band_hz: The widest band.
        max_duration_s: The longest window.
        hint: What the signal occupies, in words.
    """

    def __init__(self, note: str = "", max_band_hz: float = 0.0, max_duration_s: float = 0.0, hint: str = "") -> None:
        self.note = note
        self.max_band_hz = max_band_hz
        self.max_duration_s = max_duration_s
        self.hint = hint

    def __call__(self, record: Record) -> Record:
        return record


# ------------------------------------------------------------------------------------------------
# documents
# ------------------------------------------------------------------------------------------------

# A flow WITHOUT a contract: what its head consumes comes from the host, drawn as ONE node '<host>'.
# Ten comment lines, a $VAR the resolver leaves alone, block AND one-line flow forms.
FLOW_TEXT = f"""\
# 0 ten comment lines in all
# 1 the graph
# 2 a comment
data: !class:{MOD}.DocSource {{root: $DATA_ROOT/pets}}
flow:
  gain:
    # 3 the first step reads the host's image
    op: !class:{MOD}.DocGain {{gain: 2.0}}
  floor:
    # 4 the background level
    op: !class:{MOD}.DocBackground
      percentile: 30.0   # 5 a trailing comment survives too
    from: gain
  decide:
    # 6 the decision
    op: !class:{MOD}.DocDecide
      level: 0.5
    from: floor
  bright:
    # 7 one branch
    op: !class:{MOD}.DocGain
      gain: 0.5
      requires: bright
    from: decide
  mask:
    # 8 reads the floor's output through a bind
    op: !class:{MOD}.DocMaskFrom {{}}
    from: bright
    bind: {{threshold: floor.background}}
  # 9 the output
outputs: mask
# 10 the end
"""

# The same flow rooted in a contract: the host's record is declared, so the chain can be REFUSED by name.
# The root is APPENDED after the flow, the shape that builds: placed before it, the flow's own top-level
# ``outputs: mask`` line is a GraphContract parameter too and confluid gives it to the contract (the
# document is resolved in order, last spec wins) — CONTRACT_FIRST_TEXT below is that shape.
_CONTRACT_ROOT = """\
contract: !class:recordstream.ops.contract.GraphContract
  name: doc view
  inputs: {image: Image}
  outputs: {}
"""
CONTRACT_FLOW_TEXT = (
    f"""\
flow:
  gain:
    op: !class:{MOD}.DocGain {{gain: 2.0}}
  floor:
    op: !class:{MOD}.DocBackground
      percentile: 30.0
    from: gain
  decide:
    op: !class:{MOD}.DocDecide
      level: 0.5
    from: floor
  bright:
    op: !class:{MOD}.DocGain
      gain: 0.5
      requires: bright
    from: decide
  mask:
    op: !class:{MOD}.DocMaskFrom {{}}
    from: bright
    bind: {{threshold: floor.background}}
outputs: mask
"""
    + _CONTRACT_ROOT
)

# The shape that does NOT build (measured 2026-09-28 on the shipped Bluetooth pane).
CONTRACT_FIRST_TEXT = _CONTRACT_ROOT + CONTRACT_FLOW_TEXT[: -len(_CONTRACT_ROOT)]

# A contract-rooted SOURCE graph in the twins' shape: the root keyed `contract`, never after the slot.
SOURCE_TEXT = f"""\
stream: !class:recordstream.core.stream.Stream
  source: !class:{MOD}.DocSource {{root: $DATA_ROOT/pets}}
  ops:
    - !class:recordstream.ops.numpy.Threshold {{low_level: 0.5}}
    - !class:recordstream.ops.numpy.ConnectedComponents {{}}
names: !class:recordstream.ops.contract.ClassNamesOutput {{names: [cat, dog]}}
contract: !class:recordstream.ops.contract.GraphContract
  name: doc source
  inputs: {{files: "List[str]"}}
  outputs: {{stream: Stream, classes: "List[str]"}}
  records: {{input: Image}}
  delivered:
    stream: !ref:stream
    classes: !ref:names
"""

JOINT_TEXT = f"""\
joint: !class:recordstream.core.stream.JointStream
  streams:
    - !class:recordstream.core.stream.Stream
      source: !class:{MOD}.DocSource {{root: /a}}
    - !class:recordstream.core.stream.Stream
      source: !class:{MOD}.DocSource {{root: /b}}
"""

MISMATCH_TEXT = f"""\
flow:
  decode:
    op: !class:{MOD}.DocDecodeImage {{}}
  count:
    op: !class:{MOD}.DocCountLabel {{keys: {{label: image}}}}
    from: decode
"""

SUBCLASS_TEXT = f"""\
flow:
  decode:
    op: !class:{MOD}.DocDecodeBig {{}}
  floor:
    op: !class:{MOD}.DocBackground {{}}
    from: decode
"""


# a flow whose subgraph reads its steps from ``prep.steps.yaml`` — and a steps file whose brace on line 2 never closes,
# so PyYAML stops at line 3, column 7 (the ``:`` after ``scaled``)
INCLUDING_TEXT = """\
flow:
  prep:
    op: !class:recordstream.flow.subgraph.Subgraph
      steps:
        include: prep.steps.yaml
      result: scaled
outputs: prep
"""

UNCLOSED_STEPS_TEXT = """\
grey:
  op: !class:recordstream.ops.image.ConvertMode {mode: L, oops
scaled:
  op: !class:recordstream.ops.numpy.Scale {source_min: 0.0, source_max: 255.0}
"""


_GROUP: Group = {"id": "g1", "title": "front end", "color": "#335", "nodes": ["gain", "floor"]}


def _write(tmp_path: Path, text: str, name: str = "graph.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def _open(tmp_path: Path, text: str, name: str = "graph.yaml") -> Document:
    return Document.open(_write(tmp_path, text, name))


def _wires(model: Model, kind: Optional[str] = None) -> List[Tuple[str, str, str, str]]:
    return [(w.source, w.source_socket, w.target, w.target_socket) for w in model.wires if kind in (None, w.kind)]


def _comment_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.lstrip().startswith("#"))


def _line_of(text: str, needle: str) -> int:
    """1-based line number of the first line containing ``needle``."""
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return number
    raise AssertionError(f"{needle!r} not in text")


def _bte_path() -> Path:
    waivefront = pytest.importorskip("waivefront")
    return Path(waivefront.__file__).parent / "annotation" / "bte_view.yaml"


@pytest.fixture
def fake_layout(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """A stand-in ``streamstudio.layout`` with the exact API the parallel agent ships.

    The document imports it lazily, so these tests never depend on the real module existing;
    the sidecar is a plain YAML ``{name: [x, y]}`` file with no ``groups:`` block (the real
    module's groups are pinned in ``test_layout.py`` and through the routes in ``test_app.py``).
    """
    mod = types.ModuleType("streamstudio.layout")

    def layered_layout(model: Model) -> Dict[str, Tuple[float, float]]:
        return {node.name: (float(index * 100), 0.0) for index, node in enumerate(model.nodes)}

    def read_sidecar(path: Path) -> Dict[str, Tuple[float, float]]:
        if not path.exists():
            return {}
        loaded = yaml.safe_load(path.read_text()) or {}
        return {str(k): (float(v[0]), float(v[1])) for k, v in loaded.items()}

    def write_sidecar(
        path: Path, positions: Dict[str, Tuple[float, float]], groups: Optional[List[Any]] = None
    ) -> None:
        assert not groups, "the stand-in's sidecar holds positions only"
        path.write_text(yaml.safe_dump({k: [x, y] for k, (x, y) in positions.items()}, sort_keys=False))

    def read_groups(path: Path) -> List[Any]:  # the stand-in's sidecar has no groups block
        return []

    def rename_member(groups: List[Any], old: str, new: str) -> List[Any]:
        return list(groups)

    def rename_key(name: str, old: str, new: str) -> str:  # the real rule: the node, and every name under it
        return new + name[len(old) :] if name == old or name.startswith(f"{old}/") else name

    def positions(
        model: Model, sidecar: Path, *, kept: Optional[Dict[str, Tuple[float, float]]] = None
    ) -> Dict[str, Tuple[float, float]]:
        kept = read_sidecar(sidecar) if kept is None else dict(kept)
        laid = layered_layout(model)
        return {node.name: kept.get(node.name, laid[node.name]) for node in model.nodes}

    mod.layered_layout = layered_layout  # type: ignore[attr-defined]
    mod.read_sidecar = read_sidecar  # type: ignore[attr-defined]
    mod.write_sidecar = write_sidecar  # type: ignore[attr-defined]
    mod.positions = positions  # type: ignore[attr-defined]
    mod.read_groups = read_groups  # type: ignore[attr-defined]
    mod.rename_member = rename_member  # type: ignore[attr-defined]
    mod.rename_key = rename_key  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "streamstudio.layout", mod)
    monkeypatch.setattr(streamstudio, "layout", mod, raising=False)
    return mod


# ------------------------------------------------------------------------------------------------
# derive
# ------------------------------------------------------------------------------------------------


class TestDerive:
    def test_open_reads_the_text_and_parses_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = _write(tmp_path, "flow: [not a mapping")
        doc = Document.open(path)  # a broken document opens; the problem is reported by the model
        assert doc.path == path
        assert doc.text == "flow: [not a mapping"

    def test_a_stream_document_names_its_members_by_path_and_position(self, tmp_path: Path) -> None:
        model = _open(tmp_path, SOURCE_TEXT).model
        names = [node.name for node in model.nodes]
        assert names == ["stream", "stream.source", "stream.ops[0]", "stream.ops[1]", "names", "contract"]
        stream = model.node("stream")
        assert stream.kind == "engine"
        assert [(s.name, s.type) for s in stream.inputs] == [("source", "Source"), ("ops[0]", "Op"), ("ops[1]", "Op")]
        assert [(s.name, s.type) for s in stream.outputs] == [("stream", "Stream")]
        assert model.node("stream.source").kind == "source"
        assert model.node("stream.ops[0]").kind == "op"

    def test_a_stream_is_drawn_as_a_record_chain_between_its_ops_plus_the_list_wires(self, tmp_path: Path) -> None:
        model = _open(tmp_path, SOURCE_TEXT).model
        assert _wires(model, "record") == [("stream.ops[0]", "record", "stream.ops[1]", "record")]
        assert _wires(model, "list") == [
            ("stream.ops[0]", "op", "stream", "ops[0]"),
            ("stream.ops[1]", "op", "stream", "ops[1]"),
        ]
        assert _wires(model, "ref") == [("stream.source", "stream", "stream", "source")]
        record_wire = next(w for w in model.wires if w.kind == "record")
        assert record_wire.type == "Record"

    def test_a_flow_document_gives_record_bind_gate_and_entry_wires(self, tmp_path: Path) -> None:
        model = _open(tmp_path, FLOW_TEXT).model
        assert [node.name for node in model.nodes] == ["data", "gain", "floor", "decide", "bright", "mask", "<host>"]
        assert _wires(model, "record") == [
            ("gain", "record", "floor", "record"),
            ("floor", "record", "decide", "record"),
            ("decide", "record", "bright", "record"),
            ("bright", "record", "mask", "record"),
        ]
        assert _wires(model, "bind") == [("floor", "background", "mask", "threshold")]
        assert _wires(model, "gate") == [("decide", "bright", "bright", "record")]
        assert _wires(model, "entry") == [
            ("<host>", "image", "gain", "image"),
            ("gain", "image", "floor", "image"),
            ("gain", "image", "decide", "image"),
            ("gain", "image", "bright", "image"),
            ("bright", "image", "mask", "image"),
        ]
        assert model.problems == []

    def test_a_contract_less_graph_has_one_host_node_for_what_the_host_provides(self, tmp_path: Path) -> None:
        model = _open(tmp_path, FLOW_TEXT).model
        host = model.node("<host>")
        assert host.kind == "host"
        assert [(s.name, s.type) for s in host.outputs] == [("image", "Image")]
        assert host.inputs == []

    def test_a_contract_root_has_an_input_per_output_slot_and_the_delivered_wires(self, tmp_path: Path) -> None:
        model = _open(tmp_path, SOURCE_TEXT).model
        contract = model.node("contract")
        assert contract.kind == "contract"
        assert [(s.name, s.type, s.required) for s in contract.inputs] == [
            ("stream", "Stream", True),
            ("classes", "List[str]", True),
        ]
        assert [(s.name, s.type) for s in contract.outputs] == [("files", "List[str]")]
        assert _wires(model, "delivered") == [
            ("stream", "stream", "contract", "stream"),
            ("names", "class_names", "contract", "classes"),
        ]
        assert model.problems == []

    def test_a_value_producer_has_its_outputs_plus_an_object_output(self, tmp_path: Path) -> None:
        names = _open(tmp_path, SOURCE_TEXT).model.node("names")
        assert names.kind == "value"
        assert [(s.name, s.type) for s in names.outputs] == [("class_names", "List[str]"), ("object", "object")]
        assert names.params["names"].value == ["cat", "dog"]

    def test_an_algorithm_gets_entry_sockets_from_its_input_and_output_declarations(self, tmp_path: Path) -> None:
        floor = _open(tmp_path, FLOW_TEXT).model.node("floor")
        assert floor.kind == "algorithm"
        assert floor.class_path == f"{MOD}.DocBackground"
        assert [(s.name, s.type, s.required) for s in floor.inputs] == [
            ("record", "Record", False),
            ("image", "Image", True),
        ]
        assert [(s.name, s.type) for s in floor.outputs] == [
            ("record", "Record"),
            ("op", "Op"),
            ("background", "number"),
        ]
        assert set(floor.params) == {"percentile", "keys"}
        assert floor.params["percentile"].value == 30.0
        assert floor.params["percentile"].default == 25.0
        assert floor.params["percentile"].type == "number"
        assert floor.params["percentile"].doc == "Percentile rank across the per-row medians."
        assert floor.params["percentile"].schema["type"] == "number"

    def test_a_named_form_op_gets_entry_sockets_outcomes_and_a_report_output(self, tmp_path: Path) -> None:
        decide = _open(tmp_path, FLOW_TEXT).model.node("decide")
        assert decide.kind == "op"
        assert [(s.name, s.type) for s in decide.inputs] == [("record", "Record"), ("image", "Image")]
        assert [(s.name, s.type) for s in decide.outputs] == [
            ("record", "Record"),
            ("op", "Op"),
            ("bright", "Record"),
            ("dark", "Record"),
            ("report:decide", "string"),
        ]
        assert decide.params["level"].value == 0.5
        assert decide.params["requires"].value == ""  # left at its default
        assert decide.params["requires"].spelled is False
        assert decide.params["level"].spelled is True

    def test_a_tuple_form_transform_is_types_only_with_a_note_and_no_entry_sockets(self, tmp_path: Path) -> None:
        threshold = _open(tmp_path, SOURCE_TEXT).model.node("stream.ops[0]")
        assert threshold.class_path == "recordstream.ops.numpy.Threshold"
        assert [s.name for s in threshold.inputs] == ["record"]
        assert [s.name for s in threshold.outputs] == ["record", "op"]
        assert "Threshold declares the TYPES it consumes and produces" in threshold.note
        assert "NDArrayItem" in threshold.note and "Mask" in threshold.note

    def test_the_location_and_source_file_of_every_node_point_at_the_marker(self, tmp_path: Path) -> None:
        path = _write(tmp_path, FLOW_TEXT)
        model = Document.open(path).model
        floor = model.node("floor")
        line = _line_of(FLOW_TEXT, f"op: !class:{MOD}.DocBackground")
        assert floor.loc == f"{path}:{line}:9"
        assert floor.source_file == str(path)
        assert model.node("<host>").loc == ""

    def test_a_dollar_variable_the_environment_does_not_set_is_kept_verbatim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DATA_ROOT", raising=False)
        data = _open(tmp_path, FLOW_TEXT).model.node("data")
        assert data.kind == "source"
        assert data.params["root"].value == "$DATA_ROOT/pets"
        assert [(s.name, s.type) for s in data.outputs] == [("stream", "Source")]

    def test_model_node_names_the_known_nodes_on_a_miss(self, tmp_path: Path) -> None:
        model = _open(tmp_path, MISMATCH_TEXT).model
        with pytest.raises(KeyError, match="decode.*count"):
            model.node("nope")

    def test_the_bluetooth_pane_derives_25_nodes_and_49_wires(self) -> None:
        """Measured 2026-09-27 on the shipped ``bte_view.yaml``: 24 steps, 23 ``from:`` record wires,
        9 gate wires (one per ``requires:``), 17 entry wires (who produced what a node reads), and no
        host wire — the head node (``WindowSignal``) declares nothing. Measured again 2026-09-28, the
        day the pane gained its ``contract:`` root: 25 nodes — the 24 steps plus the root, whose
        outputs are the host's two entries — and the same 49 wires (no step reads ``input`` or
        ``regions`` by name, so the root wires to nothing and no ``<host>`` node is drawn).

        This pins the waivefront file's declarations from here; a change there shows up here first.
        """
        path = _bte_path()
        model = Document.open(path).model
        assert len(model.nodes) == 25
        assert len(model.wires) == 49
        kinds: Dict[str, int] = {}
        for wire in model.wires:
            kinds[wire.kind] = kinds.get(wire.kind, 0) + 1
        assert kinds == {"record": 23, "gate": 9, "entry": 17}
        assert model.problems == []
        gates = {(w.source, w.source_socket, w.target) for w in model.wires if w.kind == "gate"}
        assert ("pick", "plain", "burst") in gates
        assert ("classify", "ble", "ble") in gates
        root = model.node("contract")
        assert root.kind == "contract"
        assert [(s.name, s.type) for s in root.outputs] == [("input", "Signal"), ("regions", "Regions")]
        assert not any(node.kind == "host" for node in model.nodes)

    def test_a_step_whose_entry_nothing_produces_is_refused_on_the_shipped_bluetooth_pane(self, tmp_path: Path) -> None:
        """The PRO case the pane's ``contract:`` root exists for (2026-09-28): ``NoiseFloorEstimate``
        reads ``spectrogram``, which no step of the Bluetooth pane writes and the host does not provide
        (its inputs are ``input`` and ``regions``), so inserting it is REFUSED with recordstream's
        located message and the text is left as it was. The CON case beside it: the same pane with the
        root cut off is fed by a synthetic ``<host>`` that supplies anything, so the same insert is
        ACCEPTED and the host grows a ``spectrogram`` output — measured 2026-09-27, before the root.
        """
        source = _bte_path().read_text()
        rooted = tmp_path / "bte_view.yaml"
        rooted.write_text(source)
        doc = Document.open(rooted)
        step: Dict[str, Any] = dict(
            name="floor_db",
            after="floor",
            class_path="waivefront.processing.NoiseFloorEstimate",
            params={"percentile": 30},
        )
        with pytest.raises(ValueError) as refusal:
            doc.add_step(**step)
        message = str(refusal.value)
        anchor = f"{rooted}:{_line_of(source, 'MeasureNoiseFloor')}:9"
        assert message.startswith(f"add_step 'floor_db' after 'floor' refused at {anchor}: {rooted}:floor_db: ")
        assert (
            "NoiseFloorEstimate needs the record entry 'spectrogram', which nothing before it produces — "
            "the chain has input, regions at that point" in message
        )
        assert doc.text == source
        assert not any(node.kind == "host" for node in doc.model.nodes)

        mark = "# The graph's ROOT"
        assert mark in source, "the shipped pane carries its contract block (appended after its last entry)"
        unrooted = tmp_path / "bte_view_without_its_root.yaml"
        unrooted.write_text(source[: source.index(mark)])
        doc = Document.open(unrooted)
        doc.add_step(**step)
        host = doc.model.node("<host>")
        assert [(s.name, s.type) for s in host.outputs] == [("spectrogram", "Spectrogram")]
        assert doc.check() == []

    def test_the_psd_twin_derives_a_stream_and_two_ops(self) -> None:
        pytest.importorskip("waivefront.processing")  # registers the twin's registry-key classes
        path = _bte_path().with_name("psd_slice_view.yaml")
        model = Document.open(path).model
        # The twin's `contract:` root (2026-09-28) is its last entry, so it derives as the last node.
        assert [node.name for node in model.nodes] == ["stream", "stream.ops[0]", "stream.ops[1]", "contract"]
        assert model.node("stream.ops[1]").class_path.endswith("PsdSlice")
        assert _wires(model, "record") == [("stream.ops[0]", "record", "stream.ops[1]", "record")]
        assert model.problems == []

    def test_the_sink_graph_derives_five_nodes(self) -> None:
        """``save_boxes_to_store.yaml``: a processor, its stream, the stream's two contracts, the sink.

        Five wires, not the four measured yesterday: the scope (§5) draws a Stream's ops as a chain
        of record wires, and yesterday's prototype did not draw that one — the count here includes it.
        """
        pytest.importorskip("waivefront.sinks")
        path = _bte_path().with_name("save_boxes_to_store.yaml")
        model = Document.open(path).model
        assert [node.name for node in model.nodes] == [
            "processor",
            "processor.stream",
            "processor.stream.ops[0]",
            "processor.stream.ops[1]",
            "processor.sink",
        ]
        assert model.node("processor").kind == "runnable"
        assert model.node("processor.sink").kind == "sink"
        assert len(model.wires) == 5
        assert _wires(model, "ref") == [
            ("processor.stream", "stream", "processor", "stream"),
            ("processor.sink", "sink", "processor", "sink"),
        ]
        assert _wires(model, "record") == [("processor.stream.ops[0]", "record", "processor.stream.ops[1]", "record")]


# ------------------------------------------------------------------------------------------------
# problems
# ------------------------------------------------------------------------------------------------


class TestProblems:
    def test_a_dangling_required_input_is_located(self, tmp_path: Path) -> None:
        text = (
            "contract: !class:recordstream.ops.contract.GraphContract\n"
            "  name: doc source\n"
            "  outputs: {stream: Stream}\n"
        )
        path = _write(tmp_path, text)
        problems = Document.open(path).check()
        assert [p.message for p in problems] == [
            f"contract.stream (Stream) is required and nothing is wired into it at {path}:1:11"
        ]
        assert problems[0].loc == f"{path}:1:11"

    def test_a_wire_whose_types_do_not_meet_is_refused_with_the_exact_message(self, tmp_path: Path) -> None:
        path = _write(tmp_path, MISMATCH_TEXT)
        model = Document.open(path).model
        line = _line_of(MISMATCH_TEXT, f"op: !class:{MOD}.DocCountLabel")
        assert [p.message for p in model.problems] == [
            f"cannot wire decode.image (Image) into count.label (Label) at {path}:{line}:9: the types do not meet"
        ]
        assert model.problems[0].loc == f"{path}:{line}:9"
        assert _wires(model, "entry") == []  # the refused wire is not drawn

    def test_an_item_subclass_meets_its_base(self, tmp_path: Path) -> None:
        model = _open(tmp_path, SUBCLASS_TEXT).model
        assert model.problems == []
        assert _wires(model, "entry") == [("decode", "image", "floor", "image")]
        assert model.node("decode").outputs[-1].type == "DocBigImage"

    def test_a_document_that_does_not_load_reports_one_located_problem_instead_of_raising(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "flow:\n  a:\n    op: !class:x.Y {\n")
        doc = Document.open(path)
        model = doc.model
        assert model.nodes == [] and model.wires == []
        assert len(model.problems) == 1
        assert model.problems[0].loc.startswith(f"{path}:")
        assert model.problems[0].message.startswith(f"{path}: does not load")
        assert doc.check() == model.problems

    def test_a_class_that_cannot_be_resolved_is_a_located_problem(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "flow:\n  a:\n    op: !class:nowhere.Nope {}\n")
        model = Document.open(path).model
        assert [node.name for node in model.nodes] == ["a"]
        assert model.node("a").class_path == "nowhere.Nope"
        assert [p.message for p in model.problems] == [f"a: cannot resolve the class 'nowhere.Nope' at {path}:3:9"]

    def test_a_value_the_constructor_refuses_is_a_located_problem(self, tmp_path: Path) -> None:
        path = _write(tmp_path, f"flow:\n  m:\n    op: !class:{MOD}.DocMode {{mode: hybrid}}\n")
        problems = Document.open(path).check()
        assert len(problems) == 1
        assert f"{path}:3:9" in problems[0].message
        assert "'fast' or 'slow'" in problems[0].message

    def test_check_refuses_a_step_whose_entry_nothing_produces_in_recordstreams_words(self, tmp_path: Path) -> None:
        text = CONTRACT_FLOW_TEXT.replace(
            "outputs: mask\n",
            f"  spec:\n    op: !class:{MOD}.DocNeedsSpectrogram {{}}\n    from: mask\noutputs: spec\n",
        )
        path = _write(tmp_path, text)
        problems = Document.open(path).check()
        assert len(problems) == 1
        assert "DocNeedsSpectrogram needs the record entry 'spectrogram', which nothing before it produces" in (
            problems[0].message
        )
        assert problems[0].message.startswith(f"{path}:spec: ")
        assert problems[0].loc == f"{path}:{_line_of(text, 'DocNeedsSpectrogram')}:9"

    def test_a_document_that_derives_but_does_not_build_is_one_located_problem(self, tmp_path: Path) -> None:
        """The contract placed BEFORE the flow derives (the document stage keeps the markers) but does not
        BUILD: the flow's own ``outputs: mask`` is a GraphContract parameter too, so the contract is handed
        a string where a mapping belongs. Measured before the fix: ``check()`` answered ``[]`` for a
        document every consumer refuses to load. CON: the same root appended after the flow builds, and
        ``test_check_never_raises_and_passes_a_sound_graph`` pins that ``check()`` stays ``[]`` for it."""
        path = _write(tmp_path, CONTRACT_FIRST_TEXT)
        problems = Document.open(path).check()
        assert len(problems) == 1
        assert problems[0].message.startswith(f"{path}: does not build: ")
        assert "Failed to construct GraphContract" in problems[0].message
        assert problems[0].loc == f"{path}:1:11"

    def test_a_syntax_error_in_an_included_file_is_located_in_that_file(self, tmp_path: Path) -> None:
        """Measured 2026-09-30: ``loc`` was ``…/graph.yaml:3:7`` — the GRAPH file with the steps file's line
        and column — while the message itself names ``…/prep.steps.yaml``. The message stays as it was."""
        steps = _write(tmp_path, UNCLOSED_STEPS_TEXT, "prep.steps.yaml")
        path = _write(tmp_path, INCLUDING_TEXT)
        problems = Document.open(path).check()
        assert [p.loc for p in problems] == [f"{steps}:3:7"]
        assert problems[0].message.startswith(f"{path}: does not load: ParserError: ")
        assert f'in "{steps}", line 3, column 7' in problems[0].message

    def test_a_syntax_error_in_the_graphs_own_text_is_still_located_in_the_graph(self, tmp_path: Path) -> None:
        """CON case: the same unclosed brace written in the graph itself names the graph, at ITS line."""
        text = "flow:\n" + "".join(f"  {line}\n" for line in UNCLOSED_STEPS_TEXT.splitlines())
        path = _write(tmp_path, text)
        problems = Document.open(path).check()
        assert [p.loc for p in problems] == [f"{path}:4:9"]
        assert problems[0].message.startswith(f"{path}: does not load: ParserError: ")

    def test_a_syntax_error_in_a_held_included_file_is_located_in_that_file(self, tmp_path: Path) -> None:
        """A steps file the editor holds and has NOT saved (it is not on disk at all): confluid parses the held text
        under the file's real path, so the location names that file although the disk does not have it."""
        path = _write(tmp_path, INCLUDING_TEXT)
        steps = tmp_path / "prep.steps.yaml"
        document = Document(path, {path.resolve(): INCLUDING_TEXT, steps.resolve(): UNCLOSED_STEPS_TEXT})
        assert not steps.exists()
        assert [p.loc for p in document.check()] == [f"{steps}:3:7"]

    def test_a_mark_that_names_no_file_is_located_in_the_graph(self, tmp_path: Path) -> None:
        """CON case: a YAML stream without a file (PyYAML names it ``<unicode string>``) falls back to the graph."""
        path = tmp_path / "graph.yaml"
        with pytest.raises(yaml.MarkedYAMLError) as caught:
            yaml.safe_load("a: {b: 1\nc: 2\n")
        assert caught.value.problem_mark is not None and caught.value.problem_mark.name == "<unicode string>"
        assert _load_problem(path, caught.value).loc == f"{path}:2:2"

    def test_check_never_raises_and_passes_a_sound_graph(self, tmp_path: Path) -> None:
        assert _open(tmp_path, CONTRACT_FLOW_TEXT).check() == []
        assert _open(tmp_path, FLOW_TEXT).check() == []
        assert _open(tmp_path, SOURCE_TEXT).check() == []


# ------------------------------------------------------------------------------------------------
# set_param
# ------------------------------------------------------------------------------------------------


class TestSetParam:
    def test_it_patches_one_line_and_keeps_every_comment(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, FLOW_TEXT)
        before = doc.text
        assert _comment_lines(before) == 10
        doc.set_param("floor", "percentile", 40.0)
        after = doc.text
        differing = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
        assert differing == [
            (
                "      percentile: 30.0   # 5 a trailing comment survives too",
                "      percentile: 40.0   # 5 a trailing comment survives too",
            )
        ]
        assert _comment_lines(after) == 10
        assert "$DATA_ROOT/pets" in after
        assert doc.model.node("floor").params["percentile"].value == 40.0
        assert doc.check() == []

    def test_it_patches_a_one_line_flow_mapping(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, FLOW_TEXT)
        doc.set_param("gain", "gain", 3.5)
        assert f"    op: !class:{MOD}.DocGain {{gain: 3.5}}\n" in doc.text
        assert doc.model.node("gain").params["gain"].value == 3.5

    def test_a_param_the_file_left_at_its_default_is_inserted_under_the_node(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, FLOW_TEXT)
        doc.set_param("decide", "margin", 0.1)  # block form: a new line under the marker
        assert f"    op: !class:{MOD}.DocDecide\n      margin: 0.1\n      level: 0.5\n" in doc.text
        doc.set_param("mask", "threshold", 0.7)  # empty one-line mapping: filled in place
        assert f"    op: !class:{MOD}.DocMaskFrom {{threshold: 0.7}}\n" in doc.text
        doc.set_param("gain", "offset", 1.0)  # one-line mapping with a member: appended
        assert f"    op: !class:{MOD}.DocGain {{gain: 2.0, offset: 1.0}}\n" in doc.text
        assert doc.model.node("decide").params["margin"].value == 0.1
        assert doc.model.node("mask").params["threshold"].value == 0.7

    def test_a_string_and_a_container_are_spelled_as_yaml(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, FLOW_TEXT)
        doc.set_param("floor", "keys", {"image": "img"})
        assert "      keys: {image: img}\n" in doc.text
        assert doc.model.node("floor").params["keys"].value == {"image": "img"}
        doc.set_param("data", "root", "$DATA_ROOT/other")
        assert f"data: !class:{MOD}.DocSource {{root: $DATA_ROOT/other}}\n" in doc.text

    def test_a_refused_value_leaves_the_file_unchanged_and_names_the_location(self, tmp_path: Path) -> None:
        text = f"flow:\n  m:\n    op: !class:{MOD}.DocMode\n      mode: fast\n"
        path = _write(tmp_path, text)
        doc = Document.open(path)
        with pytest.raises(ValueError) as refusal:
            doc.set_param("m", "mode", "hybrid")
        assert f"{path}:3:9" in str(refusal.value)
        assert "'fast' or 'slow'" in str(refusal.value)
        assert doc.text == text
        assert path.read_text() == text
        assert doc.undo() is False  # nothing was pushed

    def test_an_unknown_parameter_is_refused_naming_the_parameters(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, FLOW_TEXT)
        with pytest.raises(ValueError, match="gain.*requires.*offset"):
            doc.set_param("gain", "nope", 1)

    def test_a_change_that_breaks_the_chain_is_refused(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, CONTRACT_FLOW_TEXT)
        text = doc.text
        with pytest.raises(ValueError, match="gated on the flag 'sunny', which no node before it raises"):
            doc.set_param("bright", "requires", "sunny")
        assert doc.text == text

    def test_a_new_constructor_parameter_between_save_and_load_shifts_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The CON case of the scope: ``DocRefuse(max_band_hz, max_duration_s, hint)`` is saved, then
        the class gains ``note`` as its FIRST parameter. The document keys its values by NAME, so
        every value stays on its parameter; a positional canvas would have shifted all three.
        """
        text = (
            f"flow:\n  size:\n    op: !class:{MOD}.DocRefuse\n"
            "      max_band_hz: 25000000.0\n      max_duration_s: 0.025\n      hint: a Bluetooth packet\n"
        )
        path = _write(tmp_path, text)
        before = Document.open(path).model.node("size")
        assert list(before.params) == ["max_band_hz", "max_duration_s", "hint"]
        monkeypatch.setattr(sys.modules[MOD], "DocRefuse", DocRefuseV2)
        after = Document.open(path).model.node("size")
        assert list(after.params) == ["note", "max_band_hz", "max_duration_s", "hint"]
        assert {k: p.value for k, p in after.params.items()} == {
            "note": "",
            "max_band_hz": 25000000.0,
            "max_duration_s": 0.025,
            "hint": "a Bluetooth packet",
        }

    def test_the_bluetooth_pane_keeps_its_152_comment_lines(self, tmp_path: Path) -> None:
        """Measured on the shipped file: 275 lines, 148 of them comments — 152 since 2026-09-28, when the
        pane gained its ``contract:`` root under a four-line comment; ``set_param`` on
        ``size.max_band_hz`` changes ONE line and every comment survives."""
        source = _bte_path()
        path = tmp_path / source.name
        path.write_text(source.read_text())
        doc = Document.open(path)
        before = doc.text
        assert _comment_lines(before) == 152
        doc.set_param("size", "max_band_hz", 30.0e6)
        after = doc.text
        assert _comment_lines(after) == 152
        differing = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
        assert differing == [("      max_band_hz: 25.0e6", "      max_band_hz: 30000000.0")]
        assert len(after.splitlines()) == len(before.splitlines())
        assert doc.model.node("size").params["max_band_hz"].value == 30.0e6


# ------------------------------------------------------------------------------------------------
# steps
# ------------------------------------------------------------------------------------------------


class TestSteps:
    def test_add_step_inserts_after_a_node_and_the_next_step_reads_the_new_one(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, CONTRACT_FLOW_TEXT)
        doc.add_step("floor2", after="gain", class_path=f"{MOD}.DocBackground", params={"percentile": 40.0})
        assert (
            f"  gain:\n    op: !class:{MOD}.DocGain {{gain: 2.0}}\n"
            f"  floor2:\n    op: !class:{MOD}.DocBackground {{percentile: 40.0}}\n    from: gain\n"
            f"  floor:\n    op: !class:{MOD}.DocBackground\n      percentile: 30.0\n    from: floor2\n"
        ) in doc.text
        model = doc.model
        assert [n.name for n in model.nodes] == ["contract", "gain", "floor2", "floor", "decide", "bright", "mask"]
        assert ("gain", "record", "floor2", "record") in _wires(model, "record")
        assert ("floor2", "record", "floor", "record") in _wires(model, "record")
        assert doc.check() == []

    def test_add_step_after_the_tail_moves_the_output_to_the_new_step(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, CONTRACT_FLOW_TEXT)
        doc.add_step("tail", after="mask", class_path=f"{MOD}.DocGain", params={})
        assert f"  tail:\n    op: !class:{MOD}.DocGain {{}}\n    from: mask\noutputs: tail\n" in doc.text
        assert doc.model.outputs == ["tail"]

    def test_add_step_is_refused_before_writing_when_its_input_nothing_produces(self, tmp_path: Path) -> None:
        path = _write(tmp_path, CONTRACT_FLOW_TEXT)
        doc = Document.open(path)
        with pytest.raises(ValueError) as refusal:
            doc.add_step("spec", after="gain", class_path=f"{MOD}.DocNeedsSpectrogram", params={"percentile": 30})
        assert "DocNeedsSpectrogram needs the record entry 'spectrogram', which nothing before it produces" in str(
            refusal.value
        )
        assert doc.text == CONTRACT_FLOW_TEXT
        assert path.read_text() == CONTRACT_FLOW_TEXT
        assert doc.undo() is False

    def test_add_step_refuses_a_name_in_use_and_an_unknown_anchor(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, CONTRACT_FLOW_TEXT)
        with pytest.raises(ValueError, match="'floor' is already a node"):
            doc.add_step("floor", after="gain", class_path=f"{MOD}.DocGain", params={})
        with pytest.raises(KeyError, match="nope"):
            doc.add_step("x", after="nope", class_path=f"{MOD}.DocGain", params={})

    def test_remove_step_re_points_its_readers_to_what_it_read_from(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, CONTRACT_FLOW_TEXT)
        doc.remove_step("bright")
        assert "bright:" not in doc.text
        assert f"  mask:\n    op: !class:{MOD}.DocMaskFrom {{}}\n    from: decide\n" in doc.text
        model = doc.model
        assert [n.name for n in model.nodes] == ["contract", "gain", "floor", "decide", "mask"]
        assert ("decide", "record", "mask", "record") in _wires(model, "record")
        assert doc.check() == []

    def test_remove_step_refuses_removing_a_gates_producer(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, CONTRACT_FLOW_TEXT)
        with pytest.raises(ValueError, match="gated on the flag 'bright', which no node before it raises"):
            doc.remove_step("decide")
        assert doc.text == CONTRACT_FLOW_TEXT

    def test_remove_step_refuses_a_step_a_bind_still_reads(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, CONTRACT_FLOW_TEXT)
        with pytest.raises(ValueError, match="floor.background"):
            doc.remove_step("floor")
        assert doc.text == CONTRACT_FLOW_TEXT

    def test_remove_step_of_the_output_step_moves_the_output_back(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, CONTRACT_FLOW_TEXT)
        doc.remove_step("mask")
        assert "    from: decide\noutputs: bright\n" in doc.text  # the root follows the flow
        assert doc.model.outputs == ["bright"]


# ------------------------------------------------------------------------------------------------
# rename
# ------------------------------------------------------------------------------------------------


class TestRename:
    def test_rename_moves_the_key_every_from_and_bind_and_the_sidecar_entry(
        self, tmp_path: Path, fake_layout: types.ModuleType
    ) -> None:
        path = _write(tmp_path, CONTRACT_FLOW_TEXT)
        sidecar = path.with_suffix(".layout.yaml")
        sidecar.write_text("floor: [10, 20]\ngain: [0, 0]\n")
        doc = Document.open(path)
        doc.rename_node("floor", "level")
        assert "  level:\n" in doc.text and "  floor:\n" not in doc.text
        assert "    from: level\n" in doc.text
        assert "    bind: {threshold: level.background}\n" in doc.text
        assert [n.name for n in doc.model.nodes] == ["contract", "gain", "level", "decide", "bright", "mask"]
        assert doc.positions["level"] == (10.0, 20.0)
        assert "floor" not in doc.positions
        assert doc.check() == []
        doc.save()
        assert yaml.safe_load(sidecar.read_text()) == {name: [x, y] for name, (x, y) in doc.positions.items()}
        assert "floor" not in yaml.safe_load(sidecar.read_text())

    def test_rename_moves_every_ref_to_a_top_level_node(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, SOURCE_TEXT)
        doc.rename_node("names", "vocabulary")
        assert "vocabulary: !class:recordstream.ops.contract.ClassNamesOutput" in doc.text
        assert "    classes: !ref:vocabulary\n" in doc.text
        assert "!ref:names" not in doc.text
        assert _wires(doc.model, "delivered") == [
            ("stream", "stream", "contract", "stream"),
            ("vocabulary", "class_names", "contract", "classes"),
        ]
        assert doc.check() == []

    def test_rename_moves_the_output_step(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, CONTRACT_FLOW_TEXT)
        doc.rename_node("mask", "masked")
        assert "\noutputs: masked\n" in doc.text  # the root follows the flow
        assert doc.model.outputs == ["masked"]

    def test_a_name_in_use_is_refused(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, CONTRACT_FLOW_TEXT)
        with pytest.raises(ValueError, match="'gain' is already a node"):
            doc.rename_node("floor", "gain")
        assert doc.text == CONTRACT_FLOW_TEXT

    def test_a_list_member_and_a_nested_slot_are_not_renamable(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, SOURCE_TEXT)
        with pytest.raises(ValueError, match="named by its position"):
            doc.rename_node("stream.ops[0]", "first")
        with pytest.raises(ValueError, match="named by the slot"):
            doc.rename_node("stream.source", "src")


# ------------------------------------------------------------------------------------------------
# history
# ------------------------------------------------------------------------------------------------


class TestHistory:
    def test_undo_and_redo_walk_across_every_edit_path(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, CONTRACT_FLOW_TEXT)
        t0 = doc.text
        doc.set_param("floor", "percentile", 40.0)
        t1 = doc.text
        doc.add_step("floor2", after="gain", class_path=f"{MOD}.DocBackground", params={})
        t2 = doc.text
        doc.rename_node("decide", "test")
        t3 = doc.text
        assert doc.undo() and doc.text == t2
        assert doc.undo() and doc.text == t1
        assert doc.undo() and doc.text == t0
        assert doc.undo() is False
        assert doc.redo() and doc.text == t1
        assert doc.redo() and doc.text == t2
        assert doc.redo() and doc.text == t3
        assert doc.redo() is False
        assert [n.name for n in doc.model.nodes] == ["contract", "gain", "floor2", "floor", "test", "bright", "mask"]

    def test_undoing_a_rename_restores_the_old_names_position_and_group(self, tmp_path: Path) -> None:
        """Measured before the fix: undo restored the TEXT only — the sidecar stayed keyed by the new name,
        so the node was laid out afresh, its saved position was dropped and its group named a member that
        no longer exists. Each history entry now carries the rename it made; undo and redo re-key."""
        doc = _open(tmp_path, CONTRACT_FLOW_TEXT)
        write_sidecar(doc.sidecar_path, {"gain": (500.0, 500.0)}, groups=[_GROUP])
        doc.rename_node("gain", "amp")
        assert doc.positions["amp"] == (500.0, 500.0)
        assert doc.undo()
        assert doc.positions["gain"] == (500.0, 500.0) and "amp" not in doc.positions
        assert doc.groups[0].get("nodes") == ["gain", "floor"]
        doc.save()
        assert read_sidecar(doc.sidecar_path)["gain"] == (500.0, 500.0)
        assert "amp" not in read_sidecar(doc.sidecar_path)
        assert read_groups(doc.sidecar_path)[0].get("nodes") == ["gain", "floor"]
        assert doc.redo()
        assert doc.positions["amp"] == (500.0, 500.0)
        assert doc.groups[0].get("nodes") == ["amp", "floor"]

    def test_a_move_made_after_a_rename_survives_the_renames_undo(self, tmp_path: Path) -> None:
        """Positions are the page's state, not the document's: undo re-keys the CURRENT positions rather
        than restoring old ones, so a node moved after the rename keeps its new place under its old name."""
        doc = _open(tmp_path, CONTRACT_FLOW_TEXT)
        write_sidecar(doc.sidecar_path, {"gain": (500.0, 500.0)})
        doc.rename_node("gain", "amp")
        doc.set_positions({**doc.positions, "amp": (700.0, 700.0)})
        assert doc.undo()
        assert doc.positions["gain"] == (700.0, 700.0)

    def test_undoing_an_edit_that_renames_nothing_leaves_positions_and_groups_alone(self, tmp_path: Path) -> None:
        """CON case: only a rename's undo re-keys; undoing a parameter edit moves nothing."""
        doc = _open(tmp_path, CONTRACT_FLOW_TEXT)
        write_sidecar(doc.sidecar_path, {"gain": (500.0, 500.0)}, groups=[_GROUP])
        doc.set_positions({**doc.positions, "floor": (900.0, 100.0)})
        before = doc.positions
        doc.set_param("floor", "percentile", 40.0)
        assert doc.undo()
        assert doc.positions == before
        assert doc.groups[0].get("nodes") == ["gain", "floor"]

    def test_the_ring_holds_fifty_edits(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, CONTRACT_FLOW_TEXT)
        assert doc.history_depth == 50
        texts = [doc.text]
        for i in range(60):
            doc.set_param("floor", "percentile", float(i))
            texts.append(doc.text)
        undone = 0
        while doc.undo():
            undone += 1
        assert undone == 50
        assert doc.text == texts[10]

    def test_a_new_edit_clears_the_redo_side(self, tmp_path: Path) -> None:
        doc = _open(tmp_path, CONTRACT_FLOW_TEXT)
        doc.set_param("floor", "percentile", 1.0)
        doc.set_param("floor", "percentile", 2.0)
        assert doc.undo()
        doc.set_param("floor", "percentile", 3.0)
        assert doc.redo() is False
        assert "percentile: 3.0" in doc.text


# ------------------------------------------------------------------------------------------------
# naming
# ------------------------------------------------------------------------------------------------


class TestNaming:
    def test_list_members_are_named_by_their_position(self, tmp_path: Path) -> None:
        model = _open(tmp_path, SOURCE_TEXT).model
        assert [n.name for n in model.nodes if ".ops[" in n.name] == ["stream.ops[0]", "stream.ops[1]"]

    def test_a_joint_streams_members_are_streams_i(self, tmp_path: Path) -> None:
        model = _open(tmp_path, JOINT_TEXT).model
        assert [n.name for n in model.nodes] == [
            "joint",
            "joint.streams[0]",
            "joint.streams[0].source",
            "joint.streams[1]",
            "joint.streams[1].source",
        ]
        joint = model.node("joint")
        assert [(s.name, s.type) for s in joint.inputs] == [("streams[0]", "Stream"), ("streams[1]", "Stream")]
        assert _wires(model, "list") == [
            ("joint.streams[0]", "stream", "joint", "streams[0]"),
            ("joint.streams[1]", "stream", "joint", "streams[1]"),
        ]
        assert model.problems == []

    def test_an_include_brings_nodes_from_the_included_file_and_set_param_writes_that_file(
        self, tmp_path: Path
    ) -> None:
        """Decision: an included node is edited in the file it came from. ``source_file`` names it,
        ``set_param`` patches that text, and ``save()`` writes every file the document spans — the
        main one and the included one alike; nothing is written before ``save()``.
        """
        sub = tmp_path / "subflow.yaml"
        sub_text = (
            f"floor: !class:{MOD}.DocBackground {{percentile: 30.0}}\n"
            f"level:\n  op: !class:{MOD}.DocDecide {{level: 0.3}}\n  from: floor\n"
        )
        sub.write_text(sub_text)
        main_text = (
            "contract: !class:recordstream.ops.contract.GraphContract\n"
            "  name: doc view\n"
            "  inputs: {image: Image}\n"
            "  outputs: {}\n"
            "flow:\n"
            "  include: subflow.yaml\n"
            "  again:\n"
            f"    op: !class:{MOD}.DocGain {{gain: 2.0}}\n"
            "    from: level\n"
            "outputs: again\n"
        )
        main = _write(tmp_path, main_text, "main.yaml")
        doc = Document.open(main)
        model = doc.model
        assert [n.name for n in model.nodes] == ["contract", "floor", "level", "again"]
        assert model.node("floor").source_file == str(sub.resolve())
        assert model.node("floor").loc == f"{sub.resolve()}:1:8"
        assert model.node("again").source_file == str(main)
        assert doc.files == [main.resolve(), sub.resolve()]
        assert _wires(model, "record") == [
            ("floor", "record", "level", "record"),
            ("level", "record", "again", "record"),
        ]
        doc.set_param("floor", "percentile", 45.0)
        assert doc.model.node("floor").params["percentile"].value == 45.0
        assert sub.read_text() == sub_text  # nothing written yet
        assert main.read_text() == main_text
        doc.save()
        assert sub.read_text() == sub_text.replace("percentile: 30.0", "percentile: 45.0")
        assert main.read_text() == main_text
        assert Document.open(main).model.node("floor").params["percentile"].value == 45.0


# ------------------------------------------------------------------------------------------------
# save
# ------------------------------------------------------------------------------------------------


class TestSave:
    def test_save_writes_the_text_and_the_sidecar_and_a_second_open_sees_the_same_model(
        self, tmp_path: Path, fake_layout: types.ModuleType
    ) -> None:
        path = _write(tmp_path, CONTRACT_FLOW_TEXT)
        doc = Document.open(path)
        doc.set_param("floor", "percentile", 40.0)
        positions = dict(doc.positions)
        positions["floor"] = (300.0, 40.0)
        doc.set_positions(positions)
        assert doc.sidecar_path == path.with_suffix(".layout.yaml")
        assert not doc.sidecar_path.exists()
        doc.save()
        assert path.read_text() == doc.text
        assert yaml.safe_load(doc.sidecar_path.read_text())["floor"] == [300.0, 40.0]
        again = Document.open(path)
        assert [n.name for n in again.model.nodes] == [n.name for n in doc.model.nodes]
        assert _wires(again.model) == _wires(doc.model)
        assert again.positions["floor"] == (300.0, 40.0)

    def test_save_without_positions_writes_no_sidecar(self, tmp_path: Path, fake_layout: types.ModuleType) -> None:
        doc = _open(tmp_path, CONTRACT_FLOW_TEXT)
        doc.set_param("floor", "percentile", 40.0)
        doc.save()
        assert not doc.sidecar_path.exists()
        assert doc.path.read_text() == doc.text


# ------------------------------------------------------------------------------------------------
# the text patcher on its own
# ------------------------------------------------------------------------------------------------


class TestPatcher:
    def test_it_replaces_a_multi_line_block_value_with_one_line(self) -> None:
        from streamstudio._yamltext import YamlText

        text = "a: !class:x.Y\n  keys:\n    - one\n    - two\n  other: 1\n"
        patched = YamlText(text).set_kwarg(1, 4, "keys", ["three"])
        assert patched == "a: !class:x.Y\n  keys: [three]\n  other: 1\n"

    def test_it_appends_to_a_bare_tag(self) -> None:
        from streamstudio._yamltext import YamlText

        text = "flow:\n  w:\n    op: !class:x.Y   # bare\n    from: a\n"
        patched = YamlText(text).set_kwarg(3, 9, "gain", 2)
        assert patched == "flow:\n  w:\n    op: !class:x.Y {gain: 2}   # bare\n    from: a\n"

    def test_it_refuses_a_location_that_is_not_a_marker(self) -> None:
        from streamstudio._yamltext import YamlText

        with pytest.raises(ValueError, match="no YAML node starts at"):
            YamlText("a: 1\n").set_kwarg(7, 7, "x", 1)

    def test_spellings(self) -> None:
        from streamstudio._yamltext import spell

        assert spell(30e6) == "30000000.0"
        assert spell("smooth") == "smooth"
        assert spell("1.0") == "'1.0'"
        assert spell(None) == "null"
        assert spell(True) == "true"
        assert spell([1, 2]) == "[1, 2]"
        assert spell({"a": 1}) == "{a: 1}"
        assert spell("$DATA_ROOT/x") == "$DATA_ROOT/x"


# ------------------------------------------------------------------------------------------------
# an edit that only MOVES an existing problem is not refused
# ------------------------------------------------------------------------------------------------

#: A flow with ONE problem that is already there: ``stamp`` binds a value ``probe`` does not have (line 7).
MOVED_TEXT = """\
# a flow whose last step binds a value its source does not have (one existing problem)
flow:
  probe:
    op: !class:recordstream.ops.debug.PrintRecordOp {label: probe}
  stamp:
    op: !class:recordstream.ops.debug.PrintRecordOp {label: stamp}
    bind: {label: probe.nosuch}
outputs: stamp
"""
PROBE = "recordstream.ops.debug.PrintRecordOp"


def _bind_problems(document: Document) -> List[str]:
    return [problem.message for problem in document.check() if "nosuch" in problem.message]


class TestAMovedProblemIsNotAGainedOne:
    """An edit is refused when it GAINS a problem. Measured before 2026-09-30: problems were compared by their whole
    message, location included, so a step inserted ABOVE an existing problem moved it from ``graph.yaml:6:9`` to
    ``graph.yaml:9:9`` and the edit was refused for "gaining" the problem that was already there."""

    def test_a_step_inserted_above_an_existing_problem_is_accepted_and_the_problem_moves_with_it(
        self, tmp_path: Path
    ) -> None:
        document = Document.open(_write(tmp_path, MOVED_TEXT))
        [before] = _bind_problems(document)
        assert "graph.yaml:6:9" in before
        document.add_step("mid", "probe", PROBE, {"label": "mid"})
        [after] = _bind_problems(document)
        assert "graph.yaml:9:9" in after  # the same problem, three lines further down
        assert "  mid:\n" in document.text

    def test_a_whole_text_that_only_moves_the_problem_is_accepted(self, tmp_path: Path) -> None:
        document = Document.open(_write(tmp_path, MOVED_TEXT))
        document.replace_text("# one more comment line on top\n" + MOVED_TEXT)
        [after] = _bind_problems(document)
        assert "graph.yaml:7:9" in after

    def test_an_edit_that_adds_a_new_problem_is_still_refused(self, tmp_path: Path) -> None:
        """The con case: a second, DIFFERENT broken bind is a gained problem, whatever the lines do."""
        document = Document.open(_write(tmp_path, MOVED_TEXT))
        broken = MOVED_TEXT.replace(
            "outputs: stamp\n",
            f"  again:\n    op: !class:{PROBE} {{label: again}}\n    bind: {{label: probe.other}}\noutputs: again\n",
        )
        with pytest.raises(ValueError, match="probe has no output 'other'"):
            document.replace_text(broken)
        assert document.text == MOVED_TEXT

    def test_a_second_copy_of_the_same_problem_is_still_a_gained_one(self) -> None:
        """Counted, not set-compared: the same message moved is one problem; the same message twice is one more."""
        before = [Problem("x: bind 'p.q' at /g/graph.yaml:6:9", "/g/graph.yaml:6:9")]
        moved = [Problem("x: bind 'p.q' at /g/graph.yaml:9:9", "/g/graph.yaml:9:9")]
        doubled = moved + [Problem("x: bind 'p.q' at /g/graph.yaml:12:9", "/g/graph.yaml:12:9")]
        assert _gained(before, moved) == []
        assert _gained(before, doubled) == [doubled[1]]

    def test_every_location_spelling_is_taken_out(self) -> None:
        """confluid's ``file:line:col`` (relative or absolute) and PyYAML's ``line N, column M``."""
        before = [
            Problem('graph.yaml: does not load: ScannerError: in "graph.yaml", line 3, column 5', "graph.yaml:3:5"),
            Problem("Failed to construct X at /a b/graph.yaml:4:9: bad", "/a b/graph.yaml:4:9"),
        ]
        after = [
            Problem('graph.yaml: does not load: ScannerError: in "graph.yaml", line 6, column 5', "graph.yaml:6:5"),
            Problem("Failed to construct X at /a b/graph.yaml:7:9: bad", "/a b/graph.yaml:7:9"),
        ]
        assert _gained(before, after) == []
