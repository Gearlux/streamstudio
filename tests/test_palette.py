"""The palette: every configurable class the registry knows, described as a typed node.

A node spec is DERIVED, never written: the kind from the class's shape (an ``Algorithm`` subclass, a
``@configurable`` category, a no-arg ``run()`` or ``__call__``), the parameter widgets from
``confluid.to_pydantic`` (so an ``Interval`` mark is ``minimum``/``maximum`` and a ``Literal`` is an
``enum`` exactly as every other introspecting consumer sees them), the sockets from the declarations
recordstream already has — ``algorithm_spec`` for an Algorithm's Input/Output slots, ``consumes`` /
``produces`` / ``flags`` / ``reports`` for any other op, the constructor annotations for what a node
can be wired INTO (a ``Stream``, an ``Iterable`` source, ``Any``). A class whose introspection fails is
still listed, with the reason in ``problem`` — the palette never drops a class and never crashes on one.
"""

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated, Any, Callable, Dict, Iterable, List, Literal, Mapping, Optional

import pytest
from annotated_types import Interval
from recordstream.algorithm import Algorithm, Input, Output, Param
from recordstream.core.stream import JointStream, Stream
from recordstream.items import Image, Mask
from recordstream.ops.contract import ClassNamesOutput, GraphContract
from recordstream.ops.numpy import ConnectedComponents, Scale, Threshold
from recordstream.ops.structure import PutField, RandomNumber, RenameField
from recordstream.processing import DatasetProcessor

from streamstudio.palette import (
    NODE_KINDS,
    NodeSpec,
    describe,
    palette,
    slot_socket_type,
    socket_type,
    spec_for,
    types_meet,
    value_socket_type,
)


class Brightness(Algorithm):
    """Mean brightness of a picture, as a plain number.

    A second paragraph the palette must NOT show as the summary.
    """

    percentile: Annotated[float, Interval(ge=0.0, le=100.0)] = Param(default=50.0, doc="Which percentile to read.")
    mode: Literal["mean", "median"] = Param(default="mean", doc="How the rows are combined.")
    image: Image = Input(doc="The picture to read.")
    brightness: float = Output(doc="The level found.")

    def compute(self) -> Mapping[str, Any]:
        return {"brightness": 0.0}


class Broken:
    """A class whose constructor names a type that does not exist."""

    def __init__(self, x: "NoSuchType" = None) -> None:  # type: ignore[name-defined]  # noqa: F821
        self.x = x


def make_widget(size: int = 3) -> object:
    """A builder function — a target confluid accepts, so the palette must too."""
    return object()


def sockets(spec: NodeSpec, side: str) -> Dict[str, str]:
    return {s["name"]: s["type"] for s in getattr(spec, side)}


class TestKindsAndSockets:
    def test_the_kind_vocabulary_is_derived_from_the_literal(self) -> None:
        assert set(NODE_KINDS) == {
            "source",
            "op",
            "algorithm",
            "engine",
            "sink",
            "runnable",
            "value",
            "contract",
            "host",
            "alias",
        }

    def test_threshold_is_an_op_whose_tuple_declaration_yields_no_entry_sockets(self) -> None:
        spec = describe(Threshold)
        assert spec.kind == "op"
        assert spec.class_path == "recordstream.ops.numpy.Threshold"
        assert spec.name == "Threshold"
        assert (spec.category, spec.group) == ("op", "numpy")
        assert sockets(spec, "inputs") == {"record": "Record"}
        assert sockets(spec, "outputs") == {"record": "Record", "op": "Op"}
        assert spec.tuple_form is True
        assert spec.consumes == {} and spec.produces == {}
        assert spec.consumes_types == ["NDArrayItem"] and spec.produces_types == ["Mask"]
        assert spec.problem == ""
        assert spec.doc == "An array-bearing field → a boolean ``Mask`` item."

    def test_a_literal_parameter_is_an_enum_in_the_schema(self) -> None:
        spec = describe(Threshold)
        assert spec.params_schema["low_op"]["enum"] == [">", ">="]
        assert spec.params_schema["low_op"]["default"] == ">"
        assert spec.params_schema["field"]["type"] == "string"

    def test_scale_declares_only_what_it_handles(self) -> None:
        spec = describe(Scale)
        assert spec.kind == "op"
        assert spec.tuple_form is True  # ``consumes`` empty means "same as handles" (recordstream.transform)
        assert spec.consumes_types == ["NDArrayItem"] and spec.produces_types == []
        assert set(spec.params_schema) == {"source_min", "source_max", "target_min", "target_max", "field"}

    def test_connected_components_consumes_a_mask_and_produces_boxes(self) -> None:
        spec = describe(ConnectedComponents)
        assert spec.consumes_types == ["Mask"] and spec.produces_types == ["Boxes"]
        assert spec.params_schema["min_area_bins"]["type"] == "integer"

    def test_a_plain_op_has_the_record_sockets_and_nothing_else(self) -> None:
        spec = describe(RenameField)
        assert spec.kind == "op"
        assert spec.tuple_form is False
        assert sockets(spec, "inputs") == {"record": "Record"}
        assert spec.consumes_types == [] and spec.produces_types == []
        assert spec.flags == [] and spec.reports == []

    def test_an_any_parameter_is_a_wildcard_input_socket_and_still_a_parameter(self) -> None:
        spec = describe(PutField)
        assert sockets(spec, "inputs") == {"record": "Record", "value": "*"}
        assert "value" in spec.params_schema and "key" in spec.params_schema
        assert [s["required"] for s in spec.inputs] == [True, False]

    def test_stream_is_an_engine_with_a_source_and_an_ops_socket(self) -> None:
        spec = describe(Stream)
        assert spec.kind == "engine"
        assert sockets(spec, "inputs") == {"source": "Source", "ops": "*"}
        assert sockets(spec, "outputs") == {"stream": "Stream"}
        assert "class_names" in spec.params_schema  # List[str] is typed, not wired

    def test_joint_stream_takes_streams(self) -> None:
        assert sockets(describe(JointStream), "inputs") == {"streams": "Stream"}

    def test_graph_contract_is_the_contract_kind_with_per_document_sockets(self) -> None:
        spec = describe(GraphContract)
        assert spec.kind == "contract"
        assert spec.inputs == [] and spec.outputs == []
        assert set(spec.params_schema) == {"name", "inputs", "outputs", "records", "delivered", "optional"}

    def test_dataset_processor_is_a_runnable_wired_from_a_stream(self) -> None:
        spec = describe(DatasetProcessor)
        assert spec.kind == "runnable"
        assert sockets(spec, "inputs") == {"stream": "Stream", "sink": "*"}
        assert spec.outputs == []
        assert "StreamConfig" in spec.params_defs  # the nested schema the ``stream`` property refers to

    def test_a_value_producer_has_its_outputs_and_an_object_output(self) -> None:
        spec = describe(RandomNumber)
        assert spec.kind == "value"
        assert sockets(spec, "outputs") == {"value": "number", "object": "object"}
        names = describe(ClassNamesOutput)
        assert names.kind == "value"
        assert sockets(names, "outputs") == {"class_names": "List[str]", "object": "object"}

    def test_a_builder_function_is_described_like_a_value(self) -> None:
        spec = describe(make_widget)
        assert spec.kind == "value"
        assert spec.class_path.endswith("test_palette.make_widget")
        assert spec.params_schema["size"] == {"default": 3, "title": "Size", "type": "integer"}
        assert sockets(spec, "outputs") == {"object": "object"}


class TestAlgorithm:
    def test_an_algorithm_gets_entry_sockets_typed_by_item_class(self) -> None:
        spec = describe(Brightness)
        assert spec.kind == "algorithm"
        assert sockets(spec, "inputs") == {"record": "Record", "image": "Image"}
        assert sockets(spec, "outputs") == {"record": "Record", "op": "Op", "brightness": "number"}
        image = next(s for s in spec.inputs if s["name"] == "image")
        assert image["required"] is True and image["doc"] == "The picture to read."
        assert spec.consumes == {"image": "Image"}
        assert spec.produces == {"brightness": "*"}
        assert spec.tuple_form is False
        assert spec.doc == "Mean brightness of a picture, as a plain number."

    def test_interval_and_literal_marks_reach_the_schema(self) -> None:
        schema = describe(Brightness).params_schema
        assert schema["percentile"]["minimum"] == 0.0 and schema["percentile"]["maximum"] == 100.0
        assert schema["percentile"]["default"] == 50.0
        assert schema["mode"]["enum"] == ["mean", "median"]
        assert "keys" in schema  # the generated constructor's entry-renaming argument
        assert "image" not in schema and "brightness" not in schema  # slots are sockets, not widgets


class TestBrokenClass:
    def test_a_class_whose_introspection_fails_is_listed_with_the_reason(self) -> None:
        spec = describe(Broken)
        assert spec.kind == "op"
        assert spec.inputs == [] and spec.outputs == []
        assert spec.params_schema == {}
        assert "NoSuchType" in spec.problem
        assert spec.class_path.endswith("test_palette.Broken")
        assert spec.doc == "A class whose constructor names a type that does not exist."


class TestSocketRules:
    @pytest.mark.parametrize(
        "annotation, expected",
        [
            (Optional[Iterable[Any]], "Source"),
            (Iterable[Dict[str, Any]], "Source"),
            (Optional[Stream], "Stream"),
            (Optional[List[Stream]], "Stream"),
            (JointStream, "Stream"),
            (Image, "Image"),
            (List[Mask], "Mask"),
            (Any, "*"),
            (Optional[Any], "*"),
            (Optional[List[Any]], "*"),
            (Callable[..., Any], "object"),
            (Interval, "object"),
            (float, None),
            (Optional[int], None),
            (str, None),
            (bool, None),
            (List[str], None),
            (Dict[str, str], None),
            (Literal["a", "b"], None),
        ],
    )
    def test_what_a_constructor_parameter_can_be_wired_from(self, annotation: Any, expected: Optional[str]) -> None:
        assert socket_type(annotation) == expected

    @pytest.mark.parametrize(
        "annotation, expected",
        [
            (float, "number"),
            (int, "integer"),
            (str, "string"),
            (bool, "boolean"),
            (Dict[str, Any], "object"),
            (List[str], "List[str]"),
            (Image, "Image"),
            (Optional[Image], "Image"),
            (Any, "*"),
            (List[int], "*"),
        ],
    )
    def test_a_declared_value_always_has_a_socket_type(self, annotation: Any, expected: str) -> None:
        assert value_socket_type(annotation) == expected

    @pytest.mark.parametrize(
        "kind, expected",
        [("float", "number"), ("int", "integer"), ("str", "string"), ("bool", "boolean")]
        + [(k, k) for k in ("Stream", "Source", "Sink", "Op", "Record", "List[str]", "Image")],
    )
    def test_a_contract_slot_kind_names_its_socket(self, kind: str, expected: str) -> None:
        assert slot_socket_type(kind) == expected

    @pytest.mark.parametrize(
        "source, target, meets",
        [
            ("Image", "Image", True),
            ("*", "Image", True),
            ("Image", "*", True),
            ("Image", "NDArrayItem", True),  # an Image IS an NDArrayItem
            ("NDArrayItem", "Image", False),
            ("Mask", "Image", False),
            ("Stream", "Source", True),  # a Stream is iterable, so it is a source of records
            ("Source", "Stream", False),
            ("number", "integer", False),
            ("Op", "Op", True),
        ],
    )
    def test_when_two_socket_types_meet(self, source: str, target: str, meets: bool) -> None:
        assert types_meet(source, target) is meets


#: The cheap set, listed in a FRESH interpreter that imports what this module imports and nothing else. Inside a test
#: run the registry holds every class the other test modules imported: measured 2026-10-06, 912 specs and 3.6 s of
#: CPU, 7.4 s under coverage (a Jenkins failure) — against 62 specs and 0.05 s alone. Its own CPU time, so another
#: process's load does not count either.
_CHEAP_SET_PROBE = """
import json, time
import recordstream.core.stream, recordstream.ops.contract, recordstream.ops.numpy
from streamstudio.palette import palette
started = time.process_time()
specs = palette(refresh=True, scan=False)
print(json.dumps({"seconds": time.process_time() - started, "names": sorted({s.name for s in specs}),
                  "count": len(specs), "sorted": specs == sorted(specs, key=lambda s: s.class_path)}))
"""


class TestPaletteAndLookup:
    def test_the_registry_as_imported_is_the_cheap_set(self, tmp_path: Path) -> None:
        done = subprocess.run(
            [sys.executable, "-c", _CHEAP_SET_PROBE], capture_output=True, text=True, timeout=120, cwd=tmp_path
        )
        assert done.returncode == 0, done.stderr
        listed = json.loads(done.stdout.strip().splitlines()[-1])
        assert {"Scale", "Threshold", "ConnectedComponents", "Stream", "GraphContract"} <= set(listed["names"])
        assert listed["count"] >= 50
        assert listed["sorted"]
        print(f"\ncheap set: {listed['count']} specs in {listed['seconds'] * 1000:.0f} ms")
        assert listed["seconds"] < 5.0

    def test_the_cached_list_is_returned_by_identity(self) -> None:
        first = palette(scan=False)
        assert palette(scan=False) is first
        assert palette(refresh=True, scan=False) is not first

    def test_spec_for_by_dotted_path_and_by_registry_key(self) -> None:
        palette(refresh=True, scan=False)
        by_path = spec_for("recordstream.ops.numpy.Threshold")
        by_key = spec_for("Threshold")
        assert by_path is by_key
        assert by_path.kind == "op"

    def test_spec_for_an_unknown_class_names_close_matches(self) -> None:
        palette(refresh=True, scan=False)
        with pytest.raises(KeyError) as caught:
            spec_for("Treshold")
        message = str(caught.value)
        assert "Treshold" in message and "Threshold" in message

    def test_spec_for_an_importable_class_the_registry_does_not_hold(self) -> None:
        spec = spec_for(f"{Brightness.__module__}.{Brightness.__qualname__}")
        assert spec.kind == "algorithm"


def test_the_full_scan_lists_every_installed_class() -> None:
    pytest.importorskip("waivefront")
    started = time.perf_counter()
    specs = palette(refresh=True)
    elapsed = time.perf_counter() - started
    print(f"\nfull scan: {len(specs)} specs in {elapsed:.1f} s")
    assert len(specs) >= 400
    assert palette() is specs
    by_name = {s.name: s for s in specs}
    floor = by_name["NoiseFloorEstimate"]
    assert floor.kind == "algorithm"
    assert sockets(floor, "inputs") == {"record": "Record", "spectrogram": "Spectrogram"}
    assert sockets(floor, "outputs") == {
        "record": "Record",
        "op": "Op",
        "noise_floor_db": "number",
        "noise_floor_method": "string",
    }
    assert floor.params_schema["percentile"]["minimum"] == 0.0
    # the registry holds a class whose constructor annotations do not resolve; it is listed, not dropped
    broken = [s for s in specs if s.problem]
    assert broken, "expected at least one class the introspection refuses"
    assert all(s.kind == "op" and s.inputs == [] for s in broken)
    # an op with flags and a report shows one record outcome per flag and a report output
    classify = by_name["ClassifyBluetooth"]
    outs = sockets(classify, "outputs")
    assert outs["ble"] == "Record" and outs["try_weak"] == "Record" and outs["report:classify"] == "object"
    assert classify.flags == ["fsk", "ble", "bredr", "try_weak", "try_classic", "try_frame", "try_edr"]
    assert classify.reports == ["classify"]
