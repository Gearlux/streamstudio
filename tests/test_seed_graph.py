"""A host-seeded interface graph converts and round-trips (the *seed* contract).

A consuming workspace seeds the editor's graph FILE from a tiny YAML document — a
source-less ``Stream`` whose only op is a pre-configured ``RecordContract`` stating the
records the workspace requires. That document must convert into a loadable workflow
(``yaml_to_workflow``, the ``streamstudio import`` verb) with the contract wired into the
Stream's ``op_0`` slot, and the native-save export (``workflow_to_yaml``) must reproduce
the contract's ``fields`` as a real mapping — otherwise the seeded interface would not
survive the user's first save. These pins are the producer-side half of that handshake.
"""

from typing import Any, Dict

import yaml

import streamstudio  # noqa: F401  (populates NODE_CLASS_MAPPINGS)
from streamstudio import NODE_CLASS_MAPPINGS
from streamstudio.graphio import workflow_to_yaml, yaml_to_workflow

CONTRACT = "Taidal_recordstream_op_contract_RecordContract"
STREAM = "Taidal_recordstream_engine_Stream"

SEED_YAML = """
stream: !class:recordstream.core.stream.Stream
  ops:
    - !class:recordstream.ops.contract.RecordContract
        name: classification output
        fields: {image: Image, class: Label}
"""


def _node(workflow: Dict[str, Any], class_type: str) -> Dict[str, Any]:
    matches = [node for node in workflow["nodes"] if node["type"] == class_type]
    assert len(matches) == 1, f"expected exactly one {class_type} node, got {len(matches)}"
    node: Dict[str, Any] = matches[0]
    return node


def test_record_contract_is_a_discovered_node() -> None:
    assert CONTRACT in NODE_CLASS_MAPPINGS


def test_a_source_less_seed_converts_with_the_contract_wired_into_op_0() -> None:
    workflow = yaml_to_workflow(SEED_YAML, NODE_CLASS_MAPPINGS, name="classification source")
    stream = _node(workflow, STREAM)
    contract = _node(workflow, CONTRACT)
    op_links = [i for i in stream.get("inputs", []) if i.get("name") == "op_0" and i.get("link") is not None]
    assert len(op_links) == 1, "the contract must arrive pre-wired into the Stream's op_0 slot"
    # the pre-filled interface travels on the contract node's widgets (fields, then name)
    widgets = contract.get("widgets_values", [])
    assert "classification output" in widgets
    assert any("image" in str(value) and "Label" in str(value) for value in widgets)


def test_the_seeded_contract_round_trips_through_the_native_save_export() -> None:
    workflow = yaml_to_workflow(SEED_YAML, NODE_CLASS_MAPPINGS, name="classification source")
    exported = workflow_to_yaml(workflow, NODE_CLASS_MAPPINGS)
    doc = yaml.safe_load(exported)
    contracts = [
        value
        for value in doc.values()
        if isinstance(value, dict) and value.get("_target_", "").endswith("RecordContract")
    ]
    assert len(contracts) == 1
    assert contracts[0]["fields"] == {"image": "Image", "class": "Label"}  # a real mapping, not a repr string
    assert contracts[0]["name"] == "classification output"
