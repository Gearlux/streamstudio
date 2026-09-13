"""Decision branches on the canvas — drawn as the test node's OUTCOME sockets, joined back by a
`Join branches` node, folded to the linear chain recordstream runs (user design 2026-09-06:
"the graph is the truth and should visually represent what is executed with branches and
test"; the flow document keeps its `requires:` form — the converter's business, never the
user's).

The picture, for the shipped Bluetooth chain::

    … → ClassifyBluetooth ├ ble         ─▶ DecodeBleAdvertising ────┐
                          ├ bredr       ─▶ DecodeClassicAccessCode ─┤
                          ├ try_weak    ─▶ CorrelateAccessAddress ──┼─▶ Join branches ─▶ BluetoothVerdict
                          └ try_classic ─▶ SearchClassicAccessCode ─┘

What runs is the chain: each branch node carries the flag that opens it, a closed gate passes
the record through, and no branch is computed "just in case". These tests use the real
Bluetooth nodes — the branch grammar exists for them — and skip without the signal package.
"""

from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

import streamstudio  # noqa: F401  (populates NODE_CLASS_MAPPINGS)
from streamstudio import NODE_CLASS_MAPPINGS
from streamstudio.bridge import _build_op_node
from streamstudio.export import JOIN_CLASS_TYPE, UnsupportedTopologyError, compile_flow_documents, fold_branches
from streamstudio.graphio import flow_yaml_to_workflow, workflow_to_yaml

waivefront = pytest.importorskip("waivefront")
confluid = pytest.importorskip("confluid")

RECORD = "RECORDSTREAM_RECORD"


def _bte_yaml() -> str:
    from waivefront.annotation import templates

    return Path(templates()["signal"]["view_bte"]).with_suffix(".yaml").read_text()


def _class_type(class_name: str) -> str:
    """The canvas node key for a wrapped op class, by its bare class name."""
    for key, node_cls in NODE_CLASS_MAPPINGS.items():
        path = getattr(node_cls, "TAIDAL_CALLABLE_PATH", "") or ""
        if (
            path.rsplit(":", 1)[-1].rsplit(".", 1)[-1] == class_name
            and getattr(node_cls, "TAIDAL_NODE_KIND", "") == "op"
        ):
            return key
    raise KeyError(class_name)


def _nodes_by_class(workflow: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for node in workflow["nodes"]:
        out[str(node["type"]).rsplit("_", 1)[-1]] = node
    return out


def _link_source(workflow: Dict[str, Any], node: Dict[str, Any], input_name: str) -> Tuple[int, str]:
    """``(source node id, source output NAME)`` of the link into ``node.input_name``."""
    link_id = next(inp["link"] for inp in node["inputs"] if inp["name"] == input_name)
    link = next(lk for lk in workflow["links"] if lk[0] == link_id)
    source = next(n for n in workflow["nodes"] if n["id"] == link[1])
    return int(link[1]), source["outputs"][link[2]]["name"]


class TestTheOutcomeSockets:
    """A test node's declared outcomes (`flags`) are RECORD outputs — one per outcome, named by it."""

    def test_the_classifier_offers_one_record_output_per_outcome(self) -> None:
        node_cls = NODE_CLASS_MAPPINGS[_class_type("ClassifyBluetooth")]
        names = list(node_cls.RETURN_NAMES)
        types = list(node_cls.RETURN_TYPES)
        assert names[:2] == ["record", "op"]
        assert names[2:] == ["fsk", "ble", "bredr", "try_weak", "try_classic"], "the outcomes, in the class's own order"
        assert types[2:] == [RECORD] * 5, "an outcome carries the RECORD, so a branch hangs off it"

    def test_a_branch_node_has_no_gate_to_type(self) -> None:
        """`requires` is the converter's word: no widget, no socket — the wire is the gate."""
        for name in ("DecodeBleAdvertising", "DecodeClassicAccessCode", "CorrelateAccessAddress"):
            spec = NODE_CLASS_MAPPINGS[_class_type(name)].INPUT_TYPES()
            declared = set(spec.get("required", {})) | set(spec.get("optional", {}))
            assert "requires" not in declared, f"{name} still shows a gate to type"

    def test_only_the_taken_outcome_carries_the_record(self) -> None:
        """Executed on the canvas, an outcome socket that did not fire emits None — a node wired
        from it receives no record, so a branch not taken computes nothing."""

        class Split:
            flags = ("hot", "cold")

            def __init__(self, threshold: float = 0.0) -> None:
                self.threshold = threshold
                self._hot = False

            def __call__(self, record: Dict[str, Any]) -> Dict[str, Any]:
                self._hot = float(record["t"]) > self.threshold
                return record

            @property
            def hot(self) -> bool:
                return self._hot

            @property
            def cold(self) -> bool:
                return not self._hot

        node_cls = _build_op_node(Split, "Split", "test")
        assert list(node_cls.RETURN_NAMES) == ["record", "op", "hot", "cold"]

        def run(record: Dict[str, Any]) -> Tuple[Any, ...]:
            out = node_cls().execute(record=record)
            return tuple(out["result"]) if isinstance(out, dict) else tuple(out)  # ComfyUI's {ui, result} envelope

        outputs = run({"t": 5.0})
        assert outputs[2] == {"t": 5.0} and outputs[3] is None
        outputs = run({"t": -1.0})
        assert outputs[2] is None and outputs[3] == {"t": -1.0}


class TestTheJoinNode:
    def test_it_is_registered_with_growable_record_inputs(self) -> None:
        node_cls = NODE_CLASS_MAPPINGS[JOIN_CLASS_TYPE]
        spec = node_cls.INPUT_TYPES()
        assert spec["required"] == {"branch_1": (RECORD,)}
        assert list(spec["optional"]) == [f"branch_{i}" for i in range(2, 7)]
        assert node_cls.RETURN_TYPES == (RECORD,) and node_cls.RETURN_NAMES == ("record",)

    def test_it_passes_whichever_branch_ran(self) -> None:
        node = NODE_CLASS_MAPPINGS[JOIN_CLASS_TYPE]()
        assert node.join(branch_1=None, branch_2={"a": 1}, branch_3=None) == ({"a": 1},)
        assert node.join(branch_1=None) == (None,)


class TestTheCanvasDrawsTheBranches:
    """Importing the shipped (linear, `requires:`-gated) document draws the picture above."""

    def _workflow(self) -> Dict[str, Any]:
        return flow_yaml_to_workflow(_bte_yaml(), NODE_CLASS_MAPPINGS, as_="graph", name="bte_view")

    def test_every_op_is_drawn_and_each_decision_gets_its_join(self) -> None:
        """Every op node of the chain, plus ONE join per decision (the importer's two preview
        adapters after the final output are not part of the analysis path).

        The shipped Bluetooth chain draws TWO decisions — the burst pick's three finders and
        the classifier's three decoders — so two joins. The op count moves whenever the domain
        adds a step; it is updated here rather than relaxed, because a DROPPED node is exactly
        what this file exists to catch."""
        workflow = self._workflow()
        joins = [n for n in workflow["nodes"] if n["type"] == JOIN_CLASS_TYPE]
        ops = [n for n in workflow["nodes"] if str(n["type"]).startswith("Taidal_waivefront")]
        assert len(ops) == 22 and len(joins) == 2

    def test_each_branch_hangs_off_its_outcome(self) -> None:
        workflow = self._workflow()
        nodes = _nodes_by_class(workflow)
        classifier = nodes["ClassifyBluetooth"]["id"]
        for branch, outcome in (
            ("DecodeBleAdvertising", "ble"),
            ("DecodeClassicAccessCode", "bredr"),
            ("CorrelateAccessAddress", "try_weak"),
            ("SearchClassicAccessCode", "try_classic"),
        ):
            assert _link_source(workflow, nodes[branch], "record") == (classifier, outcome)

    def test_the_branches_rejoin_before_the_verdict(self) -> None:
        workflow = self._workflow()
        nodes = _nodes_by_class(workflow)
        # The DECODERS' join, not the burst finders' — the chain draws two decisions, and the
        # first join in node order is whichever the converter placed first.
        join = next(
            n
            for n in workflow["nodes"]
            if n["type"] == JOIN_CLASS_TYPE
            and _link_source(workflow, n, "branch_1")[0] == nodes["DecodeBleAdvertising"]["id"]
        )
        feeders = [_link_source(workflow, join, f"branch_{i}")[0] for i in (1, 2, 3, 4)]
        # The third feeder is the DEEP search, not the weak one: the deep search is a decision
        # NESTED inside the weak branch, and a nested decision's tail joins in its place.
        assert feeders == [
            nodes["DecodeBleAdvertising"]["id"],
            nodes["DecodeClassicAccessCode"]["id"],
            nodes["SearchAccessAddress"]["id"],
            nodes["SearchClassicAccessCode"]["id"],
        ]
        assert _link_source(workflow, nodes["BluetoothVerdict"], "record") == (join["id"], "record")

    def test_no_gate_is_written_on_any_node(self) -> None:
        for node in self._workflow()["nodes"]:
            assert "requires" not in [inp["name"] for inp in node["inputs"]]
            assert "ble" not in (node.get("widgets_values") or [])

    def test_an_outcome_nothing_earlier_decides_is_refused_by_name(self) -> None:
        doc = _bte_yaml().replace("requires: ble", "requires: nope")
        with pytest.raises(UnsupportedTopologyError) as err:
            flow_yaml_to_workflow(doc, NODE_CLASS_MAPPINGS, as_="graph", name="bte_view")
        message = str(err.value)
        assert "'nope'" in message and "ble" in message and "ClassifyBluetooth" in message

    def test_an_ungated_chain_draws_no_join(self) -> None:
        """CON case: the sixteen plotting templates stay the straight chains they were."""
        from waivefront.annotation import templates

        source = Path(templates()["signal"]["view_fft"]).with_suffix(".yaml")
        if not source.is_file():
            pytest.skip("the fft template ships no YAML source")
        workflow = flow_yaml_to_workflow(source.read_text(), NODE_CLASS_MAPPINGS, as_="graph", name="fft")
        assert not [n for n in workflow["nodes"] if n["type"] == JOIN_CLASS_TYPE]


def _name(target: Any) -> str:
    """A marker's target as its bare class name — a class object or a dotted path."""
    return target.__name__ if isinstance(target, type) else str(target).rsplit(".", 1)[-1]


def _plain(value: Any) -> Any:
    """Numbers as numbers: the source YAML spells ``4.0e6`` as a string, the export as a float."""
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _ops_of(document_text: str) -> List[Tuple[str, Dict[str, Any]]]:
    """``[(class name, kwargs)]`` of the ONE Stream's ops in a whole-graph export."""
    document = confluid.load(document_text, until="document")
    stream = next(v for v in document.values() if getattr(v, "target", None) is not None)
    return [(_name(op.target), {k: _plain(v) for k, v in op.kwargs.items()}) for op in stream.kwargs["ops"]]


def _steps_of(yaml_text: str) -> List[Tuple[str, Dict[str, Any]]]:
    document = confluid.load(yaml_text, until="document")
    out = []
    for step in document["flow"].values():
        op = step["op"]
        out.append((_name(op.target), {k: _plain(v) for k, v in op.kwargs.items()}))
    return out


class TestTheChainFoldsBack:
    """Both exporters turn the drawn branches back into the linear, gated chain."""

    def test_the_whole_graph_export_equals_the_source_document(self) -> None:
        """Same ops in the same order with the same gates and values — a source kwarg that
        merely restates the class default is not on the canvas (the importer keeps only what
        differs), so it is compared against the default it equals."""
        import inspect

        from waivefront import processing

        workflow = flow_yaml_to_workflow(_bte_yaml(), NODE_CLASS_MAPPINGS, as_="graph", name="bte_view")
        exported = _ops_of(workflow_to_yaml(workflow, NODE_CLASS_MAPPINGS))
        source = _steps_of(_bte_yaml())
        assert [name for name, _ in exported] == [name for name, _ in source]
        for (name, got), (_, wanted) in zip(exported, source):
            cls = getattr(processing, name)
            # A value the canvas does not carry falls back to the op's OWN default — what the
            # constructor stores for it (a `None` default the op turns into its built-in table).
            built = cls()
            for key, value in wanted.items():
                fallback = _plain(getattr(built, key, inspect.signature(cls).parameters[key].default))
                assert got.get(key, fallback) == value, f"{name}.{key}: exported {got.get(key)!r}, source {value!r}"
            assert not set(got) - set(wanted), f"{name} exports values the source never set: {set(got) - set(wanted)}"

    def test_the_flow_export_is_linear_and_gated(self) -> None:
        from streamstudio.graphio import ui_workflow_to_prompt

        workflow = flow_yaml_to_workflow(_bte_yaml(), NODE_CLASS_MAPPINGS, as_="graph", name="bte_view")
        prompt = ui_workflow_to_prompt(workflow, NODE_CLASS_MAPPINGS)
        ((flow_map, outputs),) = compile_flow_documents(prompt, NODE_CLASS_MAPPINGS)
        steps = list(flow_map.values())
        assert all("merge_from" not in s and "bind" not in s for s in steps)
        gates = {_name(s["op"].target): s["op"].kwargs.get("requires") for s in steps if "op" in s}
        assert gates["DecodeBleAdvertising"] == "ble"
        assert gates["DecodeClassicAccessCode"] == "bredr"
        assert gates["CorrelateAccessAddress"] == "try_weak"
        assert gates["SearchClassicAccessCode"] == "try_classic"
        assert not any(JOIN_CLASS_TYPE in str(name) for name in flow_map), "the join is a drawing, not a step"


def _op(class_type: str, record: Any = None, **widgets: Any) -> Dict[str, Any]:
    inputs: Dict[str, Any] = dict(widgets)
    if record is not None:
        inputs["record"] = record
    return {"class_type": class_type, "inputs": inputs}


def _join(*sources: str) -> Dict[str, Any]:
    return {"class_type": JOIN_CLASS_TYPE, "inputs": {f"branch_{i + 1}": [src, 0] for i, src in enumerate(sources)}}


class TestWhatTheEditorRefuses:
    """Each refusal names the nodes in the user's words; none mentions the converter's `requires`."""

    def test_a_join_fed_by_nodes_that_decide_nothing(self) -> None:
        prompt = {
            "1": _op(_class_type("SelectBurst")),
            "2": _op(_class_type("MeasureEnvelope"), record=["1", 0]),
            "3": _op(_class_type("MeasureInstantaneousFrequency"), record=["1", 0]),
            "4": _join("2", "3"),
            "5": _op(_class_type("BluetoothVerdict"), record=["4", 0]),
        }
        with pytest.raises(UnsupportedTopologyError) as err:
            fold_branches(prompt, NODE_CLASS_MAPPINGS)
        message = str(err.value)
        assert (
            "Join branches" in message and "MeasureEnvelope" in message and "MeasureInstantaneousFrequency" in message
        )
        assert "decide" in message and "requires" not in message

    def test_a_node_that_cannot_be_gated_on_a_branch(self) -> None:
        classify = _class_type("ClassifyBluetooth")
        ble_slot = list(NODE_CLASS_MAPPINGS[classify].RETURN_NAMES).index("ble")
        prompt = {
            "1": _op(classify),
            "2": _op(_class_type("MeasureEnvelope"), record=["1", ble_slot]),
        }
        with pytest.raises(UnsupportedTopologyError) as err:
            fold_branches(prompt, NODE_CLASS_MAPPINGS)
        message = str(err.value)
        assert "MeasureEnvelope" in message and "'ble'" in message and "ClassifyBluetooth" in message
        assert "requires" not in message

    def test_a_branch_that_forks(self) -> None:
        classify = _class_type("ClassifyBluetooth")
        ble_slot = list(NODE_CLASS_MAPPINGS[classify].RETURN_NAMES).index("ble")
        decode = _class_type("DecodeBleAdvertising")
        prompt = {
            "1": _op(classify),
            "2": _op(decode, record=["1", ble_slot]),
            "3": _op(_class_type("DecodeClassicAccessCode"), record=["2", 0]),
            "4": _op(_class_type("CorrelateAccessAddress"), record=["2", 0]),
        }
        with pytest.raises(UnsupportedTopologyError) as err:
            fold_branches(prompt, NODE_CLASS_MAPPINGS)
        assert "forks" in str(err.value) and "DecodeBleAdvertising" in str(err.value)

    def test_a_plain_fan_out_still_refuses_as_before(self) -> None:
        """A fork that is NOT a decision (two consumers of one record output) keeps today's answer."""
        prompt = {
            "1": _op(_class_type("SelectBurst")),
            "2": _op(_class_type("MeasureEnvelope"), record=["1", 0]),
            "3": _op(_class_type("MeasureInstantaneousFrequency"), record=["1", 0]),
        }
        folded = fold_branches(prompt, NODE_CLASS_MAPPINGS)
        assert folded == prompt, "nothing to fold — the exporter's own branchy refusal applies"


class TestTheContractIsCheckedAtExport:
    """The editor honours each node's input/output contract: a chain that skips a producer is
    refused where it is drawn, with the record engine's own located message."""

    def _prompt(self, skip_ifreq: bool) -> Dict[str, Any]:
        prompt: Dict[str, Any] = {
            "1": _op(_class_type("WindowSignal")),
            "2": _op(_class_type("TuneToBand"), record=["1", 0]),
            "3": _op(_class_type("SelectBurst"), record=["2", 0]),
            "4": _op(_class_type("MeasureEnvelope"), record=["3", 0]),
        }
        previous = "4"
        if not skip_ifreq:
            prompt["5"] = _op(_class_type("MeasureInstantaneousFrequency"), record=["4", 0])
            previous = "5"
        prompt["6"] = _op(_class_type("MeasureSymbolClock"), record=[previous, 0])
        prompt["7"] = _op(_class_type("MeasureDeviation"), record=["6", 0])
        return prompt

    def test_a_skipped_producer_is_refused_by_the_engines_words(self) -> None:
        with pytest.raises(UnsupportedTopologyError) as err:
            workflow_to_yaml(self._prompt(skip_ifreq=True), NODE_CLASS_MAPPINGS)
        message = str(err.value)
        # The FIRST node whose need is unmet is named — the clock reads the frequency trace.
        assert "MeasureSymbolClock" in message and "'inst_freq'" in message and "produces" in message

    def test_the_good_chain_passes(self) -> None:
        text = workflow_to_yaml(self._prompt(skip_ifreq=False), NODE_CLASS_MAPPINGS)
        assert [name for name, _ in _ops_of(text)][-2:] == ["MeasureSymbolClock", "MeasureDeviation"]

    def test_the_flow_export_checks_it_too(self) -> None:
        with pytest.raises(UnsupportedTopologyError) as err:
            compile_flow_documents(self._prompt(skip_ifreq=True), NODE_CLASS_MAPPINGS)
        assert "'inst_freq'" in str(err.value)
