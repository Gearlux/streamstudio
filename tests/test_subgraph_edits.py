"""Every document operation takes a QUALIFIED name — a node inside a subgraph is ``prep/grey``.

The operations are the ones a top-level node has, applied to the level the node lives on:

* ``set_param('prep/scaled', …)`` patches that one line (the prototype's measurement: comments 4 → 4);
* ``add_step(name, after='prep/grey', …)`` inserts inside ``prep``'s ``steps:``; ``remove_step('prep/grey')``
  removes there; both follow the ``add_step`` rule for what read the anchor and for ``result:``;
* ``rename_node('prep/grey', 'prep/mono')`` rewrites ONLY ``prep``'s ``steps:`` mapping and its ``result:``
  — a change of prefix is refused — and renaming an OUTER ``grey`` no longer rewrites an inner
  ``from: grey`` (measured before this change: it did, and the saved file stopped running with
  ``flow step 'prep' (a subgraph): Subgraph: step 'mask' reads 'mono' (from: 'mono') …``);
* renaming the subgraph ``prep`` re-keys every ``prep/…`` position and group member (and a group's
  ``parent``), and its undo re-keys them back;
* positions: the inside is laid out on its own, by the same layered rule, keyed by qualified names;
* copy / paste as text inside a subgraph, with the paste rules of the top level; a paste renaming a step
  never reaches into a pasted subgraph's own ``steps:``.
"""

import difflib
from pathlib import Path
from typing import List

import numpy as np
import pytest
import yaml
from confluid import configurable
from recordstream import Image, Record
from recordstream.flow.graph import FlowGraph

from streamstudio.document import Document
from streamstudio.layout import MARGIN, Group

MOD = __name__
SUBGRAPH = "recordstream.flow.subgraph.Subgraph"
PROBE = "recordstream.ops.debug.PrintRecordOp"
SCALE_OP = "!class:recordstream.ops.numpy.Scale {source_min: 0.0, source_max: 255.0, target_min: 0.0, target_max: 1.0}"

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
          op: {SCALE_OP}
      result: scaled
  # the mask
  mask:
    op: !class:recordstream.ops.numpy.Threshold {{low_level: 0.5}}
outputs: mask
"""

# The critic's names/inline.yaml, in block form: an OUTER step and an INNER step share the name grey.
NAMES_TEXT = f"""\
flow:
  grey:
    op: !class:recordstream.ops.image.ConvertMode {{mode: L}}   # OUTER step named grey
  prep:
    op: !class:{SUBGRAPH}
      steps:
        grey:
          op: {SCALE_OP}
        mask:
          op: !class:recordstream.ops.numpy.Threshold {{low_level: 0.5}}
          from: grey
      result: mask
  boxes:
    op: !class:recordstream.ops.numpy.ConnectedComponents {{}}
    from: prep
outputs: boxes
"""

ONE_STEP_TEXT = f"""\
flow:
  prep:
    op: !class:{SUBGRAPH}
      steps:
        only:
          op: !class:{PROBE} {{label: only}}
  tail:
    op: !class:{PROBE} {{label: tail}}
outputs: tail
"""

BOXES = [(15, 20, 61, 71), (90, 50, 146, 106)]


@configurable(category="op", group="sb-edit-test")
class SbEditNeeds:
    """Reads an entry nothing in the flow makes — so a contract-less flow draws the host's side."""

    consumes = {"spectrogram": "*"}
    produces: dict = {}

    def __call__(self, record: Record) -> Record:
        return record


def demo_record() -> Record:
    pixels = np.empty((120, 160, 3), dtype=np.uint8)
    pixels[...] = (20, 20, 30)
    pixels[20:71, 15:61] = 230
    pixels[50:106, 90:146] = 200
    return {"image": Image(pixels)}


def _doc(tmp_path: Path, text: str, name: str = "demo.yaml") -> Document:
    path = tmp_path / name
    path.write_text(text)
    return Document.open(path)


def _changed(before: str, after: str) -> List[str]:
    """The - / + lines of a unified diff, headers left out."""
    return [
        line
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
        if line[:1] in "+-" and line[:3] not in ("+++", "---")
    ]


def _comments(text: str) -> int:
    return sum(1 for line in text.splitlines() if "#" in line)


class TestSetParam:
    def test_an_inner_parameter_patches_its_one_line_and_keeps_every_comment(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, DEMO_TEXT)
        document.set_param("prep/scaled", "target_max", 2.0)
        assert _changed(DEMO_TEXT, document.text) == [
            f"-          op: {SCALE_OP}",
            f"+          op: {SCALE_OP.replace('target_max: 1.0', 'target_max: 2.0')}",
        ]
        assert (_comments(DEMO_TEXT), _comments(document.text)) == (4, 4)
        assert document.model.node("prep/scaled").params["target_max"].value == 2.0
        assert document.dirty and (tmp_path / "demo.yaml").read_text() == DEMO_TEXT
        assert document.check() == []

    def test_result_is_set_on_the_subgraph_node_and_steps_is_refused(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, DEMO_TEXT)
        document.set_param("prep", "result", "grey")
        assert _changed(DEMO_TEXT, document.text) == ["-      result: scaled", "+      result: grey"]
        assert document.model.inside["prep"].outputs == ["prep/grey"]
        with pytest.raises(ValueError, match="prep has no parameter 'steps'"):
            document.set_param("prep", "steps", {"grey": {}})

    def test_a_boundary_node_has_no_parameter(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match=r"prep/<in> has no parameter 'x'"):
            _doc(tmp_path, DEMO_TEXT).set_param("prep/<in>", "x", 1)


class TestAddAndRemoveInside:
    def test_add_step_after_an_inner_step_inserts_inside_the_subgraph(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, DEMO_TEXT)
        document.add_step("mid", "prep/grey", PROBE, {"label": "mid"})
        assert _changed(DEMO_TEXT, document.text) == [
            "+        mid:",
            f"+          op: !class:{PROBE} {{label: mid}}",
            "+          from: grey",
        ]
        assert [n.name for n in document.model.inside["prep"].nodes] == [
            "prep/<in>",
            "prep/grey",
            "prep/mid",
            "prep/scaled",
            "prep/<out>",
        ]
        assert document.check() == []

    def test_the_new_name_may_be_written_qualified(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, DEMO_TEXT)
        document.add_step("prep/mid", "prep/grey", PROBE, {})
        assert "prep/mid" in document.model.all_names()

    def test_a_name_on_another_level_is_refused(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, DEMO_TEXT)
        with pytest.raises(ValueError, match="'other/mid' is not a name on the level of 'prep/grey'"):
            document.add_step("other/mid", "prep/grey", PROBE, {})
        assert document.text == DEMO_TEXT

    def test_an_inner_name_already_there_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="'prep/scaled' is already a node"):
            _doc(tmp_path, DEMO_TEXT).add_step("scaled", "prep/grey", PROBE, {})

    def test_after_the_result_step_the_result_moves_to_the_new_step(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, DEMO_TEXT)
        document.add_step("probe", "prep/scaled", PROBE, {})
        assert _changed(DEMO_TEXT, document.text) == [
            "-      result: scaled",
            "+        probe:",
            f"+          op: !class:{PROBE} {{}}",
            "+          from: scaled",
            "+      result: probe",
        ]
        assert document.model.inside["prep"].outputs == ["prep/probe"]

    def test_remove_an_inner_step_rewires_what_read_it_and_moves_the_result(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, NAMES_TEXT)
        document.remove_step("prep/mask")
        assert _changed(NAMES_TEXT, document.text) == [
            "-        mask:",
            "-          op: !class:recordstream.ops.numpy.Threshold {low_level: 0.5}",
            "-          from: grey",
            "-      result: mask",
            "+      result: grey",
        ]
        assert [n.name for n in document.model.nodes] == ["grey", "prep", "boxes"]  # the outer grey is untouched

    def test_removing_the_only_inner_step_is_refused(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, ONE_STEP_TEXT)
        with pytest.raises(ValueError, match="'prep/only' is the only step of the subgraph 'prep'"):
            document.remove_step("prep/only")
        assert document.text == ONE_STEP_TEXT

    def test_a_boundary_node_is_not_a_step(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match=r"'prep/<in>' is not a step of the subgraph 'prep'"):
            _doc(tmp_path, DEMO_TEXT).remove_step("prep/<in>")


class TestRename:
    def test_renaming_the_outer_grey_leaves_the_inner_from_and_the_file_runs(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, NAMES_TEXT)
        document.rename_node("grey", "mono")
        assert _changed(NAMES_TEXT, document.text) == [
            "-  grey:",
            "+  mono:",
        ]
        document.save()
        record = FlowGraph.from_yaml(str(tmp_path / "demo.yaml"))._run(demo_record())
        assert record is not None and record["boxes"].boxes == BOXES

    def test_renaming_an_inner_step_rewrites_only_its_subgraphs_steps(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, NAMES_TEXT)
        document.rename_node("prep/grey", "prep/mono")
        assert _changed(NAMES_TEXT, document.text) == [
            "-        grey:",
            "+        mono:",
            "-          from: grey",
            "+          from: mono",
        ]
        assert [n.name for n in document.model.nodes] == ["grey", "prep", "boxes"]
        assert "prep/mono" in document.model.all_names()

    def test_renaming_the_result_step_rewrites_the_result_line(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, NAMES_TEXT)
        document.rename_node("prep/mask", "final")  # a bare name is a name on the same level
        assert _changed(NAMES_TEXT, document.text) == [
            "-        mask:",
            "+        final:",
            "-      result: mask",
            "+      result: final",
        ]

    @pytest.mark.parametrize(
        "old, new",
        [("prep/grey", "other/mono"), ("grey", "prep/mono"), ("prep/grey", "prep/a/b")],
    )
    def test_a_change_of_prefix_is_refused(self, tmp_path: Path, old: str, new: str) -> None:
        document = _doc(tmp_path, NAMES_TEXT)
        with pytest.raises(ValueError, match="can only be renamed on its own level"):
            document.rename_node(old, new)
        assert document.text == NAMES_TEXT

    def test_a_boundary_node_is_not_renamed(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="the boundary of the subgraph 'prep'"):
            _doc(tmp_path, NAMES_TEXT).rename_node("prep/<in>", "prep/start")

    def test_renaming_a_subgraph_rekeys_every_inner_position_and_group_and_undo_rekeys_back(
        self, tmp_path: Path
    ) -> None:
        document = _doc(tmp_path, NAMES_TEXT)
        document.set_positions({"prep": (10.0, 10.0), "prep/grey": (20.0, 20.0), "prep/mask": (30.0, 30.0)})
        groups: List[Group] = [
            {"id": "g1", "title": "inside", "color": "#fff", "nodes": ["prep/grey", "prep/mask"], "parent": "prep"},
            {"id": "g2", "title": "top", "color": "#000", "nodes": ["prep", "boxes"], "parent": ""},
        ]
        document.set_groups(groups)
        document.rename_node("prep", "pre")
        positions = document.positions
        assert (positions["pre"], positions["pre/grey"], positions["pre/mask"]) == (
            (10.0, 10.0),
            (20.0, 20.0),
            (30.0, 30.0),
        )
        assert [(g["nodes"], g["parent"]) for g in document.groups] == [
            (["pre/grey", "pre/mask"], "pre"),
            (["pre", "boxes"], ""),
        ]
        document.undo()
        assert document.positions["prep/grey"] == (20.0, 20.0)
        assert [(g["nodes"], g["parent"]) for g in document.groups] == [
            (["prep/grey", "prep/mask"], "prep"),
            (["prep", "boxes"], ""),
        ]


class TestPositions:
    def test_each_inside_is_laid_out_on_its_own_by_the_layered_rule(self, tmp_path: Path) -> None:
        positions = _doc(tmp_path, DEMO_TEXT).positions
        column = 340.0
        assert positions == {
            "prep": (MARGIN, MARGIN),
            "mask": (MARGIN + column, MARGIN),
            "prep/<in>": (MARGIN, MARGIN),
            "prep/grey": (MARGIN + column, MARGIN),
            "prep/scaled": (MARGIN + 2 * column, MARGIN),
            "prep/<out>": (MARGIN + 3 * column, MARGIN),
        }

    def test_inner_positions_are_held_and_saved_under_their_qualified_names(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, DEMO_TEXT)
        document.set_positions({"prep/grey": (500.0, 600.0)})
        document.save()
        sidecar = (tmp_path / "demo.layout.yaml").read_text()
        assert "prep/grey: [500.0, 600.0]" in sidecar
        assert Document.open(tmp_path / "demo.yaml").positions["prep/grey"] == (500.0, 600.0)


class TestCopyAndPasteInside:
    def test_copy_inner_steps_gives_their_own_lines_dedented(self, tmp_path: Path) -> None:
        text, kind = _doc(tmp_path, NAMES_TEXT).copy_text(["prep/mask", "prep/grey"])
        assert kind == "steps"
        assert text == (
            "grey:\n"
            f"  op: {SCALE_OP}\n"
            "mask:\n"
            "  op: !class:recordstream.ops.numpy.Threshold {low_level: 0.5}\n"
            "  from: grey\n"
        )

    def test_nodes_of_two_levels_are_not_copied_together(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="copy takes the nodes of ONE level"):
            _doc(tmp_path, NAMES_TEXT).copy_text(["grey", "prep/grey"])

    def test_paste_inside_renames_taken_names_and_moves_the_result(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, NAMES_TEXT)
        text, _kind = document.copy_text(["prep/grey", "prep/mask"])
        pasted = document.paste_text(text, "prep/mask")
        assert pasted == ["prep/grey_2", "prep/mask_2"]
        assert _changed(NAMES_TEXT, document.text) == [
            "-      result: mask",
            "+        grey_2:",
            f"+          op: {SCALE_OP}",
            "+        mask_2:",
            "+          op: !class:recordstream.ops.numpy.Threshold {low_level: 0.5}",
            "+          from: grey_2",
            "+      result: mask_2",
        ]
        assert document.check() == []

    def test_a_pasted_rename_never_reaches_into_a_pasted_subgraphs_steps(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, NAMES_TEXT)
        text, _kind = document.copy_text(["grey", "prep"])
        assert document.paste_text(text, "boxes") == ["grey_2", "prep_2"]
        pasted = document.text[len(NAMES_TEXT) - len("outputs: boxes\n") :]
        assert "  grey_2:\n" in pasted and "  prep_2:\n" in pasted
        assert "          from: grey\n" in pasted  # the inner step still reads the INNER grey
        assert "grey_2" not in pasted.split("  prep_2:\n", 1)[1]
        assert document.check() == []


# ------------------------------------------------------------------------------------------------
# the edge spellings and every refusal
# ------------------------------------------------------------------------------------------------

ONE_LINE_STEPS_TEXT = f"""\
flow:
  prep:
    op: !class:{SUBGRAPH}
      steps: {{grey: {{op: !class:{PROBE} {{label: grey}}}}, scaled: {{op: !class:{PROBE} {{label: scaled}}}}}}
  tail:
    op: !class:{PROBE} {{label: tail}}
outputs: tail
"""

FLOW_STYLE_READER_TEXT = f"""\
flow:
  prep:
    op: !class:{SUBGRAPH}
      steps:
        head:
          op: !class:{PROBE} {{label: head}}
        bare: !class:{PROBE}
        reader: {{op: !class:{PROBE} {{label: reader}}, from: head}}
outputs: prep
"""

READS_TEXT = f"""\
flow:
  probe:
    op: !class:recordstream.ops.image.ConvertMode {{mode: L}}
  level:
    op: !class:recordstream.ops.image.ConvertMode {{mode: L}}
    merge_from: [probe]
  mean:
    op: !class:recordstream.ops.image.ConvertMode {{mode: L}}

  stamp:
    op: !class:{PROBE} {{label: stamp}}
    bind: {{label: mean.mode}}
outputs: stamp
"""

TWO_LISTS_TEXT = f"""\
first: !class:recordstream.core.stream.Stream
  ops:
    - !class:{PROBE} {{label: a}}
    - !class:{PROBE} {{label: b}}
    - _target_: {PROBE}
      label: c

second: !class:recordstream.core.stream.Stream
  ops:
    - !class:{PROBE} {{label: d}}
flow:
  only:
    op: !class:{PROBE} {{label: only}}
outputs: only
"""


class TestEdgeSpellings:
    def test_a_step_on_one_line_is_refused_for_an_insert_and_moves_to_a_file_as_written(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, ONE_LINE_STEPS_TEXT)
        assert [n.name for n in document.model.inside["prep"].nodes][1:3] == ["prep/grey", "prep/scaled"]
        with pytest.raises(ValueError, match=r"the steps of the subgraph 'prep' are written on one line"):
            document.add_step("mid", "prep/grey", PROBE, {})
        document.extract_steps("prep", tmp_path / "prep.steps.yaml")
        assert "      steps: {include: prep.steps.yaml}\n" in document.text
        assert document.template_text(["prep"]) == (
            f"{{grey: {{op: !class:{PROBE} {{label: grey}}}}, scaled: {{op: !class:{PROBE} {{label: scaled}}}}}}\n"
        )

    def test_removing_the_head_drops_a_one_line_from_and_skips_a_bare_step(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, FLOW_STYLE_READER_TEXT)
        document.remove_step("prep/head")
        assert f"        reader: {{op: !class:{PROBE} {{label: reader}}}}\n" in document.text
        assert "        bare: !class:" in document.text

    def test_removing_the_only_step_of_the_flow_is_refused(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, f"flow:\n  only:\n    op: !class:{PROBE} {{}}\noutputs: only\n")
        with pytest.raises(ValueError, match="'only' is the only step of the flow"):
            document.remove_step("only")

    def test_the_hosts_side_is_not_renamed(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, f"flow:\n  gain:\n    op: !class:{MOD}.SbEditNeeds {{}}\noutputs: gain\n")
        assert "<host>" in document.model.names()
        with pytest.raises(ValueError, match="the host's side of the graph"):
            document.rename_node("<host>", "provider")

    def test_an_inner_rename_to_a_name_taken_there_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="'prep/mask' is already a node"):
            _doc(tmp_path, NAMES_TEXT).rename_node("prep/grey", "mask")


class TestStepsFiles:
    def test_steps_of_a_flow_that_come_from_another_file_are_edited_there(self, tmp_path: Path) -> None:
        (tmp_path / "part.steps.yaml").write_text(f"a:\n  op: !class:{PROBE} {{label: a}}\n")
        document = _doc(tmp_path, "flow:\n  include: part.steps.yaml\noutputs: a\n")
        with pytest.raises(ValueError, match="comes from another file"):
            document.convert_to_subgraph(["a"], "sub")
        with pytest.raises(ValueError, match="rename it in that file"):
            document.rename_node("a", "b")

    def test_a_subgraph_including_several_files_or_a_nested_include_is_refused_for_edits(self, tmp_path: Path) -> None:
        (tmp_path / "a.steps.yaml").write_text(f"a:\n  op: !class:{PROBE} {{label: a}}\n")
        (tmp_path / "b.steps.yaml").write_text(f"b:\n  op: !class:{PROBE} {{label: b}}\n")
        (tmp_path / "ab.steps.yaml").write_text("include: [a.steps.yaml, b.steps.yaml]\n")
        several = (
            f"flow:\n  prep:\n    op: !class:{SUBGRAPH}\n      steps: {{include: [a.steps.yaml, b.steps.yaml]}}\n"
            "outputs: prep\n"
        )
        document = _doc(tmp_path, several)
        assert document.model.all_names()[:4] == ["prep", "prep/<in>", "prep/a", "prep/b"]
        with pytest.raises(ValueError, match="prep's steps come from several files"):
            document.add_step("c", "prep/a", PROBE, {})
        nested = _doc(tmp_path, several.replace("[a.steps.yaml, b.steps.yaml]", "ab.steps.yaml"), "nested.yaml")
        with pytest.raises(ValueError, match="is not a plain steps mapping"):
            nested.add_step("c", "prep/a", PROBE, {})

    def test_a_steps_file_found_by_confluids_search_is_edited_where_it_was_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "shared.steps.yaml").write_text(f"mono:\n  op: !class:{PROBE} {{label: mono}}\n")
        (tmp_path / "graphs").mkdir()
        monkeypatch.chdir(tmp_path)
        text = (
            f"flow:\n  prep:\n    op: !class:{SUBGRAPH}\n      steps: {{include: shared.steps.yaml}}\noutputs: prep\n"
        )
        document = _doc(tmp_path / "graphs", text)
        document.set_param("prep/mono", "label", "m")
        document.add_step("more", "prep/mono", PROBE, {})
        document.save()
        assert "more:" in (tmp_path / "shared.steps.yaml").read_text()

    def test_a_subgraph_spelling_no_steps_cannot_move_them(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, f"flow:\n  prep:\n    op: !class:{SUBGRAPH} {{result: x}}\noutputs: prep\n")
        with pytest.raises(ValueError, match="prep spells no steps: mapping"):
            document.extract_steps("prep", tmp_path / "prep.steps.yaml")

    def test_a_missing_folder_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(KeyError, match="no directory"):
            _doc(tmp_path, DEMO_TEXT).extract_steps("prep", tmp_path / "nosuch" / "prep.steps.yaml")

    def test_a_template_of_a_node_inside_a_subgraph(self, tmp_path: Path) -> None:
        nested = DEMO_TEXT.replace(
            "        grey:\n",
            f"        inner:\n          op: !class:{SUBGRAPH}\n            steps:\n              x:\n"
            f"                op: !class:{PROBE} {{label: x}}\n        grey:\n",
        )
        document = _doc(tmp_path, nested)
        assert document.template_text(["prep/inner"]) == f"x:\n  op: !class:{PROBE} {{label: x}}\n"
        assert document.template_text(["prep/grey"]).startswith("grey:\n")
        with pytest.raises(ValueError, match="'prep/<in>' is not a flow step"):
            document.copy_text(["prep/<in>"])

    def test_a_steps_file_held_but_not_saved_moves_with_save_as(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, DEMO_TEXT)
        document.extract_steps("prep", tmp_path / "prep.steps.yaml")
        (tmp_path / "elsewhere").mkdir()
        document.save_as(tmp_path / "elsewhere" / "demo.yaml")
        assert "      steps: {include: ../prep.steps.yaml}\n" in document.text
        assert (tmp_path / "prep.steps.yaml").is_file()
        assert document.check() == []


class TestInsertingSteps:
    @pytest.mark.parametrize(
        "text, message",
        [
            ("grey: [", "the steps are not YAML"),
            ("- a\n", "'front' holds no steps"),
            ("include: other.steps.yaml\n", "'front' includes another file"),
        ],
    )
    def test_what_is_not_a_steps_mapping_is_refused(self, tmp_path: Path, text: str, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            _doc(tmp_path, DEMO_TEXT).insert_steps("mask", text, "front")

    def test_a_later_step_reading_a_step_the_copy_does_not_hold_reads_the_one_before_it(self, tmp_path: Path) -> None:
        steps = f"a:\n  op: !class:{PROBE} {{}}\nb:\n  op: !class:{PROBE} {{}}\n  from: elsewhere\n"
        document = _doc(tmp_path, DEMO_TEXT)
        assert document.insert_steps("mask", steps, "2 front") == "_2_front"
        assert document.text.endswith(
            "  _2_front:\n"
            f"    op: !class:{SUBGRAPH}\n"
            "      steps:\n"
            "        a:\n"
            f"          op: !class:{PROBE} {{}}\n"
            "        b:\n"
            f"          op: !class:{PROBE} {{}}\n"
            "          from: a\n"
            "      result: b\n"
            "    from: mask\n"
            "outputs: _2_front\n"
        )

    def test_a_given_name_must_be_a_step_name(self, tmp_path: Path) -> None:
        (tmp_path / "x.steps.yaml").write_text(f"a:\n  op: !class:{PROBE} {{}}\n")
        document = _doc(tmp_path, DEMO_TEXT)
        with pytest.raises(ValueError, match="'include' cannot name a step"):
            document.include_steps("mask", tmp_path / "x.steps.yaml", "include")
        assert document.include_steps("prep/grey", tmp_path / "x.steps.yaml", "prep/inc") == "prep/inc"
        assert "            steps: {include: x.steps.yaml}\n" in document.text


class TestConvertRefusals:
    def test_nothing_to_convert(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="needs at least one node"):
            _doc(tmp_path, DEMO_TEXT).convert_to_subgraph([], "x")

    def test_a_node_that_is_not_a_step(self, tmp_path: Path) -> None:
        text = DEMO_TEXT + "contract: !class:recordstream.ops.contract.GraphContract {name: c, outputs: {}}\n"
        with pytest.raises(ValueError, match="'contract' is not a step of the flow"):
            _doc(tmp_path, text).convert_to_subgraph(["contract"], "x")

    def test_a_first_step_merging_moves_its_merge_and_a_bind_to_the_last_step_is_refused(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, READS_TEXT)
        with pytest.raises(ValueError, match=r"stamp reads mean.mode \(bind: label=mean.mode\), a value of mean"):
            document.convert_to_subgraph(["level", "mean"], "measure")
        chain = "".join(f"  {step}:\n    op: !class:{PROBE} {{}}\n" for step in ("a", "b", "c"))
        returns_a = _doc(tmp_path, f"flow:\n{chain}outputs: a\n", "returns_a.yaml")
        with pytest.raises(ValueError, match=r"the flow returns a \(outputs: a\), which would move inside"):
            returns_a.convert_to_subgraph(["a", "b"], "ab")
        merging = _doc(tmp_path, READS_TEXT.replace("    bind: {label: mean.mode}\n", ""), "merging.yaml")
        assert merging.convert_to_subgraph(["level"], "measure") == "measure"
        assert (
            "  measure:\n"
            "    op: !class:recordstream.flow.subgraph.Subgraph\n"
            "      steps:\n"
            "        level:\n"
            "          op: !class:recordstream.ops.image.ConvertMode {mode: L}\n"
            "      result: level\n"
            "    merge_from: [probe]\n"
        ) in merging.text

    def test_trailing_blank_lines_stay_below_the_subgraph_and_a_record_bind_follows_it(self, tmp_path: Path) -> None:
        text = (
            f"flow:\n  a:\n    op: !class:{PROBE} {{}}\n  b:\n    op: !class:{PROBE} {{}}\n\n"
            f"  c:\n    op: !class:{PROBE} {{}}\n    bind:\n      label: b[image]\noutputs: c\n"
        )
        document = _doc(tmp_path, text)
        document.convert_to_subgraph(["b"], "bb")
        assert "      result: b\n\n  c:\n" in document.text
        assert "    bind:\n      label: bb[image]\n" in document.text

    def test_list_members_of_two_lists_or_not_consecutive_or_mixed(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, TWO_LISTS_TEXT)
        with pytest.raises(ValueError, match="must be of ONE list"):
            document.convert_to_subgraph(["first.ops[0]", "second.ops[0]"], "x")
        with pytest.raises(ValueError, match=r"first.ops\[0\] and first.ops\[2\] are not consecutive members"):
            document.convert_to_subgraph(["first.ops[0]", "first.ops[2]"], "x")
        with pytest.raises(ValueError, match="flow steps or list members, not both"):
            document.convert_to_subgraph(["first.ops[0]", "only"], "x")

    def test_a_target_mapping_member_goes_under_op_whole(self, tmp_path: Path) -> None:
        document = _doc(tmp_path, TWO_LISTS_TEXT)
        assert document.convert_to_subgraph(["first.ops[1]", "first.ops[2]"], "x") == "first.ops[1]"
        assert (
            "    - !class:recordstream.flow.subgraph.Subgraph\n"
            "      steps:\n"
            "        print_record_op:\n"
            f"          op: !class:{PROBE} {{label: b}}\n"
            "        print_record_op_2:\n"
            "          op:\n"
            f"            _target_: {PROBE}\n"
            "            label: c\n"
            "      result: print_record_op_2\n"
            "\n"
            "second:"
        ) in document.text
        assert document.check() == []


class TestTheInsideReadsOnlyItsOwnSteps:
    def test_an_inner_from_or_merge_naming_an_outer_step_is_a_problem_quoted_as_written(self, tmp_path: Path) -> None:
        text = (
            f"flow:\n  outer:\n    op: !class:{PROBE} {{}}\n  prep:\n    op: !class:{SUBGRAPH}\n      steps:\n"
            f"        a:\n          op: !class:{PROBE} {{}}\n          from: outer\n"
            f"        b:\n          op: !class:{PROBE} {{}}\n          merge_from: [outer]\noutputs: prep\n"
        )
        document = _doc(tmp_path, text)
        model = document.model
        reason = (
            " inside the subgraph 'prep' — a step inside a subgraph reads only the record the subgraph receives and "
            "the steps before it inside at "
        )
        inner = [p.message for p in model.inside["prep"].problems]
        assert inner[0].startswith(f"prep/a: from: 'outer' does not name an earlier step{reason}")
        assert inner[1].startswith(f"prep/b: merge_from: 'outer' does not name an earlier step{reason}")
        assert [p.loc for p in document.check()] == [model.node("prep/a").loc, model.node("prep/b").loc]

    def test_one_defect_inside_is_one_message_at_the_inner_line(self, tmp_path: Path) -> None:
        """The engine refuses to build the subgraph for the same defect; that refusal is not repeated at ``prep``."""
        document = _doc(tmp_path, DEMO_TEXT.replace("{mode: L}   # to grey", "{mode: LA}   # to grey"))
        problems = document.check()
        assert [p.loc for p in problems] == [f"{tmp_path / 'demo.yaml'}:7:15"]
        assert problems[0].message.startswith("prep/grey: Failed to construct ConvertMode at ")
        assert "Input should be 'RGB', 'RGBA' or 'L'" in problems[0].message

    def test_a_graph_without_a_flow_has_no_steps_to_convert(self, tmp_path: Path) -> None:
        text = f"stream: !class:recordstream.core.stream.Stream\n  ops:\n    - !class:{PROBE} {{}}\n"
        with pytest.raises(ValueError, match="has no flow: steps"):
            _doc(tmp_path, text).convert_to_subgraph(["stream"], "x")

    def test_the_template_of_a_nested_subgraph_including_a_file_is_that_file(self, tmp_path: Path) -> None:
        (tmp_path / "x.steps.yaml").write_text(f"# x\nx:\n  op: !class:{PROBE} {{}}\n")
        document = _doc(tmp_path, DEMO_TEXT)
        document.include_steps("prep/grey", tmp_path / "x.steps.yaml", None)
        assert document.template_text(["prep/x"]) == f"# x\nx:\n  op: !class:{PROBE} {{}}\n"


class TestThePatcher:
    """``_yamltext`` pieces the subgraph edits lean on, in both spellings."""

    @pytest.mark.parametrize(
        "text, key, after",
        [
            ("s: {op: x, from: a}\n", "from", "s: {op: x}\n"),
            ("s: {from: a, op: x}\n", "from", "s: {op: x}\n"),
            ("s: {from: a}\n", "from", "s: {}\n"),
            ("s:\n  op: x\n  from: a\n", "from", "s:\n  op: x\n"),
        ],
    )
    def test_drop_a_pair_in_either_spelling(self, text: str, key: str, after: str) -> None:
        from streamstudio._yamltext import YamlText

        patched = YamlText(text)
        mapping = patched.path("s")
        assert isinstance(mapping, yaml.MappingNode)
        assert patched.apply([patched.drop_pair_edit(mapping, key)]) == after

    def test_a_top_level_rename_follows_a_ref_inside_a_subgraph_but_no_inner_from(self) -> None:
        from streamstudio._yamltext import YamlText

        text = (
            "thr: !class:X {}\n"
            "flow:\n"
            "  thr2:\n"
            f"    op: !class:{SUBGRAPH}\n"
            "      steps:\n"
            "        thr:\n"
            "          op: !ref:thr\n"
            "          from: thr\n"
            "        use: {op: !class:X {}, bind: {v: thr.level}}\n"
            "        ref: {_ref_: thr}\n"
            "outputs: thr2\n"
        )
        renamed = YamlText(text).rename("thr", "gate")
        assert renamed.splitlines() == [
            "gate: !class:X {}",
            "flow:",
            "  thr2:",
            f"    op: !class:{SUBGRAPH}",
            "      steps:",
            "        thr:",
            "          op: !ref:gate",
            "          from: thr",
            "        use: {op: !class:X {}, bind: {v: thr.level}}",
            "        ref: {_ref_: gate}",
            "outputs: thr2",
        ]
