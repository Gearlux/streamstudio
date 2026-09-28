"""The flow exporter accepts the editor's NATIVE save — a UI ``workflow.json`` (2026-08-25).

A consuming workspace owns graph FILES the editor mirrors saves into, and those files are
UI-form workflows — while the flow compiler consumes the prompt/API form only. The bridge
is ``graphio.ui_workflow_to_prompt`` (graphio owns all workflow.json knowledge; export.py
stays prompt-form only), and both entry points AUTO-DETECT the form by payload shape: a
dict carrying a ``nodes`` list is a UI workflow. These tests drive the whole path on a
real workflow (built by ``yaml_to_workflow`` from the seed shape a workspace writes) and
EXECUTE the exported flow document through recordstream, so the wiring is proven end to
end — both contract boundaries firing where they should.
"""

from pathlib import Path
from typing import Any, Dict

import numpy as np
import pytest
from recordstream import Image, Label
from recordstream.flow.graph import FlowGraph
from recordstream.ops.contract import ContractError

import streamstudio  # noqa: F401  (populates NODE_CLASS_MAPPINGS)
from streamstudio import NODE_CLASS_MAPPINGS
from streamstudio.cli import export_flow
from streamstudio.export import export_flow_yamls
from streamstudio.graphio import ui_workflow_to_prompt, yaml_to_workflow

CONTRACT = "Taidal_recordstream_op_contract_RecordContract"

#: The seed shape a workspace writes for a processing graph: input contract → output contract.
FLOW_SEED = """\
flow:
  incoming: !class:recordstream.ops.contract.RecordContract
    name: flow input
    fields: {image: "*"}
  delivered:
    op: !class:recordstream.ops.contract.RecordContract
      name: flow output
      fields: {image: Image}
    from: incoming
outputs: delivered
"""


def _ui_workflow() -> Dict[str, Any]:
    """A REAL UI-form workflow — what the editor's save mirror writes into the owned file."""
    return yaml_to_workflow(FLOW_SEED, NODE_CLASS_MAPPINGS, name="flow seed")


def test_ui_workflow_to_prompt_carries_widgets_and_wires() -> None:
    prompt = ui_workflow_to_prompt(_ui_workflow(), NODE_CLASS_MAPPINGS)
    contracts = {nid: node for nid, node in prompt.items() if node["class_type"] == CONTRACT}
    assert len(contracts) == 2
    by_name = {node["inputs"]["name"]: (nid, node) for nid, node in contracts.items()}
    input_id, _ = by_name["flow input"]
    _, output_node = by_name["flow output"]
    assert output_node["inputs"]["record"] == [input_id, 0]  # the record wire, prompt-form
    assert "image" in str(output_node["inputs"]["fields"])


def test_a_ui_workflow_round_trips_to_an_executable_flow_document(tmp_path: Path) -> None:
    prompt = ui_workflow_to_prompt(_ui_workflow(), NODE_CLASS_MAPPINGS)
    yaml_docs = export_flow_yamls(prompt, NODE_CLASS_MAPPINGS)
    assert len(yaml_docs) == 1
    doc = tmp_path / "flow.yaml"
    doc.write_text(yaml_docs[0])
    graph = FlowGraph.from_yaml(str(doc))

    # both boundaries LIVE: a conforming record passes, the input contract fires on a bad one
    good = {"image": Image(np.zeros((2, 2), dtype=np.uint8)), "class": Label(value=1)}
    graph.source = [good]
    assert list(graph)[0] is good
    graph.source = [{"class": Label(value=1)}]
    with pytest.raises(ContractError, match=r"flow input: record #\d+ has no entry 'image'"):
        list(graph)

    # ...and the OUTPUT contract fires when the pipeline breaks its promise
    graph.source = [{"image": "not an Image item"}]
    with pytest.raises(ContractError, match=r"flow output: record #\d+ entry 'image' is a str, expected Image"):
        list(graph)


def test_cli_export_auto_detects_a_ui_workflow(tmp_path: Path) -> None:
    import json

    graph_path = tmp_path / "flow.json"
    graph_path.write_text(json.dumps(_ui_workflow()))
    out = tmp_path / "flow.yaml"
    export_flow(graph_path, output=out)
    text = out.read_text()
    assert "RecordContract" in text and "flow input" in text and "flow output" in text


def test_cli_export_still_refuses_a_non_graph_json(tmp_path: Path) -> None:
    import json

    graph_path = tmp_path / "other.json"
    graph_path.write_text(json.dumps({"neither": "prompt nor workflow"}))
    with pytest.raises(SystemExit, match="not a ComfyUI"):
        export_flow(graph_path)
