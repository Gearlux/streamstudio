"""A Taidal node this installation does not register REFUSES the export (2026-09-06).

`workflow_to_yaml` labelled every unregistered node "native ComfyUI node" and dropped it
into a leading comment block. A canvas naming a Taidal op the running server has not
registered — normally an op added AFTER the editor started, since the node registry is a
snapshot taken at startup — therefore exported as a silently shortened chain.

Measured live 2026-09-05: an editor started at 15:34 converted a twelve-node Bluetooth
canvas into `WindowSignal` alone, dropping eleven `Taidal_waivefront_op_*` nodes, and the
consuming workspace reported `view graph 'bte' has no record chain to run` — a message
three layers from the cause, which cost three rounds to trace. The node key carries the
answer: `bridge._node_key` mints every discovered node as `Taidal_<package>_<category>…`,
so an unregistered `Taidal_*` name is an installation out of step with the canvas, while
an unregistered bare name is a real ComfyUI node and still drops as it always did.
"""

from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

import pytest

import streamstudio  # noqa: F401  (populates NODE_CLASS_MAPPINGS)
from streamstudio import NODE_CLASS_MAPPINGS
from streamstudio.export import UnsupportedTopologyError
from streamstudio.graphio import workflow_to_yaml

#: A real, registered Taidal op — the canvas below is valid except for what is added to it.
KNOWN = "Taidal_recordstream_op_contract_RecordContract"

#: Node keys a canvas names that a STALE server would not have registered. They are real
#: (this venv registers all three), which is the point: the failure is not a bad canvas but
#: a server whose registry predates the op, so the tests reproduce it by taking the entries
#: back OUT of the mapping the converter is handed — exactly the older server's view.
NEWER_THAN_THE_SERVER = (
    "Taidal_waivefront_op_measure_TuneToBand",
    "Taidal_waivefront_op_measure_SelectBurst",
    "Taidal_waivefront_op_bluetooth_ClassifyBluetooth",
)


def _prompt(*class_types: str) -> Dict[str, Any]:
    """A prompt-form workflow of unwired nodes, one per class type given."""
    return {
        str(index): {"class_type": class_type, "inputs": {}} for index, class_type in enumerate(class_types, start=1)
    }


def _stale(*without: str) -> Dict[str, Any]:
    """The node mapping a server started BEFORE those ops would have had."""
    return {key: value for key, value in NODE_CLASS_MAPPINGS.items() if key not in without}


def _skipped(exported: str) -> List[str]:
    """The class types the leading comment block says were left out."""
    return [line.split("(")[1].split(")")[0] for line in exported.splitlines() if line.startswith("#   - node ")]


class TestAnUnregisteredTaidalNodeRefuses:
    def test_it_raises_instead_of_shortening_the_chain(self) -> None:
        node = NEWER_THAN_THE_SERVER[0]
        assert node in NODE_CLASS_MAPPINGS, "the op is real; only the stale server lacks it"
        with pytest.raises(UnsupportedTopologyError):
            workflow_to_yaml(_prompt(KNOWN, node), _stale(node))

    def test_the_message_names_the_node_and_says_what_to_do(self) -> None:
        """The whole point: the FIRST message a reader sees carries the node type and the
        remedy, instead of an empty chain reported by whatever consumes the export."""
        node = NEWER_THAN_THE_SERVER[0]
        with pytest.raises(UnsupportedTopologyError) as caught:
            workflow_to_yaml(_prompt(KNOWN, node), _stale(node))
        message = str(caught.value)
        assert "Taidal_waivefront_op_measure_TuneToBand" in message
        assert "node 2" in message, "the canvas's own node id, so it can be found"
        assert "restart" in message.lower()

    def test_several_missing_nodes_are_all_named(self) -> None:
        """Eleven were missing in the case this exists for; naming one and hiding ten would
        send the reader round the loop again."""
        missing = list(NEWER_THAN_THE_SERVER)
        with pytest.raises(UnsupportedTopologyError) as caught:
            workflow_to_yaml(_prompt(KNOWN, *missing), _stale(*missing))
        for class_type in missing:
            assert class_type in str(caught.value)


class TestWhatMustStillConvert:
    """Three shapes drop legitimately and must be untouched — the refusal is narrow."""

    def test_a_real_native_comfyui_node_still_drops_silently(self) -> None:
        """No `Taidal_` prefix means the canvas genuinely holds a ComfyUI node (a viewer, a
        primitive). It is listed in the comment block, exactly as before."""
        exported = workflow_to_yaml(_prompt(KNOWN, "PreviewImage"), NODE_CLASS_MAPPINGS)
        assert "PreviewImage" in _skipped(exported)
        assert "RecordContract" in exported

    def test_a_registered_canvas_only_adapter_still_drops_silently(self) -> None:
        """`Taidal_SubgraphOp` carries the prefix but IS registered, so it never reaches the
        new branch — it lands in the canvas-only-adapter one. Every subgraph canvas the
        workspace ships contains it, so getting this wrong would refuse all of them."""
        assert "Taidal_SubgraphOp" in NODE_CLASS_MAPPINGS
        exported = workflow_to_yaml(_prompt(KNOWN, "Taidal_SubgraphOp"), NODE_CLASS_MAPPINGS)
        assert "Taidal_SubgraphOp" in _skipped(exported)

    @pytest.mark.parametrize("adapter", ["Taidal_MixRecords", "Taidal_Value", "Taidal_Math"])
    def test_the_other_hand_written_adapters_are_registered_too(self, adapter: str) -> None:
        assert adapter in NODE_CLASS_MAPPINGS
        workflow_to_yaml(_prompt(KNOWN, adapter), NODE_CLASS_MAPPINGS)

    def test_an_all_taidal_canvas_is_unchanged(self) -> None:
        exported = workflow_to_yaml(_prompt(KNOWN), NODE_CLASS_MAPPINGS)
        assert not exported.startswith("#"), "nothing skipped, so no comment block at all"


class TestTheShippedCanvasesConvertWhole:
    """The two canvases the failure was seen on. Gated on the domain package being
    installed; the artifacts ship there."""

    @pytest.mark.parametrize("slot, ops", [("view_repeat", 3), ("view_bte", 12)])
    def test_the_signal_domain_view_graphs_drop_nothing(self, slot: str, ops: int) -> None:
        import json

        annotation = pytest.importorskip("waivefront.annotation")
        path = annotation.templates()["signal"][slot]
        exported = workflow_to_yaml(json.loads(path.read_text()), NODE_CLASS_MAPPINGS)
        assert [c for c in _skipped(exported) if c not in NODE_CLASS_MAPPINGS] == []
        assert exported.count("_target_:") == ops + 1, "every op plus the Stream"

    def test_the_same_canvas_on_a_stale_server_refuses_by_name(self) -> None:
        """The measured case, reproduced whole: the twelve-node Bluetooth canvas against a
        registry missing the eleven ops added after the server started exported as
        `WindowSignal` alone. It now refuses, naming them."""
        import json

        annotation = pytest.importorskip("waivefront.annotation")
        canvas = json.loads(Path(annotation.templates()["signal"]["view_bte"]).read_text())
        # The CANVAS's own measure/bluetooth ops — not every such node the registry happens to
        # carry today, which grows as the domain adds nodes and has nothing to do with this canvas.
        newer = sorted(
            {
                node["type"]
                for node in canvas["nodes"]
                if node["type"].startswith(("Taidal_waivefront_op_measure_", "Taidal_waivefront_op_bluetooth_"))
            }
        )
        assert len(newer) == 11, "the eleven ops added after the stale server started"
        with pytest.raises(UnsupportedTopologyError) as caught:
            workflow_to_yaml(canvas, _stale(*newer))
        message = str(caught.value)
        assert "Taidal_waivefront_op_measure_TuneToBand" in message
        assert "Taidal_waivefront_op_bluetooth_BluetoothVerdict" in message


class TestTheCliSaysItRatherThanTracing:
    """`streamstudio export-workflow` on such a canvas now FAILS (it used to write a partial
    YAML — user decision 2026-09-06). A traceback is not a message, so it exits with the
    refusal the same way a missing file does."""

    def test_it_exits_with_the_refusal_not_a_traceback(self, tmp_path: Path) -> None:
        import json

        from streamstudio.cli import export_workflow

        canvas = tmp_path / "canvas.json"
        canvas.write_text(json.dumps(_prompt(KNOWN, NEWER_THAN_THE_SERVER[0])))
        stale = _stale(NEWER_THAN_THE_SERVER[0])
        with mock.patch.object(streamstudio, "NODE_CLASS_MAPPINGS", stale):
            with pytest.raises(SystemExit) as caught:
                export_workflow(canvas, output=tmp_path / "out.yaml")
        assert NEWER_THAN_THE_SERVER[0] in str(caught.value)
        assert not (tmp_path / "out.yaml").exists(), "a partial export is never written"
