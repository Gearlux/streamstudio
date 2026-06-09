"""Dynamic runtime generation of ComfyUI nodes from DataFlux / Waivefront / HF callables.

Discovered callables are classified into three node shapes so a real pipeline can be
wired on the ComfyUI canvas — instead of every socket collapsing to ``STRING``:

* **Source** — a class whose ``__getitem__`` / ``__iter__`` yields a
  :class:`dataflux.sample.Sample` (e.g. ``HuggingFaceSource``). The node exposes the
  constructor config as widgets and outputs a ``DATAFLUX_SOURCE`` (a
  :class:`dataflux.core.Flux` wrapping the constructed source). The engines / view-sources
  (``DatasetSplit`` / ``Flux`` / ``JointFlux``) are the same shape, but their source-typed
  constructor params ride WIRED sockets instead of widgets — a single ``source`` →
  ``DATAFLUX_SOURCE`` input, a source-list (``fluxes``) → dynamic ``source_N`` inputs, an op-list
  (``ops``) → dynamic ``op_N`` ``DATAFLUX_OP`` inputs — so they compose on the canvas from other
  Source/Op nodes (which already output themselves as ``DATAFLUX_SOURCE`` / ``DATAFLUX_OP``).
* **Op** — a ``@configurable`` class with ``__call__(self, sample: Sample) ->
  Optional[Sample]`` (e.g. ``RescaleOp``, ``ConvertToImageOp``). The node is **dual-mode**:
  constructor config as widgets, an OPTIONAL ``DATAFLUX_SAMPLE`` input, and TWO outputs —
  ``sample`` (the op applied to the input sample, for linear chaining) and ``op`` (the constructed
  op as a ``DATAFLUX_OP`` value, so it can feed a higher-order op). A higher-order op whose
  constructor takes an op-list (``Enable`` / ``Parallel`` / ``Tee``) gets dynamic ``DATAFLUX_OP``
  inputs (``op_0`` …) instead of a STRING widget.
* **Generic** — any other callable (plain value functions): scalar widgets with a ``STRING``
  fallback (the original behaviour, preserved).

The custom socket type strings ``DATAFLUX_SOURCE`` / ``DATAFLUX_SAMPLE`` / ``DATAFLUX_OP`` carry the
live Python objects (``Flux`` / ``Sample`` / a constructed op) between nodes — ComfyUI connects two
sockets iff their type strings are equal — which is what lets ops chain (via ``DATAFLUX_SAMPLE``) and
compose into higher-order ops (via ``DATAFLUX_OP``).
"""

import collections.abc as cabc
import importlib
import inspect
import json
import os
import time
import types as _types
from typing import (
    AbstractSet,
    Annotated,
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Tuple,
    Type,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

import datasets
from confluid import mandatory_param_names, output_specs, parse_param_docs
from dataflux.discovery import get_callable_path, resolve_callable, scan_module
from dataflux.typespec import infer_sample_type
from logflow import get_logger

logger = get_logger(__name__)


def _spec_to_dict(cls: Any, attr: str) -> Optional[Dict[str, Any]]:
    """JSON-serialize a callable's declared ``ACCEPTS`` / ``PRODUCES`` (:class:`SampleType`), or
    ``None`` when undeclared — the form the ``/taidal/node_specs`` route and the JS validator consume.
    """
    spec = getattr(cls, attr, None)
    to_dict = getattr(spec, "to_dict", None)
    return to_dict() if callable(to_dict) else None


# Custom ComfyUI socket types for live DataFlux objects passed between nodes.
SAMPLE_TYPE = "DATAFLUX_SAMPLE"
SOURCE_TYPE = "DATAFLUX_SOURCE"
# A constructed op instance (a callable ``Sample -> Optional[Sample]``) ridden as a wire, so an op
# node's ``op`` output can feed a higher-order op (``Enable`` / ``Parallel`` / ``Tee``) that takes
# ops as constructor input. Distinct from ``DATAFLUX_SAMPLE`` (data) — this carries the op itself.
OP_TYPE = "DATAFLUX_OP"
# A built "runnable" (a trainer / evaluator / processor — any class implementing ``run(self)``),
# output by a runnable node and consumed by the ``Run`` node which calls ``.run()``.
RUNNABLE_TYPE = "DATAFLUX_RUNNABLE"
# A built configurable MEMBER object (model / loss / optimizer / loader / lightning / metrics) — or a
# deferred ``LazyClass`` of one (when the member class is tagged ``lazy``) — ridden as a wire so an
# object node feeds a runnable's member slot. Distinct from SOURCE (datasets ride DATAFLUX_SOURCE).
OBJECT_TYPE = "DATAFLUX_OBJECT"

# Mapping Python types to ComfyUI widget types.
TYPE_MAP = {
    int: "INT",
    float: "FLOAT",
    str: "STRING",
    bool: "BOOLEAN",
    datasets.Dataset: "HF_DATASET",
}

DEFAULT_WIDGET_CONFIG: Dict[str, Dict[str, Any]] = {
    "INT": {"default": 0},
    "FLOAT": {"default": 0.0, "step": 0.01},
    "STRING": {"default": "", "multiline": False},
    "BOOLEAN": {"default": False},
}

# Types that ride a wire as input sockets (no inline widget config).
_SOCKET_TYPES = {
    SAMPLE_TYPE,
    SOURCE_TYPE,
    OP_TYPE,
    RUNNABLE_TYPE,
    OBJECT_TYPE,
    "HF_DATASET",
}


def _object_socket(category: Optional[str]) -> str:
    """The object-member socket type for a class's ``@configurable`` category.

    Object members (model / loss / metric) ride a CATEGORY-QUALIFIED socket —
    ``DATAFLUX_OBJECT:classification_model`` — so ComfyUI's string-equality
    connection rule enforces compatibility (a ``classification_model`` node docks
    only into a ``classification_model`` slot, never a loss/segmentation slot).
    Category-less members (the role-less ``lazy`` infra members optimizer/loader/
    lightning, and task-agnostic loggers) keep the bare ``DATAFLUX_OBJECT`` type.
    The ``Retag`` adapter node (``nodes.py``) is the deliberate escape hatch that
    re-types an object to a chosen category.
    """
    return f"{OBJECT_TYPE}:{category}" if category else OBJECT_TYPE


def _base_socket_type(socket_type: Any) -> Any:
    """Strip a ``:category`` suffix to the base socket type (``DATAFLUX_OBJECT``).

    Object sockets are category-qualified (see :func:`_object_socket`); consumers
    that only need to know "is this a wired socket vs a widget" (graphio's
    socket-vs-widget split) test the BASE type, not the qualified string.
    """
    if isinstance(socket_type, str) and ":" in socket_type:
        return socket_type.split(":", 1)[0]
    return socket_type


def _is_socket_type(socket_type: Any) -> bool:
    """True if ``socket_type`` is one of the live-object wire types (base-aware)."""
    return _base_socket_type(socket_type) in _SOCKET_TYPES


# A runnable's constructor params / body slots that hold a DATASET / SOURCE — rendered as a wired
# ``DATAFLUX_SOURCE`` socket (fed by a Source node) instead of a STRING widget. ``source`` (handled by
# ``_SOURCE_PARAM`` too) covers ``DatasetProcessor``. Detected by name (same convention as the op/
# source-list params); a runnable's other object members ride ``DATAFLUX_OBJECT`` sockets.
_DATASET_MEMBER_PARAMS = {"train_set", "val_set", "test_set", "dataset"}

# Discovery ROLES whose classes become OBJECT nodes (a trainer/runnable slot-filler producing a
# ``DATAFLUX_OBJECT`` wire): a task-scoped model / loss / metric, plus ``logger`` (a Lightning logger
# fed into the ``pytorch_lightning.Trainer`` ``logger`` slot). Datasets are SOURCE nodes (role
# ``"dataset"`` is routed to ``_build_source_node``), not object nodes. The role-less runtime members
# (``torch.optim.*`` / ``DataLoader`` / ``pytorch_lightning.Trainer``, registered in
# ``marainer.configurables``) are detected instead by the confluid **``lazy`` mark**
# (``__confluid_lazy__``) — deliberately NOT a category, so they don't leak into navigaitor's
# form-spec slot pickers (which resolve a picker FROM the slot's default-target category and would
# then offer the raw ``params``-taking optimizer). Loggers carry ``role="logger"`` (NOT ``lazy``) so
# they build EAGERLY — a logger's required ``save_dir`` must stay a configurable widget, not be
# skipped as a runtime-injected slot. See ``_is_object_member_class``.
_MEMBER_ROLES = {"model", "loss", "metric", "logger"}

# A runnable's object-member PARAM NAME → the ROLE its slot accepts. The slot socket is then
# ROLE-qualified (``_object_socket(role)`` → ``DATAFLUX_OBJECT:model``) — TASK-AGNOSTIC: a
# classification model and a segmentation model BOTH produce ``DATAFLUX_OBJECT:model`` and dock
# into ANY trainer's ``model`` slot. The role still guards kind (a loss can't dock into a model
# slot). Task survives only as GUIDANCE (navigaitor slot ranking + the FluxStudio palette
# label/grouping), never on the socket. Mirrors navigaitor's ``configs.generate.PARAM_NAME_TO_ROLE``
# (fluxstudio must NOT import navigaitor — the convention is small and stable). ``logger`` is
# deliberately ABSENT, as are the infra members (optimizer/loader/lightning): those stay generic
# ``DATAFLUX_OBJECT`` (no meaningful role-kind distinction to enforce).
_STRICT_SLOT_ROLES = {
    "model": "model",
    "loss_fn": "loss",
    "metrics": "metric",
    "train_metrics": "metric",
    "val_metrics": "metric",
    "test_metrics": "metric",
}

# The roles whose object sockets are ROLE-qualified (``DATAFLUX_OBJECT:<role>``). Everything else
# (logger / optimizer / loader / lightning, and untagged objects) rides the bare ``DATAFLUX_OBJECT``.
# This is the value-set of :data:`_STRICT_SLOT_ROLES`, kept as a name so the object-NODE output side
# and the runnable-SLOT input side agree on which roles qualify (else a socket mismatch silently
# refuses an otherwise-valid wire — the original "can't connect" footgun).
_QUALIFIED_OBJECT_ROLES = frozenset(_STRICT_SLOT_ROLES.values())

# Type → role inference, so a role can be ASSIGNED from a class's Python type when it carries no
# explicit ``@configurable(role=…)`` tag. Matched by a base-class qualname substring (a duck-typed
# MRO scan — NO heavy ``torch`` / ``torchmetrics`` / ``pytorch_lightning`` import at module load).
# NOTE model vs loss are BOTH ``torch.nn.Module`` so the TYPE cannot tell them apart — only the
# explicit ``role=`` tag can (it always wins over inference). So model/loss are deliberately absent.
_TYPE_ROLE_BY_BASE = (
    ("Metric", "metric"),  # torchmetrics.metric.Metric
    ("Optimizer", "optimizer"),  # torch.optim.optimizer.Optimizer
    ("DataLoader", "loader"),  # torch.utils.data.DataLoader
    ("Logger", "logger"),  # pytorch_lightning.loggers.logger.Logger
)


def _role_for_class(cls: Any) -> Optional[str]:
    """The object role of ``cls`` — explicit ``@configurable(role=…)`` tag, else inferred from type.

    The explicit ``__confluid_role__`` tag ALWAYS wins (it is the only thing that can disambiguate a
    model from a loss — both are ``nn.Module``). When a class is untagged, infer the role from its
    Python type (a base-class qualname substring scan via :data:`_TYPE_ROLE_BY_BASE`), so e.g. an
    untagged ``torchmetrics`` metric still sockets as a ``metric``. Returns ``None`` when neither a
    tag nor a recognised base applies → the object rides the generic ``DATAFLUX_OBJECT``.
    """
    role = getattr(cls, "__confluid_role__", None)
    if role:
        return str(role)
    try:
        mro_names = " ".join(getattr(base, "__qualname__", base.__name__) for base in inspect.getmro(cls))
    except (AttributeError, TypeError):  # pragma: no cover - defensive: non-class callables
        return None
    for needle, inferred in _TYPE_ROLE_BY_BASE:
        if needle in mro_names:
            return inferred
    return None


def _object_socket_for_class(cls: Any) -> str:
    """The object-output socket type for a class: ROLE-qualified for a strict role, else generic.

    ``DATAFLUX_OBJECT:model`` for a model / loss / metric (task-agnostic — see
    :data:`_QUALIFIED_OBJECT_ROLES`), bare ``DATAFLUX_OBJECT`` for everything else (logger / infra /
    untagged). Mirrors the runnable-SLOT side so an object node docks into the matching slot.
    """
    role = _role_for_class(cls)
    return _object_socket(role) if role in _QUALIFIED_OBJECT_ROLES else OBJECT_TYPE


def _infer_member_role(name: str) -> Optional[str]:
    """The role (model / loss / metric) a member NAME implies, else ``None``.

    Exact INPUT param names resolve via :data:`_STRICT_SLOT_ROLES`; a runnable ``@output`` name
    (e.g. ``trained_model``) is freer, so fall back to a keyword scan — this keeps a trainer's
    ``trained_model`` output ROLE-qualified (``DATAFLUX_OBJECT:model``, task-agnostic) so it docks
    into a downstream evaluator's likewise-qualified ``model`` slot. Returns ``None`` for role-less
    members (optimizer / loader / lightning / logger) → generic ``DATAFLUX_OBJECT``.
    """
    if name in _STRICT_SLOT_ROLES:
        return _STRICT_SLOT_ROLES[name]
    for keyword, role in (("model", "model"), ("loss", "loss"), ("metric", "metric")):
        if keyword in name:
            return role
    return None


# Constructor params (on higher-order ops) that hold a LIST OF OPS — rendered as dynamic
# ``DATAFLUX_OP`` multi-inputs (``op_0``, ``op_1``, … via ``web/op_list.js``) instead of a STRING
# widget, so concrete op nodes wire into them. ``Enable``/``Parallel`` take ``ops``; ``Tee`` takes
# ``branches`` (a list of op-lists — v1 wraps each connected op as its own single-op branch).
_OP_LIST_PARAMS = {"ops", "branches"}

# Constructor params (on source/engine classes) that hold ANOTHER source — rendered as a single
# wired ``DATAFLUX_SOURCE`` input socket instead of a STRING widget, so a Source/engine node's output
# feeds them. ``DatasetSplit(source=…)`` and ``Flux(source=…)`` use ``source``. Detected by exact
# name (the same name-convention as ``_OP_LIST_PARAMS``); safe because no concrete leaf source
# (``HuggingFaceSource``/``RFUAVSource``/``RegionsJsonSource``) has a param literally named ``source``.
_SOURCE_PARAM = "source"

# Constructor params that hold a LIST OF SOURCES — rendered as dynamic ``DATAFLUX_SOURCE`` multi-inputs
# (``source_0``, ``source_1``, … via ``web/source_list.js``), the source analogue of ``_OP_LIST_PARAMS``.
# ``JointFlux(fluxes: List[Flux])`` uses ``fluxes``.
_SOURCE_LIST_PARAMS = {"fluxes", "sources"}

# Constructor params that name a HuggingFace dataset *identifier* (a hub repo id
# like ``mnist`` / ``kitofrank/RFUAV``, or optionally a local imagefolder path) —
# NOT a filesystem directory. Recognised only on HuggingFace dataset sources (see
# ``_is_hf_dataset_id_param``); ``dataflux.sources.HuggingFaceSource`` calls it
# ``path`` (the upstream ``datasets.load_dataset`` arg name) while sonair's
# ``HuggingFaceClassificationDataset`` calls it ``repo_id``.
_HF_DATASET_ID_PARAMS = {"path", "repo_id"}

# Constructor params (on HuggingFace dataset sources) that name a dataset *feature/column* to map
# onto a Sample field — ``HuggingFaceSource.input_feature`` / ``target_feature``. Rendered as a
# free-text STRING (so any column name still types in) tagged ``taidal_widget="HF_FEATURE"``, which
# ``web/hf_feature_chooser.js`` augments with a dropdown of the SELECTED dataset's actual columns
# (fetched from ``/taidal/hf_dataset_features`` for the node's current dataset-id field). Recognised
# only on ``HuggingFace*`` sources (see ``_is_hf_feature_param``).
_HF_FEATURE_PARAMS = {"input_feature", "target_feature"}

# Constructor param (on HuggingFace dataset sources) that names the dataset *split* —
# ``HuggingFaceSource.split``. Kept a free-text STRING (HF supports slice syntax like
# ``train[:90%]`` / ``train+test``) tagged ``taidal_widget="HF_SPLIT"``, which
# ``web/hf_split_chooser.js`` augments with a dropdown of the SELECTED dataset's actual splits
# (fetched from ``/taidal/hf_dataset_splits``). Recognised only on ``HuggingFace*`` sources (see
# ``_is_hf_split_param``) — so ``DatasetSplit.split`` (a closed ``Literal["train","val","test"]``
# fraction-view selector, a DIFFERENT concept, already a combo via ``_literal_options``) is untouched.
_HF_SPLIT_PARAM = "split"

# Constructor param (on HuggingFace dataset sources) holding the LIST of metadata columns —
# ``HuggingFaceSource.metadata_features``. Kept a free-text (comma-separated) STRING tagged
# ``taidal_widget="HF_METADATA"``; ``web/hf_metadata_chooser.js`` augments it with a MULTI-select
# "add column" combo offering ``*`` (= every column except input/target — the ``METADATA_ALL_FEATURES``
# sentinel resolved by ``dataflux.sources``) plus the SELECTED dataset's columns. Recognised only on
# ``HuggingFace*`` sources (see ``_is_hf_metadata_param``).
_HF_METADATA_PARAM = "metadata_features"

# FluxStudio surfaces a node ONLY when its ``@configurable`` discovery category is one of these
# (a POSITIVE allowlist — see dataflux/confluid taxonomy). This is what a canvas can build:
#   * ``op``      — concrete Sample→Sample ops (``RescaleOp``, ``SpectrogramOp`` …).
#   * ``source``  — concrete data sources (``HuggingFaceSource``, ``RFUAVSource`` …) AND
#                   ``DatasetSplit`` (a source-view: it yields Samples and wraps another source).
#   * ``engine``  — the generic composition primitives ``Flux`` / ``JointFlux``. They ARE buildable
#                   on a canvas: their source-typed constructor params render as WIRED sockets
#                   (``_build_source_node``) — ``Flux.source`` (single ``DATAFLUX_SOURCE`` input) +
#                   ``Flux.ops`` (dynamic ``op_N`` ``DATAFLUX_OP`` inputs); ``JointFlux.fluxes``
#                   (dynamic ``source_N`` ``DATAFLUX_SOURCE`` inputs). So a Source/Op node's output
#                   (it already exposes itself as ``DATAFLUX_SOURCE`` / ``DATAFLUX_OP``) feeds them.
# Everything else is excluded by construction:
#   * the raw-callable op wrappers ``FilterOp`` / ``WrappedOp`` — bare ``@configurable``, so
#     UNCATEGORISED (neither ``op`` nor ``engine``): their constructor takes a raw Python callable,
#     nothing a widget can supply. Excluded as uncategorised, so even with ``"engine"`` allowed they
#     stay out (correct — they're not buildable).
#   * task ``dataset``s (``classification_dataset`` …) — a trainer/model-config concept (a
#     task-scoped, model-ready dataset slot), meaningful for navigaitor's config composition but
#     NOT for a per-sample DataFlux canvas, which only needs a ``source`` to yield Samples.
#   * uncategorised module-level helper functions and raw sinks (no ``__confluid_category__``).
# To surface another role (e.g. ``"sink"``) add it here AND tag the classes.
_NODE_CATEGORIES = {"op", "source", "engine"}


# The CLI's ``--no-default-nodes`` flag exports this so the fluxstudio import inside ComfyUI knows
# to (a) prune ComfyUI's built-in nodes from the palette and (b) surface the Taidal nodes at the
# palette root instead of nested under a ``Taidal/`` folder. Set explicitly to ``"0"`` by
# ``--default-nodes`` so the flag is authoritative over any ambient shell export.
_HIDE_DEFAULT_NODES_ENV = "FLUXSTUDIO_HIDE_DEFAULT_NODES"
_TRUTHY = {"1", "true", "yes", "on"}


def hide_default_comfy_nodes() -> bool:
    """True when the CLI asked (via ``--no-default-nodes``) to hide ComfyUI's built-in nodes.

    Read from the :data:`_HIDE_DEFAULT_NODES_ENV` environment variable that ``fluxstudio.cli``
    exports before launching ComfyUI (the launched ComfyUI process is a subprocess, so an env var
    is the channel). Drives both the default-node prune (``fluxstudio.__init__``) and the palette
    root (:func:`node_root`).
    """
    return os.environ.get(_HIDE_DEFAULT_NODES_ENV, "").strip().lower() in _TRUTHY


def node_root() -> str:
    """The Taidal palette root prefix: ``""`` (root-level) when default nodes are hidden, else ``"Taidal"``.

    When ComfyUI's own nodes are hidden there is nothing to disambiguate the Taidal nodes from, so
    they read more naturally at the palette root (``DataFlux/Op`` rather than ``Taidal/DataFlux/Op``).
    When the defaults are shown the ``Taidal/`` folder keeps the workspace nodes grouped together.
    """
    return "" if hide_default_comfy_nodes() else "Taidal"


def _is_hf_dataset_id_param(param_name: str, owner: Optional[Callable]) -> bool:
    """True when ``param_name`` is the dataset-id arg of a HuggingFace dataset source.

    Gated on the ``HuggingFace`` class-name convention (a naming rule, not a
    per-node special-case — it matches ``HuggingFaceSource``,
    ``HuggingFaceClassificationDataset``, and any future ``HuggingFace*`` source)
    so a plain ``path`` param on a non-HF source still resolves to the directory
    picker via ``_map_type``.
    """
    return (
        param_name in _HF_DATASET_ID_PARAMS
        and owner is not None
        and inspect.isclass(owner)
        and owner.__name__.startswith("HuggingFace")
    )


def _is_hf_feature_param(param_name: str, owner: Optional[Callable]) -> bool:
    """True when ``param_name`` is a feature-selector arg of a HuggingFace dataset source.

    These (``input_feature`` / ``target_feature``) name a dataset *column* mapped onto a
    ``Sample`` field. Gated on the SAME ``HuggingFace`` class-name convention as
    :func:`_is_hf_dataset_id_param` (a naming rule, not a per-node special-case) so a
    like-named param on a non-HF class is unaffected and stays a plain STRING widget.
    """
    return (
        param_name in _HF_FEATURE_PARAMS
        and owner is not None
        and inspect.isclass(owner)
        and owner.__name__.startswith("HuggingFace")
    )


def _is_hf_split_param(param_name: str, owner: Optional[Callable]) -> bool:
    """True when ``param_name`` is the ``split`` arg of a HuggingFace dataset source.

    Gated on the SAME ``HuggingFace`` class-name convention as :func:`_is_hf_dataset_id_param`
    so ``DatasetSplit.split`` (a closed ``Literal`` fraction-view selector on a NON-HuggingFace
    class — a different concept, already a combo via :func:`_literal_options`) is unaffected.
    """
    return (
        param_name == _HF_SPLIT_PARAM
        and owner is not None
        and inspect.isclass(owner)
        and owner.__name__.startswith("HuggingFace")
    )


def _is_hf_metadata_param(param_name: str, owner: Optional[Callable]) -> bool:
    """True when ``param_name`` is the ``metadata_features`` list arg of a HuggingFace source.

    Gated on the SAME ``HuggingFace`` class-name convention as :func:`_is_hf_dataset_id_param`.
    Kept a comma-separated STRING (``_coerce_kwargs`` splits it into the ``List[str]`` the
    constructor wants); the multi-select column picker (``web/hf_metadata_chooser.js``) only
    appends to it.
    """
    return (
        param_name == _HF_METADATA_PARAM
        and owner is not None
        and inspect.isclass(owner)
        and owner.__name__.startswith("HuggingFace")
    )


def _literal_options(annotation: Any) -> Optional[List[Any]]:
    """The members of a ``typing.Literal[...]`` annotation, or ``None`` if it is not a Literal.

    Also unwraps ``Optional[Literal[...]]`` / ``Literal[...] | None``. The members back a ComfyUI
    combo dropdown so a closed-``Literal`` constructor param (the workspace's preferred way to type a
    fixed, short value set — e.g. waivefront's ``Colormap``) is offered as an enumeration in the GUI,
    enumerated straight from the annotation via ``get_args`` (never a hand-maintained parallel list).
    """
    if get_origin(annotation) is Literal:
        return list(get_args(annotation))
    if get_origin(annotation) is Union:  # Optional[Literal[...]] / Literal[...] | None
        for arg in get_args(annotation):
            if get_origin(arg) is Literal:
                return list(get_args(arg))
    return None


def _map_type(param_name: str, annotation: Any) -> str:
    """Map a Python type annotation to a ComfyUI socket/widget type string."""
    if annotation is datasets.Dataset:
        return "HF_DATASET"

    str_ann = str(annotation)
    if "Sample" in str_ann:
        return SAMPLE_TYPE

    # Heuristic for directory/file paths -> directory-picker widget (web/directory_chooser.js).
    if "pathlib.Path" in str_ann or any(x in param_name.lower() for x in ("path", "dir", "root", "folder")):
        return "PATH"

    if annotation in TYPE_MAP:
        return TYPE_MAP[annotation]

    # Unwrap Optional/Union by scanning the annotation string for a scalar type, so numeric/bool
    # config still gets a real typed widget (not a STRING the constructor would choke on, e.g.
    # ``count: Optional[int]``).
    low = str_ann.lower()
    if "bool" in low:
        return "BOOLEAN"
    if "int" in low and "float" not in low:
        return "INT"
    if "float" in low:
        return "FLOAT"
    if "datasets.arrow_dataset.Dataset" in str_ann or "datasets.Dataset" in str_ann:
        return "HF_DATASET"
    return "STRING"  # Fallback


def _widget_for(param: inspect.Parameter, owner: Optional[Callable] = None) -> Tuple[Any, Dict[str, Any]]:
    """Build the ``(comfy_type, config)`` pair for a single constructor parameter.

    ``owner`` is the callable the parameter belongs to — used to recognise a
    HuggingFace dataset-id param (rendered as a free-text field with an optional
    cached-dataset picker) so it does NOT fall into the directory-picker branch.
    """
    has_default = param.default is not inspect.Parameter.empty

    # HuggingFace dataset identifier: a free-text STRING (a hub repo id, or a local
    # imagefolder path) tagged for the cached-dataset picker (web/hf_dataset_chooser.js)
    # — NOT a directory picker, even though the param is named ``path``.
    if _is_hf_dataset_id_param(param.name, owner):
        default = param.default if has_default and isinstance(param.default, str) else ""
        return "STRING", {"default": default, "taidal_widget": "HF_DATASET_ID"}

    # HuggingFace feature/column selector (input_feature / target_feature): a free-text STRING
    # (any column name still types in) tagged for the column picker (web/hf_feature_chooser.js),
    # which fills it from the SELECTED dataset's actual feature columns.
    if _is_hf_feature_param(param.name, owner):
        default = param.default if has_default and isinstance(param.default, str) else ""
        return "STRING", {"default": default, "taidal_widget": "HF_FEATURE"}

    # HuggingFace split: a free-text STRING (kept editable for HF slice syntax like ``train[:90%]``)
    # tagged for the split picker (web/hf_split_chooser.js), which fills it from the SELECTED
    # dataset's actual splits. Resolved before the Literal/_map_type heuristics; ``split`` is a
    # plain ``str`` on HuggingFaceSource so this only adds the picker hint.
    if _is_hf_split_param(param.name, owner):
        default = param.default if has_default and isinstance(param.default, str) else ""
        return "STRING", {"default": default, "taidal_widget": "HF_SPLIT"}

    # HuggingFace metadata_features (a List[str]): a comma-separated STRING (empty -> None -> no
    # extra metadata; _coerce_kwargs splits it into a list) tagged for the multi-select column
    # picker (web/hf_metadata_chooser.js), which appends ``*`` (= the rest) or selected columns.
    if _is_hf_metadata_param(param.name, owner):
        return "STRING", {"default": "", "taidal_widget": "HF_METADATA"}

    # A closed ``Literal[...]`` → a ComfyUI combo dropdown whose options ARE the literal's members.
    # ComfyUI represents a combo as ``(options_list, {...})``, so the LIST itself is the "type". An
    # explicit enum is a stronger signal than the name-based PATH / scalar heuristics in ``_map_type``,
    # so it is resolved first. The default is the param default when it is a member, else the first option.
    literal_opts = _literal_options(param.annotation)
    if literal_opts is not None:
        options = [str(o) for o in literal_opts]
        if has_default and param.default is None:
            # An OPTIONAL Literal that defaults to ``None`` (e.g. ``pl.Trainer.precision``): the combo
            # can't BE None, so do NOT force the first member — that silently overrides the constructor
            # default (the precision=64 → float64-on-MPS footgun). Offer a leading blank "(use default)"
            # entry, selected by default; ``_coerce_kwargs`` omits ``""`` so ``None`` (→ the library's
            # own default) applies. The user can still pick a concrete value from the dropdown.
            return [""] + options, {"default": ""}
        default = str(param.default) if has_default and param.default is not None else ""
        if default not in options:  # no usable default (or an out-of-set one) → first option
            default = options[0] if options else ""
        return options, {"default": default}

    comfy_type = _map_type(param.name, param.annotation)

    if comfy_type in _SOCKET_TYPES:
        return comfy_type, {}
    if comfy_type == "PATH":
        default = param.default if has_default and isinstance(param.default, str) else ""
        return "STRING", {"default": default, "taidal_widget": "DIRECTORY"}

    # Nullable numeric (e.g. ``Optional[int] = None`` like ``HuggingFaceSource.count``):
    # ComfyUI's INT/FLOAT widgets can't represent None, and their 0 default is a distinct,
    # often-breaking value (``count=0`` means "no samples"). Use a blank STRING that
    # _coerce_kwargs turns back into None when empty, or casts to the number when filled.
    if param.default is None and comfy_type in ("INT", "FLOAT"):
        return "STRING", {"default": "", "multiline": False}

    # Nullable bool (e.g. ``Optional[bool] = None`` like ``pl.Trainer.enable_progress_bar`` /
    # ``enable_checkpointing`` / ``enable_model_summary``): a ComfyUI BOOLEAN checkbox can't BE None, and
    # its ``False`` default would silently OVERRIDE the constructor's ``None`` — which the library
    # resolves to ITS own default (Lightning enables the progress bar / checkpointing by default). Render
    # a 3-state combo whose leading blank = "use the constructor default"; ``_coerce_kwargs`` maps blank
    # → omit (None applies) and ``true``/``false`` → the real bool. Same principle as nullable numerics.
    if param.default is None and comfy_type == "BOOLEAN":
        return ["", "true", "false"], {"default": ""}

    config = DEFAULT_WIDGET_CONFIG.get(comfy_type, {}).copy()
    if has_default and param.default is not None:
        if comfy_type == "INT" and isinstance(param.default, int):
            config["default"] = param.default
            if param.default < 0:
                config["min"] = -(2**53)
        elif comfy_type == "FLOAT" and isinstance(param.default, (int, float)):
            config["default"] = float(param.default)
            if param.default < 0:
                config["min"] = -1e308
        elif comfy_type == "BOOLEAN" and isinstance(param.default, bool):
            config["default"] = param.default
        elif comfy_type == "STRING":
            config["default"] = str(param.default)
    return comfy_type, config


def _build_config_inputs(func: Callable, skip: Optional[AbstractSet[str]] = None) -> Dict[str, Any]:
    """Build the ``required`` inputs dict (constructor config widgets) for a callable.

    Each widget's ``tooltip`` is sourced from the callable's docstring ``Args:``
    block (via :func:`confluid.parse_param_docs`) — the same per-parameter help
    navigaitor surfaces in its form-spec. Document a parameter once in the class
    docstring and it appears as a hover tooltip here; an undocumented parameter
    simply gets no tooltip (fix it upstream, never hand-write one here).

    ``skip`` names parameters to omit from the widget set — used by op nodes to
    exclude the op-list param (it becomes a ``DATAFLUX_OP`` socket, not a widget).
    """
    try:
        sig = inspect.signature(func)
    except (
        ValueError,
        TypeError,
    ):  # pragma: no cover - defensive: inspect rejects rare builtins
        return {}
    param_docs = parse_param_docs(func)
    required: Dict[str, Any] = {}
    for name, param in sig.parameters.items():
        if skip and name in skip:
            continue
        if name in ("self", "cls"):
            continue
        if param.kind in (
            inspect.Parameter.VAR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        ):
            continue
        comfy_type, config = _widget_for(param, owner=func)
        doc = param_docs.get(name)
        if doc:
            config = {**config, "tooltip": doc}
        required[name] = (comfy_type,) if not config else (comfy_type, config)
    return required


def _coerce_kwargs(func: Callable, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce ComfyUI widget values into constructor-ready Python values.

    Concrete numerics/bools already arrive correctly typed from INT/FLOAT/BOOLEAN widgets;
    this handles STRING widgets standing in for ``Optional[...]`` / ``List[...]`` params:
    ``""`` / ``"None"`` -> ``None`` (or fall back to the constructor default); a
    comma-separated string -> ``list`` for ``List`` / ``Sequence``-typed params; and a
    filled numeric string -> ``int`` / ``float`` for nullable-numeric params (rendered as
    blank STRING widgets so they can express None).
    """
    try:
        sig = inspect.signature(func)
    except (
        ValueError,
        TypeError,
    ):  # pragma: no cover - defensive: inspect rejects rare builtins
        return dict(kwargs)
    out: Dict[str, Any] = {}
    for key, value in kwargs.items():
        param = sig.parameters.get(key)
        # A Literal/enum value (from a combo dropdown) is already the exact constructor value — pass it
        # through verbatim, never through the STRING heuristics below (a member that happens to contain
        # "int" / "float" / "list", e.g. a future colormap name, must not be cast or comma-split).
        if param is not None and _literal_options(param.annotation) is not None:
            # The blank "(use default)" entry of an Optional Literal (default ``None``) → OMIT the kwarg
            # so the constructor's own default applies (e.g. omit ``precision`` → Lightning's 32-bit
            # default, never a forced ``precision=64`` that crashes float64-less backends like MPS).
            if value == "" and param.default is None:
                continue
            out[key] = value
            continue
        if isinstance(value, str):
            stripped = value.strip()
            ann = str(param.annotation) if param is not None else ""
            if stripped in ("", "None"):
                if param is not None and param.default is None:
                    out[key] = None
                elif param is not None and param.default is inspect.Parameter.empty:
                    out[key] = value  # required param — pass the literal through
                # else: optional with a concrete default -> omit so the default applies
                continue
            ann_low = ann.lower()
            if "list" in ann_low or "sequence" in ann_low:
                out[key] = [item.strip() for item in stripped.split(",") if item.strip()]
                continue
            # Cast a filled nullable-bool 3-state combo value (``true``/``false``) back to a real bool.
            if "bool" in ann_low:
                out[key] = stripped.lower() in ("true", "1", "yes", "on")
                continue
            # Cast a filled nullable-numeric STRING widget back to its number.
            if "int" in ann_low and "float" not in ann_low:
                out[key] = int(stripped)
                continue
            if "float" in ann_low:
                out[key] = float(stripped)
                continue
        out[key] = value
    return out


def _is_sample_annotation(annotation: Any) -> bool:
    """True if an annotation refers to a DataFlux ``Sample`` (handles ``Iterator[Sample]`` etc.)."""
    return "Sample" in str(annotation)


def _op_call_signature(obj: Any) -> Optional[inspect.Signature]:
    """Return ``obj.__call__``'s signature iff ``obj`` is a class whose call takes a ``Sample``."""
    if not inspect.isclass(obj) or "__call__" not in vars(obj):
        return None
    try:
        sig = inspect.signature(obj.__call__)
    except (
        ValueError,
        TypeError,
    ):  # pragma: no cover - defensive: inspect rejects rare callables
        return None
    params = [p for n, p in sig.parameters.items() if n not in ("self", "cls")]
    if params and _is_sample_annotation(params[0].annotation):
        return sig
    return None


def _is_source(obj: Any) -> bool:
    """True if ``obj`` is a class whose indexing/iteration yields ``Sample`` objects."""
    if not inspect.isclass(obj):
        return False
    for meth_name in ("__getitem__", "__iter__"):
        meth = getattr(obj, meth_name, None)
        if meth is None:
            continue
        try:
            ret = inspect.signature(meth).return_annotation
        except (
            ValueError,
            TypeError,
        ):  # pragma: no cover - defensive: inspect rejects rare callables
            continue
        if _is_sample_annotation(ret):
            return True
    return bool(obj.__name__.endswith("Source"))


def _is_runnable(obj: Any) -> bool:
    """True if ``obj`` is a class implementing the run interface — a no-arg ``run(self)`` method.

    This is the structural signal (mirroring :func:`_is_source` / :func:`_op_call_signature`) that a
    class is a trainer / evaluator / processor: an orchestration entry point, not a per-sample op or a
    Sample source. Such classes become buildable runnable nodes (members as wired sockets) regardless
    of their ``@configurable`` category (e.g. ``classification_trainer``).
    """
    if not inspect.isclass(obj):
        return False
    run = getattr(obj, "run", None)
    if not callable(run):
        return False
    try:
        params = [n for n in inspect.signature(run).parameters if n not in ("self", "cls")]
    except (ValueError, TypeError):  # pragma: no cover - defensive
        return False
    # The run interface is parameterless (everything is configured beforehand).
    return len(params) == 0


def _is_dataset_member_class(obj: Any) -> bool:
    """True if ``obj`` is a task ``role="dataset"`` class (a model-ready dataset Source).

    Task datasets are normally NOT canvas nodes, but a runnable's ``train_set`` / ``dataset`` slot
    needs a Source producer — so when a runnable is buildable we also surface these as Source nodes.
    """
    return inspect.isclass(obj) and getattr(obj, "__confluid_role__", None) == "dataset"


def _is_object_member_class(obj: Any) -> bool:
    """True if ``obj`` is a runnable MEMBER that should become a ``DATAFLUX_OBJECT`` node.

    A task-scoped model / loss / metric (``__confluid_role__`` in :data:`_MEMBER_ROLES`), OR a target
    tagged confluid ``lazy`` (``__confluid_lazy__`` — the role-less registered runtime members:
    ``torch.optim.*`` / ``DataLoader`` / ``pytorch_lightning.Trainer``). The ``lazy`` mark — not a
    category — is the signal for the role-less runtime members so they stay out of navigaitor's
    category-driven form-spec pickers.

    The target may be a class OR a plain builder/factory FUNCTION — confluid ``register`` stamps the
    same markers on a function (e.g. ``raidar.configurables`` registers the torchvision detection
    builders ``fasterrcnn_*`` with ``role="model"``), and ``_build_object_node`` introspects either
    via ``inspect.signature``. So the gate is "callable + marked", not "class + marked".
    """
    if not callable(obj):
        return False
    return getattr(obj, "__confluid_role__", None) in _MEMBER_ROLES or bool(getattr(obj, "__confluid_lazy__", False))


def _safe_callable_path(func: Callable) -> Optional[str]:
    """Importable ``module:qualname`` for a discovered callable, or ``None``.

    Used to stamp ``TAIDAL_CALLABLE_PATH`` so the ops-export compiler
    (:mod:`fluxstudio.export`) can recover the wrapped class from a generated
    node and re-emit it as a Confluid ``!class:`` marker. Anonymous callables
    (lambdas, locals — only seen in tests) have no resolvable path; return
    ``None`` rather than raise, since the path is metadata, not load-bearing
    for normal canvas execution.
    """
    try:
        return get_callable_path(func)
    except (
        TypeError,
        ValueError,
    ):  # pragma: no cover - defensive: anonymous/odd callables
        return None


def _op_list_param(cls: Any) -> Optional[str]:
    """Name of a higher-order op's op-list constructor param (``ops`` / ``branches``), or ``None``.

    Detected by name (``_OP_LIST_PARAMS``) + a ``List``/``list`` annotation — e.g. ``Enable(ops:
    list)``, ``Parallel(ops: List[Any], …)``, ``Tee(branches: List[List[Any]])``. Such a param holds
    OTHER ops, so it rides ``DATAFLUX_OP`` sockets instead of becoming a STRING widget.
    """
    try:
        sig = inspect.signature(cls)
    except (ValueError, TypeError):  # pragma: no cover - defensive
        return None
    for pname, param in sig.parameters.items():
        if pname in _OP_LIST_PARAMS and "list" in str(param.annotation).lower():
            return pname
    return None


def _collect_ops(kwargs: Dict[str, Any]) -> List[Any]:
    """Pull the dynamic ``op_0`` / ``op_1`` / … inputs out of ``kwargs``, in slot order, dropping
    unconnected (``None``) ones. The connected op nodes' ``op`` outputs arrive here as kwargs.
    """
    indexed = [(int(k[3:]), v) for k, v in kwargs.items() if k.startswith("op_") and k[3:].isdigit() and v is not None]
    return [v for _, v in sorted(indexed)]


def _collect_indexed(kwargs: Dict[str, Any], prefix: str) -> List[Any]:
    """Gather dynamic ``<prefix>_0`` / ``<prefix>_1`` / … inputs in slot order, dropping unconnected
    (``None``) ones — the generic collector behind object-list members (``logger_N`` / ``callbacks_N``).
    """
    start = len(prefix) + 1
    indexed = [
        (int(k[start:]), v)
        for k, v in kwargs.items()
        if k.startswith(prefix + "_") and k[start:].isdigit() and v is not None
    ]
    return [v for _, v in sorted(indexed)]


def _is_indexed_key(key: str, prefixes: AbstractSet[str]) -> bool:
    """True if ``key`` is a dynamic ``<prefix>_<digit>`` slot for one of ``prefixes``."""
    return any(key.startswith(p + "_") and key[len(p) + 1 :].isdigit() for p in prefixes)


def _has_param(cls: Any, name: str) -> bool:
    """True if ``cls``'s constructor has a parameter literally named ``name``."""
    try:
        sig = inspect.signature(cls)
    except (ValueError, TypeError):  # pragma: no cover - defensive
        return False
    return name in sig.parameters


def _source_param(cls: Any) -> Optional[str]:
    """Name of a single-source constructor param (``source``), or ``None``.

    Such a param holds ANOTHER source/engine, so it rides a single ``DATAFLUX_SOURCE`` input socket
    instead of becoming a STRING widget — e.g. ``DatasetSplit(source=…)`` / ``Flux(source=…)``.
    """
    return _SOURCE_PARAM if _has_param(cls, _SOURCE_PARAM) else None


def _source_list_param(cls: Any) -> Optional[str]:
    """Name of a source-list constructor param (``fluxes`` / ``sources``), or ``None``.

    Detected by name (``_SOURCE_LIST_PARAMS``) + a ``List``/``list`` annotation — e.g.
    ``JointFlux(fluxes: List[Flux])``. Such a param holds OTHER sources, so it rides dynamic
    ``DATAFLUX_SOURCE`` sockets (``source_0`` / ``source_1`` / …) instead of a STRING widget.
    """
    try:
        sig = inspect.signature(cls)
    except (ValueError, TypeError):  # pragma: no cover - defensive
        return None
    for pname, param in sig.parameters.items():
        if pname in _SOURCE_LIST_PARAMS and "list" in str(param.annotation).lower():
            return pname
    return None


def _collect_sources(kwargs: Dict[str, Any]) -> List[Any]:
    """Pull the dynamic ``source_0`` / ``source_1`` / … inputs out of ``kwargs``, in slot order,
    dropping unconnected (``None``) ones. The source nodes' ``source`` outputs arrive here as kwargs.
    """
    indexed = [
        (int(k[7:]), v) for k, v in kwargs.items() if k.startswith("source_") and k[7:].isdigit() and v is not None
    ]
    return [v for _, v in sorted(indexed)]


def _node_class(
    name: str,
    category: str,
    inputs: Dict[str, Any],
    return_types: Tuple[str, ...],
    execute: Callable,
    return_names: Optional[Tuple[str, ...]] = None,
    accepts: Optional[Dict[str, Any]] = None,
    produces: Optional[Dict[str, Any]] = None,
    kind: Optional[str] = None,
    callable_path: Optional[str] = None,
    optional_inputs: Optional[Dict[str, Any]] = None,
    output_node: bool = False,
    hidden: Optional[Dict[str, Any]] = None,
    is_random: bool = False,
) -> Type:
    """Assemble a ComfyUI node class from its parts.

    ``output_node=True`` marks the node a terminal ``OUTPUT_NODE`` (ComfyUI always executes it on
    Queue) — used by runnable nodes, which RUN on execute (:func:`_build_runnable_node`). ``hidden``
    is folded into ``INPUT_TYPES`` (e.g. ``{"unique_id": "UNIQUE_ID"}`` so a runnable node can address
    its own live-output widget). Most generated nodes pass neither.

    ``accepts`` / ``produces`` are the source callable's declared ``SampleType.to_dict()`` (or
    ``None``); they ride on the node class as ``TAIDAL_ACCEPTS`` / ``TAIDAL_PRODUCES`` so the
    ``/taidal/node_specs`` route and the canvas connection-validator can read them. They do NOT change
    the ComfyUI socket strings (every Sample wire stays ``DATAFLUX_SAMPLE`` so the pipeline still chains).

    ``kind`` (``"op"`` / ``"source"`` / ``"generic"``) and ``callable_path`` (importable
    ``module:qualname``) ride on the class as ``TAIDAL_NODE_KIND`` / ``TAIDAL_CALLABLE_PATH`` so the
    ops-export compiler (:mod:`fluxstudio.export`) can pick out the op nodes from a saved graph and
    re-emit each as a Confluid ``!class:`` marker. They have no effect on canvas execution.
    """
    input_spec: Dict[str, Any] = {"required": inputs}
    if optional_inputs:
        input_spec["optional"] = optional_inputs
    if hidden:
        input_spec["hidden"] = hidden

    # Wrap execute with wall-clock timing.  The timing label is injected into
    # ComfyUI's ui["text"] list (the same mechanism PreviewAny uses) so it
    # appears on the node after each execution.  For runnable nodes that already
    # return a ui dict with a text log, the timing is appended at the end of the
    # last text entry so the training log stays readable.  For all other nodes
    # the result tuple is wrapped as {"ui": {"text": [timing]}, "result": tuple}.
    _inner_execute = execute

    def _timed_execute(self: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        result = _inner_execute(self, **kwargs)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        timing_label = f"⏱ {elapsed_ms:.0f} ms"
        if isinstance(result, dict) and "ui" in result:
            ui = result["ui"]
            texts = list(ui.get("text", []))
            if texts:
                texts[-1] = str(texts[-1]) + f"\n{timing_label}"
            else:
                texts = [timing_label]
            return {**result, "ui": {**ui, "text": texts}}
        return {"ui": {"text": [timing_label]}, "result": result}

    attrs: Dict[str, Any] = {
        "INPUT_TYPES": classmethod(lambda cls, _spec=input_spec: _spec),
        "RETURN_TYPES": return_types,
        "FUNCTION": "execute",
        "CATEGORY": category,
        "execute": _timed_execute,
        "TAIDAL_ACCEPTS": accepts,
        "TAIDAL_PRODUCES": produces,
        "TAIDAL_NODE_KIND": kind,
        "TAIDAL_CALLABLE_PATH": callable_path,
    }
    if output_node:
        attrs["OUTPUT_NODE"] = True
    if return_names is not None:
        attrs["RETURN_NAMES"] = return_names
    if is_random:
        # float("nan") != float("nan") always → ComfyUI always considers the output stale
        # and re-executes the full upstream chain on every queue prompt.
        attrs["IS_CHANGED"] = classmethod(lambda cls, **kw: float("nan"))
    return type(name, (object,), attrs)


def _build_source_node(cls: Callable, name: str, category: str) -> Type:
    """Build a source/engine node: constructor config -> a ``DATAFLUX_SOURCE`` output (a ``Flux``).

    Source-typed constructor params ride WIRED sockets instead of STRING widgets, so the engine /
    view sources (``DatasetSplit`` / ``Flux`` / ``JointFlux``) compose on the canvas from other
    Source/Op nodes (which already expose themselves as ``DATAFLUX_SOURCE`` / ``DATAFLUX_OP``):
      * a single ``source`` param   -> one ``DATAFLUX_SOURCE`` input (``DatasetSplit.source`` /
        ``Flux.source``); a required socket when the ctor param has no default, else optional;
      * a source-list param (``fluxes``) -> dynamic ``source_N`` ``DATAFLUX_SOURCE`` inputs grown by
        ``web/source_list.js`` (``JointFlux.fluxes``);
      * an op-list param (``ops``)        -> dynamic ``op_N`` ``DATAFLUX_OP`` inputs grown by
        ``web/op_list.js`` (``Flux.ops``).
    Everything else stays a constructor-config widget. A plain leaf source (``HuggingFaceSource`` …)
    has none of these params, so it is unchanged (all-widget).
    """
    src_param = _source_param(cls)
    src_list_param = _source_list_param(cls)
    op_list_param = _op_list_param(cls)
    wired = {p for p in (src_param, src_list_param, op_list_param) if p is not None}
    required = _build_config_inputs(cls, skip=wired or None)

    optional: Dict[str, Any] = {}
    if src_param is not None:
        try:
            default = inspect.signature(cls).parameters[src_param].default
        except (ValueError, TypeError, KeyError):  # pragma: no cover - defensive
            default = inspect.Parameter.empty
        # Required socket (must be wired to run) when the param has no default — e.g. DatasetSplit;
        # optional when it does (Flux.source defaults to None -> an empty stream).
        if default is inspect.Parameter.empty:
            required[src_param] = (SOURCE_TYPE,)
        else:
            optional[src_param] = (SOURCE_TYPE,)
    if src_list_param is not None:
        optional["source_0"] = (SOURCE_TYPE,)  # seed; web/source_list.js grows source_1, source_2, …
    if op_list_param is not None:
        optional["op_0"] = (OP_TYPE,)  # seed; web/op_list.js grows op_1, op_2, …

    def execute(self: Any, **kwargs: Any) -> Tuple[Any, ...]:
        from dataflux.core import Flux

        if src_list_param is not None:
            sources = _collect_sources(kwargs)
            kwargs = {k: v for k, v in kwargs.items() if not (k.startswith("source_") and k[7:].isdigit())}
            kwargs[src_list_param] = sources
        if op_list_param is not None:
            ops = _collect_ops(kwargs)
            kwargs = {k: v for k, v in kwargs.items() if not (k.startswith("op_") and k[3:].isdigit())}
            # ``Tee.branches`` is a list of op-LISTS; v1 makes each connected op its own 1-op branch.
            kwargs[op_list_param] = [[o] for o in ops] if op_list_param == "branches" else ops
        obj = cls(**_coerce_kwargs(cls, kwargs))
        flux = obj if isinstance(obj, Flux) else Flux.from_source(obj)
        return (flux,)

    return _node_class(
        name,
        category,
        required,
        (SOURCE_TYPE,),
        execute,
        return_names=("source",),
        optional_inputs=optional or None,
        produces=_spec_to_dict(cls, "PRODUCES"),
        kind="source",
        callable_path=_safe_callable_path(cls),
        is_random=getattr(cls, "__confluid_random__", False),
    )


def _build_op_node(cls: Callable, name: str, category: str) -> Type:
    """Build a dual-mode op node: ``(SAMPLE?, op_*) -> (SAMPLE, OP[, @output…])``.

    The op node both APPLIES the op (the ``sample`` input chained to the ``sample`` output, today's
    linear-pipeline UX) AND exposes the constructed op on the ``op`` output (``DATAFLUX_OP``), so it
    can feed a higher-order op. ``sample`` is OPTIONAL: leave it unconnected to use the node purely
    as an op-value. Higher-order ops (op-list constructor param) gain dynamic ``op_*`` inputs.

    Any ``@output`` properties declared on the op class (e.g. ``applied_impairments``) become extra
    output sockets appended after ``(sample, op)`` — read from the live op instance after
    ``__call__`` completes (or their pre-call default when ``sample`` is unconnected).
    """
    accepts = getattr(cls, "ACCEPTS", None)
    op_list_param = _op_list_param(cls)
    # Config widgets EXCLUDING the op-list param (it rides DATAFLUX_OP sockets, not a widget).
    required = _build_config_inputs(cls, skip={op_list_param} if op_list_param else None)
    optional: Dict[str, Any] = {"sample": (SAMPLE_TYPE,)}
    if op_list_param is not None:
        optional["op_0"] = (OP_TYPE,)  # seed; web/op_list.js grows op_1, op_2, … as they fill

    # Collect any @output properties declared on this op class.
    out_specs = list(output_specs(cast(type, cls)))
    out_sockets = [_output_socket_type(s["name"], _output_return_anno(cls, s["name"])) for s in out_specs]

    def execute(self: Any, sample: Any = None, **kwargs: Any) -> Tuple[Any, ...]:
        if op_list_param is not None:
            ops = _collect_ops(kwargs)
            kwargs = {k: v for k, v in kwargs.items() if not (k.startswith("op_") and k[3:].isdigit())}
            # ``Tee.branches`` is a list of op-LISTS; v1 makes each connected op its own 1-op branch.
            kwargs[op_list_param] = [[o] for o in ops] if op_list_param == "branches" else ops
        op = cls(**_coerce_kwargs(cls, kwargs))
        applied: Any = None
        if sample is not None:
            # Runtime accept-check (authoritative) before applying — see the type-spec mandate.
            if accepts is not None:
                got = infer_sample_type(sample)
                if not accepts.accepts(got):
                    detail = accepts.explain_mismatch(got)
                    raise RuntimeError(
                        f"{getattr(cls, '__name__', 'op')} cannot accept the upstream sample — {detail}. "
                        f"(op expects {accepts.input}, upstream produces {got.input})"
                    )
            applied = op(sample)
            if applied is None:
                raise RuntimeError(
                    f"{getattr(cls, '__name__', 'op')} filtered out the sample (returned None); "
                    "nothing to pass downstream."
                )
        # Read each @output property from the live op instance (post-call values, or pre-call defaults).
        extra_outs: List[Any] = []
        for spec, socket in zip(out_specs, out_sockets):
            try:
                value = getattr(op, spec["name"])
            except Exception as exc:
                logger.debug("{}: output {!r} unavailable ({}); emitting None.", name, spec["name"], exc)
                value = None
            extra_outs.append(_coerce_output_value(value, socket))
        return (applied, op, *extra_outs)

    return _node_class(
        name,
        category,
        required,
        (SAMPLE_TYPE, OP_TYPE, *out_sockets),
        execute,
        return_names=("sample", "op", *[s["name"] for s in out_specs]),
        optional_inputs=optional,
        accepts=_spec_to_dict(cls, "ACCEPTS"),
        produces=_spec_to_dict(cls, "PRODUCES"),
        kind="op",
        callable_path=_safe_callable_path(cls),
        is_random=getattr(cls, "__confluid_random__", False),
    )


def _build_generic_node(func: Callable, name: str, category: str) -> Type:
    inputs = _build_config_inputs(func)
    try:
        ret_ann = inspect.signature(func).return_annotation
    except (
        ValueError,
        TypeError,
    ):  # pragma: no cover - defensive: inspect rejects rare builtins
        ret_ann = inspect.Signature.empty
    return_type = "HF_DATASET" if _map_type("return", ret_ann) == "HF_DATASET" else "STRING"

    def execute(self: Any, **kwargs: Any) -> Tuple[Any, ...]:
        result = func(**_coerce_kwargs(func, kwargs))
        return result if isinstance(result, tuple) else (result,)

    return _node_class(
        name,
        category,
        inputs,
        (return_type,),
        execute,
        kind="generic",
        callable_path=_safe_callable_path(func),
    )


def _is_scalar_anno(anno: Any) -> bool:
    """True when ``anno`` is a concrete scalar (int / float / str / bool, or ``Optional`` thereof).

    Used by :func:`_build_runnable_node` to decide a member's surface: a scalar stays a WIDGET
    (the user picks `num_classes` / `max_epochs` on the node), while ``Any`` / object / dataset
    annotations become wired sockets. ``Any`` and un-annotated slots are deliberately NOT scalar —
    a trainer's body slots (``optimizer`` / ``model`` / …) are typed ``Any`` and must ride sockets.
    """
    if anno in (Any, inspect.Parameter.empty, None, type(None)):
        return False
    origin = get_origin(anno)
    if origin is Union or str(type(anno)) == "<class 'types.UnionType'>":
        args = [a for a in get_args(anno) if a is not type(None)]
        return bool(args) and all(_is_scalar_anno(a) for a in args)
    return anno in (int, float, str, bool)


def _flatten_union(anno: Any) -> List[Any]:
    """Flatten ``Union``/``X | Y`` arms (recursively), dropping ``NoneType``; a non-Union is one arm."""
    if get_origin(anno) is Union or get_origin(anno) is getattr(_types, "UnionType", ()):
        out: List[Any] = []
        for a in get_args(anno):
            if a is type(None):
                continue
            out.extend(_flatten_union(a))
        return out
    return [anno]


def _container_element(anno: Any) -> Any:
    """The element type of a list/set/tuple/``Iterable``/``Sequence`` (peeled recursively), else ``anno``.

    ``str``/``bytes`` are NOT treated as containers (they are scalars handled elsewhere).
    """
    origin = get_origin(anno)
    if origin is None or origin in (str, bytes):
        return anno
    if origin in (list, set, frozenset, tuple) or (isinstance(origin, type) and issubclass(origin, cabc.Iterable)):
        args = [a for a in get_args(anno) if a is not Ellipsis and a is not type(None)]
        if args:
            return _container_element(args[0])
    return anno


def _is_wireable_object_param(anno: Any) -> bool:
    """True when a constructor param should be a wired ``DATAFLUX_OBJECT`` socket (not a widget).

    A param is wireable iff its annotation has a concrete OBJECT-class arm (a ``Logger`` / ``Callback`` /
    ``nn.Module`` — something a producer node builds) and NO real scalar-config arm. The rule, over the
    ``Annotated``-stripped, ``Union``-flattened, container-peeled arms:

    * a ``str`` / ``int`` / ``float`` arm ⇒ ``False`` — that is real configuration the user TYPES
      (``accelerator="gpu"``, ``precision=16``), so the param stays a widget;
    * a ``bool`` / ``NoneType`` arm ⇒ ignored — a True/False toggle / optionality, NOT the real value
      (this is the ``logger: Union[Logger, …, bool]`` case the old ``_map_type`` mis-rendered as BOOLEAN);
    * a concrete object **class** arm ⇒ marks it wireable;
    * ``Any`` / typevars / unparametrised generics ⇒ ignored (too broad to socket).

    Detection is by annotation SHAPE — never a per-class list. So ``logger`` / ``callbacks`` on a
    ``pytorch_lightning.Trainer`` become sockets while ``accelerator`` / ``strategy`` / ``devices`` /
    ``precision`` / ``profiler`` stay widgets.
    """
    inner = anno
    while get_origin(inner) is Annotated:
        inner = get_args(inner)[0]
    saw_object = False
    for arm in _flatten_union(inner):
        base = _container_element(arm)
        if base in (str, int, float):
            return False
        if base is bool or base is type(None):
            continue
        if isinstance(base, type):
            saw_object = True
    return saw_object


def _admits_object_list(anno: Any) -> bool:
    """True when an annotation has a ``list``/``Iterable``/``Sequence``/``tuple``/``set`` arm whose
    element is an OBJECT class — so the param accepts MANY objects and should render as a DYNAMIC
    ``<name>_N`` socket family (collected into a list), not a single socket.

    Paired with :func:`_is_wireable_object_param`. Covers ``logger: Union[Logger, Iterable[Logger], bool]``
    and ``callbacks: Union[List[Callback], Callback, None]`` (a single-or-list arm still admits a list).
    """
    inner = anno
    while get_origin(inner) is Annotated:
        inner = get_args(inner)[0]
    for arm in _flatten_union(inner):
        origin = get_origin(arm)
        is_container = origin in (list, set, frozenset, tuple) or (
            isinstance(origin, type) and issubclass(origin, cabc.Iterable) and origin not in (str, bytes)
        )
        if is_container:
            elem = _container_element(arm)
            if isinstance(elem, type) and elem not in (str, int, float, bool):
                return True
    return False


def _output_return_anno(cls: Any, prop_name: str) -> Any:
    """Resolve a declared ``@output`` property's return annotation WITHOUT invoking the getter.

    Uses :func:`inspect.getattr_static` (walks the MRO but never triggers the descriptor — a
    runnable's output getter raises pre-run by design) to reach the ``property`` object, then reads
    its ``fget`` return hint. Falls back to ``Any`` for an unresolvable / un-annotated getter.
    """
    member = inspect.getattr_static(cls, prop_name, None)
    fget = getattr(member, "fget", None)
    if fget is None:
        return Any
    try:
        return get_type_hints(fget).get("return", Any)
    except Exception:  # pragma: no cover - defensive: un-importable return annotation
        return Any


def _output_socket_type(prop_name: str, anno: Any) -> str:
    """ComfyUI socket type for a runnable's declared ``@output`` (every output becomes a wireable socket).

    Since a runnable node RUNS on execute (:func:`_build_runnable_node`), its ``@output`` sockets carry
    LIVE post-run values. The mapping:

    * a dataset-named output rides ``DATAFLUX_SOURCE``;
    * a scalar output a typed widget string (via :func:`_map_type` — ``INT`` / ``FLOAT`` / ``STRING`` / ``BOOLEAN``);
    * a **container** output (``Dict`` / ``List`` / ``Set`` / ``Tuple`` — e.g. ``computed_metrics`` a
      ``Dict[str, float]``, ``label_names`` a ``List[str]``) rides a plain ``STRING`` socket (the runnable
      node JSON-encodes the value), so it wires straight into ComfyUI's native *Show Text* node. This branch
      is BEFORE the role inference on purpose: it stops ``computed_metrics`` from mis-mapping to a bogus
      ``DATAFLUX_OBJECT:metric`` via the ``"metric"`` substring in :func:`_infer_member_role`;
    * a role-bearing object output (``trained_model`` → ``DATAFLUX_OBJECT:model``, task-agnostic) docks
      straight into a downstream runnable's likewise-qualified object slot (an evaluator's ``model``);
    * any other object rides the bare ``DATAFLUX_OBJECT``.
    """
    if prop_name in _DATASET_MEMBER_PARAMS:
        return SOURCE_TYPE
    if _is_scalar_anno(anno):
        return _map_type(prop_name, anno)
    if get_origin(anno) in (dict, list, set, tuple):
        return "STRING"  # container -> JSON STRING socket (wire into a native Show Text node)
    role = _infer_member_role(prop_name)
    return _object_socket(role) if role in _QUALIFIED_OBJECT_ROLES else OBJECT_TYPE


def _coerce_output_value(value: Any, socket_type: str) -> Any:
    """Coerce a runnable's LIVE ``@output`` value to its output socket (:func:`_build_runnable_node`).

    A ``STRING`` socket (a container ``@output`` like ``computed_metrics`` / ``label_names``, or a ``str``
    scalar) carries a string: containers are JSON-encoded (``default=str`` so a stray non-JSON leaf like a
    numpy scalar still serialises), ``None`` → ``""``, anything else → ``str()``. Object / source / numeric
    sockets pass the value through unchanged (the live model / flux / number).
    """
    if socket_type != "STRING":
        return value
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set)):
        try:
            return json.dumps(value, default=str, sort_keys=True)
        except (TypeError, ValueError):  # pragma: no cover - defensive: unserialisable nested object
            return str(value)
    return str(value)


def _build_object_node(cls: Callable, name: str, category: str) -> Type:
    """Build an OBJECT node: constructor config widgets -> TWO ``DATAFLUX_OBJECT`` outputs.

    Both outputs carry the configured member (model / loss / metric / lazy runtime member); a runnable
    accepts either on its member slot (``confluid.flow`` is idempotent on a live instance):

    * ``instance`` — the **flowed, live** object, built right here from the widget values. Useful for
      standalone / inspection wiring where every required value is already set on the node.
    * ``class`` — the configured-but-deferred class (a :class:`confluid.LazyClass`). Wire THIS into a
      runnable when a value is only known at ``run()`` (``torch.optim.*.params`` / ``DataLoader.dataset``,
      or a model's dataset-derived ``num_classes``): the runnable flows it with the runtime kwarg. (The
      output is named ``class`` — the user-facing "the configured class, not yet built" — while the value
      it carries is a confluid ``LazyClass``; graphio's whole-graph export keys eager/lazy off the WIRED
      output slot, not this label, so a member wired from here serialises as a deferred ``!lazy:``.)

    When the live ``instance`` cannot be built — a genuinely deferred member whose required ctor param
    is runtime-injected (logged at ``debug``: that is by-design, not an error), or a configuration that
    is simply incomplete/invalid (e.g. a model node left without ``num_classes`` — logged at
    ``warning`` so the user can fix it) — the ``instance`` output is ``None`` and only ``class`` is
    usable. ComfyUI tolerates a ``None`` socket value; downstream nodes that require the instance fail
    legibly on use.
    """
    is_lazy = bool(getattr(cls, "__confluid_lazy__", False))
    # A lazy member's REQUIRED (no-default) ctor params are the runtime-injected slot the runnable
    # fills at ``run()`` (``torch.optim.*.params`` / ``DataLoader.dataset``) — never a widget. Skip
    # them so the object node only exposes the configurable knobs (lr / batch_size / …).
    skip: Optional[AbstractSet[str]] = None
    if is_lazy:
        try:
            skip = {
                p
                for p, par in inspect.signature(cls).parameters.items()
                if p not in ("self", "cls")
                and par.default is inspect.Parameter.empty
                and par.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
            }
        except (ValueError, TypeError):  # pragma: no cover - defensive
            skip = None
    # An OBJECT-typed ctor param (``logger`` / ``callbacks`` — a non-scalar class arm with no
    # str/int/float config arm; see :func:`_is_wireable_object_param`) rides a WIRED ``DATAFLUX_OBJECT``
    # socket so a producer node docks in — instead of the bool-poisoned BOOLEAN widget ``_map_type``
    # would emit. A param that also admits a LIST/Iterable of objects (``_admits_object_list`` —
    # ``logger`` / ``callbacks`` / ``plugins``) gets a DYNAMIC ``<name>_N`` socket family (seed
    # ``<name>_0``; ``web/object_list.js`` grows the rest), collected into a list in ``execute``; a
    # single-object param gets ONE socket. Bare ``OBJECT_TYPE`` (role-less) matches the producer output.
    object_single_params: set = set()  # single-object sockets
    object_list_params: set = set()  # dynamic <name>_N list sockets
    object_required: Dict[str, Any] = {}
    object_optional: Dict[str, Any] = {}
    try:
        for p, par in inspect.signature(cls).parameters.items():
            if p in ("self", "cls") or (skip and p in skip):
                continue
            if par.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            if not _is_wireable_object_param(par.annotation):
                continue
            if _admits_object_list(par.annotation):
                object_list_params.add(p)
                object_optional[f"{p}_0"] = (OBJECT_TYPE,)  # seed; web/object_list.js grows <p>_1, <p>_2, …
            else:
                object_single_params.add(p)
                bucket = object_required if par.default is inspect.Parameter.empty else object_optional
                bucket[p] = (OBJECT_TYPE,)
    except (ValueError, TypeError):  # pragma: no cover - defensive
        pass

    inputs = _build_config_inputs(
        cls, skip=(set(skip) if skip else set()) | object_single_params | object_list_params or None
    )
    inputs.update(object_required)
    # A member with runtime-injected required params (the skipped set) is deferred BY DESIGN — there
    # is no standalone live instance to build, so don't attempt it (and don't warn — that's expected).
    deferred_required = bool(skip)

    def execute(self: Any, **kwargs: Any) -> Tuple[Any, ...]:
        from confluid import Fluid, LazyClass, flow

        def _resolve(v: Any) -> Any:
            # A wired object is passed through as-is — flow()-ed first if it's still a Fluid, so wiring
            # the producer's ``instance`` OR ``class`` output both work.
            return flow(v) if isinstance(v, Fluid) else v

        # Split wired OBJECT-socket inputs (live objects from upstream nodes) from widget inputs.
        widget_kwargs: Dict[str, Any] = {}
        object_kwargs: Dict[str, Any] = {}
        # Object-LIST params: collect their dynamic <name>_N sockets into a list (empty -> omit -> default).
        for lp in object_list_params:
            vals = [_resolve(v) for v in _collect_indexed(kwargs, lp)]
            if vals:
                object_kwargs[lp] = vals
        for k, v in kwargs.items():
            if _is_indexed_key(k, object_list_params):
                continue  # handled by the object-list collection above
            if k in object_single_params:
                if v is not None:  # unwired single object socket -> omit so the ctor default applies
                    object_kwargs[k] = _resolve(v)
            else:
                widget_kwargs[k] = v
        coerced = {**_coerce_kwargs(cls, widget_kwargs), **object_kwargs}
        lazy = LazyClass(cast(Type[Any], cls), **coerced)
        instance: Any = None
        if deferred_required:
            logger.debug(
                f"{name}: deferred member with runtime-injected param(s) {sorted(skip or [])}; "
                "no standalone instance — wire the 'class' output into a runnable."
            )
        else:
            try:
                # flow() builds the instance from a fresh LazyClass (and solidifies it) so the pristine
                # ``lazy`` output is never consumed/mutated by the build attempt.
                instance = flow(LazyClass(cast(Type[Any], cls), **coerced))
            except Exception as exc:  # noqa: BLE001 - surface ANY build failure as an actionable warning
                logger.warning(
                    f"{name}: could not build a live instance ({type(exc).__name__}: {exc}); "
                    "the 'instance' output is None — set the missing parameter(s) on this node, or "
                    "wire the 'lazy' output into a runnable that injects them at run()."
                )
                instance = None
        return (instance, lazy)

    # ROLE-qualify BOTH outputs (instance + class) so they dock only into a matching-ROLE slot
    # (a ``model`` node → a ``model`` slot), TASK-AGNOSTIC: a classification model and a segmentation
    # model both ride ``DATAFLUX_OBJECT:model``. The role is the explicit ``role=`` tag or inferred
    # from the type (``_role_for_class``); a role-less member (logger / lazy infra) keeps the bare
    # ``DATAFLUX_OBJECT``. Mirrors the runnable-SLOT side. See :func:`_object_socket_for_class`.
    obj_socket = _object_socket_for_class(cls)

    return _node_class(
        name,
        category,
        inputs,
        (obj_socket, obj_socket),
        execute,
        return_names=("instance", "class"),
        optional_inputs=object_optional or None,
        kind="object",
        callable_path=_safe_callable_path(cls),
    )


def _build_runnable_node(cls: Callable, name: str, category: str) -> Type:
    """Build a runnable (trainer / evaluator / processor) node: members as wired INPUT sockets.

    Every configurable member is enumerated from ``to_pydantic(cls)`` (which surfaces both ctor params
    AND ``__init__``-body slots, and skips ``@ignore_config`` members). Each member is rendered as:

    * a ``DATAFLUX_SOURCE`` socket — dataset/source members (:data:`_DATASET_MEMBER_PARAMS` + the
      ``source`` param), fed by Source/dataset nodes;
    * a ``DATAFLUX_OBJECT`` socket — any other non-scalar member (model / loss / optimizer / loaders /
      lightning / metrics), fed by object nodes;
    * a WIDGET — scalar members (``num_classes`` / ``max_epochs`` / ``experiment_name`` …) via the
      shared :func:`_build_config_inputs` (ComfyUI lets the user "convert to input" when they want a
      wire).

    A member that is a constructor param with NO default — OR one declared :data:`confluid.Mandatory`
    (read via :func:`confluid.mandatory_param_names`, so a defaulted-for-zero-arg slot can still be
    flagged mandatory) — is a REQUIRED socket (``model`` / ``train_set`` / ``dataset``); everything else
    is OPTIONAL — unwired ⇒ the runnable keeps its baked default ("true optional").

    The node **RUNS on execute** (it is the terminal ``OUTPUT_NODE`` — there is no separate Run node):
    ``execute`` constructs the runnable (ctor params to ``__init__``, body slots via post-construction
    ``setattr`` of the wired members), calls :func:`fluxstudio.nodes.run_runnable` (autograd-restore for
    ``__torch_runner__`` trainers, ComfyUI-interrupt handling, live stdout streaming to the node's log
    widget), then reads each declared ``@output`` (now LIVE, post-run) and emits it on a typed socket.

    OUTPUTS: slot 0 is the (post-run) runnable on a ``DATAFLUX_RUNNABLE`` wire; then EVERY declared
    ``@output`` property (:func:`confluid.output_specs`) becomes a wireable socket via
    :func:`_output_socket_type` — ``trained_model`` rides ``DATAFLUX_OBJECT:model`` into a downstream
    evaluator's ``model`` slot, and container outputs (``computed_metrics`` / ``label_names``) ride a
    ``STRING`` (JSON) socket you wire into ComfyUI's native *Show Text*. Because the node runs first,
    these carry real values (not ``None``).
    """
    from confluid import to_pydantic

    try:
        fields = dict(to_pydantic(cls).model_fields)
    except Exception:  # pragma: no cover - defensive: a class to_pydantic can't introspect
        fields = {}
    try:
        ctor_params = {p for p in inspect.signature(cls).parameters if p not in ("self", "cls")}
        ctor_required = {
            p
            for p, par in inspect.signature(cls).parameters.items()
            if p not in ("self", "cls") and par.default is inspect.Parameter.empty
        }
    except (ValueError, TypeError):  # pragma: no cover - defensive
        ctor_params, ctor_required = set(), set()

    # A REQUIRED socket is a ctor param with no default OR one explicitly declared Mandatory[...]
    # (the I/O contract marker — routes the decision through confluid, not just default-presence).
    runnable_cls = cast(type, cls)  # _build_runnable_node is only called with classes
    required_params = ctor_required | mandatory_param_names(runnable_cls)

    # An object slot for a role (model / loss / metric) is ROLE-qualified — e.g. ANY trainer's
    # ``model`` slot accepts ``DATAFLUX_OBJECT:model`` — so ComfyUI rejects a loss wire but a
    # classification model docks into a segmentation trainer's model slot just the same (TASK-
    # AGNOSTIC; task survives only as palette/navigaitor guidance). Role-less slots (optimizer /
    # loader / lightning / logger) stay generic ``DATAFLUX_OBJECT``. See :data:`_STRICT_SLOT_ROLES`
    # / :func:`_object_socket`. Mirrors the object-NODE output side (:func:`_object_socket_for_class`).
    socket_members: Dict[str, str] = {}  # member name -> SOURCE_TYPE | OBJECT_TYPE[:role]
    for fname, finfo in fields.items():
        anno = getattr(finfo, "annotation", Any)
        if fname in _DATASET_MEMBER_PARAMS or fname == _SOURCE_PARAM:
            socket_members[fname] = SOURCE_TYPE
        elif _is_scalar_anno(anno):
            continue  # scalar -> widget (handled by _build_config_inputs below)
        else:
            role = _STRICT_SLOT_ROLES.get(fname)
            socket_members[fname] = _object_socket(role) if role else OBJECT_TYPE

    # Scalars stay widgets; skip the socket members (and any non-ctor body slot, which
    # _build_config_inputs wouldn't see anyway).
    required: Dict[str, Any] = _build_config_inputs(cls, skip=set(socket_members) or None)
    optional: Dict[str, Any] = {}
    for member, socket in socket_members.items():
        if member in required_params:
            required[member] = (socket,)
        else:
            optional[member] = (socket,)

    # EVERY declared @output -> a typed output socket (mapped by _output_socket_type; containers ride a
    # STRING/JSON socket). The node runs before reading them, so they carry live values.
    out_specs = list(output_specs(runnable_cls))
    out_sockets = [_output_socket_type(s["name"], _output_return_anno(cls, s["name"])) for s in out_specs]
    extra_types = tuple(out_sockets)
    extra_names = tuple(s["name"] for s in out_specs)

    def execute(self: Any, unique_id: Optional[str] = None, **kwargs: Any) -> Dict[str, Any]:
        from fluxstudio.nodes import run_runnable  # lazy: nodes.py imports bridge (avoid an import cycle)

        ctor_kwargs: Dict[str, Any] = {}
        post: Dict[str, Any] = {}
        for k, v in kwargs.items():
            if v is None:
                continue  # unwired optional socket -> keep the runnable's baked default
            if k in ctor_params:
                ctor_kwargs[k] = v
            else:
                post[k] = v
        obj = cls(**_coerce_kwargs(cls, ctor_kwargs))
        for k, v in post.items():
            setattr(obj, k, v)
        # RUN it (this node is the OUTPUT_NODE — there is no separate Run node). run_runnable restores
        # autograd for TorchRunner trainers, honours ComfyUI's interrupt, and live-streams stdout to the
        # node's log widget; it returns the captured text.
        text = run_runnable(obj, unique_id)
        # Read each declared @output (now LIVE, post-run) and coerce to its socket type — a STRING socket
        # carries the JSON-encoded value (wire into Show Text); object/source/scalar sockets pass through.
        outs: List[Any] = []
        for spec, socket in zip(out_specs, out_sockets):
            try:
                value = getattr(obj, spec["name"])
            except Exception as exc:  # pragma: no cover - a getter that raises post-run (shouldn't happen)
                logger.debug("{}: output {!r} unavailable ({}); emitting None.", name, spec["name"], exc)
                value = None
            outs.append(_coerce_output_value(value, socket))
        return {"ui": {"text": [text]}, "result": (obj, *outs)}

    return _node_class(
        name,
        category,
        required,
        (RUNNABLE_TYPE, *extra_types),
        execute,
        return_names=("runnable", *extra_names),
        optional_inputs=optional or None,
        kind="runnable",
        callable_path=_safe_callable_path(cls),
        output_node=True,
        hidden={"unique_id": "UNIQUE_ID"},
    )


def create_comfy_node(
    func: Callable,
    name: str,
    category: str = "Taidal",
    display_name: Optional[str] = None,
) -> Type:
    """Dynamically construct a ComfyUI node class from a Python callable.

    Dispatches on the callable's DataFlux role: a ``run()``-implementing class (trainer / evaluator /
    processor) becomes a buildable RUNNABLE node (members as wired sockets); ``Sample``-taking op
    classes become ``SAMPLE -> SAMPLE`` nodes; ``Sample``-yielding sources (and task ``role="dataset"``
    classes) become ``-> SOURCE`` nodes; runnable MEMBER classes (model / loss / metric / optimizer /
    loader / lightning) become ``-> OBJECT`` nodes; everything else falls back to a generic node.
    """
    if _is_runnable(func):
        return _build_runnable_node(func, name, category)
    if _op_call_signature(func) is not None:
        return _build_op_node(func, name, category)
    if _is_source(func) or _is_dataset_member_class(func):
        return _build_source_node(func, name, category)
    if _is_object_member_class(func):
        return _build_object_node(func, name, category)
    return _build_generic_node(func, name, category)


def _humanize_node_name(name: str) -> str:
    """Palette label for a discovered callable: the identifier verbatim, ``_`` → space.

    Deliberately does NOT use ``str.title()`` — that lowercases the tail of every
    word and destroys the workspace's CamelCase class names (``HuggingFaceSource``
    → ``Huggingfacesource``). Capitals are preserved as written: ``HuggingFaceSource``
    stays ``HuggingFaceSource``; ``rescale_op`` becomes ``rescale op``.
    """
    return name.replace("_", " ")


def _package_tag(module_path: str, dist_name: Optional[str] = None) -> str:
    """The bracketed package tag shown in a node's LABEL (``[SONAIR]`` / ``[WAIVEFRONT-HELIOS]``).

    The palette is grouped by ROLE/TASK (or category) — NOT by package (see :func:`_node_category`) —
    so the producing package is surfaced in the node label instead.

    Uses the distribution name (``ep.dist.name``) when available so companion namespace packages
    (e.g. ``waivefront-helios``) are distinguished from their parent (``waivefront``).
    Falls back to the top-level Python package name when ``dist_name`` is absent.
    """
    if dist_name is not None:
        return dist_name.upper()
    return module_path.split(".")[0].upper()


def _node_category(
    confluid_category: Optional[str],
    group: Optional[str],
    root: str,
    role: Optional[str] = None,
    task: Optional[str] = None,
) -> str:
    """Build a node's ComfyUI palette path — grouped by ROLE/TASK or by category, NOT by package.

    The producing package moves to the node LABEL (a ``[SONAIR]`` tag — see :func:`_package_tag`);
    the palette folder reuses the discovery taxonomy instead:

    * a ROLE-bearing class (model / loss / metric / trainer / evaluator / dataset / logger) →
      ``<root>/<Role>[/<Task>]`` (ROLE-first, then the optional task) — so e.g. all models of every
      task collect under ``Taidal/Model/``: ``Taidal/Model/Classification``, ``Taidal/Trainer/Detection``,
      ``Taidal/Logger`` (task-agnostic → no task level). Mirrors the task-agnostic socket model.
    * a task-agnostic class (op / source / engine, and the role-less lazy infra) → its ``@configurable``
      ``<Category>`` then the optional path-like ``<group>``: ``Taidal/Op/numpy``, ``Taidal/Source``.

    An empty ``root`` (the ``--no-default-nodes`` mode — see :func:`node_root`) drops the leading
    ``Taidal/`` folder so the node sits at the palette root (``Model/Classification`` not
    ``/Model/Classification``).
    """
    if role:
        parts = [root, role.title()]
        if task:
            parts.append(task.title())
        return "/".join(p for p in parts if p)
    path = "/".join(p for p in (root, (confluid_category or "").title()) if p)
    if group:
        path = f"{path}/{group}" if path else group
    return path


def _node_key(module_path: str, confluid_category: str, group: Optional[str], name: str) -> str:
    """Build the GLOBALLY-UNIQUE ComfyUI registration key for a discovered node.

    ComfyUI identifies a node TYPE by its key in ``NODE_CLASS_MAPPINGS`` — NOT by its display
    ``CATEGORY`` — so the key must be unique. A plain ``Taidal_<ClassName>`` collides whenever two
    modules expose a same-named class (e.g. ``dataflux.ops.numpy:RescaleOp`` vs
    ``dataflux.ops.torch:RescaleOp``): the second registration silently overwrites the first and
    one node vanishes from the palette. We qualify the key with the top-level package + confluid
    category + group, e.g. ``Taidal_dataflux_op_numpy_RescaleOp`` vs ``Taidal_dataflux_op_torch_RescaleOp``.

    Built from RAW identifiers (the module's top-level package, the lowercase category, the literal
    group segments) — NOT the display-cased palette path — so the key stays stable even if a
    package's display casing (``_PACKAGE_DISPLAY_NAMES``) changes. The DISPLAY name is unchanged
    (the verbatim class name via :func:`_humanize_node_name`), so both still read as "RescaleOp" in
    their respective palette folders.
    """
    parts = ["Taidal", module_path.split(".")[0], confluid_category]
    if group:
        parts.extend(group.split("/"))
    parts.append(name)
    return "_".join(parts)


def register_module_nodes(
    module_path: str,
    target_mappings: Dict[str, Type],
    display_name_mappings: Dict[str, str],
    root: Optional[str] = None,
    dist_name: Optional[str] = None,
) -> None:
    """Scan a module and register its buildable callables as ComfyUI nodes.

    Registration is a POSITIVE allowlist: a discovered class becomes a node ONLY if its
    ``@configurable`` discovery category is in :data:`_NODE_CATEGORIES` (``op`` / ``source`` /
    ``engine``). The engines ``Flux`` / ``JointFlux`` (``category="engine"``) and the source-view
    ``DatasetSplit`` (``category="source"``) ARE buildable: their source-typed constructor params
    render as wired ``DATAFLUX_SOURCE`` / ``DATAFLUX_OP`` sockets (see :func:`_build_source_node`).
    Excluded by construction — the raw-callable op wrappers ``FilterOp`` / ``WrappedOp``
    (UNcategorised — they take a raw callable, nothing to wire, so even with ``engine`` allowed they
    stay out); task ``*_dataset`` classes (a trainer-config concept, not a canvas one); uncategorised
    module-level helper functions and raw sinks; and ``_``-private callables (skipped explicitly so a
    private op that *was* tagged still never surfaces). So a class only appears once it is
    deliberately tagged as a buildable op/source/engine.

    Each node's palette folder is ``<root>/<Package>/<Category>[/<group>]`` — see
    :func:`_node_category`. ``root`` defaults to :func:`node_root` (``"Taidal"``, or ``""`` —
    root-level — when the CLI's ``--no-default-nodes`` flag is active).

    Discovery has TWO passes (``scan_module`` filters to callables whose ``__module__`` IS this
    module, which misses classes a package exposes from a SUBMODULE or registers from a third-party
    library):

    1. ``scan_module(module_path)`` — module-local callables (functions + locally-defined classes).
    2. ``@configurable`` classes listed in the module's ``__all__`` — its deliberate public exports,
       regardless of ``__module__``. This surfaces a package's re-exported runnables / models /
       datasets (``sonair`` re-exports ``LightningClassificationTrainer`` from ``sonair.trainer``)
       AND registered third-party members (``marainer.configurables``'s ``torch`` optimizers /
       ``DataLoader`` / ``pl.Trainer``). Gated on ``__all__`` so a module's imported-for-use
       configurables (not re-exported) don't leak into the palette.

    Both feed the same admission filter.
    """
    if root is None:
        root = node_root()
    logger.info(f"ComfyFlux: Scanning module {module_path} for nodes...")

    # Pass 1: module-local callables (name -> target). ``setdefault`` so pass 2 never overrides.
    candidates: Dict[str, Any] = {}
    for item in scan_module(module_path):
        if item["name"].startswith("_"):
            logger.debug(f"ComfyFlux: Skipping private callable {item['path']}")
            continue
        target_func = resolve_callable(item["path"])
        if callable(target_func):
            candidates.setdefault(item["name"], target_func)

    # Pass 2: @configurable classes the module re-exports via __all__ (submodule re-exports +
    # registered third-party members) — invisible to scan_module's __module__ filter.
    try:
        mod = importlib.import_module(module_path)
    except Exception:  # pragma: no cover - scan_module already imported it; defensive
        mod = None
    for name in getattr(mod, "__all__", None) or []:
        if name.startswith("_") or name in candidates:
            continue
        member = getattr(mod, name, None)
        # A re-exported configurable may be a class OR a registered builder FUNCTION
        # (``raidar.configurables`` registers torchvision's ``fasterrcnn_*`` builders) — admit any
        # callable carrying the configurable marker; the admission filter below classifies it.
        if callable(member) and getattr(member, "__confluid_configurable__", False):
            candidates[name] = member

    for name, target_func in candidates.items():
        confluid_category = getattr(target_func, "__confluid_category__", None)
        # Admit a class if it is a deliberately-tagged op/source/engine, OR it implements the run
        # interface (a buildable runnable node), OR it is a runnable MEMBER class (a task dataset →
        # Source node, or a model/loss/metric/optimizer/loader/lightning → OBJECT node) whose node
        # feeds a runnable's wired member sockets.
        admitted = (
            confluid_category in _NODE_CATEGORIES
            or _is_runnable(target_func)
            or _is_dataset_member_class(target_func)
            or _is_object_member_class(target_func)
        )
        if not admitted:
            logger.debug(f"ComfyFlux: Skipping {module_path}.{name} (not an op/source/engine/runnable/member)")
            continue

        group = getattr(target_func, "__confluid_group__", None)
        # Runnables / members may be category-less (a runnable's category is e.g.
        # ``classification_trainer``; a ``lazy`` optimizer has no category) — fall back to the
        # discovery role, then a literal kind (``runnable`` vs ``object``), so the unique key +
        # palette path stay well-formed.
        key_category = (
            confluid_category
            or getattr(target_func, "__confluid_role__", None)
            or ("runnable" if _is_runnable(target_func) else "object")
        )
        node_name = _node_key(module_path, key_category, group, name)
        # Palette folder reuses ROLE/TASK (or category) — never the package; the package rides the
        # node LABEL as a ``[PKG]`` tag instead (see _node_category / _package_tag).
        node_class = create_comfy_node(
            func=target_func,
            name=node_name,
            category=_node_category(
                key_category,
                group,
                root,
                role=getattr(target_func, "__confluid_role__", None),
                task=getattr(target_func, "__confluid_task__", None),
            ),
        )
        target_mappings[node_name] = node_class
        display_name_mappings[node_name] = f"[{_package_tag(module_path, dist_name)}] {_humanize_node_name(name)}"
        logger.debug(f"ComfyFlux: Registered node {node_name} for {module_path}.{name}")
