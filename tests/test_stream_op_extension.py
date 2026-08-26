"""The contract `web/stream_op.js` depends on, pinned from Python.

**Taidal: compile to Op** turns a subgraph you drew into a `Stream` op — a `record` input, the record
contract its output labels state, and a promoted `RECORDSTREAM_OP`. It runs ONLY when asked (user
instruction 2026-08-26, withdrawing the automatic version: a canvas edit nobody asked for is one you
have to undo, and it interacted badly with ComfyUI's own Convert to Subgraph).

The behaviour lives in the frontend and is verified live — this project has no JS test harness, the
same convention as the viewer's *set source…* button. What CAN be pinned here is the cross-language
contract the extension leans on, which is exactly where a silent break would come from: the JS
hard-codes socket-type STRINGS and node class keys, and nothing else would notice a Python rename.

Verified in a real browser against a real studio (2026-08-26):

* loading a workflow changes NOTHING — no nodes appear until the command is invoked;
* `taidal.compileToOp` on the user's own `extract` subgraph (two *Extract from Record* nodes with
  `field='label'` and `field='input'`, promoted as `text` and `image`) reports
  *"extract → op — record contract: {text, image}"* and builds `RenameField(label -> text)`,
  `RenameField(input -> image)`, `SelectFields(text,image)`, terminator, `op` promoted;
* wired into `Stream.op_0`, the terminator's record ancestry compiles to exactly those three ops;
* compiling three times leaves exactly one of each node — the nodes it owns are marked and rebuilt;
* a rectangle that neither transforms records nor promises entries is REFUSED with the reason, and a
  second promoted record output is refused as a fan-in a Pipeline cannot express;
* the full wiring after a compile, every arrow present::

      ExtractRecord[text]   -> (boundary).text        ExtractRecord[text]  -> RecordBuilder.text
      ExtractRecord[image]  -> (boundary).image       ExtractRecord[image] -> RecordBuilder.image
      RecordBuilder[record] -> SubgraphOp.record      SubgraphOp[op]       -> (boundary).op

  The last arrow needed the SubgraphOutput's OWN ``connect(outputSlot, node)``: the ordinary
  ``node.connect(slot, sg.outputNode, index)`` returns ``null`` against a boundary — measured — so
  the terminator sat there with its ``op`` going nowhere while the instance socket existed;
* the right-click item appears on a subgraph instance. It is added through
  ``LGraphCanvas.prototype.getNodeMenuOptions``, NOT ``beforeRegisterNodeDef``: an instance is typed
  by the subgraph's uuid and is not a registered node type, so the per-type hook never sees one;
* three ops dropped in without wiring chain in canvas reading order
  (`[ToTensor, ConvertToImage, PrintRecordOp]`); hand-wiring wins inside a component (laid out C, B,
  A but wired C -> A compiles `[PrintRecordOp, ToTensor, ConvertToImage]`).

The renames take the subgraph's INPUT record, not the extract nodes' outputs, and that is deliberate:
*Extract from Record* is canvas glue with no recordstream counterpart, so it can never appear inside
a compiled `Pipeline`. The compile TRANSLATES what the boundary promises into record ops that can
run; the extracts stay as the on-canvas view of the same thing.
"""

import re
from pathlib import Path

import pytest

import streamstudio  # noqa: F401  (populates NODE_CLASS_MAPPINGS)
from streamstudio import NODE_CLASS_MAPPINGS
from streamstudio.bridge import OP_TYPE, RECORD_TYPE
from streamstudio.export import _SUBGRAPH_OP_CLASS_TYPE

EXTENSION = Path(streamstudio.__file__).parent / "web" / "stream_op.js"
STREAM = "Taidal_recordstream_engine_Stream"
TAP = "Taidal_ExtractRecord"
OP_NODE = "Taidal_recordstream_op_torch_ToTensor"


def _const(name: str) -> str:
    """The value of a top-level ``const NAME = "...";`` in the extension."""
    match = re.search(rf'^const {name} = "([^"]*)";', EXTENSION.read_text(), re.MULTILINE)
    assert match, f"{name} is not declared in {EXTENSION.name}"
    return match.group(1)


def test_the_extension_is_shipped_beside_the_others() -> None:
    assert EXTENSION.is_file()


def test_the_socket_types_it_hard_codes_match_the_python_vocabulary() -> None:
    """The JS cannot import from Python — a renamed socket would silently stop matching."""
    assert _const("RECORD_TYPE") == RECORD_TYPE
    assert _const("OP_TYPE") == OP_TYPE


def test_the_terminator_it_places_is_the_registered_one() -> None:
    assert _const("TERMINATOR_TYPE") == _SUBGRAPH_OP_CLASS_TYPE
    assert _SUBGRAPH_OP_CLASS_TYPE in NODE_CLASS_MAPPINGS


def test_the_terminator_really_produces_the_socket_the_extension_promotes() -> None:
    assert NODE_CLASS_MAPPINGS[_SUBGRAPH_OP_CLASS_TYPE].RETURN_TYPES == (OP_TYPE,)


def test_a_promoted_op_is_type_equal_to_a_streams_op_slot() -> None:
    """ComfyUI connects on socket-type EQUALITY — this is why the drag is legal at all."""
    produced = NODE_CLASS_MAPPINGS[_SUBGRAPH_OP_CLASS_TYPE].RETURN_TYPES[0]
    consumed = NODE_CLASS_MAPPINGS[STREAM].INPUT_TYPES()["optional"]["op_0"][0]
    assert produced == consumed == OP_TYPE


def test_the_viewer_guard_is_real_a_tap_hands_no_record_onward() -> None:
    """The extension only treats a subgraph holding a node with a RECORD output.

    That single rule is what leaves a viewer rectangle alone, so it must actually separate the two:
    a tap ends the record (IMAGE / MASK / STRING / …), an op hands one onward.
    """
    assert RECORD_TYPE not in NODE_CLASS_MAPPINGS[TAP].RETURN_TYPES
    assert RECORD_TYPE in NODE_CLASS_MAPPINGS[OP_NODE].RETURN_TYPES


def test_the_record_boundary_name_matches_the_op_nodes_record_socket() -> None:
    """The extension wires the boundary into a node's ``record`` input by TYPE, and names the
    boundary slot with this constant — it must read the same as the socket users see."""
    spec = NODE_CLASS_MAPPINGS[OP_NODE].INPUT_TYPES()
    named = {**spec.get("required", {}), **spec.get("optional", {})}
    assert _const("RECORD_SLOT") in named


# --------------------------------------------------------------------------- #
# The chain members
# --------------------------------------------------------------------------- #

CHAIN_MEMBERS = [
    "Taidal_recordstream_op_torch_ToTensor",
    "Taidal_recordstream_op_image_ConvertToImage",
    "Taidal_recordstream_op_debug_PrintRecordOp",
]


@pytest.mark.parametrize("class_type", CHAIN_MEMBERS)
def test_every_chain_member_hands_a_record_onward(class_type: str) -> None:
    """``chainAllOps`` joins ops by wiring one's record OUTPUT into the next's record INPUT.

    An op that stopped producing a record could no longer be chained, and the extension would
    silently drop it from the pipeline — the exact failure this replaced. These three are the ones
    the live browser cases chain, so they are the ones pinned.
    """
    node_cls = NODE_CLASS_MAPPINGS[class_type]
    assert RECORD_TYPE in node_cls.RETURN_TYPES
    spec = node_cls.INPUT_TYPES()
    named = {**spec.get("required", {}), **spec.get("optional", {})}
    assert _const("RECORD_SLOT") in named


def test_the_extension_declares_the_chaining_and_the_command() -> None:
    """A rename here must update this file's record of the live behaviour."""
    source = EXTENSION.read_text()
    assert "function chainAllOps(" in source
    assert "function opComponents(" in source
    assert "function compileToOp(" in source


def test_compiling_is_a_COMMAND_and_nothing_runs_automatically() -> None:
    """The withdrawn automatic version edited the canvas on every conversion.

    Pinned as source structure because there is no JS harness: the extension must register the
    command and must NOT hook the lifecycle events that made it automatic.
    """
    source = EXTENSION.read_text()
    assert '"taidal.compileToOp"' in source
    assert "nodeCreated(" not in source
    assert "afterConfigureGraph(" not in source


# --------------------------------------------------------------------------- #
# The contract ops the boundary compiles to
# --------------------------------------------------------------------------- #

RENAME = "Taidal_recordstream_op_structure_RenameField"
SELECT = "Taidal_recordstream_op_structure_SelectFields"


def test_the_canvas_gathers_outputs_into_a_RECORD_BUILDER() -> None:
    """The extension wires the promoted outputs into *Build Record*, and stops there.

    It used to place loose ``RenameField``/``SelectFields`` nodes beside the extracts instead —
    equivalent to run and unreadable, because nothing connected them to the outputs they described.
    Translating that shape into record ops is now the COMPILER's job
    (``export.expand_record_builders``, pinned in ``tests/test_record_builder.py``), which is what
    lets the canvas keep the shape the user drew.
    """
    assert _const("BUILDER_TYPE") == "Taidal_RecordBuilder"
    assert "Taidal_RecordBuilder" in NODE_CLASS_MAPPINGS
    # Named in the header prose (it explains where the translation went), never CREATED here.
    source = EXTENSION.read_text()
    assert "createNode(RENAME" not in source and "createNode(SELECT" not in source
    assert "RENAME_TYPE" not in source and "SELECT_TYPE" not in source


def test_the_contract_ops_take_the_params_the_compiler_sets() -> None:
    """``expand_record_builders`` emits `src`/`dst` on a rename and `keys` on a select — a rename
    on the recordstream side would break the translation silently."""
    rename = NODE_CLASS_MAPPINGS[RENAME].INPUT_TYPES()
    rename_params = {**rename.get("required", {}), **rename.get("optional", {})}
    assert "src" in rename_params and "dst" in rename_params

    select = NODE_CLASS_MAPPINGS[SELECT].INPUT_TYPES()
    select_params = {**select.get("required", {}), **select.get("optional", {})}
    assert "keys" in select_params


def test_the_contract_ops_chain_record_to_record() -> None:
    """They are inserted INTO the chain, so each must take and return a record."""
    for class_type in (RENAME, SELECT):
        spec = NODE_CLASS_MAPPINGS[class_type].INPUT_TYPES()
        named = {**spec.get("required", {}), **spec.get("optional", {})}
        assert _const("RECORD_SLOT") in named, class_type
        assert RECORD_TYPE in NODE_CLASS_MAPPINGS[class_type].RETURN_TYPES, class_type


def test_select_fields_closes_the_contract_by_dropping_the_rest() -> None:
    """`SelectFields` is what makes the boundary a PROMISE rather than a suggestion: after it the
    record holds the promised entries and nothing else. If it ever stopped dropping, the rectangle
    would silently state a contract it does not enforce."""
    from recordstream.ops.structure import SelectFields

    assert SelectFields(keys=["text", "image"])({"text": 3, "image": "img", "path": "/x.png", "split": "train"}) == {
        "text": 3,
        "image": "img",
    }


def test_rename_field_moves_an_entry_under_its_promoted_label() -> None:
    from recordstream.ops.structure import RenameField

    assert RenameField(src="label", dst="text")({"label": 3, "input": "img"}) == {"text": 3, "input": "img"}


def test_the_extension_declares_the_boundary_reader() -> None:
    source = EXTENSION.read_text()
    assert "function outputPromises(" in source
    assert "function buildRecordFromOutputs(" in source


def test_the_extension_uses_the_boundary_slots_own_connect() -> None:
    """`node.connect(slot, outputNode, i)` returns null against a subgraph boundary (measured).

    Using it left the terminator's `op` unconnected while the instance socket existed — the socket
    was there and the wire the user looked for was not. Pinned as source structure because there is
    no JS harness; the live check is recorded in this module's docstring.
    """
    source = EXTENSION.read_text()
    assert "boundary.connect(terminator.outputs[0], terminator)" in source


def test_the_menu_item_is_added_at_the_CANVAS_level() -> None:
    """A subgraph instance is typed by the subgraph's uuid, so the per-node-type hook never fires."""
    # Named in the comment that explains WHY, never used as a hook.
    source = EXTENSION.read_text()
    assert "getNodeMenuOptions" in source
    assert "beforeRegisterNodeDef(" not in source
