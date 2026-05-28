"""Tests for ``fluxstudio.bridge`` — the Python-callable → ComfyUI-node bridge.

These exercise the two public surfaces (``create_comfy_node`` and
``register_module_nodes``) plus the private ``_map_type`` heuristic that drives
widget selection. The heuristic matters because it is the ONLY place type →
ComfyUI-widget mapping happens (per the FluxStudio mandate "New supported types
MUST extend ``TYPE_MAP`` / ``_map_type`` rather than special-casing nodes"), so
each branch — scalar, HF dataset, path-like, union/forward-ref dataset, and the
STRING fallback — needs an explicit guard. The ``register_module_nodes`` tests
mock ``scan_module`` / ``resolve_callable`` so they assert the bridge's own
registration logic without depending on whatever entry points happen to be
installed in the dev venv.

NOTE: this module intentionally does NOT use ``from __future__ import
annotations``. ``_map_type`` resolves real type objects (``annotation in
TYPE_MAP``, ``annotation == datasets.Dataset``); stringizing the inline test
functions' annotations would route everything through the string-contains
fallback and silently defeat the very branches under test.
"""

from pathlib import Path
from typing import List, Optional
from unittest.mock import patch

import datasets
import numpy as np
import pytest
from dataflux.sample import Sample
from dataflux.typespec import ArrayType, SampleType

from fluxstudio.bridge import (
    SAMPLE_TYPE,
    SOURCE_TYPE,
    _coerce_kwargs,
    _map_type,
    create_comfy_node,
    register_module_nodes,
)


def test_create_comfy_node() -> None:
    def my_op(dataset: datasets.Dataset, threshold: float = 0.5) -> datasets.Dataset:
        """My sample op."""
        return dataset.filter(lambda x: x["val"] > threshold)

    NodeClass = create_comfy_node(my_op, "MyOpNode")

    # Check ComfyUI contract
    assert hasattr(NodeClass, "INPUT_TYPES")
    inputs = NodeClass.INPUT_TYPES()
    assert "dataset" in inputs["required"]
    assert inputs["required"]["dataset"] == ("HF_DATASET",)
    assert "threshold" in inputs["required"]
    assert inputs["required"]["threshold"][0] == "FLOAT"
    assert inputs["required"]["threshold"][1]["default"] == 0.5

    assert NodeClass.RETURN_TYPES == ("HF_DATASET",)
    assert NodeClass.FUNCTION == "execute"
    assert NodeClass.CATEGORY == "Taidal"


def test_node_execution() -> None:
    def add_val(dataset: datasets.Dataset, amount: int = 1) -> datasets.Dataset:
        return dataset.map(lambda x: {"val": x["val"] + amount})

    NodeClass = create_comfy_node(add_val, "AddValNode")
    node = NodeClass()

    ds = datasets.Dataset.from_dict({"val": [1, 2, 3]})
    result = node.execute(dataset=ds, amount=10)

    assert isinstance(result, tuple)
    assert len(result) == 1
    assert result[0]["val"] == [11, 12, 13]


# --------------------------------------------------------------------------- #
# _map_type heuristic branches
# --------------------------------------------------------------------------- #


def test_map_type_scalar_from_type_map() -> None:
    assert _map_type("count", int) == "INT"
    assert _map_type("ratio", float) == "FLOAT"
    assert _map_type("name", str) == "STRING"
    assert _map_type("flag", bool) == "BOOLEAN"


def test_map_type_exact_hf_dataset() -> None:
    assert _map_type("ds", datasets.Dataset) == "HF_DATASET"


def test_map_type_path_from_annotation() -> None:
    # A pathlib.Path annotation wins even when the param name is not path-like.
    assert _map_type("p", Path) == "PATH"


def test_map_type_path_from_param_name() -> None:
    # Name-based heuristic: any of path/dir/root/folder triggers the picker.
    for name in ("input_path", "output_dir", "data_root", "media_folder"):
        assert _map_type(name, str) == "PATH", name


def test_map_type_union_dataset_falls_through_to_str_match() -> None:
    # Optional[Dataset] is NOT == datasets.Dataset and is unhashable for the
    # TYPE_MAP lookup, so it must be caught by the string-contains branch.
    assert _map_type("maybe_ds", Optional[datasets.Dataset]) == "HF_DATASET"


def test_map_type_forward_ref_dataset_string() -> None:
    # A stringified annotation (e.g. under ``from __future__ import annotations``)
    # is matched by the same string-contains branch.
    assert _map_type("ds", "datasets.Dataset") == "HF_DATASET"


def test_map_type_unknown_falls_back_to_string() -> None:
    assert _map_type("blob", bytes) == "STRING"


# --------------------------------------------------------------------------- #
# create_comfy_node widget config branches
# --------------------------------------------------------------------------- #


def test_create_comfy_node_path_param_with_default() -> None:
    def op(output_dir: str = "/tmp/out") -> str:
        return output_dir

    inputs = create_comfy_node(op, "PathNode").INPUT_TYPES()
    comfy_type, config = inputs["required"]["output_dir"]
    assert comfy_type == "STRING"
    assert config == {"default": "/tmp/out", "taidal_widget": "DIRECTORY"}


def test_create_comfy_node_path_param_without_default() -> None:
    def op(data_root) -> str:  # type: ignore[no-untyped-def]
        return str(data_root)

    inputs = create_comfy_node(op, "PathNodeNoDefault").INPUT_TYPES()
    comfy_type, config = inputs["required"]["data_root"]
    assert comfy_type == "STRING"
    assert config == {"default": "", "taidal_widget": "DIRECTORY"}


def test_create_comfy_node_scalar_without_default_uses_widget_default() -> None:
    def op(count: int) -> str:
        return str(count)

    inputs = create_comfy_node(op, "ScalarNode").INPUT_TYPES()
    assert inputs["required"]["count"] == ("INT", {"default": 0})


def test_create_comfy_node_string_return_type() -> None:
    def op() -> str:
        return "hi"

    assert create_comfy_node(op, "StrReturnNode").RETURN_TYPES == ("STRING",)


def test_node_execution_tuple_return_passthrough() -> None:
    # When the wrapped callable already returns a tuple, execute_wrapper must
    # forward it verbatim rather than nesting it in another 1-tuple.
    def op() -> tuple:
        return ("a", "b")

    node = create_comfy_node(op, "TupleNode")()
    result = node.execute()
    assert result == ("a", "b")
    assert len(result) == 2


# --------------------------------------------------------------------------- #
# register_module_nodes
# --------------------------------------------------------------------------- #


def _sample_op(x: int) -> str:
    """Module-level so resolve_callable mocking has a real callable to hand back."""
    return str(x)


def test_register_module_nodes_registers_callable() -> None:
    mappings: dict = {}
    display: dict = {}
    discovered = [{"path": "fake.mod:sample_op", "name": "sample_op"}]

    with patch("fluxstudio.bridge.scan_module", return_value=discovered):
        with patch("fluxstudio.bridge.resolve_callable", return_value=_sample_op):
            register_module_nodes("fake.mod", mappings, display, category="Taidal/Fake")

    assert "Taidal_sample_op" in mappings
    assert mappings["Taidal_sample_op"].CATEGORY == "Taidal/Fake"
    assert display["Taidal_sample_op"] == "Sample Op"


def test_register_module_nodes_skips_non_callable() -> None:
    mappings: dict = {}
    display: dict = {}
    discovered = [{"path": "fake.mod:not_callable", "name": "not_callable"}]

    with patch("fluxstudio.bridge.scan_module", return_value=discovered):
        # resolve_callable hands back a non-callable -> the node must be skipped.
        with patch("fluxstudio.bridge.resolve_callable", return_value=123):
            register_module_nodes("fake.mod", mappings, display)

    assert mappings == {}
    assert display == {}


# --------------------------------------------------------------------------- #
# Typed node classification: Source / Op / Generic
# --------------------------------------------------------------------------- #


class _FakeSource:
    """Indexable source whose items are Samples -> classified as a DATAFLUX_SOURCE node."""

    def __init__(self, path: str, count: int = 3) -> None:
        self.path = path
        self.count = count

    def __len__(self) -> int:
        return self.count

    def __getitem__(self, index: int) -> Sample:
        return Sample(input=index, target=None, metadata={})


class _FakeOp:
    """Sample -> Sample op -> classified as a DATAFLUX_SAMPLE in/out node."""

    def __init__(self, bump: int = 1) -> None:
        self.bump = bump

    def __call__(self, sample: Sample) -> Sample:
        return sample._replace(input=sample.input + self.bump)


class _FilterishOp:
    def __init__(self) -> None: ...

    def __call__(self, sample: Sample) -> Optional[Sample]:
        return None


def test_source_class_becomes_source_node() -> None:
    node = create_comfy_node(_FakeSource, "Taidal__FakeSource")
    assert node.RETURN_TYPES == (SOURCE_TYPE,)
    inputs = node.INPUT_TYPES()["required"]
    assert "path" in inputs and "count" in inputs
    assert "sample" not in inputs  # sources do not take a Sample input


def test_source_node_execute_wraps_in_flux() -> None:
    from dataflux.core import Flux

    node = create_comfy_node(_FakeSource, "Taidal__FakeSource")()
    (out,) = node.execute(path="x", count=2)
    assert isinstance(out, Flux)
    assert len(out) == 2


def test_op_class_becomes_sample_node() -> None:
    node = create_comfy_node(_FakeOp, "Taidal__FakeOp")
    assert node.RETURN_TYPES == (SAMPLE_TYPE,)
    inputs = node.INPUT_TYPES()["required"]
    assert inputs["sample"] == (SAMPLE_TYPE,)
    assert inputs["bump"][0] == "INT"


def test_op_node_constructs_and_applies() -> None:
    node = create_comfy_node(_FakeOp, "Taidal__FakeOp")()
    (out,) = node.execute(sample=Sample(input=10), bump=5)
    assert out.input == 15


def test_op_node_raises_when_op_filters_sample() -> None:
    node = create_comfy_node(_FilterishOp, "Taidal__Filterish")()
    with pytest.raises(RuntimeError, match="filtered out"):
        node.execute(sample=Sample(input=1))


def test_generic_function_with_sample_param_gets_sample_socket() -> None:
    def describe(sample: Sample, prefix: str = "x") -> str:
        return prefix + str(sample.input)

    inputs = create_comfy_node(describe, "Taidal__Describe").INPUT_TYPES()["required"]
    assert inputs["sample"] == (SAMPLE_TYPE,)
    assert inputs["prefix"][0] == "STRING"


# --------------------------------------------------------------------------- #
# _map_type — DataFlux Sample + Optional/Union scalar unwrapping
# --------------------------------------------------------------------------- #


def test_map_type_sample_annotation() -> None:
    assert _map_type("s", Sample) == SAMPLE_TYPE
    assert _map_type("s", "Iterator[Sample]") == SAMPLE_TYPE


def test_map_type_optional_scalars_unwrap() -> None:
    assert _map_type("count", Optional[int]) == "INT"
    assert _map_type("ratio", Optional[float]) == "FLOAT"
    assert _map_type("flag", Optional[bool]) == "BOOLEAN"


# --------------------------------------------------------------------------- #
# _coerce_kwargs — widget value -> constructor value
# --------------------------------------------------------------------------- #


def test_coerce_kwargs_none_list_and_passthrough() -> None:
    def fn(a: Optional[int] = None, tags: Optional[List[str]] = None, name: str = "n", n: int = 0) -> None: ...

    out = _coerce_kwargs(fn, {"a": "None", "tags": "x, y ,z", "name": "hi", "n": 7})
    assert out["a"] is None  # "None" on an Optional-default param -> None
    assert out["tags"] == ["x", "y", "z"]  # List annotation -> comma split
    assert out["name"] == "hi"
    assert out["n"] == 7  # already-typed int passes through


def test_coerce_kwargs_empty_optional_is_omitted() -> None:
    def fn(label: str = "default") -> None: ...

    # Empty string on a param with a concrete (non-None) default -> omit so the default applies.
    assert _coerce_kwargs(fn, {"label": ""}) == {}


def test_coerce_kwargs_empty_required_passes_through() -> None:
    def fn(required_path) -> None:  # type: ignore[no-untyped-def]
        ...

    # A required param (no default) keeps the literal empty string rather than vanishing.
    assert _coerce_kwargs(fn, {"required_path": ""}) == {"required_path": ""}


# --------------------------------------------------------------------------- #
# Classifier edge branches
# --------------------------------------------------------------------------- #


def test_build_config_inputs_skips_self_and_varargs() -> None:
    def fn(self: int, a: int, *args: int, **kwargs: int) -> None: ...

    inputs = create_comfy_node(fn, "Taidal__SelfVar").INPUT_TYPES()["required"]
    assert "self" not in inputs and "args" not in inputs and "kwargs" not in inputs
    assert "a" in inputs


def test_op_call_with_non_sample_first_param_is_generic() -> None:
    class _NotAnOp:
        def __call__(self, x: int) -> int:
            return x

    # __call__ takes int, not Sample -> not an op -> generic STRING node.
    assert create_comfy_node(_NotAnOp, "Taidal__NotAnOp").RETURN_TYPES == ("STRING",)


def test_source_classified_by_name_when_unannotated() -> None:
    class WidgetSource:
        def __getitem__(self, i):  # type: ignore[no-untyped-def]
            return i

    # __getitem__ has no Sample annotation, but the *Source name triggers the fallback.
    assert create_comfy_node(WidgetSource, "Taidal__WidgetSource").RETURN_TYPES == (SOURCE_TYPE,)


def test_non_source_unannotated_class_is_generic() -> None:
    class Plain:
        def __getitem__(self, i):  # type: ignore[no-untyped-def]
            return i

    # No Sample annotation and name does not end with 'Source' -> generic.
    assert create_comfy_node(Plain, "Taidal__Plain").RETURN_TYPES == ("STRING",)


# --------------------------------------------------------------------------- #
# Nullable numeric params (Optional[int]/[float] = None) — must not default to 0
# --------------------------------------------------------------------------- #


def test_nullable_numeric_becomes_blank_string_widget() -> None:
    # Optional[int]=None must NOT become an INT widget defaulting to 0: 0 is a distinct,
    # often-breaking value (e.g. HuggingFaceSource count=0 -> "no samples"). It becomes a
    # blank STRING that _coerce_kwargs turns into None / casts to the number.
    def fn(count: Optional[int] = None, ratio: Optional[float] = None, n: int = 3) -> None: ...

    inputs = create_comfy_node(fn, "Taidal__Nullable").INPUT_TYPES()["required"]
    assert inputs["count"] == ("STRING", {"default": "", "multiline": False})
    assert inputs["ratio"] == ("STRING", {"default": "", "multiline": False})
    # A concrete-default int is still a real INT widget.
    assert inputs["n"] == ("INT", {"default": 3})


def test_coerce_kwargs_casts_filled_nullable_numeric() -> None:
    def fn(count: Optional[int] = None, ratio: Optional[float] = None) -> None: ...

    out = _coerce_kwargs(fn, {"count": "5", "ratio": "0.25"})
    assert out["count"] == 5 and isinstance(out["count"], int)
    assert out["ratio"] == 0.25 and isinstance(out["ratio"], float)
    # Blank -> None (i.e. "unset" / all).
    assert _coerce_kwargs(fn, {"count": ""})["count"] is None


# --------------------------------------------------------------------------- #
# Declared type specs on generated nodes + runtime accept-check
# --------------------------------------------------------------------------- #


class _TypedOp:
    """Op declaring a type contract: accepts a 2-D numpy array, produces a 2-D bool numpy array."""

    ACCEPTS = SampleType(input=ArrayType(ndim=2, frameworks={"numpy"}))
    PRODUCES = SampleType(input=ArrayType(ndim=2, dtype="bool", frameworks={"numpy"}))

    def __call__(self, sample: Sample) -> Sample:
        return sample._replace(input=sample.input > 0)


def test_op_node_stashes_declared_specs() -> None:
    node = create_comfy_node(_TypedOp, "Taidal__TypedOp")
    assert node.TAIDAL_ACCEPTS == _TypedOp.ACCEPTS.to_dict()
    assert node.TAIDAL_PRODUCES == _TypedOp.PRODUCES.to_dict()
    # an un-annotated op carries None (so the canvas validator treats it as permissive)
    assert create_comfy_node(_FakeOp, "Taidal__FakeOp").TAIDAL_ACCEPTS is None


def test_source_node_stashes_produces_none_when_undeclared() -> None:
    node = create_comfy_node(_FakeSource, "Taidal__FakeSource")
    assert node.TAIDAL_PRODUCES is None


def test_op_node_runtime_check_passes_matching_sample() -> None:
    node = create_comfy_node(_TypedOp, "Taidal__TypedOp")()
    (out,) = node.execute(sample=Sample(input=np.zeros((4, 4), dtype=np.float32)))
    assert out.input.dtype == bool


def test_op_node_runtime_check_rejects_mismatched_sample() -> None:
    node = create_comfy_node(_TypedOp, "Taidal__TypedOp")()
    with pytest.raises(RuntimeError, match="cannot accept the upstream sample"):
        node.execute(sample=Sample(input=5))  # a bare int is not a 2-D numpy array
