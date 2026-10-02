"""A CHAINED op's op-LIST fan-in survives export — every ``op_N`` wire, in slot order.

The standalone-node export path accumulates dynamic ``op_N`` wires into a list, but the
folded-chain path (``_chain_op_instance``) assigned ``kwargs[base]`` per wire — last wire
wins — so a ``ConfigureOp`` drawn with TWO compute ops exported with only the second
(measured 2026-09-02 on the threshold-detector model canvas: ``ops: {_ref_: formula_op}``
with the first op hoisted as an orphan; the side branch then raised ``KeyError`` at the
first record). Order is load-bearing: the compute chain is sequential.
"""

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from confluid import flow, load

import streamstudio  # noqa: F401  (populates NODE_CLASS_MAPPINGS)
from streamstudio import NODE_CLASS_MAPPINGS
from streamstudio.graphio import workflow_to_yaml, yaml_to_workflow

#: A record chain whose ConfigureOp computes ``(level * 2) + 1`` on a side branch and
#: injects it as the Threshold's low_level — the two formulas do NOT commute, so the
#: executed result proves both list membership AND order.
FLOW_DOC = """
flow:
  seed:
    op: !class:recordstream.ops.formula.FormulaOp
      field: level
      formula: "a"
  threshold:
    from: seed
    op: !class:recordstream.ops.configure.ConfigureOp
      ops:
        - !class:recordstream.ops.formula.FormulaOp
          field: level
          formula: "a * 2"
        - !class:recordstream.ops.formula.FormulaOp
          field: level
          formula: "a + 1"
      source: level
      param: low_level
      target: !class:recordstream.ops.numpy.Threshold
        field: image
outputs: threshold
"""


def _exported_stream_ops(doc_text: str) -> Any:
    document = load(doc_text)
    stream = next(flow(v) for v in document.values() if type(flow(v)).__name__ == "Stream")
    return [flow(op) for op in stream.ops]


@pytest.fixture(scope="module")
def exported() -> str:
    workflow = yaml_to_workflow(FLOW_DOC, NODE_CLASS_MAPPINGS, name="chain op list")
    return workflow_to_yaml(workflow, NODE_CLASS_MAPPINGS)


class TestTheOpListSurvivesTheChainExport:
    def test_both_compute_ops_export_in_slot_order(self, exported: str) -> None:
        ops = _exported_stream_ops(exported)
        configure = next(op for op in ops if type(op).__name__ == "ConfigureOp")
        compute = [flow(op) for op in configure.ops]
        assert [type(op).__name__ for op in compute] == ["FormulaOp", "FormulaOp"]
        assert [op.formula for op in compute] == ["a * 2", "a + 1"], "the side branch is sequential"

    def test_the_exported_chain_computes_the_non_commuting_result(self, exported: str) -> None:
        """(5 * 2) + 1 = 11: cells above 11 pass. Collapsed-to-last would inject 6 and the
        12 would not be alone; a dropped first op would KeyError before any mask."""
        ops = _exported_stream_ops(exported)
        record = {"image": np.array([[5.0, 12.0], [10.0, 11.0]]), "level": 5.0}
        for op in ops:
            record = op(record)
        assert np.asarray(record["mask"]).tolist() == [[False, True], [False, False]]


class TestTheShippedThresholdDetectorCanvasRoundTrips:
    """The signal domain's drawn threshold-detector model canvas (the Stage-0
    pre-annotation model of the detection review loop) is exactly the fan-in case the
    chain-export fix exists for — its ConfigureOp nests two compute ops. Gated on the
    domain package being installed; the artifact itself ships there."""

    def test_the_canvas_exports_the_full_chain(self) -> None:
        import json

        waivefront_annotation = pytest.importorskip("waivefront.annotation")
        # the CANVAS beside the document the domain hands out (YAML since 2026-10-01)
        path = Path(waivefront_annotation.templates()["detection"]["model"]).with_suffix(".json")
        exported = workflow_to_yaml(json.loads(path.read_text()), NODE_CLASS_MAPPINGS)
        ops = _exported_stream_ops(exported)
        assert [type(op).__name__ for op in ops] == [
            "Spectrogram",
            "Magnitude",
            "ConfigureOp",
            "ConnectedComponents",
            "BoxesToRegions",
            "RegionsToDetectionBoxes",
            "SelectFields",
        ]
        configure = next(op for op in ops if type(op).__name__ == "ConfigureOp")
        compute = [flow(op) for op in configure.ops]
        assert [type(op).__name__ for op in compute] == ["NoiseFloorEstimate", "FormulaOp"]
