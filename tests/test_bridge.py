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

import json
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple, Union
from unittest.mock import MagicMock, patch

import datasets
import numpy as np
import pytest
from confluid import configurable
from confluid.decorators import output
from dataflux.sample import Sample
from dataflux.typespec import ArrayType, SampleType

from fluxstudio.bridge import (
    ARRAY_TYPE,
    DEFAULT_WIDGET_CONFIG,
    OP_TYPE,
    SAMPLE_TYPE,
    SOURCE_TYPE,
    VALUE_TYPE,
    _coerce_kwargs,
    _has_bare_scalar_union_arm,
    _humanize_node_name,
    _is_hf_dataset_id_param,
    _is_hf_feature_param,
    _is_hf_metadata_param,
    _is_hf_split_param,
    _is_nested_container,
    _is_socket_type,
    _literal_options,
    _map_type,
    _needs_value_socket,
    _node_category,
    _op_list_params,
    _package_tag,
    _single_op_params,
    _value_call_signature,
    create_comfy_node,
    hide_default_comfy_nodes,
    kept_default_node_names,
    node_root,
    only_taidal_nodes,
    register_module_nodes,
)


def test_kept_default_node_names_user_config_adds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # PreviewImage / PreviewAny are ALWAYS kept; the config file's keep_nodes list ADDS more (NOT an
    # env var — FLUXSTUDIO_CONFIG carries only the file's LOCATION).
    cfg = tmp_path / "config.yaml"
    cfg.write_text("keep_nodes:\n  - CustomViewerXYZ\n  - LoadImage\n")
    monkeypatch.setenv("FLUXSTUDIO_CONFIG", str(cfg))
    assert {"PreviewImage", "PreviewAny", "CustomViewerXYZ", "LoadImage"} <= kept_default_node_names()


def test_kept_default_node_names_falls_back_to_packaged_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No user config (FLUXSTUDIO_CONFIG points at a missing file) -> the SHIPPED config.default.yaml is
    # used, so its appropriate defaults (e.g. MaskToImage) apply.
    monkeypatch.setenv("FLUXSTUDIO_CONFIG", str(tmp_path / "missing.yaml"))
    kept = kept_default_node_names()
    assert {"PreviewImage", "PreviewAny"} <= kept
    assert "MaskToImage" in kept  # comes from the packaged config.default.yaml fallback


def test_kept_default_node_names_last_resort_when_no_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Neither the user config NOR the packaged default is readable -> the safety pair (so the native
    # viewers never vanish on a broken install).
    monkeypatch.setenv("FLUXSTUDIO_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.setattr("fluxstudio.bridge._packaged_default_config_path", lambda: tmp_path / "also_missing.yaml")
    assert kept_default_node_names() == {"PreviewImage", "PreviewAny"}


def test_kept_default_node_names_ignores_malformed_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A malformed user config must never break node discovery — it falls THROUGH to the packaged
    # default (which still keeps PreviewImage/PreviewAny).
    cfg = tmp_path / "bad.yaml"
    cfg.write_text("keep_nodes: : : not valid yaml [")
    monkeypatch.setenv("FLUXSTUDIO_CONFIG", str(cfg))
    assert {"PreviewImage", "PreviewAny"} <= kept_default_node_names()


def test_only_taidal_nodes_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLUXSTUDIO_ONLY_TAIDAL_NODES", raising=False)
    assert only_taidal_nodes() is False
    monkeypatch.setenv("FLUXSTUDIO_ONLY_TAIDAL_NODES", "1")
    assert only_taidal_nodes() is True
    monkeypatch.setenv("FLUXSTUDIO_ONLY_TAIDAL_NODES", "0")
    assert only_taidal_nodes() is False


def test_array_type_is_a_wire_socket_not_a_widget() -> None:
    # DATAFLUX_ARRAY (the Extract→Histogram array wire) is a live-object socket type, so graphio's
    # socket-vs-widget split treats it as a wire. It must NOT be a TYPE_MAP entry (that would retype
    # auto-generated op params), only a hand-wired adapter socket.
    assert ARRAY_TYPE == "DATAFLUX_ARRAY"
    assert _is_socket_type(ARRAY_TYPE) is True
    from fluxstudio.bridge import TYPE_MAP

    assert ARRAY_TYPE not in TYPE_MAP.values()


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
    result = node.execute(dataset=ds, amount=10)["result"]

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


def test_map_type_union_int_str_returns_string() -> None:
    # Union[int, str] (SampleSpec) must return STRING, not INT.
    assert _map_type("length", Union[int, str]) == "STRING"


def test_map_type_optional_int_still_int() -> None:
    # Optional[int] has no "str" → still INT.
    assert _map_type("count", Optional[int]) == "INT"


def test_map_type_union_int_float_still_float() -> None:
    # Union[int, float] has no "str" → falls through to float branch.
    assert _map_type("value", Union[int, float]) == "FLOAT"


def test_needs_value_socket_only_for_nested_containers() -> None:
    # A NESTED container (a container whose element is itself a container) can't be a sensible widget →
    # a wired DATAFLUX_VALUE socket. A flat container / fixed tuple stays a typeable widget.
    Box = Tuple[int, int, int, int]
    Color = Tuple[int, int, int]
    assert _needs_value_socket(List[Box]) is True  # list of tuples (boxes)
    assert _needs_value_socket(Optional[Union[Color, List[Color]]]) is True  # list of colours
    assert _needs_value_socket(Dict[str, List[int]]) is True  # dict of lists
    assert _needs_value_socket(List[List[int]]) is True
    # flat / scalar — stay widgets
    assert _needs_value_socket(Color) is False  # a single 3-tuple
    assert _needs_value_socket(List[str]) is False
    assert _needs_value_socket(List[int]) is False
    assert _needs_value_socket(Dict[str, float]) is False
    assert _needs_value_socket(Tuple[float, float]) is False  # a range pair
    assert _needs_value_socket(Optional[int]) is False


def test_is_nested_container_shape_rule() -> None:
    assert _is_nested_container(List[Tuple[int, int]]) is True
    assert _is_nested_container(List[int]) is False
    assert _is_nested_container(Dict[str, List[int]]) is True  # nests on the VALUE
    assert _is_nested_container(Dict[str, float]) is False
    assert _is_nested_container(int) is False


def test_nested_container_param_is_value_socket_not_widget() -> None:
    # A generated op node renders a nested-container param as an OPTIONAL DATAFLUX_VALUE input socket;
    # a flat fixed-tuple param stays a widget. Wiring a real value into it passes through to the op.
    from confluid import configurable as _cfg

    @_cfg(category="op")
    class _BoxOp:
        def __init__(self, boxes: List[Tuple[int, int, int, int]] = [], color: Tuple[int, int, int] = (255, 0, 0)):
            self.boxes = boxes
            self.color = color

        def __call__(self, sample: Sample) -> Sample:
            return sample

    node_cls = create_comfy_node(_BoxOp, "Taidal__BoxOp")
    spec = node_cls.INPUT_TYPES()
    assert spec["optional"]["boxes"] == (VALUE_TYPE,)  # nested container → wired value socket
    assert "boxes" not in spec.get("required", {})
    assert spec["required"]["color"][0] == "STRING"  # flat fixed tuple stays a widget
    # Wiring a real Python value into the socket flows through to the constructed op.
    _, op = node_cls().execute(boxes=[(20, 10, 80, 50)])["result"]
    assert op.boxes == [(20, 10, 80, 50)]
    # Unwired → the constructor default.
    _, op2 = node_cls().execute()["result"]
    assert op2.boxes == []


def test_map_type_range_only_for_two_element_or_variadic_numeric_tuple() -> None:
    # The ``__lo``/``__hi`` RANGE pair is ONLY a TOP-LEVEL 2-element / variable-length numeric tuple —
    # NOT a List OF tuples (``OverlayBoxes.boxes``) nor a fixed != 2 tuple (a 3-tuple ``Color``, a
    # 4-tuple ``Box``). The old ``"tuple" in str(anno)`` substring heuristic wrongly ranged all of them
    # (``boxes__lo``/``boxes__hi``).
    Box = Tuple[int, int, int, int]
    Color = Tuple[int, int, int]
    # legitimate ranges — unchanged
    assert _map_type("r", Tuple[float, float]) == "RANGE_FLOAT"
    assert _map_type("r", Tuple[int, int]) == "RANGE_INT"
    assert _map_type("axis", Tuple[int, ...]) == "RANGE_INT"
    assert _map_type("axis", Union[int, Tuple[int, ...]]) == "RANGE_INT"
    # the bug — these are NOT ranges now
    assert _map_type("boxes", List[Box]) == "STRING"
    assert _map_type("default_color", Color) == "STRING"
    assert _map_type("colors", Optional[Union[Color, List[Color]]]) == "STRING"


def test_map_type_non_range_container_is_string_not_scalar() -> None:
    # A container whose ELEMENT type contains "int"/"float" must NOT collapse to a single scalar widget.
    assert _map_type("ids", List[int]) == "STRING"  # was wrongly INT
    assert _map_type("ratios", List[float]) == "STRING"  # was wrongly FLOAT
    assert _map_type("ov", Dict[str, float]) == "STRING"  # was wrongly FLOAT
    # a concrete iterable class (datasets.Dataset) is NOT a container annotation — keeps its own type
    assert _map_type("maybe_ds", Optional[datasets.Dataset]) == "HF_DATASET"
    # scalars unaffected
    assert _map_type("count", Optional[int]) == "INT"
    assert _map_type("v", Union[int, float]) == "FLOAT"


def test_coerce_kwargs_decodes_fixed_numeric_tuple() -> None:
    # A non-range numeric tuple (a 3-tuple Color) rides a STRING widget; its "(255, 0, 0)" / "255,0,0"
    # value decodes back to a real tuple (not int("(255, 0, 0)") crash, not a passed-through string).
    def fn(color: Tuple[int, int, int] = (255, 0, 0), scale: Tuple[float, float, float] = (1.0, 1.0, 1.0)) -> None: ...

    assert _coerce_kwargs(fn, {"color": "(255, 0, 0)"}) == {"color": (255, 0, 0)}
    assert _coerce_kwargs(fn, {"color": "255, 0, 0"}) == {"color": (255, 0, 0)}
    assert _coerce_kwargs(fn, {"color": "[255, 0, 0]"}) == {"color": (255, 0, 0)}
    assert _coerce_kwargs(fn, {"scale": "(0.5, 0.5, 0.5)"}) == {"scale": (0.5, 0.5, 0.5)}


def test_coerce_kwargs_list_branch_parses_literal_then_comma_splits() -> None:
    # A List param's STRING widget: a "[...]" literal (the empty ``[]`` default + an explicit box list)
    # parses via ast/json (a bare comma-split of "[]" would yield the junk ``["[]"]``); a plain
    # comma string still splits.
    def fn(boxes: List[Tuple[int, int, int, int]] = [], names: List[str] = []) -> None: ...

    assert _coerce_kwargs(fn, {"boxes": "[]"}) == {"boxes": []}
    assert _coerce_kwargs(fn, {"boxes": "[(20, 10, 80, 50)]"}) == {"boxes": [(20, 10, 80, 50)]}
    assert _coerce_kwargs(fn, {"names": "a, b, c"}) == {"names": ["a", "b", "c"]}


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
    assert inputs["required"]["count"] == ("INT", {**DEFAULT_WIDGET_CONFIG["INT"], "default": 0})


def test_create_comfy_node_string_return_type() -> None:
    def op() -> str:
        return "hi"

    assert create_comfy_node(op, "StrReturnNode").RETURN_TYPES == ("STRING",)


# --------------------------------------------------------------------------- #
# Literal[...] -> combo dropdown (closed-Literal enumeration)
# --------------------------------------------------------------------------- #


def test_literal_options_extracts_members() -> None:
    assert _literal_options(Literal["a", "b", "c"]) == ["a", "b", "c"]
    # Unwrap Optional[Literal[...]].
    assert _literal_options(Optional[Literal["x", "y"]]) == ["x", "y"]
    # Non-Literal annotations return None (so the scalar/path heuristics still run).
    assert _literal_options(str) is None
    assert _literal_options(int) is None


def test_literal_param_becomes_combo_with_default() -> None:
    def op(colormap: Literal["viridis", "gray", "hot"] = "gray") -> str:
        return colormap

    required = create_comfy_node(op, "LiteralNode").INPUT_TYPES()["required"]
    options, config = required["colormap"]
    # The combo's options ARE the literal members (a list, ComfyUI's enumeration form), default honoured.
    assert options == ["viridis", "gray", "hot"]
    assert config["default"] == "gray"


def test_literal_param_default_falls_back_to_first_option() -> None:
    # A Literal param with no default uses the first member as the combo default.
    def op(mode: Literal["fast", "slow"]) -> str:
        return mode

    required = create_comfy_node(op, "LiteralNoDefaultNode").INPUT_TYPES()["required"]
    options, config = required["mode"]
    assert options == ["fast", "slow"]
    assert config["default"] == "fast"


def test_coerce_kwargs_passes_literal_value_through() -> None:
    # A combo value must NOT be run through the STRING list/int heuristics, even if a member
    # contains "list"/"int" — it is the exact constructor value.
    def op(mode: Literal["int8", "list_mode", "plain"] = "plain") -> str:
        return mode

    assert _coerce_kwargs(op, {"mode": "int8"}) == {"mode": "int8"}
    assert _coerce_kwargs(op, {"mode": "list_mode"}) == {"mode": "list_mode"}


def test_optional_literal_none_default_offers_blank_use_default() -> None:
    # An Optional[Literal[...]] that defaults to None (e.g. pl.Trainer.precision) renders a combo with a
    # leading blank "(use default)" entry SELECTED by default — NOT the first member (the precision=64 →
    # float64-on-MPS footgun). The blank coerces to OMIT so the constructor's own default applies.
    def op(precision: Optional[Literal[64, 32, 16]] = None, mode: Literal["a", "b"] = "a") -> str:
        return str(precision)

    required = create_comfy_node(op, "OptLiteralNode").INPUT_TYPES()["required"]
    opts, config = required["precision"]
    assert opts[0] == "" and config["default"] == ""  # blank leads + is the default
    assert "64" in opts  # members still selectable
    # blank -> omitted (ctor default None applies); an explicit pick is kept verbatim.
    assert _coerce_kwargs(op, {"precision": ""}) == {}
    assert _coerce_kwargs(op, {"precision": "16"}) == {"precision": "16"}
    # a concrete-default Literal (mode="a") is unaffected: no blank, default stays "a".
    m_opts, m_config = required["mode"]
    assert "" not in m_opts and m_config["default"] == "a"


def test_real_lightning_trainer_precision_defaults_to_blank() -> None:
    # Regression on the actual pl.Trainer: precision (Optional[Literal[64,32,16],…]=None) no longer
    # defaults to 64 — it defaults blank/omitted, so an imported Trainer uses Lightning's 32-bit default
    # (float32) instead of forcing float64 (which crashes on MPS).
    import marainer.configurables  # noqa: F401
    import pytorch_lightning as pl

    required = create_comfy_node(pl.Trainer, "PLTrainerPrec").INPUT_TYPES()["required"]
    opts, config = required["precision"]
    assert config["default"] == ""
    assert _coerce_kwargs(pl.Trainer, {"precision": ""}) == {}


def test_optional_bool_none_default_is_tristate_combo() -> None:
    # Optional[bool] = None (e.g. pl.Trainer.enable_progress_bar) renders a 3-state combo
    # ["", "true", "false"] defaulting BLANK — not a checkbox forced to False (which would silently
    # disable a feature the library enables by default). Blank → None (ctor default); true/false → bool.
    def op(flag: Optional[bool] = None, hard: bool = True) -> str:
        return str(flag)

    required = create_comfy_node(op, "OptBoolNode").INPUT_TYPES()["required"]
    opts, config = required["flag"]
    assert opts == ["", "true", "false"] and config["default"] == ""
    assert _coerce_kwargs(op, {"flag": ""}) == {"flag": None}
    assert _coerce_kwargs(op, {"flag": "true"}) == {"flag": True}
    assert _coerce_kwargs(op, {"flag": "false"}) == {"flag": False}
    # A concrete-default bool stays a plain BOOLEAN checkbox.
    assert required["hard"][0] == "BOOLEAN" and required["hard"][1]["default"] is True


def test_real_lightning_trainer_enable_flags_default_blank() -> None:
    # Regression: pl.Trainer's Optional[bool]=None flags no longer force False (which disabled the
    # progress bar / checkpointing in an imported eval) — they default blank → Lightning's own default.
    import marainer.configurables  # noqa: F401
    import pytorch_lightning as pl

    required = create_comfy_node(pl.Trainer, "PLTrainerFlags").INPUT_TYPES()["required"]
    for flag in ("enable_progress_bar", "enable_checkpointing", "enable_model_summary"):
        opts, config = required[flag]
        assert opts == ["", "true", "false"] and config["default"] == ""
    assert _coerce_kwargs(pl.Trainer, {"enable_progress_bar": ""}) == {"enable_progress_bar": None}


def test_node_execution_tuple_return_passthrough() -> None:
    # When the wrapped callable already returns a tuple, execute_wrapper must
    # forward it verbatim rather than nesting it in another 1-tuple.
    def op() -> tuple:
        return ("a", "b")

    node = create_comfy_node(op, "TupleNode")()
    result = node.execute()["result"]
    assert result == ("a", "b")
    assert len(result) == 2


# --------------------------------------------------------------------------- #
# register_module_nodes
# --------------------------------------------------------------------------- #


def _sample_op(x: int) -> str:
    """Module-level so resolve_callable mocking has a real callable to hand back."""
    return str(x)


# Registration is a POSITIVE allowlist on the @configurable category (op/source), so the
# fake callable must carry one to be registered at all.
_sample_op.__confluid_category__ = "op"  # type: ignore[attr-defined]


def test_register_module_nodes_registers_callable() -> None:
    mappings: dict = {}
    display: dict = {}
    discovered = [{"path": "fake.mod:sample_op", "name": "sample_op"}]

    with patch("fluxstudio.bridge.scan_module", return_value=discovered):
        with patch("fluxstudio.bridge.resolve_callable", return_value=_sample_op):
            register_module_nodes("fake.mod", mappings, display)

    # Key is qualified by package + category (module "fake.mod" -> "fake", category "op"), so it's
    # globally unique; no group on _sample_op.
    assert "Taidal_fake_op_sample_op" in mappings
    # Palette path is grouped by CATEGORY (not package): an op with no role/group -> Taidal/Op.
    assert mappings["Taidal_fake_op_sample_op"].CATEGORY == "Taidal/Op"
    # Label = [PKG] tag + verbatim identifier (``_`` -> space, NO title-casing).
    assert display["Taidal_fake_op_sample_op"] == "[FAKE] sample op"


def test_register_module_nodes_palette_path_with_group_and_tag() -> None:
    """A grouped op nests as <root>/<Category>/<group> (NOT under the package), and its label carries
    the canonical-cased package tag (dataflux -> [DATAFLUX]). The registration KEY still uses RAW
    package+category+group identifiers (globally unique)."""

    def _grouped_op(x: int) -> str:
        return str(x)

    _grouped_op.__confluid_category__ = "op"  # type: ignore[attr-defined]
    _grouped_op.__confluid_group__ = "numpy"  # type: ignore[attr-defined]

    mappings: dict = {}
    display: dict = {}
    discovered = [{"path": "dataflux.ops.numpy:grouped_op", "name": "grouped_op"}]

    with patch("fluxstudio.bridge.scan_module", return_value=discovered):
        with patch("fluxstudio.bridge.resolve_callable", return_value=_grouped_op):
            register_module_nodes("dataflux.ops.numpy", mappings, display)

    assert "Taidal_dataflux_op_numpy_grouped_op" in mappings
    assert mappings["Taidal_dataflux_op_numpy_grouped_op"].CATEGORY == "Taidal/Op/numpy"
    assert display["Taidal_dataflux_op_numpy_grouped_op"] == "[DATAFLUX] grouped op"


def test_package_tag_uses_dist_name_when_provided() -> None:
    """dist_name (ep.dist.name) takes precedence over the module path so companion namespace
    packages like waivefront-helios produce [WAIVEFRONT-HELIOS] instead of [WAIVEFRONT]."""
    assert _package_tag("waivefront.helios", dist_name="waivefront-helios") == "WAIVEFRONT-HELIOS"
    assert _package_tag("waivefront.torchsig.synthetic", dist_name="waivefront-torchsig") == "WAIVEFRONT-TORCHSIG"
    assert _package_tag("waivefront.processing", dist_name="waivefront") == "WAIVEFRONT"
    assert _package_tag("sonair", dist_name="sonair") == "SONAIR"


def test_package_tag_falls_back_to_top_level_package_without_dist_name() -> None:
    """When dist_name is absent the fallback is the first dotted segment uppercased."""
    assert _package_tag("waivefront.processing") == "WAIVEFRONT"
    assert _package_tag("dataflux.ops.numpy") == "DATAFLUX"
    assert _package_tag("sonair") == "SONAIR"


def test_register_module_nodes_label_uses_dist_name() -> None:
    """Passing dist_name to register_module_nodes surfaces the correct [PKG] tag."""
    mappings: dict = {}
    display: dict = {}
    discovered = [{"path": "waivefront.helios:helios_op", "name": "helios_op"}]

    def _helios_op(x: int) -> str:
        return str(x)

    _helios_op.__confluid_category__ = "op"  # type: ignore[attr-defined]

    with patch("fluxstudio.bridge.scan_module", return_value=discovered):
        with patch("fluxstudio.bridge.resolve_callable", return_value=_helios_op):
            register_module_nodes("waivefront.helios", mappings, display, dist_name="waivefront-helios")

    key = "Taidal_waivefront_op_helios_op"
    assert key in display
    assert display[key] == "[WAIVEFRONT-HELIOS] helios op"


def test_register_module_nodes_same_name_different_module_no_collision() -> None:
    """Two same-named ops in different modules (e.g. numpy vs torch ``RescaleOp``) must BOTH
    register — the package/category/group-qualified key keeps them distinct instead of the second
    silently overwriting the first in NODE_CLASS_MAPPINGS."""

    def _numpy_rescale(x: int) -> str:
        return str(x)

    def _torch_rescale(x: int) -> str:
        return str(x)

    for fn, grp in ((_numpy_rescale, "numpy"), (_torch_rescale, "torch")):
        fn.__confluid_category__ = "op"  # type: ignore[attr-defined]
        fn.__confluid_group__ = grp  # type: ignore[attr-defined]

    mappings: dict = {}
    display: dict = {}

    for mod, fn in (("dataflux.ops.numpy", _numpy_rescale), ("dataflux.ops.torch", _torch_rescale)):
        with patch("fluxstudio.bridge.scan_module", return_value=[{"path": f"{mod}:RescaleOp", "name": "RescaleOp"}]):
            with patch("fluxstudio.bridge.resolve_callable", return_value=fn):
                register_module_nodes(mod, mappings, display)

    # Both survive under distinct keys, in distinct palette folders, with the SAME [PKG]-tagged label.
    assert "Taidal_dataflux_op_numpy_RescaleOp" in mappings
    assert "Taidal_dataflux_op_torch_RescaleOp" in mappings
    assert mappings["Taidal_dataflux_op_numpy_RescaleOp"].CATEGORY == "Taidal/Op/numpy"
    assert mappings["Taidal_dataflux_op_torch_RescaleOp"].CATEGORY == "Taidal/Op/torch"
    assert display["Taidal_dataflux_op_numpy_RescaleOp"] == "[DATAFLUX] RescaleOp"
    assert display["Taidal_dataflux_op_torch_RescaleOp"] == "[DATAFLUX] RescaleOp"


def test_hide_default_comfy_nodes_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Drives both the default-node prune and the palette root. Truthy spellings are accepted.
    monkeypatch.delenv("FLUXSTUDIO_HIDE_DEFAULT_NODES", raising=False)
    assert hide_default_comfy_nodes() is False
    assert node_root() == "Taidal"
    for truthy in ("1", "true", "YES", "on"):
        monkeypatch.setenv("FLUXSTUDIO_HIDE_DEFAULT_NODES", truthy)
        assert hide_default_comfy_nodes() is True
        assert node_root() == ""  # root-level palette when defaults are hidden
    for falsy in ("0", "", "no", "off"):
        monkeypatch.setenv("FLUXSTUDIO_HIDE_DEFAULT_NODES", falsy)
        assert hide_default_comfy_nodes() is False
        assert node_root() == "Taidal"


def test_node_category_groups_by_category_or_role_task() -> None:
    # Ops/sources (no role) group by CATEGORY then group; a role-bearing class by ROLE then TASK.
    # The package is NOT in the path (it's the [PKG] label tag).
    assert _node_category("op", "numpy", "Taidal") == "Taidal/Op/numpy"
    assert _node_category("op", None, "Taidal") == "Taidal/Op"
    assert _node_category("source", None, "Taidal") == "Taidal/Source"
    # Role-first, then task; a task-agnostic role (logger) has no task level.
    assert _node_category("classification_model", None, "Taidal", role="model", task="classification") == (
        "Taidal/Model/Classification"
    )
    assert _node_category(None, None, "Taidal", role="logger", task=None) == "Taidal/Logger"


def test_node_category_empty_root_drops_taidal_folder() -> None:
    # An empty root (--no-default-nodes) drops the leading Taidal/ WITHOUT emitting a leading slash.
    assert _node_category("op", "numpy", "") == "Op/numpy"
    assert _node_category("classification_model", None, "", role="model", task="classification") == (
        "Model/Classification"
    )


def test_register_module_nodes_root_level_when_defaults_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    # When --no-default-nodes is active (env set), register_module_nodes defaults root to "" so the
    # node sits at the palette root (DataFlux/Op) rather than under a Taidal/ folder.
    monkeypatch.setenv("FLUXSTUDIO_HIDE_DEFAULT_NODES", "1")
    mappings: dict = {}
    display: dict = {}
    discovered = [{"path": "fake.mod:sample_op", "name": "sample_op"}]

    with patch("fluxstudio.bridge.scan_module", return_value=discovered):
        with patch("fluxstudio.bridge.resolve_callable", return_value=_sample_op):
            register_module_nodes("fake.mod", mappings, display)

    # Key is unchanged (raw identifiers, not the display path) so saved workflows still resolve.
    assert "Taidal_fake_op_sample_op" in mappings
    # ...but the palette path no longer has the Taidal/ root.
    assert mappings["Taidal_fake_op_sample_op"].CATEGORY == "Op"


def test_register_module_nodes_preserves_camelcase_display_name() -> None:
    """``.title()`` used to mangle CamelCase (HuggingFaceSource -> Huggingfacesource)."""
    mappings: dict = {}
    display: dict = {}
    discovered = [{"path": "fake.mod:HuggingFaceSource", "name": "HuggingFaceSource"}]

    with patch("fluxstudio.bridge.scan_module", return_value=discovered):
        with patch("fluxstudio.bridge.resolve_callable", return_value=_sample_op):
            register_module_nodes("fake.mod", mappings, display)

    assert display["Taidal_fake_op_HuggingFaceSource"] == "[FAKE] HuggingFaceSource"


def test_register_module_nodes_honors_display_name_override() -> None:
    """A ``__taidal_display_name__`` stamp overrides the palette LABEL; the registration KEY is unaffected.

    The waivefront-helios bridge sets it to the class name minus the leading ``Helios`` so the label reads
    cleanly (``ThresholdOp``) while the callable name — the confluid registry key AND the node key segment —
    keeps the prefix (``HeliosThresholdOp``), avoiding a flat-registry collision with the native dataflux op.
    """
    mappings: dict = {}
    display: dict = {}
    discovered = [{"path": "waivefront.helios.ops:HeliosThresholdOp", "name": "HeliosThresholdOp"}]

    def _helios_threshold(x: int) -> str:
        return str(x)

    _helios_threshold.__confluid_category__ = "op"  # type: ignore[attr-defined]
    _helios_threshold.__taidal_display_name__ = "ThresholdOp"  # type: ignore[attr-defined]

    with patch("fluxstudio.bridge.scan_module", return_value=discovered):
        with patch("fluxstudio.bridge.resolve_callable", return_value=_helios_threshold):
            register_module_nodes("waivefront.helios.ops", mappings, display, dist_name="waivefront-helios")

    key = "Taidal_waivefront_op_HeliosThresholdOp"  # KEY keeps the prefixed callable name (registry-safe)
    assert key in display
    assert display[key] == "[WAIVEFRONT-HELIOS] ThresholdOp"  # LABEL drops the prefix


def test_register_module_nodes_label_without_override_uses_callable_name() -> None:
    """No ``__taidal_display_name__`` stamp → the label is the verbatim callable name (unchanged behaviour)."""
    mappings: dict = {}
    display: dict = {}
    discovered = [{"path": "fake.mod:PlainOp", "name": "PlainOp"}]

    with patch("fluxstudio.bridge.scan_module", return_value=discovered):
        with patch("fluxstudio.bridge.resolve_callable", return_value=_sample_op):
            register_module_nodes("fake.mod", mappings, display)

    assert display["Taidal_fake_op_PlainOp"] == "[FAKE] PlainOp"


def test_register_module_nodes_skips_private_callables() -> None:
    """Underscore-prefixed callables (internal helpers) must never become nodes."""
    mappings: dict = {}
    display: dict = {}
    discovered = [
        {"path": "fake.mod:_to_uint8", "name": "_to_uint8"},
        {"path": "fake.mod:sample_op", "name": "sample_op"},
    ]

    with patch("fluxstudio.bridge.scan_module", return_value=discovered):
        with patch("fluxstudio.bridge.resolve_callable", return_value=_sample_op):
            register_module_nodes("fake.mod", mappings, display)

    assert not any("to_uint8" in key for key in mappings)
    assert "Taidal_fake_op_sample_op" in mappings


def test_register_module_nodes_is_a_positive_category_allowlist() -> None:
    """Only ``category in {op, source, engine, sink}`` registers; task datasets AND uncategorised
    are excluded.

    The generic engines (``Flux``/``JointFlux``) are ``category="engine"`` and now DO register
    (they compose on the canvas via wired source/op sockets). DataFlux storage sinks tagged
    ``category="sink"`` register too (object-member producers wired into a ``DatasetProcessor`` node).
    The raw-callable wrappers ``FilterOp``/``WrappedOp`` are UNcategorised (neither op nor engine), so
    they stay out even with ``engine`` allowed; task datasets are ``*_dataset`` (a trainer-config
    concept, not a canvas one); an UNtagged sink (a ``Storage`` subclass without ``category="sink"``)
    and uncategorised helper functions carry no category. A class appears only once it is deliberately
    tagged op/source/engine/sink.
    """

    class _FakeEngine:  # category="engine" -> NOW kept (canvas-composable)
        __confluid_category__ = "engine"

        def __getitem__(self, i: int) -> Sample:
            return Sample(input=i, target=None, metadata={})

    class _FakeDataset:  # task dataset -> excluded (dataset is an MCP/trainer concept)
        __confluid_category__ = "classification_dataset"

        def __getitem__(self, i: int) -> Sample:
            return Sample(input=i, target=None, metadata={})

    class _FakeUncategorised:  # no category -> excluded (the key allowlist behaviour)
        def __getitem__(self, i: int) -> Sample:
            return Sample(input=i, target=None, metadata={})

    class _FakeSource:  # category="source" -> kept
        __confluid_category__ = "source"

        def __getitem__(self, i: int) -> Sample:
            return Sample(input=i, target=None, metadata={})

    class _FakeSink:  # category="sink" -> kept (a DataFlux storage sink -> DatasetProcessor.sink)
        __confluid_category__ = "sink"

        def write(self, sample: Sample) -> None: ...

    mappings: dict = {}
    display: dict = {}
    discovered = [
        {"path": "fake.mod:Engine", "name": "Engine"},
        {"path": "fake.mod:Dataset", "name": "Dataset"},
        {"path": "fake.mod:Uncat", "name": "Uncat"},
        {"path": "fake.mod:RealSource", "name": "RealSource"},
        {"path": "fake.mod:RealSink", "name": "RealSink"},
    ]
    resolved = {
        "fake.mod:Engine": _FakeEngine,
        "fake.mod:Dataset": _FakeDataset,
        "fake.mod:Uncat": _FakeUncategorised,
        "fake.mod:RealSource": _FakeSource,
        "fake.mod:RealSink": _FakeSink,
    }

    with patch("fluxstudio.bridge.scan_module", return_value=discovered):
        with patch("fluxstudio.bridge.resolve_callable", side_effect=lambda p: resolved[p]):
            register_module_nodes("fake.mod", mappings, display)

    assert "Taidal_fake_engine_Engine" in mappings  # engine now kept (canvas-composable)
    assert not any("Dataset" in key for key in mappings)  # task dataset excluded
    assert not any("Uncat" in key for key in mappings)  # uncategorised excluded (positive allowlist)
    assert "Taidal_fake_source_RealSource" in mappings  # tagged source kept
    assert "Taidal_fake_sink_RealSink" in mappings  # tagged sink kept (DatasetProcessor sink node)


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
        # Bound-checked so Python's sequence-protocol iteration (no __iter__) terminates at
        # ``count`` — otherwise iterating a Flux wrapping this source loops forever.
        if index < 0:
            index += self.count
        if not 0 <= index < self.count:
            raise IndexError(index)
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
    (out,) = node.execute(path="x", count=2)["result"]
    assert isinstance(out, Flux)
    assert len(out) == 2


def test_op_class_becomes_dual_mode_sample_node() -> None:
    node = create_comfy_node(_FakeOp, "Taidal__FakeOp")
    # Dual-mode: SAMPLE (applied) + OP (the constructed op) outputs.
    assert node.RETURN_TYPES == (SAMPLE_TYPE, OP_TYPE)
    assert node.RETURN_NAMES == ("sample", "op")
    spec = node.INPUT_TYPES()
    # ``bump`` config is a required widget; ``sample`` is OPTIONAL (so the node can be an op-value).
    assert spec["required"]["bump"][0] == "INT"
    assert spec["optional"]["sample"] == (SAMPLE_TYPE,)


def test_negative_float_default_sets_min_in_widget_config() -> None:
    """A FLOAT param with a negative default gets ``min`` set so ComfyUI allows negative input."""
    from confluid import configurable as _cfg

    @_cfg(category="op")
    class _NegDefaultOp:
        def __init__(self, noise_power_db: float = -30.0) -> None:
            self.noise_power_db = noise_power_db

        def __call__(self, sample: Sample) -> Sample:
            return sample

    spec = create_comfy_node(_NegDefaultOp, "Taidal__NegDefaultOp").INPUT_TYPES()
    widget_type, widget_cfg = spec["required"]["noise_power_db"]
    assert widget_type == "FLOAT"
    assert widget_cfg["default"] == -30.0
    assert "min" in widget_cfg and widget_cfg["min"] < 0


def test_numeric_widgets_always_carry_full_range_bounds() -> None:
    """EVERY numeric widget carries explicit wide min/max — the ComfyUI frontend clamps
    a spec without them to ``[0, 2048]``, which silently blocked negative dB values
    (``TorchSigMetadataOp.noise_power_db = -120``, whose DEFAULT is non-negative) and
    large ones (``sample_rate = 1e7``)."""
    from confluid import configurable as _cfg

    @_cfg(category="op")
    class _PowerOp:
        def __init__(self, noise_power_db: float = 0.0, num_taps: int = 1) -> None:
            self.noise_power_db = noise_power_db
            self.num_taps = num_taps

        def __call__(self, sample: Sample) -> Sample:
            return sample

    spec = create_comfy_node(_PowerOp, "Taidal__PowerOp").INPUT_TYPES()
    float_type, float_cfg = spec["required"]["noise_power_db"]
    assert float_type == "FLOAT"
    assert float_cfg["min"] <= -120.0
    assert float_cfg["max"] >= 1e7
    int_type, int_cfg = spec["required"]["num_taps"]
    assert int_type == "INT"
    assert int_cfg["min"] <= -(2**31)
    assert int_cfg["max"] >= 2**31


def test_range_widgets_carry_full_range_bounds() -> None:
    """The ``__lo``/``__hi`` pair of a Tuple range param inherits the wide bounds —
    dB ranges are routinely negative (``noise_power_low=(-80.0, -60.0)``)."""
    from typing import Tuple as _Tuple

    from confluid import configurable as _cfg

    @_cfg(category="op")
    class _RangeOp:
        def __init__(self, noise_power_low: _Tuple[float, float] = (-80.0, -60.0)) -> None:
            self.noise_power_low = noise_power_low

        def __call__(self, sample: Sample) -> Sample:
            return sample

    spec = create_comfy_node(_RangeOp, "Taidal__RangeOp").INPUT_TYPES()
    lo_type, lo_cfg = spec["required"]["noise_power_low__lo"]
    hi_type, hi_cfg = spec["required"]["noise_power_low__hi"]
    assert lo_type == hi_type == "FLOAT"
    assert lo_cfg["default"] == -80.0 and hi_cfg["default"] == -60.0
    assert lo_cfg["min"] <= -120.0 and hi_cfg["min"] <= -120.0
    assert lo_cfg["max"] >= 1e7 and hi_cfg["max"] >= 1e7


def test_op_node_constructs_and_applies() -> None:
    node = create_comfy_node(_FakeOp, "Taidal__FakeOp")()
    applied, op = node.execute(sample=Sample(input=10), bump=5)["result"]
    assert applied.input == 15
    assert isinstance(op, _FakeOp)  # the constructed op is exposed on the second output


def test_op_node_value_mode_without_sample() -> None:
    # Leave ``sample`` unconnected -> applied is None, but the op is still constructed and exposed.
    applied, op = create_comfy_node(_FakeOp, "Taidal__FakeOp")().execute(bump=3)["result"]
    assert applied is None
    assert isinstance(op, _FakeOp) and op.bump == 3


def test_op_node_raises_when_op_filters_sample() -> None:
    node = create_comfy_node(_FilterishOp, "Taidal__Filterish")()
    with pytest.raises(RuntimeError, match="filtered out"):
        node.execute(sample=Sample(input=1))


def test_random_op_node_has_is_changed() -> None:
    from confluid import configurable as _cfg

    @_cfg(category="op", random=True)
    class _RandomOp:
        def __init__(self) -> None: ...

        def __call__(self, sample: Sample) -> Sample:
            return sample

    node = create_comfy_node(_RandomOp, "Taidal__RandomOp")
    assert hasattr(node, "IS_CHANGED")
    assert node.IS_CHANGED() != node.IS_CHANGED()  # nan != nan → always considered changed


def test_deterministic_op_node_has_no_is_changed() -> None:
    node = create_comfy_node(_FakeOp, "Taidal__FakeOpDeterministic")
    assert not hasattr(node, "IS_CHANGED")


def test_op_node_with_output_property_adds_extra_sockets() -> None:
    """An op that declares @output properties gets extra output sockets beyond (sample, op)."""
    from confluid import configurable as _cfg
    from confluid.decorators import output as _output

    @_cfg(category="op")
    class _OpWithOutput:
        def __init__(self) -> None:
            self._last: str = ""

        @property
        @_output
        def last_label(self) -> str:
            return self._last

        def __call__(self, sample: Sample) -> Sample:
            self._last = str(sample.input)
            return sample

    node_cls = create_comfy_node(_OpWithOutput, "Taidal__OpWithOutput")
    # Three outputs: sample, op, last_label (STRING because str is scalar)
    assert node_cls.RETURN_TYPES == (SAMPLE_TYPE, OP_TYPE, "STRING")
    assert node_cls.RETURN_NAMES == ("sample", "op", "last_label")


def test_op_node_output_property_returns_live_value_after_call() -> None:
    """The @output socket carries the live value that the op property returns after __call__."""
    import json
    from typing import List as _List

    from confluid import configurable as _cfg
    from confluid.decorators import output as _output

    @_cfg(category="op")
    class _OpWithOutput:
        def __init__(self) -> None:
            self._applied: _List[str] = []

        @property
        @_output
        def applied_names(self) -> _List[str]:
            return self._applied

        def __call__(self, sample: Sample) -> Sample:
            self._applied = ["x"]
            return sample

    node = create_comfy_node(_OpWithOutput, "Taidal__OpWithOutput2")()
    _applied_sample, _op, names_json = node.execute(sample=Sample(input=1))["result"]
    assert json.loads(names_json) == ["x"]


def test_op_node_output_property_returns_pre_call_default_without_sample() -> None:
    """When sample is None (op-value mode), @output carries the pre-call default."""
    import json
    from typing import List as _List

    from confluid import configurable as _cfg
    from confluid.decorators import output as _output

    @_cfg(category="op")
    class _OpWithOutput:
        def __init__(self) -> None:
            self._applied: _List[str] = []

        @property
        @_output
        def applied_names(self) -> _List[str]:
            return self._applied

        def __call__(self, sample: Sample) -> Sample:
            self._applied = ["x"]
            return sample

    node = create_comfy_node(_OpWithOutput, "Taidal__OpWithOutput3")()
    _none, _op, names_json = node.execute()["result"]
    assert json.loads(names_json) == []


class _FakeHigherOrderOp:
    """A higher-order op: its constructor takes a list of OPS and applies them in sequence."""

    def __init__(self, ops: list) -> None:  # type: ignore[type-arg]
        self.ops = ops

    def __call__(self, sample: Sample) -> Sample:
        for op in self.ops:
            sample = op(sample)
        return sample


def test_higher_order_op_gets_op_list_inputs_and_composes() -> None:
    node_cls = create_comfy_node(_FakeHigherOrderOp, "Taidal__HOO")
    spec = node_cls.INPUT_TYPES()
    # The ``ops`` list param is NOT a STRING widget — it's a dynamic DATAFLUX_OP input (seed op_0).
    assert "ops" not in spec["required"]
    assert spec["optional"]["op_0"] == (OP_TYPE,)
    # Build two op-values (bump +1, +2) and compose them via the op-list inputs.
    _, op1 = create_comfy_node(_FakeOp, "Taidal__FakeOp1")().execute(bump=1)["result"]
    _, op2 = create_comfy_node(_FakeOp, "Taidal__FakeOp2")().execute(bump=2)["result"]
    applied, hoo = node_cls().execute(sample=Sample(input=10), op_0=op1, op_1=op2)["result"]
    assert applied.input == 13  # 10 +1 +2, in order
    assert isinstance(hoo, _FakeHigherOrderOp) and len(hoo.ops) == 2


class _FakeHeliosComposite:
    """Mimics a waivefront-helios auto-bridged composite: helios's ``*transforms``
    variadic mirrored as an optional ``transforms`` LIST param."""

    def __init__(self, transforms: list = None) -> None:  # type: ignore[type-arg, assignment]
        self.transforms = transforms or []

    def __call__(self, sample: Sample) -> Sample:
        for op in self.transforms:
            sample = op(sample)
        return sample


def test_transforms_list_param_gets_op_list_inputs() -> None:
    """``transforms`` (the helios-bridge convention) is an op-list name like ``ops``/``branches``."""
    node_cls = create_comfy_node(_FakeHeliosComposite, "Taidal__HeliosComposite")
    spec = node_cls.INPUT_TYPES()
    assert "transforms" not in spec["required"] and "transforms" not in spec["optional"]
    assert spec["optional"]["op_0"] == (OP_TYPE,)
    _, op1 = create_comfy_node(_FakeOp, "Taidal__FakeOpT1")().execute(bump=5)["result"]
    applied, composite = node_cls().execute(sample=Sample(input=10), op_0=op1)["result"]
    assert applied.input == 15
    assert isinstance(composite, _FakeHeliosComposite) and len(composite.transforms) == 1


def test_execute_always_returns_ui_timing_dict() -> None:
    """Every generated node's execute returns {"ui": {"text": [timing]}, "result": ...}."""
    node = create_comfy_node(_FakeSource, "Taidal__TimingSrc")()
    raw = node.execute(path="x", count=1)
    assert isinstance(raw, dict), "execute must return a dict with ui+result"
    assert "ui" in raw and "result" in raw
    text = raw["ui"]["text"]
    assert len(text) >= 1
    assert "ms" in text[-1], f"timing label expected in text, got {text}"


def test_execute_timing_is_non_negative() -> None:
    node = create_comfy_node(_FakeOp, "Taidal__TimingOp")()
    raw = node.execute(sample=Sample(input=0), bump=0)
    text = raw["ui"]["text"][-1]
    # Extract the numeric part: "⏱ 12 ms" -> 12.0
    import re

    match = re.search(r"([\d.]+)\s*ms", text)
    assert match is not None, f"expected ms in timing label, got {text!r}"
    assert float(match.group(1)) >= 0


def test_generic_function_with_sample_param_gets_sample_socket() -> None:
    def describe(sample: Sample, prefix: str = "x") -> str:
        return prefix + str(sample.input)

    inputs = create_comfy_node(describe, "Taidal__Describe").INPUT_TYPES()["required"]
    assert inputs["sample"] == (SAMPLE_TYPE,)
    assert inputs["prefix"][0] == "STRING"


# --------------------------------------------------------------------------- #
# Engine composition: source / source-list / op-list ctor params -> wired sockets
# (so DatasetSplit / Flux / JointFlux build on the canvas from other nodes' outputs)
# --------------------------------------------------------------------------- #


class _FakeView:
    """A view-source requiring ANOTHER source (``source``, no default) + a scalar widget.

    Mirrors ``DatasetSplit``'s shape so the source-input socket is tested without the real class."""

    def __init__(self, source: object, start: int = 0) -> None:
        self._items: List[Sample] = [source[i] for i in range(start, len(source))]  # type: ignore[index, arg-type]

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, index: int) -> Sample:
        return self._items[index]


def test_required_source_param_is_a_wired_socket_not_widget() -> None:
    spec = create_comfy_node(_FakeView, "Taidal__FakeView").INPUT_TYPES()
    # ``source`` has no default -> REQUIRED DATAFLUX_SOURCE input (NOT a STRING widget).
    assert spec["required"]["source"] == (SOURCE_TYPE,)
    assert spec["required"]["start"][0] == "INT"  # plain scalar stays a widget


def test_source_node_consumes_a_wired_source() -> None:
    from dataflux.core import Flux

    upstream = create_comfy_node(_FakeSource, "Taidal__Up")()
    (src,) = upstream.execute(path="x", count=4)["result"]  # a Flux of 4 samples (inputs 0..3)
    (out,) = create_comfy_node(_FakeView, "Taidal__View")().execute(source=src, start=1)["result"]
    assert isinstance(out, Flux)
    assert [s.input for s in out] == [1, 2, 3]  # index 0 dropped by start=1


def test_flux_engine_node_wires_source_and_op_collection() -> None:
    from dataflux.core import Flux

    node_cls = create_comfy_node(Flux, "Taidal__Flux")
    spec = node_cls.INPUT_TYPES()
    # source defaults to None -> OPTIONAL socket; ops (List) -> dynamic op_0 seed; chunk_size widget.
    assert spec["optional"]["source"] == (SOURCE_TYPE,)
    assert spec["optional"]["op_0"] == (OP_TYPE,)
    assert "ops" not in spec.get("required", {}) and "source" not in spec.get("required", {})

    (src,) = create_comfy_node(_FakeSource, "Taidal__Up2")().execute(path="x", count=3)["result"]
    _, op1 = create_comfy_node(_FakeOp, "Taidal__Op1")().execute(bump=1)["result"]
    (out,) = node_cls().execute(source=src, op_0=op1)["result"]
    assert isinstance(out, Flux)
    assert [s.input for s in out] == [1, 2, 3]  # each of inputs 0,1,2 bumped +1


def test_jointflux_engine_node_wires_source_list() -> None:
    from dataflux.core import Flux, JointFlux

    node_cls = create_comfy_node(JointFlux, "Taidal__JointFlux")
    spec = node_cls.INPUT_TYPES()
    # fluxes (List[Flux]) -> dynamic source_0 seed (DATAFLUX_SOURCE), NOT a STRING widget.
    assert spec["optional"]["source_0"] == (SOURCE_TYPE,)
    assert "fluxes" not in spec.get("required", {})

    (a,) = create_comfy_node(_FakeSource, "Taidal__A")().execute(path="a", count=2)["result"]
    (b,) = create_comfy_node(_FakeSource, "Taidal__B")().execute(path="b", count=3)["result"]
    (out,) = node_cls().execute(source_0=a, source_1=b)["result"]
    assert isinstance(out, Flux)
    assert len(out) == 5  # 2 + 3 concatenated


def test_real_dataset_split_node_wires_source_and_splits() -> None:
    from dataflux.core import Flux
    from dataflux.sources import DatasetSplit

    node_cls = create_comfy_node(DatasetSplit, "Taidal__DatasetSplit")
    spec = node_cls.INPUT_TYPES()
    # ``source`` is now defaulted (lazy / zero-arg convention) -> wired socket in the OPTIONAL
    # bucket (required-when-no-default, else optional); split/val_fraction/seed stay widgets.
    assert spec["optional"]["source"] == (SOURCE_TYPE,)
    assert "source" not in spec.get("required", {})

    (src,) = create_comfy_node(_FakeSource, "Taidal__SplitUp")().execute(path="x", count=10)["result"]
    (out,) = node_cls().execute(source=src, split="val", val_fraction=0.2, seed=0)["result"]
    assert isinstance(out, Flux)
    assert len(out) == 2  # 20% of 10


def test_real_range_source_node_wires_source() -> None:
    from dataflux.core import Flux
    from dataflux.sources import RangeSource

    node_cls = create_comfy_node(RangeSource, "Taidal__RangeSource")
    spec = node_cls.INPUT_TYPES()
    # ``source`` is now defaulted (lazy / zero-arg convention) -> wired DATAFLUX_SOURCE input in the
    # OPTIONAL bucket (required-when-no-default, else optional); start/end stay widgets.
    assert spec["optional"]["source"] == (SOURCE_TYPE,)
    (src,) = create_comfy_node(_FakeSource, "Taidal__RangeUp")().execute(path="x", count=10)["result"]
    (out,) = node_cls().execute(source=src, start=2, end=5)["result"]
    assert isinstance(out, Flux)
    assert [s.input for s in out] == [2, 3, 4]


def test_real_concat_source_node_wires_source_list() -> None:
    from dataflux.core import Flux
    from dataflux.sources import ConcatSource

    node_cls = create_comfy_node(ConcatSource, "Taidal__ConcatSource")
    spec = node_cls.INPUT_TYPES()
    # ``sources`` (List) -> dynamic source_0 seed (DATAFLUX_SOURCE), not a STRING widget.
    assert spec["optional"]["source_0"] == (SOURCE_TYPE,)
    assert "sources" not in spec.get("required", {})
    (a,) = create_comfy_node(_FakeSource, "Taidal__CA")().execute(path="a", count=2)["result"]
    (b,) = create_comfy_node(_FakeSource, "Taidal__CB")().execute(path="b", count=3)["result"]
    (out,) = node_cls().execute(source_0=a, source_1=b)["result"]
    assert isinstance(out, Flux)
    assert len(out) == 5  # 2 + 3 concatenated


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
# Container-input round-trip — a Tuple[str,...] / Dict[...] STRING input socket
# decoding a JSON-encoded container @output (the input-side complement to the
# container-@output JSON encoding). See the "container-input round-trip" mandate.
# --------------------------------------------------------------------------- #


def test_coerce_kwargs_decodes_json_tuple_and_dict() -> None:
    def fn(disabled: Tuple[str, ...] = (), overrides: Optional[Dict[str, float]] = None) -> None: ...

    out = _coerce_kwargs(fn, {"disabled": json.dumps(["A", "B"]), "overrides": json.dumps({"A": 0.5})})
    assert out["disabled"] == ("A", "B")  # JSON array -> tuple
    assert out["overrides"] == {"A": 0.5}  # JSON object -> dict


def test_coerce_kwargs_tuple_accepts_comma_string() -> None:
    def fn(disabled: Tuple[str, ...] = ()) -> None: ...

    # A hand-typed comma string (not JSON) on a Tuple[str,...] param -> tuple of names.
    assert _coerce_kwargs(fn, {"disabled": "A, B ,C"})["disabled"] == ("A", "B", "C")


def test_coerce_kwargs_tuple_accepts_python_literal() -> None:
    """A Tuple[str, ...] widget that holds a PYTHON tuple literal decodes correctly — crucially the
    empty-tuple default ``"()"`` must become ``()`` (falsy), NOT ``("()",)``. The latter (a comma-split
    of ``"()"``) is TRUTHY, which made a helios ``ImpairmentsChain.tx_disabled`` left at its ``()``
    default OVERRIDE the metadata config stamped by ``ImpairmentsTxConfig`` — silently disabling the
    whole tx/rx impairment-config mechanism. The documented ``("IQImbalanceOp", "QuantizeOp")`` form
    must also round-trip."""

    def fn(disabled: Tuple[str, ...] = ()) -> None: ...

    assert _coerce_kwargs(fn, {"disabled": "()"})["disabled"] == ()  # empty tuple repr -> () (falsy!)
    assert _coerce_kwargs(fn, {"disabled": "( )"})["disabled"] == ()  # whitespace tolerated
    assert _coerce_kwargs(fn, {"disabled": '("A", "B")'})["disabled"] == ("A", "B")  # documented form
    assert _coerce_kwargs(fn, {"disabled": "('A',)"})["disabled"] == ("A",)  # single-element literal
    # A non-literal starting with "(" still falls through to the comma-split (best-effort).
    assert _coerce_kwargs(fn, {"disabled": "(A, B"})["disabled"] == ("(A", "B")


def test_coerce_kwargs_non_json_dict_left_raw() -> None:
    def fn(overrides: Optional[Dict[str, float]] = None) -> None: ...

    # A Dict param can't be expressed as a comma string; a non-JSON value passes through unchanged.
    assert _coerce_kwargs(fn, {"overrides": "not json"})["overrides"] == "not json"


def test_coerce_kwargs_accepts_python_dict_literal() -> None:
    # A Dict param typed as a Python dict LITERAL (int keys + single quotes) — natural to type for a
    # `Dict[int, str]` like YoloSink.class_names but INVALID JSON — is accepted via ast.literal_eval.
    # Live regression: `class_names must be a non-empty Dict[int, str]` when the user typed
    # `{0:'BG',1:'5GNR'}` (json.loads rejects it; the value stayed a string and pydantic refused it).
    def fn(class_names: Optional[Dict[int, str]] = None) -> None: ...

    assert _coerce_kwargs(fn, {"class_names": "{0:'BG',1:'5GNR'}"})["class_names"] == {0: "BG", 1: "5GNR"}
    # A `{`-leading value that is neither JSON nor a valid Python literal still passes through raw.
    assert _coerce_kwargs(fn, {"class_names": "{garbage"})["class_names"] == "{garbage"


def test_coerce_kwargs_list_still_comma_splits_after_container_branch() -> None:
    # Regression: List/Sequence params keep their comma-split (NOT JSON-decoded).
    def fn(tags: Optional[List[str]] = None) -> None: ...

    assert _coerce_kwargs(fn, {"tags": "x, y"})["tags"] == ["x", "y"]


def test_has_bare_scalar_union_arm_detects_str_int_arms() -> None:
    # A Union with a bare str/int arm (the value is a scalar, NOT a list to split).
    assert _has_bare_scalar_union_arm(Union[List[int], str, int]) is True
    assert _has_bare_scalar_union_arm(Union[List[int], str]) is True
    assert _has_bare_scalar_union_arm(int | str) is True  # PEP 604
    # Pure list params (incl. Optional) have NO bare scalar arm -> still split.
    assert _has_bare_scalar_union_arm(List[str]) is False
    assert _has_bare_scalar_union_arm(Optional[List[str]]) is False
    assert _has_bare_scalar_union_arm(None) is False


def test_coerce_kwargs_union_with_str_arm_passes_string_through_not_split() -> None:
    # A Union[List[int], str, int] STRING widget value (devices='auto', or the comma-string
    # '1,2,3' the library parses itself) MUST pass through VERBATIM — NOT comma-split into
    # ['auto'] / ['1','2','3'], a "sequence of str" Lightning rejects. The default-graph crash.
    def fn(devices: Union[List[int], str, int] = "auto") -> None: ...

    assert _coerce_kwargs(fn, {"devices": "auto"})["devices"] == "auto"
    assert _coerce_kwargs(fn, {"devices": "1,2,3"})["devices"] == "1,2,3"
    assert _coerce_kwargs(fn, {"devices": "1"})["devices"] == "1"


def test_real_lightning_trainer_devices_default_builds() -> None:
    # Regression on the actual pl.Trainer: the DEFAULT-valued node must build. ``devices``'s
    # default ('auto') is a STRING widget whose annotation (list[int] | str | int) contains
    # "list" — it must NOT be comma-split into ['auto'] (the "you passed a sequence of str"
    # crash that broke every freshly-created Trainer graph). It passes through and constructs.
    import marainer.configurables  # noqa: F401
    import pytorch_lightning as pl

    required = create_comfy_node(pl.Trainer, "PLTrainerDevices").INPUT_TYPES()["required"]
    assert required["devices"][1]["default"] == "auto"
    coerced = _coerce_kwargs(pl.Trainer, {"devices": "auto"})
    assert coerced["devices"] == "auto"
    pl.Trainer(**coerced)  # the live-instance build no longer raises


# --------------------------------------------------------------------------- #
# Compound container params (a LIST whose elements are tuples/pairs), e.g. the
# helios ``Config.keys: str | list[str | tuple[str, str]]`` rename map. ComfyUI
# shows it as a STRING; the generic decoder accepts a Python/JSON literal OR a
# friendly comma list with ``src:dst`` renames. See the container-round-trip mandate.
# --------------------------------------------------------------------------- #


def _config_keys_fn(keys: Union[str, List[Union[str, Tuple[str, str]]]] = "") -> None:
    """Mirrors keysight.ai.transforms.Config.keys (the bridged HeliosConfigOp ``keys`` widget)."""


def test_coerce_kwargs_list_of_pairs_python_literal() -> None:
    # A Python literal with a rename PAIR + a bare key decodes faithfully (real tuples).
    out = _coerce_kwargs(_config_keys_fn, {"keys": '[("snr_db", "snr"), "noise_power_db"]'})
    assert out["keys"] == [("snr_db", "snr"), "noise_power_db"]


def test_coerce_kwargs_list_of_pairs_json_inner_list_becomes_tuple() -> None:
    # JSON has no tuples, so a pair arrives as an inner list — normalised back to a tuple so a
    # pair stays a pair (this is the shape a wired container @output rides as, json.dumps-encoded).
    out = _coerce_kwargs(_config_keys_fn, {"keys": '[["a", "b"], "c"]'})
    assert out["keys"] == [("a", "b"), "c"]


def test_coerce_kwargs_list_of_pairs_colon_comma_convention() -> None:
    # The friendly comma list: a bare item stays a string (src==dst); ``src:dst`` is a rename pair.
    out = _coerce_kwargs(_config_keys_fn, {"keys": "snr_db, noise_power_db:nf, center_freq"})
    assert out["keys"] == ["snr_db", ("noise_power_db", "nf"), "center_freq"]


def test_coerce_kwargs_list_of_pairs_bare_comma_keys() -> None:
    out = _coerce_kwargs(_config_keys_fn, {"keys": "snr_db, noise_power_db"})
    assert out["keys"] == ["snr_db", "noise_power_db"]


def test_coerce_kwargs_list_of_pairs_blank_omitted() -> None:
    # Blank ⇒ omit ⇒ the constructor's own default ("") applies (never a forced [""]).
    assert "keys" not in _coerce_kwargs(_config_keys_fn, {"keys": ""})


def test_coerce_kwargs_list_of_pairs_malformed_literal_falls_back_to_comma() -> None:
    # A "["-leading value that is neither JSON nor a valid Python literal does NOT crash —
    # it falls through to the comma convention (best-effort, like the flat-tuple branch).
    out = _coerce_kwargs(_config_keys_fn, {"keys": "[oops"})
    assert out["keys"] == ["[oops"]


def test_coerce_kwargs_compound_branch_leaves_flat_tuple_and_list_unaffected() -> None:
    # The compound branch is gated on BOTH "list" AND "tuple" in the annotation, so a flat
    # Tuple[str, ...] and a flat List[str] keep their existing handling (no ":" pair splitting).
    def flat_tuple(disabled: Tuple[str, ...] = ()) -> None: ...

    def flat_list(names: Optional[List[str]] = None) -> None: ...

    assert _coerce_kwargs(flat_tuple, {"disabled": "a:b, c"})["disabled"] == ("a:b", "c")  # no pair split
    assert _coerce_kwargs(flat_list, {"names": "a:b, c"})["names"] == ["a:b", "c"]  # no pair split


# --------------------------------------------------------------------------- #
# Value-producer nodes (no-arg __call__, e.g. ImpairmentsTxConfig): config
# widgets -> @output sockets, NO Sample I/O. See the value-node mandate.
# --------------------------------------------------------------------------- #


@configurable(category="op")
class _FakeValueConfig:
    """A value producer: a bool widget + a Tuple @output (no Sample I/O)."""

    def __init__(self, drop_a: bool = False) -> None:
        self._drop_a = drop_a

    @property
    @output
    def disabled(self) -> Tuple[str, ...]:
        return ("A",) if self._drop_a else ()

    def __call__(self) -> Tuple[str, ...]:
        return self.disabled


def test_value_call_signature_detects_no_arg_call() -> None:
    # The no-arg __call__ marks a value producer, NOT an op (an op's first param is a Sample).
    assert _value_call_signature(_FakeValueConfig) is not None
    from fluxstudio.bridge import _op_call_signature

    assert _op_call_signature(_FakeValueConfig) is None


def test_value_node_builds_widgets_and_emits_output_socket() -> None:
    node = create_comfy_node(_FakeValueConfig, "Taidal__FakeValueConfig")
    assert node.TAIDAL_NODE_KIND == "value"
    assert "drop_a" in node.INPUT_TYPES()["required"]  # a real config widget
    # Value node: outputs are ONLY the declared @outputs — no sample/op sockets, no sample input.
    assert node.RETURN_NAMES == ("disabled",)
    assert node.RETURN_TYPES == ("STRING",)
    assert "sample" not in node.INPUT_TYPES().get("optional", {})
    (disabled_json,) = node().execute(drop_a=True)["result"]
    assert json.loads(disabled_json) == ["A"]  # container @output JSON-encoded on the STRING socket


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
    # Optional[int/float]=None must NOT become an INT/FLOAT widget defaulting to 0: 0 is a
    # distinct, often-breaking value (e.g. HuggingFaceSource count=0 -> "no samples";
    # ThresholdOp low_level=0 -> a real threshold at zero, not "disabled").
    # So the INLINE widget stays a blank STRING ("" -> None / metadata fallback, "5" -> cast). It now
    # ALSO carries the NULLABLE_NUMERIC tag + its real numeric type, so web/nullable_numeric_socket.js
    # retypes the "Convert to input" socket from STRING to FLOAT/INT (making the param connectable).
    def fn(count: Optional[int] = None, ratio: Optional[float] = None, n: int = 3) -> None: ...

    node_types = create_comfy_node(fn, "Taidal__Nullable").INPUT_TYPES()
    required = node_types.get("required", {})
    optional = node_types.get("optional", {})
    # Nullable numerics stay in required as blank STRING widgets (not optional sockets), now tagged.
    assert required["count"] == (
        "STRING",
        {"default": "", "multiline": False, "taidal_widget": "NULLABLE_NUMERIC", "taidal_numeric": "INT"},
    )
    assert required["ratio"] == (
        "STRING",
        {"default": "", "multiline": False, "taidal_widget": "NULLABLE_NUMERIC", "taidal_numeric": "FLOAT"},
    )
    assert "count" not in optional
    assert "ratio" not in optional
    # A concrete-default int is still a required INT widget with the right default.
    assert required["n"] == ("INT", {**DEFAULT_WIDGET_CONFIG["INT"], "default": 3})


def test_optional_int_or_variadic_tuple_is_nullable_numeric_not_range() -> None:
    # An Optional param whose annotation ALSO has a variadic Tuple arm — helios
    # Max/Min/Mean/Squeeze/Reduce ``axis: int | Tuple[int, ...] | None`` (reduce one axis,
    # several, or ALL when None) — must render the flexible-optional blank-STRING nullable
    # numeric (like taidal's Optional[int]), NOT a forced ``axis__lo``/``axis__hi`` RANGE pair
    # that can't be None. ``_map_type`` still returns RANGE_INT for the ``Tuple[int, ...]`` arm,
    # but a ``None`` default routes it to the nullable widget in ``_widget_for``. A CONCRETE-default
    # tuple (no None) keeps its RANGE ``__lo``/``__hi`` widgets.
    def fn(
        axis: Optional[Union[int, Tuple[int, ...]]] = None,
        target_length: Union[int, Tuple[int, int]] = 0,
    ) -> None: ...

    required = create_comfy_node(fn, "Taidal__AxisTuple").INPUT_TYPES().get("required", {})
    assert required["axis"] == (
        "STRING",
        {"default": "", "multiline": False, "taidal_widget": "NULLABLE_NUMERIC", "taidal_numeric": "INT"},
    )
    assert "axis__lo" not in required and "axis__hi" not in required
    # A concrete-default tuple is unchanged — still the RANGE __lo/__hi pair.
    assert "target_length__lo" in required and "target_length__hi" in required
    # Coercion: blank -> None (the constructor's "all axes" default); a number -> int.
    assert _coerce_kwargs(fn, {"axis": ""}) == {"axis": None}
    assert _coerce_kwargs(fn, {"axis": "0"}) == {"axis": 0}


def test_nullable_numeric_validate_inputs_accepts_numeric_wire() -> None:
    # A node with a nullable-numeric param gets a VALIDATE_INPUTS so ComfyUI accepts a FLOAT/INT wire
    # into it (the NoiseFloorOp.applied_snr_db -> noise_power_db case). Without it, the canvas socket
    # is retyped to FLOAT but the backend INPUT_TYPES is STRING -> Queue fails "Return type mismatch".
    @configurable(category="op")
    class _NoiseLike:
        def __init__(self, noise_power_db: Optional[float] = None) -> None:
            self.noise_power_db = noise_power_db

        def __call__(self, sample: Sample) -> Sample:
            return sample

    node = create_comfy_node(_NoiseLike, "Taidal__NoiseLike")
    assert hasattr(node, "VALIDATE_INPUTS")
    # A FLOAT/INT @output wired into the nullable param validates (the wire we want to allow).
    assert node.VALIDATE_INPUTS(input_types={"noise_power_db": "FLOAT"}) is True
    assert node.VALIDATE_INPUTS(input_types={"noise_power_db": "INT"}) is True
    # Other sockets still type-check: an exact match passes, a genuine mismatch is rejected.
    assert node.VALIDATE_INPUTS(input_types={"sample": "DATAFLUX_SAMPLE"}) is True
    bad = node.VALIDATE_INPUTS(input_types={"sample": "FLOAT"})
    assert bad is not True and "sample" in bad


def test_node_without_nullable_numeric_has_no_validate_inputs() -> None:
    # VALIDATE_INPUTS is added ONLY when a node exposes a nullable-numeric param — other nodes keep
    # ComfyUI's native return-type check untouched.
    @configurable(category="op")
    class _PlainOp:
        def __init__(self, gain: float = 1.0) -> None:
            self.gain = gain

        def __call__(self, sample: Sample) -> Sample:
            return sample

    node = create_comfy_node(_PlainOp, "Taidal__PlainOp")
    assert not hasattr(node, "VALIDATE_INPUTS")


def test_coerce_kwargs_casts_filled_nullable_numeric() -> None:
    def fn(count: Optional[int] = None, ratio: Optional[float] = None) -> None: ...

    out = _coerce_kwargs(fn, {"count": "5", "ratio": "0.25"})
    assert out["count"] == 5 and isinstance(out["count"], int)
    assert out["ratio"] == 0.25 and isinstance(out["ratio"], float)
    # Blank -> None (i.e. "unset" / all).
    assert _coerce_kwargs(fn, {"count": ""})["count"] is None


def test_single_op_param_renders_op_socket() -> None:
    # ConfigureOp.target / RandomApply.op are SINGLE-op params: one optional DATAFLUX_OP
    # input socket each (the single-op counterpart of the op_N list family), not a widget.
    from dataflux.ops.configure import ConfigureOp
    from dataflux.ops.random_apply import RandomApply

    cfg_node = create_comfy_node(ConfigureOp, "Taidal_ConfigureOp")
    spec = cfg_node.INPUT_TYPES()
    assert spec["optional"]["target"] == (OP_TYPE,)
    assert spec["optional"]["op_0"] == (OP_TYPE,)  # the `ops` compute chain rides op_N
    assert "target" not in spec["required"] and "ops" not in spec["required"]

    ra_node = create_comfy_node(RandomApply, "Taidal_RandomApply")
    assert ra_node.INPUT_TYPES()["optional"]["op"] == (OP_TYPE,)


def test_configure_op_node_executes_with_wired_target_and_compute() -> None:
    from dataflux.ops.configure import ConfigureOp
    from dataflux.ops.numpy import MaxOp, ThresholdOp

    node = create_comfy_node(ConfigureOp, "Taidal_ConfigureOp")()
    sample = Sample(input=np.array([1.0, 6.0, 2.0]), target=None, metadata={})
    out = node.execute(sample=sample, target=ThresholdOp(low_op=">="), op_0=MaxOp(), param="low_level")
    applied, op = out["result"][0], out["result"][1]
    assert isinstance(op, ConfigureOp)
    assert applied.meta["low_level"] == 6.0
    np.testing.assert_array_equal(applied.input, [False, True, False])


# ---- stamped op-value params (the waivefront-helios __taidal_op_*__ contract) ----
# These mirror what waivefront.helios.bridge stamps on a bridged helios composite — op slots named
# the helios way (compute/target/delegate/true_transforms/…) that the dataflux name allowlists can't
# know. Tested with synthetic stamped classes so fluxstudio stays hermetic (no helios dependency).


class _HeliosLikeConfigure:
    """Two single-op slots named the helios way (compute/target) — surfaced only via the stamp."""

    def __init__(self, compute: object = None, target: object = None, key: str = "k") -> None:
        self.compute, self.target, self.key = compute, target, key

    def __call__(self, sample: Sample) -> Sample:
        return sample


_HeliosLikeConfigure.__taidal_op_params__ = ("compute", "target")  # type: ignore[attr-defined]
_HeliosLikeConfigure.__taidal_op_list_params__ = ()  # type: ignore[attr-defined]


class _HeliosLikeIf:
    """Two op-LIST slots named the helios way (true/false_transforms) — surfaced only via the stamp."""

    def __init__(self, true_transforms: object = None, false_transforms: object = None) -> None:
        self.true_transforms, self.false_transforms = true_transforms, false_transforms

    def __call__(self, sample: Sample) -> Sample:
        return sample


_HeliosLikeIf.__taidal_op_params__ = ()  # type: ignore[attr-defined]
_HeliosLikeIf.__taidal_op_list_params__ = ("true_transforms", "false_transforms")  # type: ignore[attr-defined]


def test_stamped_single_op_params_each_get_a_socket() -> None:
    assert _single_op_params(_HeliosLikeConfigure) == ["compute", "target"]
    spec = create_comfy_node(_HeliosLikeConfigure, "Taidal_HeliosLikeConfigure").INPUT_TYPES()
    assert spec["optional"]["compute"] == (OP_TYPE,)
    assert spec["optional"]["target"] == (OP_TYPE,)
    assert "compute" not in spec["required"] and "target" not in spec["required"]
    assert spec["required"]["key"][0] == "STRING"  # the scalar param stays a widget


def test_stamped_single_op_params_inject_each_post_construction() -> None:
    node = create_comfy_node(_HeliosLikeConfigure, "Taidal_HeliosLikeConfigure2")()
    c, t = object(), object()
    applied, op = node.execute(sample=None, compute=c, target=t, key="z")["result"]
    assert applied is None  # sample unconnected -> pure op-value build
    assert isinstance(op, _HeliosLikeConfigure)
    assert op.compute is c and op.target is t and op.key == "z"


def test_stamped_op_list_params_seed_distinct_families_and_collect() -> None:
    assert _op_list_params(_HeliosLikeIf) == {
        "true_transforms": "true_transforms",
        "false_transforms": "false_transforms",
    }
    node = create_comfy_node(_HeliosLikeIf, "Taidal_HeliosLikeIf")
    spec = node.INPUT_TYPES()
    assert spec["optional"]["true_transforms_0"] == (OP_TYPE,)
    assert spec["optional"]["false_transforms_0"] == (OP_TYPE,)

    a, b, c = _FakeOp(), _FakeOp(), _FakeOp()
    _applied, op = node().execute(sample=None, true_transforms_0=a, true_transforms_1=b, false_transforms_0=c)["result"]
    assert op.true_transforms == [a, b]
    assert op.false_transforms == [c]


def test_coerce_kwargs_leaves_expression_string_on_float_str_union() -> None:
    # A ``Union[float, int, str]`` param (e.g. dataflux ThresholdOp.low_level) accepts
    # call-time metadata expressions like "{snr}" — the coercion must NOT float()-cast
    # the widget string (that crashed the node); the constructor resolves it at call time.
    def fn(low_level: Optional[Union[float, int, str]] = None) -> None: ...

    assert _coerce_kwargs(fn, {"low_level": "{snr}"})["low_level"] == "{snr}"
    assert _coerce_kwargs(fn, {"low_level": "-30.5"})["low_level"] == "-30.5"  # str passes verbatim
    assert _coerce_kwargs(fn, {"low_level": ""})["low_level"] is None  # blank -> None default


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


# --------------------------------------------------------------------------- #
# Display-name humanization (verbatim identifier, capitals preserved)
# --------------------------------------------------------------------------- #


def test_humanize_node_name_preserves_capitals() -> None:
    # CamelCase stays verbatim (the .title() bug turned these to e.g. "Huggingfacesource").
    assert _humanize_node_name("HuggingFaceSource") == "HuggingFaceSource"
    assert _humanize_node_name("ConvertToImageOp") == "ConvertToImageOp"
    # snake_case -> spaces, but no case change.
    assert _humanize_node_name("rescale_op") == "rescale op"


# --------------------------------------------------------------------------- #
# HuggingFace dataset-id widget (free-text + optional cached picker, NOT a dir)
# --------------------------------------------------------------------------- #


class HuggingFaceFakeDataset:
    """A HuggingFace* source so the dataset-id param resolves to the HF picker.

    Args:
        path: The HF dataset identifier (hub repo id or local path).
        split: Which split to load.
        input_feature: Dataset column mapped onto Sample.input.
        target_feature: Dataset column mapped onto Sample.target.
        metadata_features: Columns to copy onto Sample.metadata.
    """

    def __init__(
        self,
        path: str,
        split: str = "train",
        input_feature: str = "image",
        target_feature: str = "label",
        metadata_features: Optional[List[str]] = None,
    ) -> None:
        self.path = path
        self.split = split
        self.input_feature = input_feature
        self.target_feature = target_feature
        self.metadata_features = metadata_features or []

    def __getitem__(self, i: int) -> Sample:
        return Sample(input=i, target=None, metadata={})


class PlainPathSource:
    """A non-HuggingFace source whose ``path`` is a genuine directory.

    Args:
        path: A filesystem directory.
    """

    def __init__(self, path: str) -> None:
        self.path = path

    def __getitem__(self, i: int) -> Sample:
        return Sample(input=i, target=None, metadata={})


def test_is_hf_dataset_id_param_gating() -> None:
    import inspect

    p = inspect.Parameter("path", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str)
    r = inspect.Parameter("repo_id", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str)
    assert _is_hf_dataset_id_param("path", HuggingFaceFakeDataset) is True
    assert _is_hf_dataset_id_param("repo_id", HuggingFaceFakeDataset) is True
    # gated on the HuggingFace* class-name convention
    assert _is_hf_dataset_id_param("path", PlainPathSource) is False
    # only the id params, not e.g. cache_dir
    assert _is_hf_dataset_id_param("cache_dir", HuggingFaceFakeDataset) is False
    # parameters object signature is irrelevant — gating is name + owner
    del p, r


def test_hf_source_id_param_renders_as_hf_dataset_widget() -> None:
    inputs = create_comfy_node(HuggingFaceFakeDataset, "Taidal__HFFake").INPUT_TYPES()["required"]
    comfy_type, config = inputs["path"]
    assert comfy_type == "STRING"
    assert config["taidal_widget"] == "HF_DATASET_ID"  # NOT "DIRECTORY"
    # and the docstring Args flows through as a tooltip
    assert config.get("tooltip")


def test_non_hf_path_param_stays_directory_picker() -> None:
    inputs = create_comfy_node(PlainPathSource, "Taidal__PlainPath").INPUT_TYPES()["required"]
    _, config = inputs["path"]
    assert config["taidal_widget"] == "DIRECTORY"


# --------------------------------------------------------------------------- #
# HuggingFace feature-selector widget (free-text + optional column picker)
# --------------------------------------------------------------------------- #


def test_is_hf_feature_param_gating() -> None:
    # input_feature / target_feature on a HuggingFace* source resolve to the column picker.
    assert _is_hf_feature_param("input_feature", HuggingFaceFakeDataset) is True
    assert _is_hf_feature_param("target_feature", HuggingFaceFakeDataset) is True
    # gated on the HuggingFace* class-name convention
    assert _is_hf_feature_param("input_feature", PlainPathSource) is False
    # only the feature params, not e.g. split
    assert _is_hf_feature_param("split", HuggingFaceFakeDataset) is False
    # no owner -> not a feature param
    assert _is_hf_feature_param("input_feature", None) is False


def test_hf_source_feature_params_render_as_hf_feature_widget() -> None:
    inputs = create_comfy_node(HuggingFaceFakeDataset, "Taidal__HFFeat").INPUT_TYPES()["required"]
    for field, default in (("input_feature", "image"), ("target_feature", "label")):
        comfy_type, config = inputs[field]
        assert comfy_type == "STRING"
        assert config["taidal_widget"] == "HF_FEATURE"  # NOT a directory or plain string
        assert config["default"] == default  # the constructor default is preserved
        # and the docstring Args flows through as a tooltip
        assert config.get("tooltip")


# --------------------------------------------------------------------------- #
# HuggingFace split widget (free-text + optional split picker)
# --------------------------------------------------------------------------- #


def test_is_hf_split_param_gating() -> None:
    # `split` on a HuggingFace* source resolves to the split picker.
    assert _is_hf_split_param("split", HuggingFaceFakeDataset) is True
    # gated on the HuggingFace* class-name convention -> DatasetSplit.split (a Literal on a
    # non-HuggingFace class) is NOT caught and stays a combo via _literal_options.
    assert _is_hf_split_param("split", PlainPathSource) is False
    # only the split param, not e.g. input_feature
    assert _is_hf_split_param("input_feature", HuggingFaceFakeDataset) is False
    # no owner -> not a split param
    assert _is_hf_split_param("split", None) is False


def test_hf_source_split_param_renders_as_hf_split_widget() -> None:
    inputs = create_comfy_node(HuggingFaceFakeDataset, "Taidal__HFSplit").INPUT_TYPES()["required"]
    comfy_type, config = inputs["split"]
    assert comfy_type == "STRING"  # free-text kept for HF slice syntax
    assert config["taidal_widget"] == "HF_SPLIT"
    assert config["default"] == "train"  # the constructor default is preserved
    assert config.get("tooltip")  # docstring Args flows through


# --------------------------------------------------------------------------- #
# HuggingFace metadata_features widget (comma-separated STRING + multi-select picker)
# --------------------------------------------------------------------------- #


def test_is_hf_metadata_param_gating() -> None:
    assert _is_hf_metadata_param("metadata_features", HuggingFaceFakeDataset) is True
    # gated on the HuggingFace* class-name convention
    assert _is_hf_metadata_param("metadata_features", PlainPathSource) is False
    # only the metadata_features param, not e.g. input_feature
    assert _is_hf_metadata_param("input_feature", HuggingFaceFakeDataset) is False
    assert _is_hf_metadata_param("metadata_features", None) is False


def test_hf_source_metadata_param_renders_as_hf_metadata_widget() -> None:
    inputs = create_comfy_node(HuggingFaceFakeDataset, "Taidal__HFMeta").INPUT_TYPES()["required"]
    comfy_type, config = inputs["metadata_features"]
    assert comfy_type == "STRING"  # comma-separated free-text (NOT a STRING list fallback per-name)
    assert config["taidal_widget"] == "HF_METADATA"
    assert config["default"] == ""  # empty -> None -> no extra metadata (backward-compatible)
    assert config.get("tooltip")  # docstring Args flows through


def test_hf_metadata_string_coerces_to_list_and_star() -> None:
    # The comma-separated STRING widget round-trips to the List[str] the constructor wants;
    # "*" passes through as a single-element list (the dataflux sentinel for "the rest").
    assert _coerce_kwargs(HuggingFaceFakeDataset, {"metadata_features": "id, src"})["metadata_features"] == [
        "id",
        "src",
    ]
    assert _coerce_kwargs(HuggingFaceFakeDataset, {"metadata_features": "*"})["metadata_features"] == ["*"]
    # empty -> None (param default) -> source treats as "no extra metadata"
    assert _coerce_kwargs(HuggingFaceFakeDataset, {"metadata_features": ""})["metadata_features"] is None


# --------------------------------------------------------------------------- #
# Constructor-param tooltips sourced from the docstring Args: block
# --------------------------------------------------------------------------- #


def test_widget_tooltip_sourced_from_docstring() -> None:
    class _DocumentedOp:
        """An op.

        Args:
            bump: How much to add to the input.
        """

        def __init__(self, bump: int = 1) -> None:
            self.bump = bump

        def __call__(self, sample: Sample) -> Sample:
            return sample._replace(input=sample.input + self.bump)

    inputs = create_comfy_node(_DocumentedOp, "Taidal__Documented").INPUT_TYPES()["required"]
    _, config = inputs["bump"]
    assert config["tooltip"] == "How much to add to the input."


def test_undocumented_param_has_no_tooltip() -> None:
    def fn(blob: int = 0) -> None: ...

    _, config = create_comfy_node(fn, "Taidal__Undoc").INPUT_TYPES()["required"]["blob"]
    assert "tooltip" not in config


def test_source_node_stashes_produces_none_when_undeclared() -> None:
    node = create_comfy_node(_FakeSource, "Taidal__FakeSource")
    assert node.TAIDAL_PRODUCES is None


def test_op_node_runtime_check_passes_matching_sample() -> None:
    node = create_comfy_node(_TypedOp, "Taidal__TypedOp")()
    out, _op = node.execute(sample=Sample(input=np.zeros((4, 4), dtype=np.float32)))["result"]
    assert out.input.dtype == bool


def test_op_node_runtime_check_rejects_mismatched_sample() -> None:
    node = create_comfy_node(_TypedOp, "Taidal__TypedOp")()
    with pytest.raises(RuntimeError, match="cannot accept the upstream sample"):
        node.execute(sample=Sample(input=5))  # a bare int is not a 2-D numpy array


# --------------------------------------------------------------------------- #
# Runnable + object nodes (trainers / evaluators / processors as canvas nodes)
# --------------------------------------------------------------------------- #

from typing import Any, Type  # noqa: E402

from confluid import LazyClass, Mandatory  # noqa: E402

from fluxstudio.bridge import (  # noqa: E402
    OBJECT_TYPE,
    RUNNABLE_TYPE,
    _admits_object_list,
    _build_object_node,
    _build_runnable_node,
    _is_object_member_class,
    _is_runnable,
    _is_wireable_object_param,
    _object_socket,
    _object_socket_for_class,
    _output_socket_type,
)


@configurable(task="faketask", role="trainer", validate=False)
class _FakeTrainer:
    """A minimal-ctor runnable: model/train_set required, deferred body slots, a run().

    ``@configurable`` because ``to_pydantic``'s body-slot scan (which surfaces the deferred
    ``optimizer`` / ``val_set`` members) walks only the ``@configurable`` MRO chain — exactly
    like the real trainers (sonair / raidar / deltaid).
    """

    def __init__(self, model: Any, train_set: Any, num_classes: Optional[int] = None, run_name: str = "train") -> None:
        self.model = model
        self.train_set = train_set
        self.num_classes = num_classes
        self.run_name = run_name
        self.optimizer: Any = LazyClass(int)  # deferred object body slot -> OBJECT socket
        self.val_set: Any = None  # dataset body slot -> SOURCE socket
        self.ran = False

    def run(self) -> None:
        self.ran = True


def test_is_runnable() -> None:
    assert _is_runnable(_FakeTrainer) is True

    class _NotRunnable:
        pass

    assert _is_runnable(_NotRunnable) is False

    class _RunNeedsArg:
        def run(self, x: int) -> None: ...

    # The run interface is parameterless — a run(self, x) is not a runnable.
    assert _is_runnable(_RunNeedsArg) is False


def test_build_runnable_node_member_sockets_and_widgets() -> None:
    node = _build_runnable_node(_FakeTrainer, "Taidal_Fake", "Taidal/Fake")
    spec = node.INPUT_TYPES()
    req, opt = spec["required"], spec.get("optional", {})

    assert node.RETURN_TYPES == (RUNNABLE_TYPE,)
    assert node.TAIDAL_NODE_KIND == "runnable"
    # The ``model`` slot is ROLE-qualified and TASK-AGNOSTIC — it accepts ``DATAFLUX_OBJECT:model``
    # from ANY model (any task), so a loss can't dock but a cross-task model can.
    assert req["model"] == (_object_socket("model"),)
    assert req["train_set"] == (SOURCE_TYPE,)
    # ``num_classes: Optional[int] = None`` is a nullable-numeric param → blank STRING widget
    # (preserves None/fallback), tagged NULLABLE_NUMERIC so the JS retypes its converted socket to INT.
    assert req["num_classes"] == (
        "STRING",
        {"default": "", "multiline": False, "taidal_widget": "NULLABLE_NUMERIC", "taidal_numeric": "INT"},
    )
    assert req["run_name"][0] == "STRING"
    # Deferred / dataset body slots -> OPTIONAL sockets (unwired keeps the baked default).
    # ``optimizer`` is role-less (not in _STRICT_SLOT_ROLES) -> generic OBJECT_TYPE, not qualified.
    assert opt["optimizer"] == (OBJECT_TYPE,)
    assert opt["val_set"] == (SOURCE_TYPE,)


def test_runnable_node_execute_builds_post_sets_and_runs() -> None:
    # The node RUNS on execute (it is the OUTPUT_NODE): it builds the runnable (ctor + post-setattr),
    # then calls run(). Returns the {"ui": ..., "result": (runnable, *outputs)} display form.
    node = _build_runnable_node(_FakeTrainer, "Taidal_Fake", "Taidal/Fake")
    model, train_set = object(), object()
    custom_opt = LazyClass(float)
    result = node().execute(model=model, train_set=train_set, num_classes=3, optimizer=custom_opt, val_set=None)
    built = result["result"][0]

    assert isinstance(built, _FakeTrainer)
    assert built.model is model  # ctor param
    assert built.num_classes == 3
    assert built.optimizer is custom_opt  # wired body slot set post-construction
    assert built.val_set is None  # unwired optional -> keeps baked default
    assert built.ran is True  # the node RAN it on execute (no separate Run node)


def test_create_comfy_node_dispatches_runnable() -> None:
    node = create_comfy_node(_FakeTrainer, "Taidal_Fake", "Taidal/Fake")
    assert node.TAIDAL_NODE_KIND == "runnable"
    assert node.RETURN_TYPES == (RUNNABLE_TYPE,)
    assert getattr(node, "OUTPUT_NODE", False) is True  # a runnable node runs on Queue


@configurable(task="faketask", role="trainer", validate=False)
class _FakeTrainerWithOutput:
    """A runnable that declares a confluid ``@output`` — its trained model artifact."""

    def __init__(self, model: Any, train_set: Any) -> None:
        self.model = model
        self.train_set = train_set
        self._ran = False

    def run(self) -> None:
        self._ran = True

    @property
    @output
    def trained_model(self) -> Any:
        """The produced model — only available after run()."""
        if not self._ran:
            raise RuntimeError("trained_model is only available after run()")
        return self.model


def test_runnable_node_appends_declared_outputs() -> None:
    node = _build_runnable_node(_FakeTrainerWithOutput, "Taidal_FakeOut", "Taidal/Fake")
    # The declared @output rides alongside the runnable wire, ROLE-qualified (``model`` keyword ->
    # ``DATAFLUX_OBJECT:model``, task-agnostic) so it docks into a downstream evaluator's model slot.
    assert node.RETURN_NAMES == ("runnable", "trained_model")
    assert node.RETURN_TYPES == (RUNNABLE_TYPE, _object_socket("model"))


def test_runnable_node_runs_and_emits_live_outputs() -> None:
    # The node RUNS the runnable, then reads each @output LIVE (post-run) and emits it on its socket —
    # NOT None. trained_model (an object) passes through; the getter no longer raises because run() ran.
    node = _build_runnable_node(_FakeTrainerWithOutput, "Taidal_FakeOut", "Taidal/Fake")
    model = object()
    result = node().execute(model=model, train_set=object())
    runnable, trained = result["result"]
    assert isinstance(runnable, _FakeTrainerWithOutput)
    assert runnable._ran is True
    assert trained is model  # live post-run value, not None


def test_output_socket_type_maps_containers_to_string() -> None:
    from typing import Dict, List

    import torch.nn as nn

    # Containers ride a STRING (JSON) socket — and computed_metrics must NOT mis-map to OBJECT:metric.
    assert _output_socket_type("computed_metrics", Dict[str, float]) == "STRING"
    assert _output_socket_type("label_names", List[str]) == "STRING"
    # A role-bearing object output stays a role-qualified DATAFLUX_OBJECT socket.
    assert _output_socket_type("trained_model", nn.Module) == _object_socket("model")
    # A dataset-named output rides DATAFLUX_SOURCE.
    assert _output_socket_type("dataset", object) == SOURCE_TYPE


@configurable(task="faketask", role="trainer", validate=False)
class _FakeTrainerWithDisplayOutputs:
    """A runnable declaring an object output (``trained_model``) AND container outputs."""

    def __init__(self, model: Any, train_set: Any) -> None:
        self.model = model
        self.train_set = train_set

    def run(self) -> None:
        pass

    @property
    @output
    def trained_model(self) -> Any:
        return self.model

    @property
    @output
    def computed_metrics(self) -> "dict[str, float]":
        return {"acc": 1.0}

    @property
    @output
    def label_names(self) -> "list[str]":
        return ["a"]


def test_runnable_node_is_output_node_with_all_outputs() -> None:
    # EVERY declared @output becomes a static wireable socket (no dynamic outputs): the object output
    # rides DATAFLUX_OBJECT:model, the container outputs ride STRING (JSON). The node is an OUTPUT_NODE
    # with a hidden unique_id (for the live-output widget).
    node = _build_runnable_node(_FakeTrainerWithDisplayOutputs, "Taidal_FakeDisp", "Taidal/Fake")
    assert getattr(node, "OUTPUT_NODE", False) is True
    assert node.INPUT_TYPES()["hidden"]["unique_id"] == "UNIQUE_ID"
    assert node.RETURN_NAMES == ("runnable", "trained_model", "computed_metrics", "label_names")
    assert node.RETURN_TYPES == (RUNNABLE_TYPE, _object_socket("model"), "STRING", "STRING")


def test_runnable_node_emits_container_outputs_as_json_strings() -> None:
    # On execute the container @outputs come back JSON-encoded on their STRING sockets (wire to Show Text).
    node = _build_runnable_node(_FakeTrainerWithDisplayOutputs, "Taidal_FakeDisp", "Taidal/Fake")
    result = node().execute(model=object(), train_set=object())
    _runnable, _trained, metrics_json, names_json = result["result"]
    assert json.loads(metrics_json) == {"acc": 1.0}
    assert json.loads(names_json) == ["a"]


def test_runnable_node_required_inputs_include_mandatory_marked() -> None:
    # A Mandatory-marked slot is a REQUIRED socket even when it carries a default (zero-arg build).
    @configurable(task="faketask", role="trainer", validate=False)
    class _MandatoryDefault:
        def __init__(self, model: Mandatory[Any] = None, lr: float = 0.1) -> None:
            self.model = model
            self.lr = lr

        def run(self) -> None: ...

    req = _build_runnable_node(_MandatoryDefault, "Taidal_Mand", "Taidal/Fake").INPUT_TYPES()["required"]
    assert "model" in req  # required despite the ``= None`` default, via the Mandatory marker


class _EagerMember:
    def __init__(self, channels: int = 3) -> None:
        self.channels = channels


class _LazyOptimizer:
    """A lazy member whose required ``params`` is the runtime-injected slot."""

    def __init__(self, params: Any, lr: float = 0.1) -> None:
        self.params = params
        self.lr = lr


_LazyOptimizer.__confluid_lazy__ = True  # type: ignore[attr-defined]


def test_object_node_non_lazy_returns_instance_and_lazy() -> None:
    # Dual output: a flowed live instance AND a deferred LazyClass of the same config.
    node = _build_object_node(_EagerMember, "Taidal_Eager", "Taidal/Obj")
    assert node.RETURN_TYPES == (OBJECT_TYPE, OBJECT_TYPE)
    assert node.RETURN_NAMES == ("instance", "class")
    instance, lazy = node().execute(channels=5)["result"]
    assert isinstance(instance, _EagerMember) and instance.channels == 5
    assert isinstance(lazy, LazyClass) and lazy.kwargs.get("channels") == 5


def test_object_node_lazy_defers_instance_and_skips_runtime_param() -> None:
    node = _build_object_node(_LazyOptimizer, "Taidal_Opt", "Taidal/Obj")
    spec = node.INPUT_TYPES()
    # The runtime-injected required param is skipped; configurable knobs remain.
    assert "params" not in spec["required"]
    assert "lr" in spec["required"]
    instance, lazy = node().execute(lr=0.5)["result"]
    # A runtime-injected required param (``params``) -> no standalone instance, only the LazyClass.
    assert instance is None
    assert isinstance(lazy, LazyClass) and lazy.kwargs.get("lr") == 0.5


# --------------------------------------------------------------------------- #
# A registered builder FUNCTION (not a class) becomes an OBJECT node.
# The canonical case: ``raidar.configurables`` registers torchvision's detection
# builders (``fasterrcnn_*``) with role="model" + lazy=True. A local function
# stands in (markers stamped by hand) so the test carries no torchvision dep.
# --------------------------------------------------------------------------- #
def _build_detector(num_classes: Optional[int] = None, weights_backbone: str = "DEFAULT") -> Dict[str, Any]:
    """A builder FUNCTION (mirrors a torchvision detection builder): all params defaulted."""
    return {"num_classes": num_classes, "weights_backbone": weights_backbone}


_build_detector.__confluid_configurable__ = True  # type: ignore[attr-defined]
_build_detector.__confluid_role__ = "model"  # type: ignore[attr-defined]
_build_detector.__confluid_category__ = "detection_model"  # type: ignore[attr-defined]
_build_detector.__confluid_lazy__ = True  # type: ignore[attr-defined]


def test_object_member_predicate_accepts_marked_function() -> None:
    # The gate is "callable + marked", not "class + marked".
    assert _is_object_member_class(_build_detector) is True
    # A bare unmarked function is NOT an object member.
    assert _is_object_member_class(lambda: None) is False


def test_object_node_from_builder_function() -> None:
    """A builder FUNCTION yields an OBJECT node: role-qualified socket + signature widgets,
    and ``class`` is a ``LazyClass`` of the FUNCTION (wires straight into a model slot)."""
    # role="model" -> the output docks into a runnable's ``model`` slot with no Retag.
    assert _object_socket_for_class(_build_detector) == f"{OBJECT_TYPE}:model"
    node = _build_object_node(_build_detector, "Taidal_Detector", "Taidal/Model/Detection")
    assert node.RETURN_TYPES == (f"{OBJECT_TYPE}:model", f"{OBJECT_TYPE}:model")
    spec = node.INPUT_TYPES()
    widgets = {**spec.get("required", {}), **spec.get("optional", {})}
    assert "num_classes" in widgets  # introspected from the FUNCTION's own signature
    instance, lazy = node().execute(num_classes=2, weights_backbone="DEFAULT")["result"]
    assert isinstance(lazy, LazyClass) and lazy.target is _build_detector
    assert lazy.kwargs.get("num_classes") == 2
    # All params defaulted -> a live instance is buildable (not deferred).
    assert instance == {"num_classes": 2, "weights_backbone": "DEFAULT"}


def test_register_module_nodes_admits_reexported_function(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass 2 admits a registered builder FUNCTION re-exported via ``__all__`` (not just classes)."""
    import sys
    import types as _types

    mod = _types.ModuleType("fake.builders")
    mod.build_detector = _build_detector  # type: ignore[attr-defined]
    mod.__all__ = ["build_detector"]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake.builders", mod)

    mappings: Dict[str, Type] = {}
    display: Dict[str, str] = {}
    with patch("fluxstudio.bridge.scan_module", return_value=[]):
        register_module_nodes("fake.builders", mappings, display)
    assert len(mappings) == 1
    (cls,) = mappings.values()
    assert cls.RETURN_TYPES == (f"{OBJECT_TYPE}:model", f"{OBJECT_TYPE}:model")


# --------------------------------------------------------------------------- #
# Object-typed ctor params (logger / callbacks) -> wired DATAFLUX_OBJECT sockets
# --------------------------------------------------------------------------- #
class _FakeLogger:
    def __init__(self, save_dir: str = "logs") -> None:
        self.save_dir = save_dir


class _FakeAccel:
    pass


def test_is_wireable_object_param_shape_rule() -> None:
    from typing import List, Literal, Optional, Union

    # Object-class arm + only a bool/None scalar arm -> wireable (the logger / callbacks case).
    assert _is_wireable_object_param(Union[_FakeLogger, bool]) is True
    assert _is_wireable_object_param(Optional[List[_FakeLogger]]) is True
    assert _is_wireable_object_param(Optional[_FakeLogger]) is True
    # A real str/int/float config arm disqualifies (accelerator="gpu", precision=16) -> stays a widget.
    assert _is_wireable_object_param(Union[str, _FakeAccel]) is False
    assert _is_wireable_object_param(Union[int, _FakeAccel]) is False
    # Plain scalars / Literals are never wireable.
    assert _is_wireable_object_param(int) is False
    assert _is_wireable_object_param(Optional[int]) is False
    assert _is_wireable_object_param(str) is False
    assert _is_wireable_object_param(Literal["a", "b"]) is False


class _ObjConfig:
    """An object node with a logger-like object param (bool-poisoned) and a str-config param.

    Real (non-stringized) annotations — this module deliberately avoids ``from __future__ import
    annotations`` so the bridge sees live type objects (see the module docstring).
    """

    def __init__(
        self,
        logger: _FakeLogger | bool = True,
        accel: str | _FakeAccel = "auto",
        max_epochs: int = 1,
    ) -> None:
        self.logger = logger
        self.accel = accel
        self.max_epochs = max_epochs


def test_object_node_object_param_is_a_socket_not_a_widget() -> None:
    node = _build_object_node(_ObjConfig, "Taidal_ObjCfg", "Taidal/Obj")
    spec = node.INPUT_TYPES()
    required, optional = spec["required"], spec.get("optional", {})
    # logger (object arm, only a bool scalar arm) -> a wired DATAFLUX_OBJECT OPTIONAL socket, NOT BOOLEAN.
    assert optional.get("logger") == (OBJECT_TYPE,)
    assert "logger" not in required
    # accel (has a str arm) stays a STRING widget; max_epochs stays a scalar widget.
    assert required["accel"][0] == "STRING"
    assert "accel" not in optional
    assert required["max_epochs"][0] in ("INT", "STRING")


def test_object_node_wires_object_into_kwargs_and_omits_unwired() -> None:
    node = _build_object_node(_ObjConfig, "Taidal_ObjCfg", "Taidal/Obj")
    wired = _FakeLogger(save_dir="run1")
    instance, lazy = node().execute(logger=wired, accel="gpu", max_epochs=2)["result"]
    # The wired live object is passed through verbatim into the built instance + LazyClass kwargs.
    assert isinstance(instance, _ObjConfig) and instance.logger is wired
    assert lazy.kwargs.get("logger") is wired and lazy.kwargs.get("accel") == "gpu"
    # An unwired (None) object socket is omitted so the ctor default (True) applies.
    instance2, lazy2 = node().execute(logger=None, accel="cpu", max_epochs=1)["result"]
    assert "logger" not in lazy2.kwargs and instance2.logger is True


def test_admits_object_list_shape_rule() -> None:
    from typing import Iterable, List, Optional, Union

    # A list / Iterable arm whose element is an object class -> admits a list (dynamic <name>_N).
    assert _admits_object_list(Union[_FakeLogger, Iterable[_FakeLogger], bool]) is True
    assert _admits_object_list(Optional[List[_FakeLogger]]) is True
    # A single-object param (no list arm) is NOT a list.
    assert _admits_object_list(Union[_FakeLogger, bool]) is False
    assert _admits_object_list(Optional[_FakeLogger]) is False
    # Scalars / list-of-scalars are never object-lists.
    assert _admits_object_list(int) is False
    assert _admits_object_list(List[str]) is False


class _ObjListConfig:
    """An object node with a list-capable logger param -> dynamic logger_N sockets."""

    def __init__(self, logger: _FakeLogger | list[_FakeLogger] | bool = True, max_epochs: int = 1) -> None:
        self.logger = logger
        self.max_epochs = max_epochs


def test_object_node_list_param_seeds_indexed_socket() -> None:
    node = _build_object_node(_ObjListConfig, "Taidal_ObjList", "Taidal/Obj")
    spec = node.INPUT_TYPES()
    optional = spec.get("optional", {})
    # A list-capable logger -> a dynamic ``logger_0`` seed socket (web/object_list.js grows logger_1, …),
    # NOT a single ``logger`` socket.
    assert optional.get("logger_0") == (OBJECT_TYPE,)
    assert "logger" not in optional and "logger" not in spec["required"]


def test_object_node_collects_indexed_objects_into_list() -> None:
    node = _build_object_node(_ObjListConfig, "Taidal_ObjList", "Taidal/Obj")
    a, b = _FakeLogger("a"), _FakeLogger("b")
    # Two wired loggers (index-ordered) -> a list; the single socket key never appears.
    instance, lazy = node().execute(logger_0=a, logger_1=b, max_epochs=2)["result"]
    assert lazy.kwargs.get("logger") == [a, b]
    assert isinstance(instance, _ObjListConfig) and instance.logger == [a, b]
    # One wired -> a list of one; none wired -> omitted so the ctor default (True) applies.
    _, lazy_one = node().execute(logger_0=a)["result"]
    assert lazy_one.kwargs.get("logger") == [a]
    inst_none, lazy_none = node().execute(logger_0=None, logger_1=None)["result"]
    assert "logger" not in lazy_none.kwargs and inst_none.logger is True


def test_real_lightning_trainer_logger_is_a_dynamic_list_socket() -> None:
    # Regression on the actual pl.Trainer: logger/callbacks accept a LIST of objects, so they become
    # dynamic ``logger_0`` / ``callbacks_0`` seed sockets; the string-settable accelerator stays a widget.
    import marainer.configurables  # noqa: F401 - registers the logger nodes
    import pytorch_lightning as pl
    from pytorch_lightning.loggers import CSVLogger

    node = _build_object_node(pl.Trainer, "Taidal_PLTrainer", "Taidal/Obj")
    spec = node.INPUT_TYPES()
    allin = {**spec.get("required", {}), **spec.get("optional", {})}
    assert allin["logger_0"] == (OBJECT_TYPE,)
    assert allin["callbacks_0"] == (OBJECT_TYPE,)
    assert "logger" not in allin  # replaced by the indexed seed
    assert allin["accelerator"][0] == "STRING"
    # Two loggers collect into a list passed to the Trainer.
    _, lazy = node().execute(logger_0=CSVLogger("a"), logger_1=CSVLogger("b"))["result"]
    assert isinstance(lazy.kwargs.get("logger"), list) and len(lazy.kwargs["logger"]) == 2


@configurable(task="faketask", role="model", validate=False)
class _FakeModel:
    def __init__(self, num_classes: int = 0) -> None:
        self.num_classes = num_classes


@configurable(task="faketask", role="loss", validate=False)
class _FakeLoss:
    def __init__(self, weight: float = 1.0) -> None:
        self.weight = weight


def test_object_node_output_socket_is_role_qualified() -> None:
    """A role-tagged object node outputs a ROLE-QUALIFIED socket (both instance + class) — TASK-
    AGNOSTIC, so ComfyUI lets it dock into ANY trainer's same-ROLE slot regardless of task."""
    node = _build_object_node(_FakeModel, "Taidal_FakeModel", "Taidal/Obj")
    assert node.RETURN_TYPES == (_object_socket("model"), _object_socket("model"))
    # And it MATCHES the trainer's role slot — the contract that makes them dock.
    trainer = _build_runnable_node(_FakeTrainer, "Taidal_Fake", "Taidal/Fake")
    assert trainer.INPUT_TYPES()["required"]["model"] == (node.RETURN_TYPES[0],)


def test_object_node_output_socket_generic_when_uncategorised() -> None:
    """A role-less member (no ``@configurable`` role — the lazy infra members / loggers)
    keeps the bare ``DATAFLUX_OBJECT`` so it stays interchangeable on generic slots."""
    node = _build_object_node(_EagerMember, "Taidal_Eager", "Taidal/Obj")
    assert node.RETURN_TYPES == (OBJECT_TYPE, OBJECT_TYPE)


def test_role_for_class_explicit_tag_wins_over_type() -> None:
    """An explicit ``@configurable(role=…)`` tag is authoritative (it is the ONLY thing that can
    tell a model from a loss — both are ``nn.Module``)."""
    from fluxstudio.bridge import _role_for_class

    assert _role_for_class(_FakeModel) == "model"  # tagged role="model" (task dropped)
    assert _role_for_class(_FakeLoss) == "loss"


def test_role_for_class_infers_from_type_when_untagged() -> None:
    """A role can be ASSIGNED from the Python type: an UNtagged class whose base qualname matches a
    known kind (Metric/Optimizer/DataLoader/Logger) gets that role — so even third-party objects
    socket correctly without a tag. Model vs loss is deliberately NOT inferable (both nn.Module)."""
    from fluxstudio.bridge import _role_for_class

    class _SomeMetric:  # stands in for a torchmetrics.Metric subclass (matched by base qualname)
        pass

    class _Sub(_SomeMetric):
        pass

    # Rename the base's qualname so the MRO scan sees "Metric" (mirrors torchmetrics.metric.Metric).
    _SomeMetric.__qualname__ = "Metric"
    assert _role_for_class(_Sub) == "metric"

    class _Plain:  # no tag, no recognised base -> generic (None)
        pass

    assert _role_for_class(_Plain) is None


def test_object_node_socket_role_inferred_from_type() -> None:
    """An untagged metric-typed object node sockets as ``DATAFLUX_OBJECT:metric`` via type inference,
    so it docks into a trainer's metric slot without an explicit tag."""

    class _Metric:
        pass

    _Metric.__qualname__ = "Metric"

    class _MyMetric(_Metric):
        def __init__(self, k: int = 1) -> None:
            self.k = k

    node = _build_object_node(_MyMetric, "Taidal_MyMetric", "Taidal/Obj")
    assert node.RETURN_TYPES == (_object_socket("metric"), _object_socket("metric"))


def test_strict_sockets_reject_cross_role() -> None:
    """The role guarantee: a loss node's output type differs from the model slot's type, so
    ComfyUI's string-equality connection rule refuses to wire a loss into a model slot — while a
    model of ANY task still docks (the socket carries the role, not the task)."""
    loss_out = _build_object_node(_FakeLoss, "Taidal_FakeLoss", "Taidal/Obj").RETURN_TYPES[0]
    model_slot = _build_runnable_node(_FakeTrainer, "Taidal_Fake", "Taidal/Fake").INPUT_TYPES()["required"]["model"][0]
    assert loss_out == _object_socket("loss")
    assert loss_out != model_slot  # cross-role wire is rejected by ComfyUI


# --------------------------------------------------------------------------- #
# Sink nodes (category="sink" → an object-member producer feeding DatasetProcessor)
# --------------------------------------------------------------------------- #


@configurable(category="sink", validate=False)
class _FakeStorageSink:
    """A DataFlux storage sink (``category="sink"``) — built as a ``DATAFLUX_OBJECT:sink`` producer."""

    def __init__(self, path: str = "./out", overwrite: bool = False) -> None:
        self.path = path
        self.overwrite = overwrite

    def write(self, sample: Sample) -> None: ...  # the whole-Flux sink contract (no run())


@configurable(validate=False)
class _FakeProcessor:
    """A ``DatasetProcessor``-shaped runnable: a ``flux`` SOURCE input + an optional ``sink`` member."""

    def __init__(self, flux: Any, sink: Any = None) -> None:
        self.flux = flux
        self.sink = sink

    def run(self) -> None: ...


def test_is_sink_class() -> None:
    from fluxstudio.bridge import _is_sink_class

    assert _is_sink_class(_FakeStorageSink) is True
    assert _is_sink_class(_FakeModel) is False  # a role-tagged object member, not a sink
    assert _is_sink_class(object) is False  # untagged class


def test_role_for_class_maps_sink_category_to_sink_role() -> None:
    """A ``category="sink"`` class carries no ``role=`` tag, but ``_role_for_class`` maps it to a
    synthetic ``"sink"`` role so its object output sockets as ``DATAFLUX_OBJECT:sink`` — matching the
    ``DatasetProcessor.sink`` slot."""
    from fluxstudio.bridge import _role_for_class

    assert _role_for_class(_FakeStorageSink) == "sink"


def test_create_comfy_node_dispatches_sink_to_object_node() -> None:
    """A ``category="sink"`` storage sink (no run / no __call__(sample) / no role tag) is dispatched to
    an OBJECT producer node: instance+class outputs on a role-qualified ``DATAFLUX_OBJECT:sink`` wire,
    its ctor config rendered as widgets."""
    node = create_comfy_node(_FakeStorageSink, "Taidal_fake_sink_FakeStorageSink", "Taidal/Sink")
    assert node.TAIDAL_NODE_KIND == "object"
    assert node.RETURN_TYPES == (_object_socket("sink"), _object_socket("sink"))
    assert node.RETURN_NAMES == ("instance", "class")
    req = node.INPUT_TYPES()["required"]
    assert "path" in req and "overwrite" in req  # ctor config -> widgets, not sockets


def test_runnable_sink_slot_matches_sink_node_and_flux_is_source() -> None:
    """A ``DatasetProcessor``-shaped runnable renders ``flux`` as a ``DATAFLUX_SOURCE`` socket and
    ``sink`` as a ``DATAFLUX_OBJECT:sink`` slot — and the sink node's output type EQUALS that slot, so
    ComfyUI's string-equality rule lets a sink node dock into the processor (the whole feature)."""
    proc = _build_runnable_node(_FakeProcessor, "Taidal_fake_FakeProcessor", "Taidal/Proc")
    spec = proc.INPUT_TYPES()
    assert spec["required"]["flux"] == (SOURCE_TYPE,)  # a Flux engine node docks into ``flux``
    sink_slot = spec["optional"]["sink"][0]
    assert sink_slot == _object_socket("sink")
    sink_out = create_comfy_node(_FakeStorageSink, "Taidal_sink2", "Taidal/Sink").RETURN_TYPES[0]
    assert sink_out == sink_slot  # the dock contract: sink-node output == processor sink slot


class _PickyMember:
    """A non-lazy member that rejects an incomplete config at construction (like a model needing a
    positive ``num_classes``)."""

    def __init__(self, size: int = 0) -> None:
        if size < 1:
            raise ValueError("size must be >= 1")
        self.size = size


def test_object_node_warns_when_instance_cannot_be_built(monkeypatch: pytest.MonkeyPatch) -> None:
    import fluxstudio.bridge as bridge

    log = MagicMock()
    monkeypatch.setattr(bridge, "logger", log)

    node = _build_object_node(_PickyMember, "Taidal_Picky", "Taidal/Obj")
    instance, lazy = node().execute(size=0)["result"]  # invalid -> build fails
    assert instance is None  # could not build -> None
    assert isinstance(lazy, LazyClass)  # lazy is always available
    assert log.warning.called  # actionable warning emitted
    assert "could not build a live instance" in log.warning.call_args[0][0]

    # A complete config builds the live instance and emits no warning.
    log.reset_mock()
    instance2, _ = node().execute(size=4)["result"]
    assert isinstance(instance2, _PickyMember) and instance2.size == 4
    assert not log.warning.called


def test_register_module_nodes_admits_runnable_and_member() -> None:
    member = type("Model", (), {"__confluid_role__": "model", "__confluid_category__": "faketask_model"})
    # ``item["name"]`` must NOT start with ``_`` (register_module_nodes skips private callables).
    discovered = [
        {"name": "FakeTrainer", "path": "fake.mod.FakeTrainer"},
        {"name": "Model", "path": "fake.mod.Model"},
    ]

    def _resolve(path: str) -> Any:
        return {"fake.mod.FakeTrainer": _FakeTrainer, "fake.mod.Model": member}[path]

    mappings: dict = {}
    names: dict = {}
    with patch("fluxstudio.bridge.scan_module", return_value=discovered):
        with patch("fluxstudio.bridge.resolve_callable", side_effect=_resolve):
            register_module_nodes("fake.mod", mappings, names)

    # The runnable (no op/source/engine category) AND the role="model" member are both admitted.
    assert any(v.TAIDAL_NODE_KIND == "runnable" for v in mappings.values())
    assert any(v.TAIDAL_NODE_KIND == "object" for v in mappings.values())


# --------------------------------------------------------------------------- #
# strict_typing — Union[int, str] dual-socket behaviour
# --------------------------------------------------------------------------- #


def test_strict_typing_union_int_str_gets_dual_optional_sockets() -> None:
    """An op with __confluid_strict_typing__ gets {name}_samples + {name}_duration sockets."""
    from confluid import configurable as cfg
    from dataflux.sample import Sample

    from fluxstudio.bridge import _build_config_inputs

    @cfg(category="op", strict_typing=True)
    class _StrictOp:
        def __init__(self, length: Union[int, str] = 1024, mode: str = "front") -> None:
            self.length = length
            self.mode = mode

        def __call__(self, sample: Sample) -> Sample:
            return sample

    required, optional, split_map = _build_config_inputs(_StrictOp.__init__, strict_typing=True)
    # The Union[int, str] param is NOT in required (no normal widget).
    assert "length" not in required
    # It generates two optional sockets.
    assert "length_samples" in optional
    assert "length_duration" in optional
    assert optional["length_samples"][0] == "INT"
    assert optional["length_duration"][0] == "STRING"
    # split_map records the mapping.
    assert split_map == {"length": ("length_samples", "length_duration")}
    # Plain str param is still in required as a normal widget.
    assert "mode" in required


def test_strict_typing_false_union_int_str_gets_single_string_widget() -> None:
    """Default (strict_typing=False): Union[int, str] → single STRING widget, no split_map."""
    from fluxstudio.bridge import _build_config_inputs

    def fn(length: Union[int, str] = 1024) -> None: ...

    required, optional, split_map = _build_config_inputs(fn, strict_typing=False)
    assert "length" in required
    assert required["length"][0] == "STRING"
    assert split_map == {}


def test_strict_typing_node_execute_merges_duration_string() -> None:
    """The generated node merges {name}_duration back into the canonical param name."""
    from confluid import configurable as cfg
    from dataflux.sample import Sample

    from fluxstudio.bridge import create_comfy_node

    received: dict = {}

    @cfg(category="op", strict_typing=True)
    class _MergeOp:
        def __init__(self, length: Union[int, str] = 1024) -> None:
            received["length"] = length

        def __call__(self, sample: Sample) -> Sample:
            return sample

    node_cls = create_comfy_node(_MergeOp, "Taidal__MergeOp")
    node = node_cls()

    # Wire the duration socket: pass "5ms", leave samples absent.
    node.execute(length_duration="5ms")
    assert received["length"] == "5ms"

    # Wire the samples socket: pass 2048, leave duration empty.
    node.execute(length_samples=2048, length_duration="")
    assert received["length"] == 2048

    # Both absent: constructor default (1024) applies — neither key in kwargs.
    received.clear()
    node.execute()
    assert received.get("length", 1024) == 1024


def test_strict_typing_node_execute_str_wins_over_samples() -> None:
    """Duration string takes priority over samples INT when both are supplied."""
    from confluid import configurable as cfg
    from dataflux.sample import Sample

    from fluxstudio.bridge import create_comfy_node

    received: dict = {}

    @cfg(category="op", strict_typing=True)
    class _PriorityOp:
        def __init__(self, length: Union[int, str] = 1024) -> None:
            received["length"] = length

        def __call__(self, sample: Sample) -> Sample:
            return sample

    node = create_comfy_node(_PriorityOp, "Taidal__PriorityOp")()
    node.execute(length_samples=512, length_duration="2ms")
    assert received["length"] == "2ms"


# ---------------------------------------------------------------------------
# Tuple[T, T] range-selector widget tests
# ---------------------------------------------------------------------------


def test_map_type_tuple_float_returns_range_float() -> None:
    from typing import Tuple

    assert _map_type("power_range", Tuple[float, float]) == "RANGE_FLOAT"


def test_map_type_tuple_int_returns_range_int() -> None:
    from typing import Tuple

    assert _map_type("freq_range", Tuple[int, int]) == "RANGE_INT"


def test_map_type_optional_tuple_float_returns_range_float() -> None:
    from typing import Optional, Tuple

    assert _map_type("power_range", Optional[Tuple[float, float]]) == "RANGE_FLOAT"


def test_map_type_bare_float_still_returns_float() -> None:
    assert _map_type("threshold", float) == "FLOAT"


def test_build_config_inputs_tuple_float_emits_lo_hi_widgets() -> None:
    from typing import Tuple

    from fluxstudio.bridge import _build_config_inputs

    def fn(power_range: Tuple[float, float] = (0.01, 10.0)) -> None: ...

    required, _, _ = _build_config_inputs(fn)
    assert "power_range__lo" in required
    assert "power_range__hi" in required
    assert required["power_range__lo"][0] == "FLOAT"
    assert required["power_range__hi"][0] == "FLOAT"
    assert required["power_range__lo"][1]["default"] == pytest.approx(0.01)
    assert required["power_range__hi"][1]["default"] == pytest.approx(10.0)
    # Original name must NOT appear as a widget
    assert "power_range" not in required


def test_build_config_inputs_tuple_int_emits_lo_hi_widgets() -> None:
    from typing import Tuple

    from fluxstudio.bridge import _build_config_inputs

    def fn(n_range: Tuple[int, int] = (2, 8)) -> None: ...

    required, _, _ = _build_config_inputs(fn)
    assert required["n_range__lo"] == ("INT", {**DEFAULT_WIDGET_CONFIG["INT"], "default": 2})
    assert required["n_range__hi"] == ("INT", {**DEFAULT_WIDGET_CONFIG["INT"], "default": 8})


def test_build_config_inputs_tuple_no_default_uses_fallback() -> None:
    from typing import Tuple

    from fluxstudio.bridge import _build_config_inputs

    def fn(r: Tuple[float, float]) -> None: ...

    required, _, _ = _build_config_inputs(fn)
    assert required["r__lo"] == ("FLOAT", {**DEFAULT_WIDGET_CONFIG["FLOAT"], "default": 0.0})
    assert required["r__hi"] == ("FLOAT", {**DEFAULT_WIDGET_CONFIG["FLOAT"], "default": 1.0})


def test_coerce_kwargs_reconstructs_float_tuple() -> None:
    from typing import Tuple

    def fn(power_range: Tuple[float, float] = (0.01, 10.0)) -> None: ...

    result = _coerce_kwargs(fn, {"power_range__lo": 0.05, "power_range__hi": 8.0})
    assert result == {"power_range": (0.05, 8.0)}


def test_coerce_kwargs_reconstructs_int_tuple() -> None:
    from typing import Tuple

    def fn(n_range: Tuple[int, int] = (2, 8)) -> None: ...

    result = _coerce_kwargs(fn, {"n_range__lo": 3, "n_range__hi": 7})
    assert result == {"n_range": (3, 7)}


def test_coerce_kwargs_tuple_leaves_other_kwargs_intact() -> None:
    from typing import Tuple

    def fn(power_range: Tuple[float, float] = (0.0, 1.0), scale: float = 1.0) -> None: ...

    result = _coerce_kwargs(fn, {"power_range__lo": 0.1, "power_range__hi": 0.9, "scale": 2.0})
    assert result["power_range"] == (0.1, 0.9)
    assert result["scale"] == pytest.approx(2.0)


def test_range_widget_node_execute_passes_tuple_to_constructor() -> None:
    from typing import Tuple

    from confluid import configurable as cfg
    from dataflux.sample import Sample

    received: dict = {}

    @cfg(category="op")
    class _RangeOp:
        def __init__(self, power_range: Tuple[float, float] = (0.01, 10.0)) -> None:
            received["power_range"] = power_range

        def __call__(self, sample: Sample) -> Sample:
            return sample

    node_cls = create_comfy_node(_RangeOp, "Taidal__RangeOp")
    # Verify the INPUT_TYPES has lo/hi widgets, not a single tuple widget
    required = node_cls.INPUT_TYPES()["required"]
    assert "power_range__lo" in required
    assert "power_range__hi" in required
    assert "power_range" not in required
    # Verify execute reconstructs the tuple
    node_cls().execute(power_range__lo=0.05, power_range__hi=5.0)
    assert received["power_range"] == pytest.approx((0.05, 5.0))


def test_interval_bounds_extraction() -> None:
    """_interval_bounds reads Interval/Ge/Le metadata, unwraps Optional, (None, None) when unmarked."""
    from typing import Annotated as _Ann
    from typing import Optional as _Opt

    from annotated_types import Ge as _Ge
    from annotated_types import Interval as _Interval

    from fluxstudio.bridge import _interval_bounds

    assert _interval_bounds(_Ann[float, _Interval(ge=-200.0, le=50.0)]) == (-200.0, 50.0)
    assert _interval_bounds(_Ann[float, _Ge(ge=0)]) == (0.0, None)  # partial: lower bound only
    assert _interval_bounds(_Opt[_Ann[float, _Interval(ge=-1.0, le=1.0)]]) == (-1.0, 1.0)
    assert _interval_bounds(float) == (None, None)
    assert _interval_bounds(_Opt[float]) == (None, None)


def test_interval_marked_widgets_get_bounds() -> None:
    """A PEP-593 Interval mark narrows the full-range default bounds on FLOAT and INT widgets."""
    from typing import Annotated as _Ann

    from annotated_types import Interval as _Interval
    from confluid import configurable as _cfg

    @_cfg(category="op")
    class _MarkedOp:
        def __init__(
            self,
            noise_power_db: _Ann[float, _Interval(ge=-200.0, le=50.0)] = -30.0,
            n_bits: _Ann[int, _Interval(ge=1, le=32)] = 8,
        ) -> None:
            self.noise_power_db = noise_power_db
            self.n_bits = n_bits

        def __call__(self, sample: Sample) -> Sample:
            return sample

    spec = create_comfy_node(_MarkedOp, "Taidal__MarkedOp").INPUT_TYPES()
    f_type, f_cfg = spec["required"]["noise_power_db"]
    assert f_type == "FLOAT" and f_cfg["min"] == -200.0 and f_cfg["max"] == 50.0
    i_type, i_cfg = spec["required"]["n_bits"]
    assert i_type == "INT" and i_cfg["min"] == 1 and i_cfg["max"] == 32


def test_interval_marked_tuple_range_bounds_both_endpoints() -> None:
    """Annotated[Tuple[float, float], Interval(...)] bounds BOTH __lo/__hi widgets;
    a partial mark (Ge only) leaves the other side at the full-range default."""
    from typing import Annotated as _Ann
    from typing import Tuple as _Tup

    from annotated_types import Interval as _Interval
    from confluid import configurable as _cfg

    @_cfg(category="op")
    class _RangeMarkedOp:
        def __init__(
            self,
            db_window: _Ann[_Tup[float, float], _Interval(ge=-200.0, le=100.0)] = (-80.0, -60.0),
            watts: _Ann[_Tup[float, float], _Interval(ge=0.0)] = (0.01, 10.0),
        ) -> None:
            self.db_window = db_window
            self.watts = watts

        def __call__(self, sample: Sample) -> Sample:
            return sample

    spec = create_comfy_node(_RangeMarkedOp, "Taidal__RangeMarkedOp").INPUT_TYPES()
    for key in ("db_window__lo", "db_window__hi"):
        _, cfg = spec["required"][key]
        assert cfg["min"] == -200.0 and cfg["max"] == 100.0
    _, w_cfg = spec["required"]["watts__lo"]
    assert w_cfg["min"] == 0.0 and w_cfg["max"] == 1e308  # ge-only: max stays full-range


def test_optional_interval_marked_param_stays_nullable_string_and_coerces() -> None:
    """Optional[Annotated[float, Interval]] = None keeps the blank-STRING nullable
    convention, and a typed value still casts to float."""
    from typing import Annotated as _Ann
    from typing import Optional as _Opt

    from annotated_types import Interval as _Interval
    from confluid import configurable as _cfg

    @_cfg(category="op")
    class _NullableMarkedOp:
        def __init__(self, maybe_db: _Opt[_Ann[float, _Interval(ge=-200.0, le=50.0)]] = None) -> None:
            self.maybe_db = maybe_db

        def __call__(self, sample: Sample) -> Sample:
            return sample

    spec = create_comfy_node(_NullableMarkedOp, "Taidal__NullableMarkedOp").INPUT_TYPES()
    widget_type, cfg = spec["required"]["maybe_db"]
    assert widget_type == "STRING"
    # Still nullable-STRING, but tagged so its converted socket retypes to FLOAT (connectable).
    assert cfg["taidal_widget"] == "NULLABLE_NUMERIC" and cfg["taidal_numeric"] == "FLOAT"
    assert _coerce_kwargs(_NullableMarkedOp, {"maybe_db": "-120"})["maybe_db"] == -120.0
    assert _coerce_kwargs(_NullableMarkedOp, {"maybe_db": ""})["maybe_db"] is None


def test_map_type_unwraps_annotated() -> None:
    """Annotated metadata must not reach the string heuristics — the repr of
    ``Interval`` contains the substring "int" and would mis-classify."""
    from typing import Annotated as _Ann
    from typing import Tuple as _Tup

    from annotated_types import Interval as _Interval

    from fluxstudio.bridge import _map_type

    assert _map_type("x", _Ann[float, _Interval(ge=-1.0, le=1.0)]) == "FLOAT"
    assert _map_type("x", _Ann[int, _Interval(ge=0, le=10)]) == "INT"
    assert _map_type("x", _Ann[_Tup[float, float], _Interval(ge=0.0)]) == "RANGE_FLOAT"
