"""Dual-mode runnable nodes + SAMPLEFLUX_RUNNABLE input sockets (workflow combinators, Phase B).

A workflow COMBINATOR (sampleflux.workflow Sequence / Conditional / Switch) is a runnable that HOLDS
other runnables. FluxStudio renders its runnable-typed members as SAMPLEFLUX_RUNNABLE input sockets
(single, or a dynamic ``<name>_N`` family), and a runnable node feeding such a socket switches to
INSTANCE mode (built, not run) so the combinator runs the selected branch — no double-run.
"""

from typing import Any, List, Optional

from confluid import configurable

from fluxstudio.bridge import RUNNABLE_TYPE, _runnable_consumed_as_instance, create_comfy_node


@configurable
class _Runnable:
    """A minimal runnable (no-arg ``run()``)."""

    def __init__(self, tag: str = "") -> None:
        self.tag = tag
        self.ran = False

    def run(self) -> None:
        self.ran = True


@configurable
class _Combinator:
    """A fake workflow combinator: single + list runnable members (detected by NAME) + a scalar."""

    def __init__(
        self,
        if_true: Any = None,
        if_false: Any = None,
        steps: Optional[List[Any]] = None,
        max_epochs: int = 1,  # a scalar -> stays a widget, never a runnable socket
    ) -> None:
        self.if_true = if_true
        self.if_false = if_false
        self.steps = list(steps) if steps else []
        self.max_epochs = max_epochs

    def run(self) -> None:  # pragma: no cover - patched out in execute tests
        pass


def _node() -> Any:
    return create_comfy_node(_Combinator, "Combinator")


def _run_recorder(store: List[Any]) -> Any:
    """A ``run_runnable`` stub that records the runnable it was asked to run (no actual run)."""

    def _run(obj: Any, uid: Any = None) -> str:
        store.append(obj)
        return "ran"

    return _run


# --------------------------------------------------------------------------- #
# Socket rendering
# --------------------------------------------------------------------------- #
def test_single_runnable_members_are_runnable_sockets() -> None:
    opt = _node().INPUT_TYPES().get("optional", {})
    assert opt["if_true"][0] == RUNNABLE_TYPE
    assert opt["if_false"][0] == RUNNABLE_TYPE


def test_runnable_list_member_seeds_indexed_socket() -> None:
    opt = _node().INPUT_TYPES().get("optional", {})
    assert opt["steps_0"][0] == RUNNABLE_TYPE
    assert "steps" not in opt  # the list param itself never becomes a widget


def test_scalar_member_stays_a_widget_not_a_runnable_socket() -> None:
    spec = _node().INPUT_TYPES()
    entry = spec.get("required", {}).get("max_epochs") or spec.get("optional", {}).get("max_epochs")
    assert entry is not None and entry[0] != RUNNABLE_TYPE


def test_runnable_node_declares_prompt_hidden_input() -> None:
    assert _node().INPUT_TYPES().get("hidden", {}).get("prompt") == "PROMPT"


def test_runnable_node_output_slot0_is_runnable() -> None:
    node = _node()
    assert node.RETURN_TYPES[0] == RUNNABLE_TYPE
    assert getattr(node, "OUTPUT_NODE", False) is True


# --------------------------------------------------------------------------- #
# execute: collection + dual-mode
# --------------------------------------------------------------------------- #
def test_execute_collects_indexed_steps_in_order(monkeypatch: Any) -> None:
    ran: List[Any] = []
    monkeypatch.setattr("fluxstudio.nodes.run_runnable", _run_recorder(ran))
    r1, r2 = _Runnable("1"), _Runnable("2")
    res = _node()().execute(steps_0=r1, steps_1=r2)
    obj = res["result"][0]
    assert obj.steps == [r1, r2]  # collected in slot order
    assert len(ran) == 1  # terminal mode -> run_runnable called once


def test_execute_terminal_runs_when_not_consumed(monkeypatch: Any) -> None:
    ran: List[Any] = []
    monkeypatch.setattr("fluxstudio.nodes.run_runnable", _run_recorder(ran))
    _node()().execute()  # no prompt -> not consumed -> terminal
    assert len(ran) == 1


def test_execute_dual_mode_returns_instance_without_running(monkeypatch: Any) -> None:
    ran: List[Any] = []
    monkeypatch.setattr("fluxstudio.nodes.run_runnable", _run_recorder(ran))
    monkeypatch.setattr("fluxstudio.bridge._runnable_consumed_as_instance", lambda *a, **k: True)
    r1 = _Runnable("t")
    res = _node()().execute(if_true=r1)
    obj = res["result"][0]
    assert isinstance(obj, _Combinator) and obj.if_true is r1
    assert ran == []  # consumed -> NOT run; the consuming combinator runs it


# --------------------------------------------------------------------------- #
# _runnable_consumed_as_instance
# --------------------------------------------------------------------------- #
def _consumer(input_name: str, input_type: str) -> Any:
    class _C:
        @classmethod
        def INPUT_TYPES(cls) -> Any:
            return {"optional": {input_name: (input_type,)}}

    return _C


def test_consumed_true_for_runnable_input() -> None:
    prompt = {"9": {"class_type": "Cond", "inputs": {"if_true": ["5", 0]}}}
    assert _runnable_consumed_as_instance(prompt, "5", {"Cond": _consumer("if_true", RUNNABLE_TYPE)}) is True


def test_consumed_false_for_non_runnable_input() -> None:
    prompt = {"9": {"class_type": "Obj", "inputs": {"model": ["5", 0]}}}
    assert _runnable_consumed_as_instance(prompt, "5", {"Obj": _consumer("model", "SAMPLEFLUX_OBJECT")}) is False


def test_consumed_false_without_prompt() -> None:
    assert _runnable_consumed_as_instance(None, "5", {}) is False
    assert _runnable_consumed_as_instance({}, "5", {}) is False


def test_consumed_resolves_dynamic_indexed_slot_to_seed() -> None:
    prompt = {"9": {"class_type": "Seq", "inputs": {"steps_3": ["5", 0]}}}
    assert _runnable_consumed_as_instance(prompt, "5", {"Seq": _consumer("steps_0", RUNNABLE_TYPE)}) is True


def test_consumed_ignores_non_slot0_link() -> None:
    # A link from our @output (slot 1), not the runnable (slot 0), is not instance-consumption.
    prompt = {"9": {"class_type": "Cond", "inputs": {"if_true": ["5", 1]}}}
    assert _runnable_consumed_as_instance(prompt, "5", {"Cond": _consumer("if_true", RUNNABLE_TYPE)}) is False
