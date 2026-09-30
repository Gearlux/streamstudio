"""A subgraph is ONE node on its level and a nested model inside — derived by the same builder.

A ``flow:`` step (or a ``Stream``'s ``ops`` member) whose op is a ``recordstream.flow.subgraph.Subgraph``
marker becomes one node of kind ``subgraph``: ``record`` in, ``record`` and ``op`` out, plus the entry
sockets the engine DERIVES from its inner ops (``consumes`` / ``produces`` / ``flags``), and ``result`` as
its only parameter — ``steps`` is never a form field (measured on the prototype: a form edit of it
flattened the whole block, 18 → 11 lines, 4 → 3 comments). Its inside is ``model.inside[<node>]``: the
inner steps with QUALIFIED names (``prep/grey``; a list member's ``stream.ops[0]/grey``; nested
``prep/inner/x``) at their own ``file:line:col``, plus two ``boundary`` nodes, ``prep/<in>`` (the record the
subgraph receives, and every entry its inside reads from it) and ``prep/<out>`` (the record of the result
step).

The subgraph marker is BUILT while deriving, which is what keeps the canvas's chain check on for a
flow holding one (measured before this change: one subgraph step made the whole flow's check answer
``[]``, hiding a real defect further down), and a refusal of the engine's (``result:`` naming no inner step)
is a located problem at the subgraph.
"""

from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pytest
from confluid import configurable
from recordstream import Image, Record
from recordstream.flow.graph import FlowGraph

from streamstudio.app import _model_dict
from streamstudio.document import Document

MOD = __name__
SUBGRAPH = "recordstream.flow.subgraph.Subgraph"
SCALE = "!class:recordstream.ops.numpy.Scale {source_min: 0.0, source_max: 255.0, target_min: 0.0, target_max: 1.0}"


@configurable(category="op", group="sb-model-test")
class SbMakeSpectrogram:
    """Makes the spectrogram entry from the image."""

    consumes = {"image": "*"}
    produces = {"spectrogram": "*"}

    def __call__(self, record: Record) -> Record:
        return {**record, "spectrogram": record["image"]}


@configurable(category="op", group="sb-model-test")
class SbNeedsSpectrogram:
    """Reads the spectrogram entry, writes a floor.

    Args:
        percentile: The percentile rank.
    """

    consumes = {"spectrogram": "*"}
    produces = {"floor": "*"}

    def __init__(self, percentile: float = 25.0) -> None:
        self.percentile = percentile

    def __call__(self, record: Record) -> Record:
        return {**record, "floor": 0.0}


# The design document's subgraph (DESIGN §0.2), with four comment lines.
DEMO_TEXT = f"""\
flow:
  prep:
    op: !class:{SUBGRAPH}
      steps:
        # the inside
        grey:
          op: !class:recordstream.ops.image.ConvertMode {{mode: L}}   # to grey
        scaled:
          # to 0..1
          op: {SCALE}
      result: scaled
  # the mask
  mask:
    op: !class:recordstream.ops.numpy.Threshold {{low_level: 0.5}}
outputs: mask
"""

PREP_STEPS = f"""\
# the prep steps
grey:
  op: !class:recordstream.ops.image.ConvertMode {{mode: L}}
scaled:
  op: {SCALE}
"""

# One steps file, included by two subgraph steps (DESIGN §0.3.3): no anchor, no !ref — two copies on load.
TWICE_TEXT = f"""\
flow:
  read: {{}}
  prep:
    op: !class:{SUBGRAPH}
      steps:
        include: prep.steps.yaml
      result: scaled
  mask:
    op: !class:recordstream.ops.numpy.Threshold {{low_level: 0.5}}
  prep2:
    from: read
    op: !class:{SUBGRAPH}
      steps:
        include: prep.steps.yaml
outputs: mask
"""

ENTRIES_TEXT = f"""\
flow:
  make:
    op: !class:{MOD}.SbMakeSpectrogram {{}}
  measure:
    op: !class:{SUBGRAPH}
      steps:
        floor:
          op: !class:{MOD}.SbNeedsSpectrogram {{}}
outputs: measure
"""

# The critic's §2.7 shape: an UNRELATED earlier subgraph step used to switch the whole flow's check off.
BLIND_TEXT = f"""\
flow:
  prep:
    op: !class:{SUBGRAPH}
      steps:
        grey:
          op: !class:recordstream.ops.image.ConvertMode {{mode: L}}
  floor:
    op: !class:{MOD}.SbNeedsSpectrogram {{}}
outputs: floor
contract: !class:recordstream.ops.contract.GraphContract {{name: blind, inputs: {{image: Image}}, outputs: {{}}}}
"""

INNER_DEFECT_TEXT = f"""\
flow:
  prep:
    op: !class:{SUBGRAPH}
      steps:
        grey:
          op: !class:recordstream.ops.image.ConvertMode {{mode: L}}
        floor:
          op: !class:{MOD}.SbNeedsSpectrogram {{}}
outputs: prep
contract: !class:recordstream.ops.contract.GraphContract {{name: inner, inputs: {{image: Image}}, outputs: {{}}}}
"""

NO_RESULT_TEXT = f"""\
flow:
  prep:
    op: !class:{SUBGRAPH}
      steps:
        grey:
          op: !class:recordstream.ops.image.ConvertMode {{mode: L}}
      result: nosuch
outputs: prep
"""

NESTED_TEXT = f"""\
flow:
  prep:
    op: !class:{SUBGRAPH}
      steps:
        grey:
          op: !class:recordstream.ops.image.ConvertMode {{mode: L}}
        inner:
          op: !class:{SUBGRAPH}
            steps:
              scaled:
                op: {SCALE}
  mask:
    op: !class:recordstream.ops.numpy.Threshold {{low_level: 0.5}}
outputs: mask
"""

PIPE_TEXT = f"""\
stream: !class:recordstream.core.stream.Stream
  ops:
    - !class:{SUBGRAPH}
      steps:
        grey:
          op: !class:recordstream.ops.image.ConvertMode {{mode: L}}
        scaled:
          op: {SCALE}
    - !class:recordstream.ops.numpy.Threshold {{low_level: 0.5}}
"""


def demo_record() -> Record:
    """The 120x160 demo image of the design: two light rectangles on a dark ground (mask mean 0.2855)."""
    pixels = np.empty((120, 160, 3), dtype=np.uint8)
    pixels[...] = (20, 20, 30)
    pixels[20:71, 15:61] = 230
    pixels[50:106, 90:146] = 200
    return {"image": Image(pixels)}


def _write(folder: Path, name: str, text: str) -> Path:
    path = folder / name
    path.write_text(text)
    return path


def _doc(tmp_path: Path, text: str, name: str = "demo.yaml") -> Document:
    return Document.open(_write(tmp_path, name, text))


def _rows(model: Any) -> List[tuple]:  # a Model: (name, kind) per node
    return [(node.name, node.kind) for node in model.nodes]


def _wires(model: Any) -> List[str]:  # a Model: one "a.x -> b.y [kind]" line per wire
    return [f"{w.source}.{w.source_socket} -> {w.target}.{w.target_socket} [{w.kind}]" for w in model.wires]


class TestTheSubgraphNode:
    def test_one_node_of_kind_subgraph_record_in_record_and_op_out(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, DEMO_TEXT)
        model = document.model
        assert _rows(model) == [("prep", "subgraph"), ("mask", "op")]
        prep = model.node("prep")
        assert prep.class_path == SUBGRAPH
        assert prep.loc == f"{tmp_path / 'demo.yaml'}:3:9"
        assert [s.name for s in prep.inputs] == ["record"]
        assert [s.name for s in prep.outputs] == ["record", "op"]
        assert _wires(model) == ["prep.record -> mask.record [record]"]
        assert document.check() == []

    def test_result_is_its_only_parameter_and_steps_is_not_a_form_field(self, tmp_path: Path) -> None:
        prep = _doc(tmp_path, DEMO_TEXT).model.node("prep")
        assert list(prep.params) == ["result"]
        assert prep.params["result"].value == "scaled" and prep.params["result"].spelled

    def test_the_entry_sockets_are_what_the_engine_derives_from_the_inner_ops(self, tmp_path: Path) -> None:
        model = _doc(tmp_path, ENTRIES_TEXT).model
        measure = model.node("measure")
        assert [(s.name, s.entry, s.required) for s in measure.inputs] == [
            ("record", "", False),
            ("spectrogram", "spectrogram", True),
        ]
        assert [(s.name, s.entry) for s in measure.outputs] == [("record", ""), ("op", ""), ("floor", "floor")]
        assert "make.spectrogram -> measure.spectrogram [entry]" in _wires(model)


class TestTheInside:
    def test_the_inside_is_a_nested_model_with_qualified_names_and_two_boundaries(self, tmp_path: Path) -> None:
        model = _doc(tmp_path, DEMO_TEXT).model
        assert list(model.inside) == ["prep"]
        inside = model.inside["prep"]
        assert _rows(inside) == [
            ("prep/<in>", "boundary"),
            ("prep/grey", "op"),
            ("prep/scaled", "op"),
            ("prep/<out>", "boundary"),
        ]
        assert _wires(inside) == [
            "prep/<in>.record -> prep/grey.record [record]",
            "prep/grey.record -> prep/scaled.record [record]",
            "prep/scaled.record -> prep/<out>.record [record]",
        ]
        assert inside.outputs == ["prep/scaled"]
        assert inside.problems == [] and inside.inside == {}

    def test_every_inner_node_carries_its_own_line(self, tmp_path: Path) -> None:
        model = _doc(tmp_path, DEMO_TEXT).model
        file = tmp_path / "demo.yaml"
        assert model.node("prep/grey").loc == f"{file}:7:15"
        assert model.node("prep/scaled").loc == f"{file}:10:15"
        assert model.node("prep/<in>").loc == "" and model.node("prep/<out>").loc == ""

    def test_the_names_of_every_level_and_the_levels(self, tmp_path: Path) -> None:
        model = _doc(tmp_path, DEMO_TEXT).model
        assert model.names() == ["prep", "mask"]
        assert model.all_names() == ["prep", "prep/<in>", "prep/grey", "prep/scaled", "prep/<out>", "mask"]
        assert [level.names() for level in model.levels()] == [
            ["prep", "mask"],
            ["prep/<in>", "prep/grey", "prep/scaled", "prep/<out>"],
        ]

    def test_an_unknown_qualified_name_is_refused_naming_every_node(self, tmp_path: Path) -> None:
        model = _doc(tmp_path, DEMO_TEXT).model
        with pytest.raises(KeyError) as refused:
            model.node("prep/nosuch")
        assert "'prep/nosuch'" in str(refused.value) and "prep/scaled" in str(refused.value)

    def test_in_carries_every_entry_the_inside_reads_from_the_record(self, tmp_path: Path) -> None:
        inside = _doc(tmp_path, ENTRIES_TEXT).model.inside["measure"]
        boundary = inside.node("measure/<in>")
        assert [(s.name, s.entry) for s in boundary.outputs] == [("record", ""), ("spectrogram", "spectrogram")]
        assert "measure/<in>.spectrogram -> measure/floor.spectrogram [entry]" in _wires(inside)

    def test_nested_subgraphs_nest_their_models_and_their_names(self, tmp_path: Path) -> None:
        model = _doc(tmp_path, NESTED_TEXT).model
        assert model.node("prep/inner").kind == "subgraph"
        inner = model.inside["prep"].inside["prep/inner"]
        assert _rows(inner) == [
            ("prep/inner/<in>", "boundary"),
            ("prep/inner/scaled", "op"),
            ("prep/inner/<out>", "boundary"),
        ]
        assert model.node("prep/inner/scaled").loc == f"{tmp_path / 'demo.yaml'}:11:21"
        assert _doc(tmp_path, NESTED_TEXT).check() == []

    def test_a_stream_member_is_a_subgraph_node_too(self, tmp_path: Path) -> None:
        model = _doc(tmp_path, PIPE_TEXT).model
        assert _rows(model) == [("stream", "engine"), ("stream.ops[0]", "subgraph"), ("stream.ops[1]", "op")]
        assert _rows(model.inside["stream.ops[0]"]) == [
            ("stream.ops[0]/<in>", "boundary"),
            ("stream.ops[0]/grey", "op"),
            ("stream.ops[0]/scaled", "op"),
            ("stream.ops[0]/<out>", "boundary"),
        ]
        assert "stream.ops[0].record -> stream.ops[1].record [record]" in _wires(model)


class TestTheCheck:
    def test_a_subgraph_step_no_longer_switches_the_flows_check_off(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, BLIND_TEXT)
        problems = document.check()
        assert len(problems) == 1, problems
        assert "floor: SbNeedsSpectrogram needs the record entry 'spectrogram', which nothing before it produces" in (
            problems[0].message
        )
        assert problems[0].loc == document.model.node("floor").loc

    def test_a_defect_inside_is_placed_at_the_inner_node(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, INNER_DEFECT_TEXT)
        problems = document.check()
        assert len(problems) == 1, problems
        assert "prep/floor: SbNeedsSpectrogram needs the record entry 'spectrogram'" in problems[0].message
        assert problems[0].loc == f"{tmp_path / 'demo.yaml'}:8:15"

    def test_a_result_naming_no_inner_step_is_one_located_problem_at_the_subgraph(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, NO_RESULT_TEXT)
        problems = document.check()
        assert [p.loc for p in problems] == [f"{tmp_path / 'demo.yaml'}:3:9"]
        assert problems[0].message.startswith(
            "prep: Subgraph: result 'nosuch' does not name an inner step (the steps are: ['grey'])"
        )
        assert "prep/<out>" not in {wire.target for wire in document.model.inside["prep"].wires}

    def test_a_problem_inside_is_listed_on_the_inside_and_on_the_top(self, tmp_path: Path) -> None:
        text = DEMO_TEXT.replace("recordstream.ops.image.ConvertMode", f"{MOD}.NoSuchClass")
        model = _doc(tmp_path, text).model
        inner = [p.message for p in model.inside["prep"].problems]
        assert len(inner) == 1 and inner[0].startswith("prep/grey: cannot resolve the class")
        assert inner[0] in [p.message for p in model.problems]


class TestUsingOneStepsFileTwice:
    """DESIGN §0.3.3 — ``steps: {include: …}`` twice: two independent insides, no anchor, no !ref."""

    @pytest.fixture
    def document(self, tmp_path: Path) -> Document:
        _write(tmp_path, "prep.steps.yaml", PREP_STEPS)
        return _doc(tmp_path, TWICE_TEXT)

    def test_two_subgraph_nodes_each_with_its_own_inside(self, document: Document, tmp_path: Path) -> None:
        model = document.model
        assert _rows(model) == [("read", "op"), ("prep", "subgraph"), ("mask", "op"), ("prep2", "subgraph")]
        assert list(model.inside) == ["prep", "prep2"]
        assert [n.name for n in model.inside["prep2"].nodes] == [
            "prep2/<in>",
            "prep2/grey",
            "prep2/scaled",
            "prep2/<out>",
        ]
        steps_file = tmp_path / "prep.steps.yaml"
        for use in ("prep", "prep2"):
            grey = model.node(f"{use}/grey")
            assert grey.source_file == str(steps_file)
            assert grey.loc == f"{steps_file}:3:7"
        assert document.check() == []

    def test_the_run_is_right_and_the_two_uses_are_separate_objects(self, document: Document) -> None:
        built = document.load()
        graph = FlowGraph(flow=built["flow"], outputs=built["outputs"])
        first, second = graph.steps[1].op, graph.steps[3].op
        assert first is not None and second is not None
        assert first.steps["grey"]["op"] is not second.steps["grey"]["op"]
        record = graph._run(demo_record())
        assert record is not None
        assert round(float(np.asarray(record["mask"]).mean()), 4) == 0.2855

    def test_an_edit_through_one_use_changes_the_shared_file_so_both_uses(
        self, document: Document, tmp_path: Path
    ) -> None:
        main_before = document.text
        document.set_param("prep/scaled", "target_max", 2.0)
        assert document.text == main_before  # the main file is not where the step lives
        steps_file = tmp_path / "prep.steps.yaml"
        assert steps_file.read_text() == PREP_STEPS  # held until Save
        model = document.model
        assert model.node("prep/scaled").params["target_max"].value == 2.0
        assert model.node("prep2/scaled").params["target_max"].value == 2.0
        document.save()
        assert "target_max: 2.0" in steps_file.read_text()


class TestTheJson:
    def test_the_model_json_carries_the_inside_recursively(self, tmp_path: Path) -> None:
        body: Dict[str, Any] = _model_dict(_doc(tmp_path, NESTED_TEXT).model)
        assert set(body) == {"nodes", "wires", "problems", "outputs", "inside"}
        assert list(body["inside"]) == ["prep"]
        prep = body["inside"]["prep"]
        assert [node["name"] for node in prep["nodes"]] == ["prep/<in>", "prep/grey", "prep/inner", "prep/<out>"]
        assert [node["kind"] for node in prep["nodes"]] == ["boundary", "op", "subgraph", "boundary"]
        assert list(prep["inside"]) == ["prep/inner"]
        assert prep["inside"]["prep/inner"]["inside"] == {}
        assert body["inside"]["prep"]["outputs"] == ["prep/inner"]
