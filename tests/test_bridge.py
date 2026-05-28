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
from typing import Optional
from unittest.mock import patch

import datasets

from fluxstudio.bridge import _map_type, create_comfy_node, register_module_nodes


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
        with patch("dataflux.discovery.resolve_callable", return_value=_sample_op):
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
        with patch("dataflux.discovery.resolve_callable", return_value=123):
            register_module_nodes("fake.mod", mappings, display)

    assert mappings == {}
    assert display == {}
