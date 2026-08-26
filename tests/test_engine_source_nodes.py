"""An ``@configurable(category="engine")`` class builds a SOURCE node, so a source wires into it.

``Stream`` / ``FlowGraph`` take a ``source`` (and ``Stream`` an ``ops`` list), so their canvas nodes
must expose those as WIRED ``RECORDSTREAM_SOURCE`` / ``RECORDSTREAM_OP`` sockets and hand back a
``RECORDSTREAM_SOURCE`` — otherwise a leaf source's ``source`` output (``RECORDSTREAM_SOURCE``) has
nothing type-equal to dock into and ComfyUI silently refuses the wire (the frontend connects on
socket-type STRING EQUALITY).

The classification used to be purely structural (:func:`~streamstudio.bridge._is_source` reading the
``__iter__`` / ``__getitem__`` return annotation), which made it hostage to an annotation: ``JointStream``
declares ``Iterator[Record]`` and became a source node, while ``Stream`` and ``FlowGraph`` declare
``Iterator[Any]`` — duck-typed on purpose, since a Stream may wrap ANY iterable — and silently fell
through to the generic builder (``source`` a STRING widget, output a bare ``STRING``). The engine
CATEGORY is the deliberate tag, so it decides; these pins hold that, and hold the leaf-source case
that must NOT change with it.
"""

from typing import Any, Dict, Set

import streamstudio  # noqa: F401  (populates NODE_CLASS_MAPPINGS)
from streamstudio import NODE_CLASS_MAPPINGS
from streamstudio.bridge import OP_TYPE, SOURCE_TYPE
from streamstudio.graphio import yaml_to_workflow

STREAM = "Taidal_recordstream_engine_Stream"
FLOWGRAPH = "Taidal_recordstream_engine_FlowGraph"
JOINT = "Taidal_recordstream_engine_JointStream"
HF_SOURCE = "Taidal_recordstream_source_HuggingFaceSource"


def _slots(class_type: str, section: str) -> Dict[str, str]:
    """``{param: socket_type}`` for one INPUT_TYPES section of a registered node."""
    spec = NODE_CLASS_MAPPINGS[class_type].INPUT_TYPES().get(section, {})
    return {name: value[0] if isinstance(value, (list, tuple)) else value for name, value in spec.items()}


# --------------------------------------------------------------------------- #
# PRO: the engines expose wired sockets and hand back a source
# --------------------------------------------------------------------------- #


def test_the_stream_node_takes_its_source_on_a_wire_not_a_text_widget() -> None:
    assert "source" not in _slots(STREAM, "required"), "source must not be a STRING widget"
    assert _slots(STREAM, "optional")["source"] == SOURCE_TYPE


def test_the_stream_node_hands_back_a_source_so_it_chains_into_another_engine() -> None:
    cls = NODE_CLASS_MAPPINGS[STREAM]
    assert cls.RETURN_TYPES == (SOURCE_TYPE,)
    assert cls.RETURN_NAMES == ("source",)


def test_a_leaf_sources_output_is_type_equal_to_the_stream_nodes_source_slot() -> None:
    """The actual defect: ComfyUI connects on socket-type EQUALITY, so these two strings must match."""
    produced = NODE_CLASS_MAPPINGS[HF_SOURCE].RETURN_TYPES[0]
    consumed = _slots(STREAM, "optional")["source"]
    assert produced == consumed == SOURCE_TYPE


def test_the_stream_node_seeds_the_dynamic_op_socket_family() -> None:
    """``ops`` is an op-LIST param -> the ``op_0`` seed socket web/op_list.js grows, never a widget."""
    assert "ops" not in _slots(STREAM, "required")
    assert _slots(STREAM, "optional")["op_0"] == OP_TYPE


def test_the_flowgraph_node_is_a_source_node_like_its_stream_twin() -> None:
    """FlowGraph is the named-step twin of Stream over the same kernel — same duck-typed ``source``."""
    assert _slots(FLOWGRAPH, "optional")["source"] == SOURCE_TYPE
    assert NODE_CLASS_MAPPINGS[FLOWGRAPH].RETURN_TYPES == (SOURCE_TYPE,)


def test_every_engine_node_is_classified_as_a_source_node() -> None:
    """The kind rides on the class (``TAIDAL_NODE_KIND``) and is read by the export / import path."""
    for class_type in (STREAM, FLOWGRAPH, JOINT):
        assert NODE_CLASS_MAPPINGS[class_type].TAIDAL_NODE_KIND == "source", class_type


def test_a_wired_source_reaches_the_constructed_stream() -> None:
    """Executing the node with a wired source produces a Stream over exactly that source."""
    from recordstream.core import Stream

    records = [{"input": 1}, {"input": 2}]
    node = NODE_CLASS_MAPPINGS[STREAM]()
    # A node's execute returns ComfyUI's {"ui": ..., "result": (...)} envelope (the timing badge).
    (built,) = getattr(node, node.FUNCTION)(source=records, chunk_size=0, class_names="")["result"]
    assert isinstance(built, Stream)
    assert list(built) == records


# --------------------------------------------------------------------------- #
# CON: a leaf source is all-widget and must stay that way
# --------------------------------------------------------------------------- #


def test_a_leaf_source_gains_no_source_socket() -> None:
    """HuggingFaceSource has no source/ops constructor params — it stays a pure producer."""
    optional = _slots(HF_SOURCE, "optional")
    assert "source" not in optional and "op_0" not in optional and "source_0" not in optional
    assert NODE_CLASS_MAPPINGS[HF_SOURCE].RETURN_TYPES == (SOURCE_TYPE,)


def test_a_leaf_sources_own_settings_stay_text_widgets() -> None:
    """The dataset id / split / feature knobs are config, not wires."""
    required = _slots(HF_SOURCE, "required")
    assert required["path"] == "STRING"
    assert required["split"] == "STRING"


# --------------------------------------------------------------------------- #
# The phantom-slot regression: the importer emitted an op_0 the node did not have
# --------------------------------------------------------------------------- #


SEED_YAML = """
stream: !class:recordstream.core.stream.Stream
  ops:
    - !class:recordstream.ops.contract.RecordContract
        name: classification output
        fields: {image: Image, class: Label}
"""


def test_every_slot_the_importer_emits_exists_on_the_node_it_targets() -> None:
    """``yaml_to_workflow`` wired the contract into a ``Stream.op_0`` that INPUT_TYPES did not declare.

    A saved workflow whose input slot is unknown to the node class cannot be executed, so the
    importer's output is only meaningful when every slot it names is real.
    """
    workflow = yaml_to_workflow(SEED_YAML, NODE_CLASS_MAPPINGS, name="classification source")
    for node in workflow["nodes"]:
        spec: Dict[str, Any] = NODE_CLASS_MAPPINGS[node["type"]].INPUT_TYPES()
        declared: Set[str] = {*spec.get("required", {}), *spec.get("optional", {})}
        emitted = {slot["name"] for slot in node.get("inputs", [])}
        assert emitted <= declared, f"{node['type']} emits undeclared slots {sorted(emitted - declared)}"
