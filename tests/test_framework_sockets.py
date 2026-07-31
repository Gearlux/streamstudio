"""Framework-aware object sockets — a torch loss must not dock into a Keras trainer.

confluid tags a class with the ENGINE whose API it belongs to, and a discovery service filters
slot options by it. The canvas had no such guard: every object socket was role-qualified only, so
a cross-engine wire drew fine and failed at run time with a type error far from its cause.

The rule is deliberately asymmetric — **exclusion requires two claims**. A class that declares no
engine keeps connecting everywhere, which is what lets untagged projects behave exactly as before.
No arrangement of socket STRINGS can express that (a shared bare token allows everything; no bare
token refuses untagged producers), so the claim rides the string and two validators apply the rule:
:func:`streamstudio.bridge._validate_node_inputs` on the backend and ``web/framework.js`` on the
canvas.

These also restore a net for the socket-typing helpers generally: the suites that used to pin
``_role_for_class`` / ``_object_socket_for_class`` / cross-role rejection were deleted in 44ddb5b
and never restored, so nothing else here would catch a regression in them.
"""

from typing import Any, Dict, Optional

import pytest
from confluid import configurable
from confluid.decorators import output

from streamstudio.bridge import (
    OBJECT_TYPE,
    _framework_of,
    _frameworks_compatible,
    _object_socket,
    _object_socket_for_class,
    _output_socket_type,
    _role_for_class,
    _socket_frameworks,
    _validate_node_inputs,
    create_comfy_node,
)

TORCH_LOSS = f"{OBJECT_TYPE}:loss:torch,{OBJECT_TYPE}:loss"
KERAS_LOSS = f"{OBJECT_TYPE}:loss:keras,{OBJECT_TYPE}:loss"
BARE_LOSS = f"{OBJECT_TYPE}:loss"


def _slots(node: Any) -> Dict[str, Any]:
    spec = node.INPUT_TYPES()
    return {**spec.get("required", {}), **spec.get("optional", {})}


# ---------------------------------------------------------------------------
# reading the claim
# ---------------------------------------------------------------------------


def test_framework_comes_from_the_explicit_tag() -> None:
    @configurable(task="t", role="loss", framework="keras")
    class Tagged:
        pass

    assert _framework_of(Tagged) == "keras"


def test_framework_falls_back_to_the_mro() -> None:
    """A ``register()``-ed third-party class carries no tag, so the base classes answer."""
    torch = pytest.importorskip("torch")

    assert _framework_of(torch.nn.CrossEntropyLoss) == "torch"


def test_a_keras_class_is_not_inferred_as_torch() -> None:
    """The MRO scan matches the FULL ``module.qualname``.

    The role scan next door matches bare qualnames, so its ``"Metric"`` needle hits a Keras
    metric too — a framework scan built the same way would call every Keras class torch.
    """
    keras = pytest.importorskip("keras")

    assert _framework_of(keras.metrics.Precision) == "keras"


def test_an_untagged_class_claims_nothing() -> None:
    class Plain:
        pass

    assert _framework_of(Plain) is None


# ---------------------------------------------------------------------------
# the socket string
# ---------------------------------------------------------------------------


def test_a_framework_claim_rides_a_union_with_the_bare_role() -> None:
    """The bare member is what keeps a tagged producer wireable into an untagged slot.

    Both litegraph and ComfyUI's backend treat a comma type as "compatible if the sets
    intersect", so dropping it would refuse every legitimate tagged↔untagged wire.
    """
    assert _object_socket("loss", "torch") == TORCH_LOSS
    assert _object_socket("loss") == BARE_LOSS
    assert _object_socket(None, "torch") == OBJECT_TYPE  # no role to qualify


def test_the_union_keeps_its_base_type_readable() -> None:
    """Every base-aware consumer splits on the FIRST colon, so the extra segment is transparent."""
    from streamstudio.bridge import _base_socket_type, _is_socket_type

    assert _base_socket_type(TORCH_LOSS) == OBJECT_TYPE
    assert _is_socket_type(TORCH_LOSS)


def test_socket_frameworks_reads_only_a_real_claim() -> None:
    assert _socket_frameworks(TORCH_LOSS) == {"torch"}
    assert _socket_frameworks(BARE_LOSS) == set()
    assert _socket_frameworks(OBJECT_TYPE) == set()
    assert _socket_frameworks("*") == set()
    assert _socket_frameworks(None) == set()


# ---------------------------------------------------------------------------
# the rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "received, expected, allowed",
    [
        (TORCH_LOSS, KERAS_LOSS, False),  # the whole point
        (KERAS_LOSS, TORCH_LOSS, False),
        (TORCH_LOSS, TORCH_LOSS, True),
        (TORCH_LOSS, BARE_LOSS, True),  # only one side claims -> allowed
        (BARE_LOSS, KERAS_LOSS, True),  # ...and the other way round
        (BARE_LOSS, BARE_LOSS, True),
        ("*", KERAS_LOSS, True),  # the Retag escape hatch claims nothing
    ],
)
def test_exclusion_requires_two_claims(received: str, expected: str, allowed: bool) -> None:
    assert _frameworks_compatible(received, expected) is allowed


# ---------------------------------------------------------------------------
# producer and consumer, on the REAL classes
# ---------------------------------------------------------------------------


def _sonair() -> Any:
    """The real two-engine classes, or skip — fakes cannot pin the tags they actually carry."""
    pytest.importorskip("sonair")
    pytest.importorskip("keras")
    import sonair.keras_classification  # noqa: F401 — registers the keras set
    import sonair.torch_classification  # noqa: F401 — registers the torch set
    from confluid import get_registry
    from sonair.keras_classifier import KerasClassifier
    from sonair.lightning_classifier import LightningClassifier

    registry = get_registry()
    return (
        LightningClassifier,
        KerasClassifier,
        registry.get_class("CrossEntropyLoss", role="loss", framework="torch"),
        registry.get_class("SparseCategoricalCrossentropy", role="loss", framework="keras"),
    )


def test_a_loss_node_output_claims_its_engine() -> None:
    _lt, _kt, torch_loss, keras_loss = _sonair()

    assert _object_socket_for_class(torch_loss) == TORCH_LOSS
    assert _object_socket_for_class(keras_loss) == KERAS_LOSS


def test_a_trainer_slot_claims_its_engine() -> None:
    lightning_cls, keras_cls, _tl, _kl = _sonair()

    lightning = _slots(create_comfy_node(lightning_cls, "Taidal_TestLightning"))
    keras = _slots(create_comfy_node(keras_cls, "Taidal_TestKeras"))

    assert lightning["loss_fn"][0] == TORCH_LOSS
    assert keras["loss"][0] == KERAS_LOSS


def test_the_keras_trainers_loss_slot_is_role_qualified_at_all() -> None:
    """Its param is named ``loss``, not ``loss_fn``.

    Missing from the slot-role table, it rendered BARE while every registered Keras loss node
    output ``…:loss`` — so no Keras loss could be wired into the Keras trainer at all.
    """
    _lt, keras_cls, _tl, _kl = _sonair()

    slot = _slots(create_comfy_node(keras_cls, "Taidal_TestKerasLoss"))["loss"][0]
    assert slot.startswith(f"{OBJECT_TYPE}:loss")


@pytest.mark.parametrize(
    "producer, consumer, slot, allowed",
    [
        ("torch", "keras", "loss", False),
        ("keras", "torch", "loss_fn", False),
        ("torch", "torch", "loss_fn", True),
        ("keras", "keras", "loss", True),
    ],
)
def test_the_backend_refuses_only_a_cross_engine_wire(producer: str, consumer: str, slot: str, allowed: bool) -> None:
    lightning_cls, keras_cls, torch_loss, keras_loss = _sonair()
    node = create_comfy_node(
        lightning_cls if consumer == "torch" else keras_cls, f"Taidal_TestRule_{producer}_{consumer}"
    )
    received = _object_socket_for_class(torch_loss if producer == "torch" else keras_loss)

    verdict = _validate_node_inputs(node, {slot: received})

    assert (verdict is True) is allowed
    if not allowed:
        assert "different engines" in verdict and producer in verdict


def test_an_untagged_producer_still_docks_into_a_tagged_slot() -> None:
    """The asymmetry, end to end: untagged projects behave exactly as before."""
    _lt, keras_cls, _tl, _kl = _sonair()
    node = create_comfy_node(keras_cls, "Taidal_TestUntaggedIn")

    assert _validate_node_inputs(node, {"loss": BARE_LOSS}) is True


def test_a_tagged_producer_still_docks_into_an_untagged_slot() -> None:
    pytest.importorskip("recordstream")
    from recordstream.processing import DatasetProcessor

    node = create_comfy_node(DatasetProcessor, "Taidal_TestUntaggedSlot")
    assert _framework_of(DatasetProcessor) is None

    assert _validate_node_inputs(node, {"sink": f"{OBJECT_TYPE}:sink:torch,{OBJECT_TYPE}:sink"}) is True


def test_a_trainers_trained_model_output_claims_the_trainers_engine() -> None:
    """A Keras trainer's ``trained_model`` must not dock into a torch evaluator."""
    _lt, keras_cls, _tl, _kl = _sonair()

    node = create_comfy_node(keras_cls, "Taidal_TestKerasOutputs")
    types = dict(zip(node.RETURN_NAMES, node.RETURN_TYPES))

    assert types["trained_model"] == f"{OBJECT_TYPE}:model:keras,{OBJECT_TYPE}:model"


# ---------------------------------------------------------------------------
# the pre-existing behaviour this must not regress
# ---------------------------------------------------------------------------


def test_cross_role_is_still_refused() -> None:
    """A loss must not dock into a model slot — the role guard predates the framework one."""
    from streamstudio.bridge import _fallback_compatible

    assert not _fallback_compatible(f"{OBJECT_TYPE}:loss", f"{OBJECT_TYPE}:model")
    assert not _fallback_compatible(TORCH_LOSS, f"{OBJECT_TYPE}:model:torch,{OBJECT_TYPE}:model")


def test_the_fallback_matcher_implements_comma_unions() -> None:
    """It stands in for ComfyUI's own matcher when ComfyUI is not importable.

    Without the union case the fallback disagrees with production about the very thing these
    tests pin — every tagged wire would read as refused outside ComfyUI.
    """
    from streamstudio.bridge import _fallback_compatible

    assert _fallback_compatible(TORCH_LOSS, BARE_LOSS)
    assert _fallback_compatible(BARE_LOSS, TORCH_LOSS)
    assert _fallback_compatible("*", KERAS_LOSS)
    assert not _fallback_compatible(TORCH_LOSS, f"{OBJECT_TYPE}:model")


def test_role_inference_is_unchanged_for_an_untagged_metric() -> None:
    torchmetrics = pytest.importorskip("torchmetrics")

    assert _role_for_class(torchmetrics.classification.MulticlassAccuracy) == "metric"


def test_a_role_less_object_stays_bare() -> None:
    """Optimizers / loaders / loggers have no kind to enforce, so they interchange."""

    @configurable(role="logger")
    class SomeLogger:
        pass

    assert _object_socket_for_class(SomeLogger) == OBJECT_TYPE


def test_a_container_output_is_still_a_json_string() -> None:
    """The container branch runs BEFORE role inference, so ``computed_metrics`` stays STRING."""

    @configurable(task="t", role="trainer", framework="torch")
    class Trainer:
        @property
        @output
        def computed_metrics(self) -> Dict[str, float]:  # pragma: no cover - introspected only
            return {}

    assert _output_socket_type("computed_metrics", Dict[str, float], Trainer) == "STRING"


def test_a_node_without_object_sockets_needs_no_validator() -> None:
    """The validator is installed only where a rule can apply — it replaces ComfyUI's own check."""

    @configurable(category="op")
    class PlainOp:
        def __init__(self, scale: float = 1.0) -> None:
            self.scale = scale

        def __call__(self, record: Optional[dict] = None) -> Optional[dict]:  # pragma: no cover
            return record

    node = create_comfy_node(PlainOp, "Taidal_TestPlainOp")
    assert not hasattr(node, "VALIDATE_INPUTS")
