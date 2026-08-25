"""Tests for the StreamStudio MODEL node type, the path-output socket fix, and stamped widget kinds.

A MODEL node (``@configurable(category="model")``) RUNS inference: like an op it takes an optional
``record`` (a plain-dict ``Record`` on the ``RECORDSTREAM_RECORD`` wire) and outputs the applied
record, but its second output is the constructed model on a ``RECORDSTREAM_OBJECT:model`` socket
(dockable into a downstream evaluator).
"""

import inspect
from typing import Optional, cast

import pytest
from confluid import configurable
from confluid.decorators import output
from recordstream import Record

from streamstudio.bridge import (
    _NODE_CATEGORIES,
    OBJECT_TYPE,
    RECORD_TYPE,
    _model_params,
    _object_socket,
    _output_socket_type,
    _widget_for,
    create_comfy_node,
)


@configurable(category="model")
class _FakeModel:
    """A model node: config knobs + ``__call__(record)`` that stamps a prediction."""

    def __init__(self, model_uri: str = "", k: int = 5) -> None:
        self.model_uri = model_uri
        self.k = k

    def __call__(self, record: Record) -> Record:
        return {**record, "detection_labels": ["WLAN"]}


@configurable(category="model")
class _NoneModel:
    def __init__(self) -> None: ...

    def __call__(self, record: Record) -> Optional[Record]:
        return None


@configurable(category="model")
class _FakeInference:
    """A ``ModelInference``-style node: a WIRED ``model`` (stamped) + config widget, delegating infer.

    Mirrors a companion package's model-inference bridge shape — ``__taidal_model_params__`` marks
    ``model`` as a wired ``RECORDSTREAM_OBJECT:model`` input; ``__call__`` delegates to it.
    """

    __taidal_model_params__ = ("model",)

    def __init__(self, model: Optional[object] = None, threshold: float = 0.5) -> None:
        self.model = model
        self.threshold = threshold

    def __call__(self, record: Record) -> Record:
        if self.model is None:
            raise ValueError("model not wired")
        return cast(Record, self.model(record))  # type: ignore[operator]


# --------------------------------------------------------------------- category allowlist + dispatch


def test_model_in_node_categories() -> None:
    assert "model" in _NODE_CATEGORIES


def test_model_class_becomes_model_node_not_op() -> None:
    node = create_comfy_node(_FakeModel, "Taidal__FakeModel")
    # Applied record + the model object on a role-qualified RECORDSTREAM_OBJECT:model socket.
    assert node.RETURN_TYPES == (RECORD_TYPE, _object_socket("model"))
    assert node.RETURN_NAMES == ("record", "model")
    assert node.TAIDAL_NODE_KIND == "model"  # NOT "op"
    spec = node.INPUT_TYPES()
    assert spec["required"]["model_uri"][0] == "STRING"
    assert spec["required"]["k"][0] == "INT"
    assert spec["optional"]["record"] == (RECORD_TYPE,)


def test_model_object_socket_is_role_qualified() -> None:
    assert _object_socket("model") == f"{OBJECT_TYPE}:model"


def test_model_node_execute_applies_and_returns_model() -> None:
    node = create_comfy_node(_FakeModel, "Taidal__FakeModel")()
    s: Record = {"input": 1}
    applied, model = node.execute(record=s, model_uri="maf:/X/1.0", k=3)["result"]
    assert applied["detection_labels"] == ["WLAN"]  # inference ran
    assert isinstance(model, _FakeModel)  # the model object is the second output
    assert model.model_uri == "maf:/X/1.0" and model.k == 3


def test_model_node_without_record_returns_model_only() -> None:
    node = create_comfy_node(_FakeModel, "Taidal__FakeModel")()
    applied, model = node.execute(model_uri="", k=5)["result"]
    assert applied is None  # no record wired → no inference
    assert isinstance(model, _FakeModel)


def test_model_node_raises_on_none_result() -> None:
    node = create_comfy_node(_NoneModel, "Taidal__NoneModel")()
    with pytest.raises(RuntimeError):
        node.execute(record={"input": 1})


# --------------------------------------------------- wired model input (ModelInference decoupling)


def test_model_params_reads_stamp() -> None:
    assert _model_params(_FakeInference) == ["model"]
    assert _model_params(_FakeModel) == []  # self-contained model: no wired model slot


def test_model_input_param_is_wired_socket_not_widget() -> None:
    node = create_comfy_node(_FakeInference, "Taidal__FakeInference")
    spec = node.INPUT_TYPES()
    # `model` rides a RECORDSTREAM_OBJECT:model input socket; it is NOT a widget.
    assert spec["optional"]["model"] == (_object_socket("model"),)
    assert "model" not in spec["required"]
    # Other config params stay widgets.
    assert spec["required"]["threshold"][0] == "FLOAT"
    assert spec["optional"]["record"] == (RECORD_TYPE,)


def test_model_node_wires_model_and_delegates() -> None:
    inference = create_comfy_node(_FakeInference, "Taidal__FakeInference")()
    loader = _FakeModel(model_uri="maf:/X/1.0")  # a Loader node's model output (a model object)
    s: Record = {"input": 1}
    applied, model_obj = inference.execute(record=s, model=loader, threshold=0.7)["result"]
    # The wired model ran inference (delegated through _FakeInference.__call__ → loader).
    assert applied["detection_labels"] == ["WLAN"]
    assert isinstance(model_obj, _FakeInference)  # the node outputs ITSELF as the model object
    assert model_obj.model is loader and model_obj.threshold == 0.7  # wired model set, widget coerced


def test_model_node_unwired_model_is_none() -> None:
    inference = create_comfy_node(_FakeInference, "Taidal__FakeInference")()
    # No record wired → no inference attempt; model object built with model=None.
    applied, model_obj = inference.execute(threshold=0.5)["result"]
    assert applied is None
    assert model_obj.model is None


# --------------------------------------------------------------------- path-output socket fix


def test_path_named_output_is_string_socket() -> None:
    # A path-named scalar @output must ride a STRING socket (outputs are never directory pickers),
    # so it wires into a reader's STRING-backed `directory` widget.
    assert _output_socket_type("directory", str) == "STRING"
    assert _output_socket_type("output_dir", str) == "STRING"
    # A non-path scalar is unaffected.
    assert _output_socket_type("count", int) == "INT"


@configurable(category="value")
class _FakeDownloadDataset:
    """A value producer whose ``directory`` @output is a path-like string."""

    def __init__(self, name: str = "") -> None:
        self.name = name

    def __call__(self) -> str:
        return self.directory

    @property
    @output
    def directory(self) -> str:
        return f"/data/{self.name}"


def test_value_node_directory_output_is_string() -> None:
    node = create_comfy_node(_FakeDownloadDataset, "Taidal__FakeDownloadDataset")
    assert node.RETURN_TYPES == ("STRING",)
    assert node.RETURN_NAMES == ("directory",)


# ------------------------------------------------------------- producer-stamped widget kinds


@configurable(category="value")
class _StampedDataset:
    """A value producer whose package stamped widget kinds onto two of its params.

    ``__taidal_widget_params__`` is the cross-package stamp protocol: the producing package maps a
    param NAME to an opaque ``taidal_widget`` tag that its own web extension consumes — the bridge
    attaches no meaning to the tag string itself.
    """

    __taidal_widget_params__ = {"name": "PLATFORM_DATASET_ID", "dataset_dir": "PLATFORM_DIR_PICKER"}

    def __init__(self, name: str = "", dataset_dir: str = "", k: int = 5) -> None:
        self.name = name
        self.dataset_dir = dataset_dir
        self.k = k

    def __call__(self) -> str:
        return self.name


def test_stamped_widget_param_is_tagged() -> None:
    node = create_comfy_node(_StampedDataset, "Taidal__StampedDataset")
    name_spec = node.INPUT_TYPES()["required"]["name"]
    assert name_spec[0] == "STRING"
    assert name_spec[1]["taidal_widget"] == "PLATFORM_DATASET_ID"


def test_stamped_path_named_param_is_not_a_directory_picker() -> None:
    # The stamp is checked BEFORE the path-name affordance: a stamped `*_dir` param keeps the
    # producer's tag instead of falling into the directory-picker branch.
    spec = create_comfy_node(_StampedDataset, "Taidal__StampedDataset").INPUT_TYPES()["required"]["dataset_dir"]
    assert spec[0] == "STRING"
    assert spec[1]["taidal_widget"] == "PLATFORM_DIR_PICKER"


def test_unstamped_param_on_a_stamped_class_is_unaffected() -> None:
    node = create_comfy_node(_StampedDataset, "Taidal__StampedDataset")
    assert node.INPUT_TYPES()["required"]["k"][0] == "INT"  # not in the stamp → normal mapping


def test_unstamped_class_gets_no_widget_tag() -> None:
    # Without a stamp there is NO widget kind — a like-named `name` param is a plain STRING
    # (no class-name convention resurrects a picker; the stamp is the only trigger).
    name_spec = create_comfy_node(_FakeDownloadDataset, "Taidal__FakeDownloadDataset").INPUT_TYPES()["required"]["name"]
    assert name_spec[0] == "STRING"
    assert "taidal_widget" not in name_spec[1]


def test_widget_for_without_owner_is_safe() -> None:
    param = inspect.Parameter("name", inspect.Parameter.KEYWORD_ONLY, default="", annotation=str)
    comfy_type, config = _widget_for(param, owner=None)
    assert comfy_type == "STRING"
    assert "taidal_widget" not in config
