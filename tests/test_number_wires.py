"""A node whose class IS a number feeds a number setting (user request 2026-10-09: "that graph should have a node that
can convert the unit to the rate and connect that to the upsample rate … make this node generic"; user pick 2: the
editor accepts the wire from ANY node whose result is a number, not this one node alone).

A value node offers its built instance as its ``object`` output. When that instance is a number, a text, a whole
number or a yes/no — its class a subclass of ``float``, ``str``, ``int`` or ``bool``, as recordstream's
``Quantity`` (``2.5 MSa/s`` → ``2500000.0``) is a ``float`` — the output is typed so, and meets the setting of that
type, which every parameter row offers a handle for. Measured before: ``up_rate.object (object) cannot be wired into
upsample_rate.samplerate (number): the types do not meet``.
"""

from pathlib import Path

import pytest
from confluid import configurable
from recordstream.quantity import Quantity

from streamstudio.document import Document
from streamstudio.palette import describe

MOD = __name__


@configurable(group="number-wire-test")
class Taker:
    """A card with settings of each kind.

    Args:
        rate: A rate in Hz.
        count: A whole number.
        name: A text.
    """

    def __init__(self, rate: float = 0.0, count: int = 1, name: str = "") -> None:
        self.rate = rate
        self.count = count
        self.name = name


@configurable(category="value", group="number-wire-test")
class Whole(int):
    """A whole number, as a node.

    Args:
        value: The number.
    """

    def __new__(cls, value: int = 0) -> "Whole":
        return super().__new__(cls, value)

    def __init__(self, value: int = 0) -> None:
        self.value = value


@configurable(category="value", group="number-wire-test")
class Thing:
    """A value node that is no number.

    Args:
        size: Its size.
    """

    def __init__(self, size: float = 1.0) -> None:
        self.size = size


GRAPH = f"""\
rate_value: !class:recordstream.quantity.Quantity
  value: 2.5 MSa/s
  kind: rate
count_value: !class:{MOD}.Whole
  value: 3
thing: !class:{MOD}.Thing {{}}
taker: !class:{MOD}.Taker {{}}
"""


def _open(tmp_path: Path) -> Document:
    path = tmp_path / "graph.yaml"
    path.write_text(GRAPH)
    return Document.open(path)


class TestTheOutput:
    def test_a_number_node_offers_a_number(self) -> None:
        assert [(s["name"], s["type"]) for s in describe(Quantity).outputs] == [("object", "number")]
        assert [(s["name"], s["type"]) for s in describe(Whole).outputs] == [("object", "integer")]

    def test_CON_a_node_that_is_no_number_still_offers_an_object(self) -> None:
        assert [(s["name"], s["type"]) for s in describe(Thing).outputs] == [("object", "object")]

    def test_the_documents_card_says_the_same(self, tmp_path: Path) -> None:
        nodes = {node.name: node for node in _open(tmp_path).model.nodes}
        assert [(o.name, o.type) for o in nodes["rate_value"].outputs] == [("object", "number")]
        assert [(o.name, o.type) for o in nodes["thing"].outputs] == [("object", "object")]


class TestTheWire:
    def test_a_number_node_is_wired_into_a_number_setting(self, tmp_path: Path) -> None:
        document = _open(tmp_path)
        document.add_wire("rate_value", "object", "taker", "rate")
        assert f"taker: !class:{MOD}.Taker {{rate: !ref:rate_value }}\n" in document.text, "the card's own spelling"
        assert not document.model.problems
        assert [(w.source, w.target, w.target_socket) for w in document.model.wires] == [
            ("rate_value", "taker", "rate")
        ]

    def test_a_whole_number_meets_a_number_setting(self, tmp_path: Path) -> None:
        document = _open(tmp_path)
        document.add_wire("count_value", "object", "taker", "rate")
        assert f"taker: !class:{MOD}.Taker {{rate: !ref:count_value }}\n" in document.text

    def test_CON_a_number_does_not_meet_a_whole_number_or_a_text(self, tmp_path: Path) -> None:
        document = _open(tmp_path)
        with pytest.raises(
            ValueError, match=r"rate_value.object \(number\) cannot be wired into taker.count \(integer\)"
        ):
            document.add_wire("rate_value", "object", "taker", "count")
        with pytest.raises(
            ValueError, match=r"rate_value.object \(number\) cannot be wired into taker.name \(string\)"
        ):
            document.add_wire("rate_value", "object", "taker", "name")

    def test_CON_a_node_that_is_no_number_is_still_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match=r"thing.object \(object\) cannot be wired into taker.rate \(number\)"):
            _open(tmp_path).add_wire("thing", "object", "taker", "rate")
