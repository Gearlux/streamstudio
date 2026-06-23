"""Tests for the FluxStudio MODEL node type, the path-output socket fix, and the Melody picker tag.

A MODEL node (``@configurable(category="model")``) RUNS inference: like an op it takes an optional
``sample`` and outputs the applied ``sample``, but its second output is the constructed model on a
``DATAFLUX_OBJECT:model`` socket (dockable into a downstream evaluator).
"""

from typing import Optional, cast

import pytest
from confluid import configurable
from confluid.decorators import output
from dataflux.sample import Sample

from fluxstudio.bridge import (
    _NODE_CATEGORIES,
    OBJECT_TYPE,
    SAMPLE_TYPE,
    _is_melody_dataset_id_param,
    _model_params,
    _object_socket,
    _output_socket_type,
    create_comfy_node,
)


@configurable(category="model")
class _FakeModel:
    """A model node: config knobs + ``__call__(sample)`` that stamps a prediction."""

    def __init__(self, model_uri: str = "", k: int = 5) -> None:
        self.model_uri = model_uri
        self.k = k

    def __call__(self, sample: Sample) -> Sample:
        meta = dict(sample.meta)
        meta["detection_labels"] = ["WLAN"]
        return sample._replace(metadata=meta)


@configurable(category="model")
class _NoneModel:
    def __init__(self) -> None: ...

    def __call__(self, sample: Sample) -> Optional[Sample]:
        return None


@configurable(category="model")
class _FakeInference:
    """A ``ModelInference``-style node: a WIRED ``model`` (stamped) + config widget, delegating infer.

    Mirrors the waivefront-helios ``HeliosModelInference`` shape — ``__taidal_model_params__`` marks
    ``model`` as a wired ``DATAFLUX_OBJECT:model`` input; ``__call__`` delegates to it.
    """

    __taidal_model_params__ = ("model",)

    def __init__(self, model: Optional[object] = None, threshold: float = 0.5) -> None:
        self.model = model
        self.threshold = threshold

    def __call__(self, sample: Sample) -> Sample:
        if self.model is None:
            raise ValueError("model not wired")
        return cast(Sample, self.model(sample))  # type: ignore[operator]


# --------------------------------------------------------------------- category allowlist + dispatch


def test_model_in_node_categories() -> None:
    assert "model" in _NODE_CATEGORIES


def test_model_class_becomes_model_node_not_op() -> None:
    node = create_comfy_node(_FakeModel, "Taidal__FakeModel")
    # Sample (applied) + the model object on a role-qualified DATAFLUX_OBJECT:model socket.
    assert node.RETURN_TYPES == (SAMPLE_TYPE, _object_socket("model"))
    assert node.RETURN_NAMES == ("sample", "model")
    assert node.TAIDAL_NODE_KIND == "model"  # NOT "op"
    spec = node.INPUT_TYPES()
    assert spec["required"]["model_uri"][0] == "STRING"
    assert spec["required"]["k"][0] == "INT"
    assert spec["optional"]["sample"] == (SAMPLE_TYPE,)


def test_model_object_socket_is_role_qualified() -> None:
    assert _object_socket("model") == f"{OBJECT_TYPE}:model"


def test_model_node_execute_applies_and_returns_model() -> None:
    node = create_comfy_node(_FakeModel, "Taidal__FakeModel")()
    s = Sample(input=1, target=None, metadata={})
    applied, model = node.execute(sample=s, model_uri="maf:/X/1.0", k=3)["result"]
    assert applied.metadata["detection_labels"] == ["WLAN"]  # inference ran
    assert isinstance(model, _FakeModel)  # the model object is the second output
    assert model.model_uri == "maf:/X/1.0" and model.k == 3


def test_model_node_without_sample_returns_model_only() -> None:
    node = create_comfy_node(_FakeModel, "Taidal__FakeModel")()
    applied, model = node.execute(model_uri="", k=5)["result"]
    assert applied is None  # no sample wired → no inference
    assert isinstance(model, _FakeModel)


def test_model_node_raises_on_none_result() -> None:
    node = create_comfy_node(_NoneModel, "Taidal__NoneModel")()
    with pytest.raises(RuntimeError):
        node.execute(sample=Sample(input=1, target=None, metadata={}))


# --------------------------------------------------- wired model input (ModelInference decoupling)


def test_model_params_reads_stamp() -> None:
    assert _model_params(_FakeInference) == ["model"]
    assert _model_params(_FakeModel) == []  # self-contained model: no wired model slot


def test_model_input_param_is_wired_socket_not_widget() -> None:
    node = create_comfy_node(_FakeInference, "Taidal__FakeInference")
    spec = node.INPUT_TYPES()
    # `model` rides a DATAFLUX_OBJECT:model input socket; it is NOT a widget.
    assert spec["optional"]["model"] == (_object_socket("model"),)
    assert "model" not in spec["required"]
    # Other config params stay widgets.
    assert spec["required"]["threshold"][0] == "FLOAT"
    assert spec["optional"]["sample"] == (SAMPLE_TYPE,)


def test_model_node_wires_model_and_delegates() -> None:
    inference = create_comfy_node(_FakeInference, "Taidal__FakeInference")()
    loader = _FakeModel(model_uri="maf:/X/1.0")  # a Loader node's model output (a model object)
    s = Sample(input=1, target=None, metadata={})
    applied, model_obj = inference.execute(sample=s, model=loader, threshold=0.7)["result"]
    # The wired model ran inference (delegated through _FakeInference.__call__ → loader).
    assert applied.metadata["detection_labels"] == ["WLAN"]
    assert isinstance(model_obj, _FakeInference)  # the node outputs ITSELF as the model object
    assert model_obj.model is loader and model_obj.threshold == 0.7  # wired model set, widget coerced


def test_model_node_unwired_model_is_none() -> None:
    inference = create_comfy_node(_FakeInference, "Taidal__FakeInference")()
    # No sample wired → no inference attempt; model object built with model=None.
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
class _FakeMelodyDataset:
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
    node = create_comfy_node(_FakeMelodyDataset, "Taidal__FakeMelodyDataset")
    assert node.RETURN_TYPES == ("STRING",)
    assert node.RETURN_NAMES == ("directory",)


# --------------------------------------------------------------------- Melody dataset-id picker tag


def test_is_melody_dataset_id_param() -> None:
    assert _is_melody_dataset_id_param("name", _FakeMelodyDataset) is True
    assert _is_melody_dataset_id_param("name", _FakeModel) is False  # not a Melody class
    assert _is_melody_dataset_id_param("k", _FakeMelodyDataset) is False  # not a dataset-id param


def test_melody_name_widget_is_tagged() -> None:
    node = create_comfy_node(_FakeMelodyDataset, "Taidal__FakeMelodyDataset")
    name_spec = node.INPUT_TYPES()["required"]["name"]
    assert name_spec[0] == "STRING"
    assert name_spec[1]["taidal_widget"] == "MELODY_DATASET_ID"
