"""A value producer's ``Any`` params are WIRED, and it emits itself so it can be connected.

The graph — not a text field, and not a JS picker — is what delivers a value like a class
vocabulary. That needs two things a value node did not have:

* a param that takes another NODE cannot be a widget, so a bare-``Any`` annotation renders as a
  wired input socket accepting a source or another producer's object;
* a producer must be connectable ONWARD, so every value node emits itself on a trailing
  ``object`` output.

Why the consumer holds the whole object rather than a reference to its attribute: attribute
references (``!ref:src.class_names``) were REMOVED from the config layer, and its own error names
the replacement — "give ``src``'s class a selector parameter and reference the whole object".
Measured: a document spelling the attribute form raises ``ConfigurationError``, while the
whole-object form loads and yields the vocabulary.
"""

from typing import Any, List, Optional

from confluid import configurable, output

from streamstudio.bridge import VALUE_INPUT_TYPE, _wired_value_params, create_comfy_node


def _run(node_cls: Any, **kwargs: Any) -> Any:
    """The output tuple — a generated node wraps ``execute`` as ``{"ui": …, "result": (…)}``."""
    returned = node_cls().execute(**kwargs)
    return returned["result"] if isinstance(returned, dict) else returned


@configurable(category="value")
class _Vocabulary:
    """A producer with one wired param, one widget param, and a declared output."""

    def __init__(self, carrier: Optional[Any] = None, prefix: str = "") -> None:
        self.carrier = carrier
        self.prefix = prefix

    @property
    @output
    def names(self) -> List[str]:
        found = getattr(self.carrier, "names", None) or []
        return [f"{self.prefix}{n}" for n in found]

    def __call__(self) -> None:
        return None


class TestWhichParamsAreWired:
    def test_a_bare_Any_param_is_wired(self) -> None:
        assert _wired_value_params(_Vocabulary) == ["carrier"]

    def test_a_typed_param_stays_a_widget(self) -> None:
        """The CON case: anything with a real annotation is still something you TYPE."""
        node = create_comfy_node(_Vocabulary, "Taidal__Vocabulary")
        assert "prefix" in node.INPUT_TYPES()["required"]
        assert "carrier" not in node.INPUT_TYPES()["required"]

    def test_the_wired_param_is_an_OPTIONAL_socket(self) -> None:
        """Optional so a template can PLACE the node before anything feeds it."""
        node = create_comfy_node(_Vocabulary, "Taidal__Vocabulary")
        assert node.INPUT_TYPES()["optional"]["carrier"] == (VALUE_INPUT_TYPE,)

    def test_the_socket_accepts_a_source_OR_another_producer(self) -> None:
        """A comma union — both ComfyUI halves read it as 'compatible when the sets intersect'."""
        assert "RECORDSTREAM_SOURCE" in VALUE_INPUT_TYPE and "RECORDSTREAM_OBJECT" in VALUE_INPUT_TYPE
        assert "," in VALUE_INPUT_TYPE


class TestTheProducerIsConnectableOnward:
    def test_it_emits_itself_after_its_declared_outputs(self) -> None:
        node = create_comfy_node(_Vocabulary, "Taidal__Vocabulary")
        assert node.RETURN_NAMES == ("names", "object")
        assert node.RETURN_TYPES[-1] == "RECORDSTREAM_OBJECT"

    def test_the_declared_outputs_keep_their_slot_indices(self) -> None:
        """A saved canvas addresses outputs positionally — the socket is APPENDED, never inserted."""
        node = create_comfy_node(_Vocabulary, "Taidal__Vocabulary")
        assert node.RETURN_NAMES[0] == "names"

    def test_execute_returns_the_live_object_on_that_slot(self) -> None:
        node = create_comfy_node(_Vocabulary, "Taidal__Vocabulary")

        class _Carrier:
            names = ["cat", "dog"]

        result = _run(node, carrier=_Carrier(), prefix="x-")
        assert result[-1].carrier is not None
        assert result[-1].names == ["x-cat", "x-dog"]

    def test_a_wired_value_reaches_the_constructor_uncoerced(self) -> None:
        """The wired object must pass through as-is — it is not a string to be parsed."""
        node = create_comfy_node(_Vocabulary, "Taidal__Vocabulary")
        carrier = object()
        assert _run(node, carrier=carrier, prefix="")[-1].carrier is carrier

    def test_an_unwired_param_keeps_the_constructor_default(self) -> None:
        node = create_comfy_node(_Vocabulary, "Taidal__Vocabulary")
        result = _run(node, prefix="")
        assert result[-1].carrier is None and result[0] == "[]"


class TestASourceCanSayWhatItKnows:
    """A source's declared ``@output``s ride alongside its ``source`` socket.

    A HuggingFace ``ClassLabel`` carries its class names; the source knowing that was invisible
    on the canvas until now — the node had exactly one output. Reported as *"I do not see an
    output node with class names on huggingfacesource"*.
    """

    def test_declared_outputs_are_appended_after_the_source_socket(self) -> None:
        from recordstream.sources.huggingface import HuggingFaceSource

        node = create_comfy_node(HuggingFaceSource, "Taidal_HF")
        assert node.RETURN_NAMES[0] == "source", "the source socket keeps slot 0"
        assert "class_names" in node.RETURN_NAMES

    def test_a_source_declaring_nothing_is_unchanged(self) -> None:
        """The CON case: most sources declare no @output and keep their single socket."""
        from recordstream.sources.range import RangeSource

        node = create_comfy_node(RangeSource, "Taidal_Range")
        assert node.RETURN_NAMES == ("source",)

    def test_the_vocabulary_rides_a_readable_STRING_not_an_opaque_object(self) -> None:
        """A container output renders as JSON you can preview; an object socket would only
        invite a wire that a config document cannot express."""
        from recordstream.sources.huggingface import HuggingFaceSource

        node = create_comfy_node(HuggingFaceSource, "Taidal_HF")
        assert node.RETURN_TYPES[node.RETURN_NAMES.index("class_names")] == "STRING"


@configurable(category="op")
class _StampOp:
    """An op with one wired-``Any`` param and one typed param — the ``PutField`` shape."""

    def __init__(self, key: str = "", value: Optional[Any] = None) -> None:
        self.key = key
        self.value = value

    def __call__(self, record: Any) -> Any:
        drawn = self.value() if callable(self.value) else self.value
        return {**record, self.key: drawn}


class TestAnOpsWiredAnyParam:
    """An OP node's bare-``Any`` param is a wired socket too — a producer docks into it.

    This is what lets a template decompose "stamp a random score" into a ``RandomNumber``
    producer wired into a ``PutField`` consumer instead of one fused op.
    """

    def test_a_bare_Any_op_param_is_an_OPTIONAL_socket(self) -> None:
        node = create_comfy_node(_StampOp, "Taidal__StampOp")
        assert node.INPUT_TYPES()["optional"]["value"] == (VALUE_INPUT_TYPE,)
        assert "value" not in node.INPUT_TYPES()["required"]

    def test_the_typed_param_stays_a_widget(self) -> None:
        node = create_comfy_node(_StampOp, "Taidal__StampOp")
        assert "key" in node.INPUT_TYPES()["required"]

    def test_a_wired_object_reaches_the_constructor_uncoerced(self) -> None:
        """The wired producer bypasses widget coercion — it is an object, not a string."""
        node = create_comfy_node(_StampOp, "Taidal__StampOp")
        returned = node().execute(key="score", value=lambda: 0.25)
        op = (returned["result"] if isinstance(returned, dict) else returned)[-1]
        assert op({"class": 7}) == {"class": 7, "score": 0.25}

    def test_an_unwired_socket_keeps_the_constructor_default(self) -> None:
        node = create_comfy_node(_StampOp, "Taidal__StampOp")
        returned = node().execute(key="score")
        op = (returned["result"] if isinstance(returned, dict) else returned)[-1]
        assert op.value is None


class TestAnObjectWireIntoAChainedOpSurvivesExport:
    """A producer's ``object`` output wired into a chained op exports as a WHOLE-NODE ``!ref:``.

    The declared ``@output`` slots fold at export — evaluated once into a frozen literal — which
    is exactly wrong for a ``random=True`` producer: every record would get the SAME draw. The
    trailing ``object`` output is the live instance, so the exported document must reference the
    hoisted node and let the consumer call it per record. Measured before the fix: the wire was
    dropped ("value node not marked constant") and the op kept its constructor default.
    """

    PUTFIELD = "Taidal_recordstream_op_structure_PutField"
    RANDOM = "Taidal_recordstream_value_structure_RandomNumber"

    def _prompt(self, slot: int) -> dict:
        """Stream ← Subgraph-Op terminator ← PutField ← RandomNumber's output ``slot``."""
        return {
            "1": {"class_type": self.RANDOM, "inputs": {"low": 0.0, "high": 1.0, "seed": ""}},
            "2": {"class_type": self.PUTFIELD, "inputs": {"key": "score", "value": ["1", slot]}},
            "3": {"class_type": "Taidal_SubgraphOp", "inputs": {"record": ["2", 0]}},
            "4": {"class_type": "Taidal_recordstream_engine_Stream", "inputs": {"op_0": ["3", 0]}},
        }

    def _compiled_op(self, doc: str) -> Any:
        from confluid import flow, load

        document = load(doc)
        stream = next(flow(v) for v in document.values() if type(flow(v)).__name__ == "Stream")
        return flow(stream.ops[0])

    def test_the_object_slot_becomes_a_live_reference_drawing_per_record(self) -> None:
        import streamstudio
        from streamstudio.graphio import workflow_to_yaml

        object_slot = streamstudio.NODE_CLASS_MAPPINGS[self.RANDOM].RETURN_NAMES.index("object")
        doc = workflow_to_yaml(self._prompt(object_slot), streamstudio.NODE_CLASS_MAPPINGS)
        put_field = self._compiled_op(doc)
        first = put_field({"class": 7})["score"].value
        second = put_field({"class": 3})["score"].value
        assert first != second, "the reference must hand over the LIVE producer, not a frozen draw"

    def test_a_declared_output_on_a_nonconstant_producer_still_drops(self) -> None:
        """The CON case: slot 0 is the ``value`` @output — folding it would freeze a random
        draw into the document, so the non-constant gate still refuses it."""
        import streamstudio
        from streamstudio.graphio import workflow_to_yaml

        doc = workflow_to_yaml(self._prompt(0), streamstudio.NODE_CLASS_MAPPINGS)
        put_field = self._compiled_op(doc)
        assert put_field.value is None, "the wire must drop — the op keeps its constructor default"
        assert "not marked constant" in doc


class TestARoleNamedWiredParamDocksItsRolesProducers:
    """An op's wired-``Any`` param named after a strict slot role carries the ROLE socket.

    Comma socket types connect on set INTERSECTION, so a `model` param typed with the generic
    value union (`RECORDSTREAM_SOURCE,RECORDSTREAM_OBJECT`) can never accept a model node's
    `RECORDSTREAM_OBJECT:model:torch,RECORDSTREAM_OBJECT:model` — the sets are disjoint and the
    editor refuses the wire. The role-qualified slot is what the runnable slots already use.
    """

    def test_a_model_param_is_a_model_slot(self) -> None:
        @configurable(category="op")
        class _Classify:
            def __init__(self, model: Optional[Any] = None) -> None:
                self.model = model

            def __call__(self, record: Any) -> Any:
                return record

        node = create_comfy_node(_Classify, "Taidal__Classify")
        socket = node.INPUT_TYPES()["optional"]["model"][0]
        assert "RECORDSTREAM_OBJECT:model" in socket
        producer = "RECORDSTREAM_OBJECT:model:torch,RECORDSTREAM_OBJECT:model"
        assert set(socket.split(",")) & set(producer.split(",")), "the model node must dock"

    def test_a_role_less_wired_param_keeps_the_generic_union(self) -> None:
        """The CON case: `carrier` names no role — the generic value union stays."""
        node = create_comfy_node(_Vocabulary, "Taidal__Vocabulary")
        assert node.INPUT_TYPES()["optional"]["carrier"] == (VALUE_INPUT_TYPE,)


@configurable
class _Producer:  # no category → an OBJECT node with instance/lazy outputs
    def __init__(self, factor: float = 2.0) -> None:
        self.factor = factor

    def __call__(self) -> float:
        return self.factor


class TestAnObjectInstanceWireSurvivesExport:
    """An OBJECT node's ``instance`` output into an op's param hoists as an eager whole-node ref."""

    PUTFIELD = "Taidal_recordstream_op_structure_PutField"

    def test_the_instance_slot_becomes_a_hoisted_reference(self) -> None:
        import streamstudio
        from streamstudio.graphio import workflow_to_yaml

        mappings = dict(streamstudio.NODE_CLASS_MAPPINGS)
        mappings["Taidal__Producer"] = create_comfy_node(_Producer, "Taidal__Producer")
        prompt = {
            "1": {"class_type": "Taidal__Producer", "inputs": {"factor": 3.5}},
            "2": {"class_type": self.PUTFIELD, "inputs": {"key": "score", "value": ["1", 0]}},
            "3": {"class_type": "Taidal_SubgraphOp", "inputs": {"record": ["2", 0]}},
            "4": {"class_type": "Taidal_recordstream_engine_Stream", "inputs": {"op_0": ["3", 0]}},
        }
        doc = workflow_to_yaml(prompt, mappings)
        assert "_ref_" in doc and "factor: 3.5" in doc, "the wire must survive as a whole-node ref"
        assert "omitted" not in doc
