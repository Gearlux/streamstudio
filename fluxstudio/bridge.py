"""Dynamic runtime generation of ComfyUI nodes from SampleFlux / Waivefront / HF callables.

Discovered callables are classified into three node shapes so a real pipeline can be
wired on the ComfyUI canvas — instead of every socket collapsing to ``STRING``:

* **Source** — a class whose ``__getitem__`` / ``__iter__`` yields sample records (plain
  dicts of typed items — :data:`sampleflux.items.Record`; e.g. ``HuggingFaceSource``). The
  node exposes the constructor config as widgets and outputs a ``SAMPLEFLUX_SOURCE`` (a
  :class:`sampleflux.core.Flux` wrapping the constructed source). The engines / view-sources
  (``DatasetSplit`` / ``Flux`` / ``JointFlux``) are the same shape, but their source-typed
  constructor params ride WIRED sockets instead of widgets — a single ``source`` →
  ``SAMPLEFLUX_SOURCE`` input, a source-list (``fluxes``) → dynamic ``source_N`` inputs, an op-list
  (``ops``) → dynamic ``op_N`` ``SAMPLEFLUX_OP`` inputs — so they compose on the canvas from other
  Source/Op nodes (which already output themselves as ``SAMPLEFLUX_SOURCE`` / ``SAMPLEFLUX_OP``).
* **Op** — a ``@configurable(category="op")`` class (``__call__(record) -> Optional[Record]``,
  e.g. ``RescaleOp``, ``ConvertToImageOp``) — classified by the POSITIVE category tag, the
  same allowlist the palette uses (a kernel-only ``Transform`` subclass inherits its
  ``__call__``, so a signature sniff can't see it). The node is **dual-mode**:
  constructor config as widgets, an OPTIONAL ``SAMPLEFLUX_SAMPLE`` input, and TWO outputs —
  ``sample`` (the op applied to the input record, for linear chaining) and ``op`` (the constructed
  op as a ``SAMPLEFLUX_OP`` value, so it can feed a higher-order op). A higher-order op whose
  constructor takes an op-list (``Enable`` / ``Parallel`` / ``Pipeline``) gets dynamic ``SAMPLEFLUX_OP``
  inputs (``op_0`` …) instead of a STRING widget.
* **Generic** — any other callable (plain value functions): scalar widgets with a ``STRING``
  fallback (the original behaviour, preserved).

The custom socket type strings ``SAMPLEFLUX_SOURCE`` / ``SAMPLEFLUX_SAMPLE`` / ``SAMPLEFLUX_OP`` carry the
live Python objects (``Flux`` / a record dict / a constructed op) between nodes — ComfyUI connects two
sockets iff their type strings are equal — which is what lets ops chain (via ``SAMPLEFLUX_SAMPLE``) and
compose into higher-order ops (via ``SAMPLEFLUX_OP``).
"""

import ast
import collections.abc as cabc
import importlib
import inspect
import json
import os
import time
import types as _types
import typing
from pathlib import Path
from typing import (
    AbstractSet,
    Annotated,
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Set,
    Tuple,
    Type,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

import datasets
from annotated_types import Ge, Gt, Interval, Le, Lt
from confluid import mandatory_param_names, output_specs, parse_param_docs
from loggair import get_logger
from sampleflux import Record as _Record
from sampleflux.discovery import get_callable_path, resolve_callable, scan_module

logger = get_logger(__name__)


# Custom ComfyUI socket types for live SampleFlux objects passed between nodes.
# ``SAMPLEFLUX_SAMPLE`` carries a sample RECORD — a plain dict of typed items (the socket type
# string is unchanged so saved canvases keep their wires).
SAMPLE_TYPE = "SAMPLEFLUX_SAMPLE"
SOURCE_TYPE = "SAMPLEFLUX_SOURCE"
# A constructed op instance (a callable ``record -> Optional[Record]``) ridden as a wire, so an op
# node's ``op`` output can feed a higher-order op (``Enable`` / ``Parallel`` / ``Pipeline``) that takes
# ops as constructor input. Distinct from ``SAMPLEFLUX_SAMPLE`` (data) — this carries the op itself.
OP_TYPE = "SAMPLEFLUX_OP"
# A built "runnable" (a trainer / evaluator / processor — any class implementing ``run(self)``),
# output by a runnable node and consumed by the ``Run`` node which calls ``.run()``.
RUNNABLE_TYPE = "SAMPLEFLUX_RUNNABLE"
# A built configurable MEMBER object (model / loss / optimizer / loader / lightning / metrics) — or a
# deferred ``LazyClass`` of one (when the member class is tagged ``lazy``) — ridden as a wire so an
# object node feeds a runnable's member slot. Distinct from SOURCE (datasets ride SAMPLEFLUX_SOURCE).
OBJECT_TYPE = "SAMPLEFLUX_OBJECT"
# A raw numpy array / torch tensor ridden as a wire — the value behind a record entry, carried OUT of
# the record extractor and INTO the Array/Tensor Histogram viewer for value inspection. A hand-wired
# ADAPTER socket only (``nodes.py``); deliberately NOT in ``TYPE_MAP`` — no op-param annotation maps
# to it, so auto-generated op nodes are unaffected (an ``np.ndarray`` param stays a STRING widget).
ARRAY_TYPE = "SAMPLEFLUX_ARRAY"
# An arbitrary structured Python VALUE (a list of boxes, a list of lists, a dict of containers) ridden
# as a wire. A constructor param whose type is a NESTED container can't be sensibly typed into a single
# widget, so it renders as an OPTIONAL ``SAMPLEFLUX_VALUE`` input socket (``_needs_value_socket``) fed by
# the hand-written **Value** node (``nodes.py``), which parses a Python/JSON literal into the value.
VALUE_TYPE = "SAMPLEFLUX_VALUE"

# Mapping Python types to ComfyUI widget types.
TYPE_MAP = {
    int: "INT",
    float: "FLOAT",
    str: "STRING",
    bool: "BOOLEAN",
    datasets.Dataset: "HF_DATASET",
}

# Numeric widgets MUST carry explicit full-range bounds: when ``min``/``max`` are absent
# from the spec, the ComfyUI frontend clamps to ``min ?? 0`` and ``max ?? 2048`` — silently
# rejecting negative values (every dB knob: ``noise_power_db=-120``) and large ones
# (``sample_rate=1e7``, frequencies in Hz). The annotation carries no range info, so the
# widget is unbounded; an op that needs validation does it in its constructor.
DEFAULT_WIDGET_CONFIG: Dict[str, Dict[str, Any]] = {
    "INT": {"default": 0, "min": -(2**53), "max": 2**53},
    "FLOAT": {"default": 0.0, "step": 0.01, "min": -1e308, "max": 1e308},
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
    ARRAY_TYPE,
    VALUE_TYPE,
    "HF_DATASET",
}


def _object_socket(category: Optional[str]) -> str:
    """The object-member socket type for a class's ``@configurable`` category.

    Object members (model / loss / metric) ride a CATEGORY-QUALIFIED socket —
    ``SAMPLEFLUX_OBJECT:classification_model`` — so ComfyUI's string-equality
    connection rule enforces compatibility (a ``classification_model`` node docks
    only into a ``classification_model`` slot, never a loss/segmentation slot).
    Category-less members (the role-less ``lazy`` infra members optimizer/loader/
    lightning, and task-agnostic loggers) keep the bare ``SAMPLEFLUX_OBJECT`` type.
    The ``Retag`` adapter node (``nodes.py``) is the deliberate escape hatch that
    re-types an object to a chosen category.
    """
    return f"{OBJECT_TYPE}:{category}" if category else OBJECT_TYPE


def _base_socket_type(socket_type: Any) -> Any:
    """Strip a ``:category`` suffix to the base socket type (``SAMPLEFLUX_OBJECT``).

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


# A runnable's constructor params / body slots that hold a DATASET / SOURCE / FLUX — rendered as a
# wired ``SAMPLEFLUX_SOURCE`` socket (fed by a Source / ``Flux`` engine node) instead of a STRING
# widget. ``flux`` covers ``sampleflux.processing.DatasetProcessor`` (its sole non-sink input is the
# ``Flux`` to write); ``source`` is also handled by ``_SOURCE_PARAM``. Detected by name (same
# convention as the op/source-list params); a runnable's other object members ride ``SAMPLEFLUX_OBJECT``
# sockets.
_DATASET_MEMBER_PARAMS = {"train_set", "val_set", "test_set", "dataset", "flux"}

# Discovery ROLES whose classes become OBJECT nodes (a trainer/runnable slot-filler producing a
# ``SAMPLEFLUX_OBJECT`` wire): a task-scoped model / loss / metric, plus ``logger`` (a Lightning logger
# fed into the ``pytorch_lightning.Trainer`` ``logger`` slot). Datasets are SOURCE nodes (role
# ``"dataset"`` is routed to ``_build_source_node``), not object nodes. The role-less runtime members
# (``torch.optim.*`` / ``DataLoader`` / ``pytorch_lightning.Trainer``, registered in
# ``marainer.lightning.configurables``) are detected instead by the confluid **``lazy`` mark**
# (``__confluid_lazy__``) — deliberately NOT a category, so they don't leak into navigaitor's
# form-spec slot pickers (which resolve a picker FROM the slot's default-target category and would
# then offer the raw ``params``-taking optimizer). Loggers carry ``role="logger"`` (NOT ``lazy``) so
# they build EAGERLY — a logger's required ``save_dir`` must stay a configurable widget, not be
# skipped as a runtime-injected slot. See ``_is_object_member_class``. (SampleFlux sinks are NOT here:
# they're admitted by ``category="sink"`` in ``_NODE_CATEGORIES`` and dispatched by ``_is_sink_class``,
# not by role — but ``_role_for_class`` maps them to a synthetic ``"sink"`` role for socket-typing.)
_MEMBER_ROLES = {"model", "loss", "metric", "logger"}

# A runnable's object-member PARAM NAME → the ROLE its slot accepts. The slot socket is then
# ROLE-qualified (``_object_socket(role)`` → ``SAMPLEFLUX_OBJECT:model``) — TASK-AGNOSTIC: a
# classification model and a segmentation model BOTH produce ``SAMPLEFLUX_OBJECT:model`` and dock
# into ANY trainer's ``model`` slot. The role still guards kind (a loss can't dock into a model
# slot). Task survives only as GUIDANCE (navigaitor slot ranking + the FluxStudio palette
# label/grouping), never on the socket. Mirrors navigaitor's ``configs.generate.PARAM_NAME_TO_ROLE``
# (fluxstudio must NOT import navigaitor — the convention is small and stable). ``logger`` is
# deliberately ABSENT, as are the infra members (optimizer/loader/lightning): those stay generic
# ``SAMPLEFLUX_OBJECT`` (no meaningful role-kind distinction to enforce). ``sink`` IS qualified so a
# SampleFlux storage sink (output ``SAMPLEFLUX_OBJECT:sink`` via ``_role_for_class``) docks ONLY into a
# ``DatasetProcessor.sink`` slot — never a model/loss slot, and vice versa.
_STRICT_SLOT_ROLES = {
    "model": "model",
    "loss_fn": "loss",
    "metrics": "metric",
    "train_metrics": "metric",
    "val_metrics": "metric",
    "test_metrics": "metric",
    "sink": "sink",
}

# The roles whose object sockets are ROLE-qualified (``SAMPLEFLUX_OBJECT:<role>``). Everything else
# (logger / optimizer / loader / lightning, and untagged objects) rides the bare ``SAMPLEFLUX_OBJECT``.
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
    tag nor a recognised base applies → the object rides the generic ``SAMPLEFLUX_OBJECT``.

    A ``category="sink"`` class maps to a synthetic ``"sink"`` role (SampleFlux storage sinks carry no
    ``role=`` tag and their ``Storage`` base isn't in :data:`_TYPE_ROLE_BY_BASE`) so the sink node's
    object output sockets as ``SAMPLEFLUX_OBJECT:sink`` — matching the ``DatasetProcessor.sink`` slot.
    Checked first: it is the deliberate sink marker (see :data:`_NODE_CATEGORIES` / :func:`_is_sink_class`).
    """
    if getattr(cls, "__confluid_category__", None) == "sink":
        return "sink"
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

    ``SAMPLEFLUX_OBJECT:model`` for a model / loss / metric (task-agnostic — see
    :data:`_QUALIFIED_OBJECT_ROLES`), bare ``SAMPLEFLUX_OBJECT`` for everything else (logger / infra /
    untagged). Mirrors the runnable-SLOT side so an object node docks into the matching slot.
    """
    role = _role_for_class(cls)
    return _object_socket(role) if role in _QUALIFIED_OBJECT_ROLES else OBJECT_TYPE


def _infer_member_role(name: str) -> Optional[str]:
    """The role (model / loss / metric) a member NAME implies, else ``None``.

    Exact INPUT param names resolve via :data:`_STRICT_SLOT_ROLES`; a runnable ``@output`` name
    (e.g. ``trained_model``) is freer, so fall back to a keyword scan — this keeps a trainer's
    ``trained_model`` output ROLE-qualified (``SAMPLEFLUX_OBJECT:model``, task-agnostic) so it docks
    into a downstream evaluator's likewise-qualified ``model`` slot. Returns ``None`` for role-less
    members (optimizer / loader / lightning / logger) → generic ``SAMPLEFLUX_OBJECT``.
    """
    if name in _STRICT_SLOT_ROLES:
        return _STRICT_SLOT_ROLES[name]
    for keyword, role in (("model", "model"), ("loss", "loss"), ("metric", "metric")):
        if keyword in name:
            return role
    return None


# Constructor params (on higher-order ops) that hold a LIST OF OPS — rendered as dynamic
# ``SAMPLEFLUX_OP`` multi-inputs (``op_0``, ``op_1``, … via ``web/op_list.js``) instead of a STRING
# widget, so concrete op nodes wire into them. ``Enable``/``Parallel`` take ``ops``; ``transforms``
# is ``sampleflux.transform.Pipeline``'s param (ALSO the helios convention surfaced by
# waivefront-helios's auto-bridged composites — ``HeliosComposeOp`` & co mirror a helios
# ``*transforms`` variadic as a list param).
_OP_LIST_PARAMS = {"ops", "transforms"}

# Single-op constructor params (one op VALUE, not a chain): ``RandomApply.op``,
# ``ConfigureOp.target``. Rendered as ONE optional ``SAMPLEFLUX_OP`` input socket.
_SINGLE_OP_PARAMS = {"op", "target"}

# Constructor params (on source/engine classes) that hold ANOTHER source — rendered as a single
# wired ``SAMPLEFLUX_SOURCE`` input socket instead of a STRING widget, so a Source/engine node's output
# feeds them. ``DatasetSplit(source=…)`` and ``Flux(source=…)`` use ``source``. Detected by exact
# name (the same name-convention as ``_OP_LIST_PARAMS``); safe because no concrete leaf source
# (``HuggingFaceSource``/``RFUAVSource``/``RegionsJsonSource``) has a param literally named ``source``.
_SOURCE_PARAM = "source"

# Constructor params that hold a LIST OF SOURCES — rendered as dynamic ``SAMPLEFLUX_SOURCE`` multi-inputs
# (``source_0``, ``source_1``, … via ``web/source_list.js``), the source analogue of ``_OP_LIST_PARAMS``.
# ``JointFlux(fluxes: List[Flux])`` uses ``fluxes``.
_SOURCE_LIST_PARAMS = {"fluxes", "sources"}

# A runnable COMBINATOR's constructor params that hold ANOTHER runnable (a trainer / evaluator /
# DatasetProcessor / nested combinator), rendered as ``SAMPLEFLUX_RUNNABLE`` INPUT sockets instead of a
# generic ``SAMPLEFLUX_OBJECT`` socket — the consuming half of the dual-mode runnable node. SINGLE-runnable
# params get one socket each; LIST params get a dynamic ``<name>_N`` family grown by ``web/runnable_list.js``
# (collected by :func:`_collect_indexed`). sampleflux.workflow's ``Conditional.if_true``/``if_false`` +
# ``Switch.default`` (single) and ``Sequence.steps`` (list). Detected by NAME (like ``_OP_LIST_PARAMS`` /
# ``_SINGLE_OP_PARAMS``), never per-class — a combinator HOLDS runnables and runs the selected one(s).
_RUNNABLE_PARAMS = {"if_true", "if_false", "default"}
_RUNNABLE_LIST_PARAMS = {"steps"}

# Constructor params that name a HuggingFace dataset *identifier* (a hub repo id
# like ``mnist`` / ``kitofrank/RFUAV``, or optionally a local imagefolder path) —
# NOT a filesystem directory. Recognised only on HuggingFace dataset sources (see
# ``_is_hf_dataset_id_param``); ``sampleflux.sources.HuggingFaceSource`` calls it
# ``path`` (the upstream ``datasets.load_dataset`` arg name) while sonair's
# ``HuggingFaceClassificationDataset`` calls it ``repo_id``.
_HF_DATASET_ID_PARAMS = {"path", "repo_id"}

# Constructor param that names a Melody/MAF dataset *identifier* (a platform dataset name) — NOT a
# filesystem path. Recognised only on a Melody value-producer node (see ``_is_melody_dataset_id_param``,
# gated on the ``Melody`` class-name convention — matches the bridged ``HeliosMelodyDataset``). Rendered
# as a free-text STRING + an optional platform-dataset picker (``web/melody_dataset_chooser.js``).
_MELODY_DATASET_ID_PARAMS = {"name", "dataset"}

# Constructor params (on HuggingFace dataset sources) that name a dataset *feature/column* to map
# onto a record key — ``HuggingFaceSource.input_feature`` / ``target_feature``. Rendered as a
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
# sentinel resolved by ``sampleflux.sources``) plus the SELECTED dataset's columns. Recognised only on
# ``HuggingFace*`` sources (see ``_is_hf_metadata_param``).
_HF_METADATA_PARAM = "metadata_features"

# FluxStudio surfaces a node ONLY when its ``@configurable`` discovery category is one of these
# (a POSITIVE allowlist — see sampleflux/confluid taxonomy). This is what a canvas can build:
#   * ``op``      — concrete record→record ops (``RescaleOp``, ``SpectrogramOp`` …).
#   * ``source``  — concrete data sources (``HuggingFaceSource``, ``RFUAVSource`` …) AND
#                   ``DatasetSplit`` (a source-view: it yields records and wraps another source).
#   * ``engine``  — the generic composition primitives ``Flux`` / ``JointFlux``. They ARE buildable
#                   on a canvas: their source-typed constructor params render as WIRED sockets
#                   (``_build_source_node``) — ``Flux.source`` (single ``SAMPLEFLUX_SOURCE`` input) +
#                   ``Flux.ops`` (dynamic ``op_N`` ``SAMPLEFLUX_OP`` inputs); ``JointFlux.fluxes``
#                   (dynamic ``source_N`` ``SAMPLEFLUX_SOURCE`` inputs). So a Source/Op node's output
#                   (it already exposes itself as ``SAMPLEFLUX_SOURCE`` / ``SAMPLEFLUX_OP``) feeds them.
#   * ``sink``    — the SampleFlux storage sinks (``HDF5Sink`` / ``YoloSink`` / ``JsonSink`` …) that
#                   write a whole Flux (``write(record)`` / ``open`` / ``flush`` / ``close``). A sink
#                   is an object-member PRODUCER (``_build_object_node`` → an ``instance`` + ``class``
#                   output on a ROLE-qualified ``SAMPLEFLUX_OBJECT:sink`` wire, see ``_is_sink_class`` /
#                   ``_role_for_class``) that docks into a ``DatasetProcessor`` runnable node's
#                   ``sink`` slot — the same model/loss/logger → trainer relationship. The prediction
#                   sinks (``DetectionPredictionsSink`` / ``ClassificationPredictionsSink``,
#                   ``write(prediction, metadata)``) are a DIFFERENT contract and stay UNcategorised.
# Everything else is excluded by construction:
#   * the raw-callable op wrappers ``FilterOp`` / ``WrappedOp`` — bare ``@configurable``, so
#     UNCATEGORISED (neither ``op`` nor ``engine``): their constructor takes a raw Python callable,
#     nothing a widget can supply. Excluded as uncategorised, so even with ``"engine"`` allowed they
#     stay out (correct — they're not buildable).
#   * task ``dataset``s (``classification_dataset`` …) — a trainer/model-config concept (a
#     task-scoped, model-ready dataset slot), meaningful for navigaitor's config composition but
#     NOT for a per-sample SampleFlux canvas, which only needs a ``source`` to yield records.
#   * uncategorised module-level helper functions (no ``__confluid_category__``); an UNtagged sink
#     (a raw ``Storage`` subclass without ``category="sink"``) is likewise excluded.
# To surface another role add it here AND tag the classes.
_NODE_CATEGORIES = {"op", "model", "source", "engine", "sink", "value"}


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


# ALWAYS-kept base: PreviewImage + PreviewAny ("Preview as text"), the native viewers the
# Extract-from-Sample adapter feeds. The config file's ``keep_nodes:`` ADDS to these — the extra
# "appropriate defaults" ship in `config.default.yaml` (`_packaged_default_config_path`), installed
# with the package and used whenever the user hasn't provided their own config.
_DEFAULT_KEEP_NODES = ("PreviewImage", "PreviewAny")
# True ⇒ show ONLY Taidal nodes + the keep-list (hide built-ins AND other custom packs).
_ONLY_TAIDAL_NODES_ENV = "FLUXSTUDIO_ONLY_TAIDAL_NODES"
# Carries the config-file LOCATION (not its values) across the CLI→ComfyUI-subprocess boundary; the
# keep-list itself lives IN the file. Empty/unset ⇒ the default user path.
_CONFIG_PATH_ENV = "FLUXSTUDIO_CONFIG"


def palette_config_path() -> Path:
    """The USER palette config path: ``$FLUXSTUDIO_CONFIG`` (the CLI's ``--config <file>``) else the
    default ``~/.fluxstudio/config.yaml``. The shipped :func:`_packaged_default_config_path` is the
    fallback when this file is absent (so "appropriate defaults" apply when the user hasn't overridden).
    """
    env = os.environ.get(_CONFIG_PATH_ENV, "").strip()
    return Path(env) if env else Path.home() / ".fluxstudio" / "config.yaml"


def _packaged_default_config_path() -> Path:
    """The default palette config SHIPPED with the package (source control) — `fluxstudio/config.default.yaml`."""
    return Path(__file__).resolve().parent / "config.default.yaml"


def _load_palette_config() -> Dict[str, Any]:
    """Parse the palette config YAML → a dict, trying the user path THEN the shipped default.

    First readable file wins (user override → shipped defaults). Returns ``{}`` only if neither is
    readable; a malformed file logs a `warning` and falls through (never breaks node discovery).
    """
    for path in (palette_config_path(), _packaged_default_config_path()):
        if not path.exists():
            continue
        try:
            import yaml

            data = yaml.safe_load(path.read_text()) or {}
            if isinstance(data, dict):
                return data
        except Exception as exc:  # noqa: BLE001 - a malformed config must never break node discovery
            logger.warning(f"FluxStudio: ignoring unreadable palette config {path}: {exc}")
    return {}


def kept_default_node_names() -> Set[str]:
    """Default-node class names spared from pruning: PreviewImage/PreviewAny ∪ the config's ``keep_nodes``.

    ``PreviewImage`` / ``PreviewAny`` are ALWAYS kept (the native image / "Preview as text" viewers);
    the palette config's ``keep_nodes:`` list ADDS more. The config is the user's
    (:func:`palette_config_path` — ``$FLUXSTUDIO_CONFIG`` / ``--config`` else ``~/.fluxstudio/config.yaml``)
    if present, ELSE the SHIPPED `config.default.yaml` (installed from source control — so its
    appropriate defaults apply when the user hasn't overridden). Honored by BOTH the
    ``--no-default-nodes`` backend prune (``fluxstudio.__init__``) and the ``--only-taidal-nodes``
    frontend filter (``web/palette_filter.js`` via ``/taidal/palette_config``).
    """
    keep = _load_palette_config().get("keep_nodes")
    extra = {str(n).strip() for n in keep if str(n).strip()} if isinstance(keep, list) else set()
    return set(_DEFAULT_KEEP_NODES) | extra


def only_taidal_nodes() -> bool:
    """True when the CLI asked (via ``--only-taidal-nodes``) to show ONLY Taidal nodes + the keep-list.

    Hides EVERY non-Taidal node — ComfyUI built-ins AND other third-party custom packs — from the
    palette. Read from :data:`_ONLY_TAIDAL_NODES_ENV`; drives the post-load frontend filter
    (``web/palette_filter.js``). Stronger than :func:`hide_default_comfy_nodes`; the CLI sets both env
    vars together so the built-ins are also backend-pruned and the palette roots.
    """
    return os.environ.get(_ONLY_TAIDAL_NODES_ENV, "").strip().lower() in _TRUTHY


def node_root() -> str:
    """The Taidal palette root prefix: ``""`` (root-level) when default nodes are hidden, else ``"Taidal"``.

    When ComfyUI's own nodes are hidden there is nothing to disambiguate the Taidal nodes from, so
    they read more naturally at the palette root (``SampleFlux/Op`` rather than ``Taidal/SampleFlux/Op``).
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


def _is_melody_dataset_id_param(param_name: str, owner: Optional[Callable]) -> bool:
    """True when ``param_name`` is the dataset-id arg of a Melody/MAF dataset value node.

    Gated on the ``Melody`` class-name convention (matches the bridged ``HeliosMelodyDataset``
    value-producer) — a naming rule, not a per-node special-case — so a like-named ``name`` param
    on an unrelated class stays a plain STRING widget.
    """
    return (
        param_name in _MELODY_DATASET_ID_PARAMS
        and owner is not None
        and inspect.isclass(owner)
        and "Melody" in owner.__name__
    )


def _is_hf_feature_param(param_name: str, owner: Optional[Callable]) -> bool:
    """True when ``param_name`` is a feature-selector arg of a HuggingFace dataset source.

    These (``input_feature`` / ``target_feature``) name a dataset *column* mapped onto a
    record key. Gated on the SAME ``HuggingFace`` class-name convention as
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
    annotation = _strip_annotated(annotation)  # Mandatory[Literal[...]] / NoBroadcast[Literal[...]]
    if get_origin(annotation) is Literal:
        return list(get_args(annotation))
    if get_origin(annotation) is Union:  # Optional[Literal[...]] / Literal[...] | None / marker Fluid arms
        for arg in get_args(annotation):
            if get_origin(_strip_annotated(arg)) is Literal:
                return list(get_args(_strip_annotated(arg)))
    return None


def _as_float(value: Any) -> Optional[float]:
    """Numeric annotated_types bound -> float; non-numeric (rare custom bound) -> None."""
    return float(value) if isinstance(value, (int, float)) else None


def _interval_bounds(annotation: Any) -> Tuple[Optional[float], Optional[float]]:
    """Extract ``(min, max)`` from PEP-593 ``Annotated[..., Interval/Ge/Le/Gt/Lt]`` metadata.

    The range-mark convention: a numeric param annotated e.g.
    ``Annotated[float, Interval(ge=-200.0, le=50.0)]`` renders as a BOUNDED widget
    (this helper feeds min/max), and ``confluid.to_pydantic`` carries the same mark
    into navigaitor's JSON schema — one mark, every GUI. ``Optional``/``Union`` arms
    are unwrapped; an unmarked annotation returns ``(None, None)`` so the full-range
    ``DEFAULT_WIDGET_CONFIG`` bounds stay in effect. Marks are UX/schema metadata —
    the constructor remains the validation authority.
    """
    origin = get_origin(annotation)
    if origin is Union:
        for arm in get_args(annotation):
            lo, hi = _interval_bounds(arm)
            if (lo, hi) != (None, None):
                return lo, hi
        return None, None
    if origin is not Annotated:
        return None, None
    lo = hi = None
    for meta in get_args(annotation)[1:]:
        if isinstance(meta, Interval):
            lo = _as_float(meta.ge if meta.ge is not None else meta.gt)
            hi = _as_float(meta.le if meta.le is not None else meta.lt)
        elif isinstance(meta, Ge):
            lo = _as_float(meta.ge)
        elif isinstance(meta, Gt):
            lo = _as_float(meta.gt)
        elif isinstance(meta, Le):
            hi = _as_float(meta.le)
        elif isinstance(meta, Lt):
            hi = _as_float(meta.lt)
    if (lo, hi) == (None, None):
        # No range mark in THIS layer's metadata — a marker alias (Mandatory[DbPower]
        # == Annotated[Union[Annotated[float, Interval], Fluid], marker]) prevents
        # Annotated flattening, so the mark sits one payload level down.
        return _interval_bounds(get_args(annotation)[0])
    return lo, hi


def _range_tuple_type(annotation: Any) -> Optional[str]:
    """If ``annotation`` is a ``(min, max)`` numeric TUPLE → its RANGE widget type, else ``None``.

    A RANGE pair (the ``__lo``/``__hi`` widget pair) is a TOP-LEVEL ``Tuple`` (after unwrapping
    ``Annotated`` / ``Optional`` / ``Union``) that is EITHER exactly two numeric elements
    (``Tuple[int, int]`` / ``Tuple[float, float]`` — the ``(lo, hi)`` pair) OR a variable-length
    ``Tuple[int, ...]`` / ``Tuple[float, ...]``. It deliberately does NOT match:

    * a ``List`` / ``Sequence`` OF tuples (``OverlayBoxes.boxes: List[Tuple[int, int, int, int]]``) —
      the nested ``tuple``/``int`` in the type's repr is what fooled the old ``"tuple" in str(anno)``
      substring heuristic into a bogus ``boxes__lo`` / ``boxes__hi`` pair; nor
    * a FIXED-arity tuple with a length other than 2 (a 3-tuple ``Color = Tuple[int, int, int]``, a
      4-tuple ``Box``) — a colour / box is not a min/max range.

    Structural (``get_origin`` / ``get_args``), never a string scan — that is the whole point.
    """
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    for arm in _flatten_union(annotation):
        arm = _strip_annotated(arm)
        if get_origin(arm) is not tuple and arm is not tuple:
            continue
        args = get_args(arm)
        if len(args) == 2 and args[1] is Ellipsis:  # Tuple[T, ...] — variable-length
            elems: Tuple[Any, ...] = (args[0],)
        elif len(args) == 2:  # Tuple[T, T] — the (lo, hi) pair
            elems = args
        else:  # 0 / 1 / 3+ fixed elements (a colour, a box) → NOT a range pair
            continue
        if all(e is float for e in elems):
            return "RANGE_FLOAT"
        if all(e is int for e in elems):
            return "RANGE_INT"
    return None


def _is_nonrange_container(annotation: Any) -> bool:
    """True if ``annotation`` is a list / tuple / set / dict / ``Sequence`` container.

    Routes a NON-range container (``List[Box]``, a 3-tuple ``Color``, ``List[int]``, ``Dict[...]``) to a
    STRING widget instead of letting the bare ``int`` / ``float`` string-heuristics in :func:`_map_type`
    mis-map it to a single scalar widget because its ELEMENT type's repr contains ``"int"`` / ``"float"``.
    A genuine 2-element numeric Tuple range is matched by :func:`_range_tuple_type` FIRST, so it never
    reaches here. ``str`` / ``bytes`` are scalars, not containers.
    """
    # A parameterized typing container (``List[X]`` / ``Tuple[...]`` / ``Dict[...]`` / ``Sequence[X]``)
    # has a CONTAINER ORIGIN — restricted to the standard container abcs, NOT every ``Iterable`` (so a
    # concrete iterable class used bare as an annotation, e.g. ``datasets.Dataset``, falls through to its
    # own type heuristic, here ``HF_DATASET``); ``str``/``bytes`` (origin ``None``) are scalars.
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return any(_has_container_origin(_strip_annotated(arm)) for arm in _flatten_union(annotation))


def _fixed_numeric_tuple_cast(annotation: Any) -> Optional[type]:
    """If ``annotation`` is a TOP-LEVEL numeric ``Tuple`` (a ``Color`` / ``Box`` — NOT a ``List`` of
    tuples), return the element cast (``int`` / ``float``) for decoding its STRING widget, else ``None``.

    The 2-element / variable-length numeric RANGE tuples NEVER reach :func:`_coerce_kwargs` (they ride
    the ``__lo`` / ``__hi`` widget pair, in ``skip_keys``), so any numeric tuple seen there is a
    non-range FIXED tuple (a 3-tuple colour, a 4-tuple box) that :func:`_map_type` routed to a STRING
    widget — its ``"(255, 0, 0)"`` value must decode back to a real ``(255, 0, 0)`` tuple, not crash the
    bare-``int`` cast. A ``List[Tuple[...]]`` has a top-level ``list`` origin → returns ``None`` (it stays
    a list, decoded by the list branch).
    """
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    for arm in _flatten_union(annotation):
        arm = _strip_annotated(arm)
        if get_origin(arm) is not tuple and arm is not tuple:
            continue
        args = get_args(arm)
        # A 2-element OR variable-length numeric tuple is a RANGE pair (``__lo``/``__hi`` widgets, in
        # ``skip_keys``) — it never reaches ``_coerce_kwargs``, so it is NOT a fixed tuple to decode here
        # (mirrors :func:`_range_tuple_type`). Only a fixed arity != 2 (a 3-tuple colour, a 4-tuple box).
        if not args or Ellipsis in args or len(args) == 2:
            continue
        if all(a is float for a in args):
            return float
        if all(a is int for a in args):
            return int
    return None


# Namespace for resolving a STRINGIZED / forward-ref annotation (``from __future__ import annotations``)
# to a real typing object, so the classification below can inspect it STRUCTURALLY instead of scanning
# its repr. Holds the ``typing`` constructs plus the domain types this module classifies. An annotation
# referencing a name outside it (a custom op alias) is left as a string → the STRING fallback. The real
# entry point (:func:`_build_config_inputs`) resolves with ``get_type_hints`` against the owner's own
# namespace first; this is the best-effort safety net for direct / odd calls.
_ANNOTATION_NS: Dict[str, Any] = {
    **vars(typing),
    "datasets": datasets,
    "Path": Path,
    # The record carrier: ``Record`` is the current spelling; ``Sample`` is kept so a stringized
    # legacy annotation still resolves to the same alias (harmless — both mean "the sample dict").
    "Record": _Record,
    "Sample": _Record,
    "NoneType": type(None),
}


def _resolve_annotation(annotation: Any) -> Any:
    """Resolve a stringized / ``ForwardRef`` annotation to a real typing object when possible."""
    raw: Any = annotation
    if type(annotation).__name__ == "ForwardRef":
        raw = getattr(annotation, "__forward_arg__", annotation)
    if isinstance(raw, str):
        try:
            return eval(raw, dict(_ANNOTATION_NS))  # noqa: S307 — controlled typing-only namespace
        except Exception:  # noqa: BLE001 — an unresolvable name keeps the string → STRING fallback
            return annotation
    return annotation


def _type_mentions(annotation: Any, target: type) -> bool:
    """True if ``target`` IS ``annotation`` or appears anywhere in its type arguments (recursively).

    Structural replacement for ``target.__name__ in str(annotation)`` — matches a bare ``Path``,
    ``Optional[Path]``, ``Union[…, datasets.Dataset]`` by IDENTITY, not repr.
    """
    if annotation is target:
        return True
    return any(arg is target or _type_mentions(arg, target) for arg in get_args(annotation))


def _mentions_record(annotation: Any) -> bool:
    """True if ``annotation`` IS the sample-record alias (``Record`` == ``Dict[str, Any]``) or
    carries it anywhere in its type arguments (``Optional[Record]`` / ``Iterator[Record]``).

    The record carrier is a typing ALIAS, not a class, so the identity test used for real types
    (:func:`_type_mentions`) cannot see it — generic-alias EQUALITY is the structural match. A
    plain ``Dict[str, Any]`` annotation is indistinguishable from ``Record`` at runtime (the alias
    erases); ops annotate their carrier with ``Record``, and the config-dict params on the palette
    surface use narrower value types (``Dict[str, str]``, ``Dict[int, str]``), so alias equality
    is the record signal in practice.
    """
    try:
        if annotation == _Record:
            return True
    except Exception:  # noqa: BLE001 - exotic annotation objects may not support ==
        return False
    return any(_mentions_record(arg) for arg in get_args(annotation))


def _strip_annotated(t: Any) -> Any:
    """``Annotated[T, …]`` → ``T`` (one level); anything else unchanged.

    Applied per Union-arm so a nested ``Optional[Annotated[float, Interval(...)]]`` / ``Union[…,
    Annotated[Tuple[int,int], …]]`` classifies by the real underlying type, not the ``Annotated`` wrapper.
    """
    return get_args(t)[0] if get_origin(t) is Annotated else t


_CONTAINER_ORIGINS = (list, tuple, set, frozenset, dict)


def _has_container_origin(t: Any) -> bool:
    """True if ``t`` is a typing container (``List``/``Tuple``/``Dict``/``Set``/``Sequence``/…) or a
    bare builtin container — by ORIGIN, not repr. ``str``/``bytes`` (origin ``None``) are NOT containers."""
    origin = get_origin(t)
    if origin in _CONTAINER_ORIGINS:
        return True
    if isinstance(origin, type) and issubclass(origin, (cabc.Sequence, cabc.Set, cabc.Mapping)):
        return True
    return t in _CONTAINER_ORIGINS


def _is_nested_container(t: Any) -> bool:
    """True if ``t`` is a container whose ELEMENT type is itself a container — a ``List`` of tuples
    (boxes), a ``List`` of lists, a ``Dict`` with container values, a ``Tuple`` of containers.

    These can't be sensibly typed into a single widget. A FLAT container of scalars (``List[str]`` /
    ``List[int]``, a fixed numeric tuple ``Color``) is NOT nested — it stays a comma/CSV-typeable widget.
    """
    if not _has_container_origin(t):
        return False
    args = get_args(t)
    # ``Dict[K, V]`` nests only on its VALUE type; other containers on any element.
    elems = [args[1]] if (get_origin(t) is dict and len(args) == 2) else [a for a in args if a is not Ellipsis]
    return any(_has_container_origin(_strip_annotated(e)) for e in elems)


def _needs_value_socket(annotation: Any) -> bool:
    """True if a param's type can't be a sensible widget and should ride a wired ``SAMPLEFLUX_VALUE``
    input socket (fed by the **Value** node) — i.e. ANY Union arm is a NESTED container
    (:func:`_is_nested_container`). A scalar / flat container / numeric tuple stays a widget."""
    annotation = _resolve_annotation(annotation)
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return any(_is_nested_container(_strip_annotated(arm)) for arm in _flatten_union(annotation))


def _map_type(param_name: str, annotation: Any) -> str:
    """Map a Python type annotation to a ComfyUI socket/widget type string — STRUCTURALLY.

    Inspects the resolved type via ``typing`` introspection (``get_origin`` / ``get_args`` / identity),
    NOT substring matching on ``str(annotation)``. A stringized / forward-ref annotation is resolved
    first (:func:`_resolve_annotation`). The ONLY name-based rule is the deliberate PATH affordance (a
    param whose NAME signals a directory → a picker), which is a UX choice, not a type inference.
    """
    annotation = _resolve_annotation(annotation)

    # Annotated[T, …] → classify by T (the metadata, e.g. Interval, is read by _interval_bounds).
    if get_origin(annotation) is Annotated:
        return _map_type(param_name, get_args(annotation)[0])

    # A sample record (bare ``Record`` / ``Optional[Record]`` / ``Iterator[Record]``) → the sample
    # wire. ``Record`` erases to ``Dict[str, Any]`` at runtime, so the match is alias EQUALITY
    # (see :func:`_mentions_record`), not class identity. MUST precede the container branch —
    # a record IS a dict, but it rides the wire, never a STRING widget.
    if _mentions_record(annotation):
        return SAMPLE_TYPE
    # HuggingFace dataset → the HF_DATASET wire.
    if _type_mentions(annotation, datasets.Dataset):
        return "HF_DATASET"
    # A path: the annotation IS pathlib.Path, OR the param NAME signals a path/dir → directory picker.
    if _type_mentions(annotation, Path) or any(seg in param_name.lower() for seg in _PATH_NAME_SEGMENTS):
        return "PATH"

    # A (min, max) numeric RANGE tuple (2-element OR variadic numeric tuple) → the __lo/__hi pair.
    range_type = _range_tuple_type(annotation)
    if range_type is not None:
        return range_type
    # A non-range container (List/fixed-!=2-tuple/Dict/Sequence) → STRING (the ctor / _coerce_kwargs
    # parse it). MUST precede the scalar resolution: a container whose ELEMENT is numeric must not
    # collapse to a single scalar widget.
    if _is_nonrange_container(annotation):
        return "STRING"

    # A scalar — resolve the NON-None arms of a Union/Optional structurally (a bare type is one arm),
    # unwrapping a nested ``Annotated`` per arm (``Optional[Annotated[float, Interval]]`` → ``float``).
    arms = [_strip_annotated(a) for a in _flatten_union(annotation)]
    if any(a is bool for a in arms):
        return "BOOLEAN"
    has_int = any(a is int for a in arms)
    has_float = any(a is float for a in arms)
    if has_int and any(a is str for a in arms):
        return "STRING"  # Union[int, str] (SampleSpec) — STRING covers "1024" AND "1ms"
    if has_int and not has_float:
        return "INT"
    if has_float:
        return "FLOAT"
    return "STRING"  # str / unknown / unresolved


# The param-NAME segments that signal a directory/file path → a directory-picker widget
# (``web/directory_chooser.js``). This is a deliberate UX affordance keyed on the NAME, NOT a type
# inference (a ``str`` named ``output_dir`` SHOULD get the picker), so it stays name-based by design.
_PATH_NAME_SEGMENTS = ("path", "dir", "root", "folder")


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

    # Melody/MAF dataset identifier: a free-text STRING (the platform dataset name) tagged for the
    # platform-dataset picker (web/melody_dataset_chooser.js) — selecting one fills the field, which
    # stays editable. The downloaded directory is exposed on this node's ``directory`` @output.
    if _is_melody_dataset_id_param(param.name, owner):
        default = param.default if has_default and isinstance(param.default, str) else ""
        return "STRING", {"default": default, "taidal_widget": "MELODY_DATASET_ID"}

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

    # Nullable numeric (e.g. ``Optional[int] = None``, ``Optional[float] = None`` like
    # ``NoiseFloorOp.noise_power_db`` / ``HuggingFaceSource.count``):
    # INT/FLOAT widgets can't represent None — 0 is a distinct, often-breaking value — so the
    # inline widget stays a blank STRING (``"" → None`` = use the constructor default / stamped
    # metadata fallback; ``"5.5" → cast`` in ``_coerce_kwargs``). The ``taidal_widget`` tag lets
    # ``web/nullable_numeric_socket.js`` RETYPE the socket created by ComfyUI's "Convert to input"
    # from STRING to the real ``taidal_numeric`` (FLOAT/INT) — so a numeric output (a Math result,
    # another op's @output) can wire in (and the export compilers turn the wire into a ConfigureOp).
    # (The str-capable ``Optional[Union[float, int, str]] = None`` params — ``ThresholdOp.low_level``
    # with its ``"{meta}"`` expression support — map to STRING above and never reach here, so their
    # blank-STRING-and-stays-STRING behaviour is unchanged.)
    #
    # The RANGE_* arms cover an Optional param whose annotation ALSO has a (variadic) tuple arm —
    # e.g. helios ``Max.axis: int | Tuple[int, ...] | None`` (reduce one axis, several axes, or ALL
    # when None). ``_map_type`` returns RANGE_INT for the ``Tuple[int, ...]`` arm (→ a forced
    # ``axis__lo``/``axis__hi`` pair that can't be None), but a ``None`` default means "flexible
    # optional": render the same blank-STRING nullable scalar taidal gives ``Optional[int]/[float]``
    # (``"" → None`` = the constructor default / "all axes"; ``"0" → 0``). The rarer multi-axis tuple
    # form isn't expressible on a single widget — wire a value or pass it in the constructor. A
    # tuple param with a CONCRETE default (``model_order=(3, 5)``, ``target_length: int | Tuple[int,
    # int] = 0``) is NOT None-defaulted, so it keeps its RANGE ``__lo``/``__hi`` widgets.
    if param.default is None and comfy_type in ("INT", "FLOAT", "RANGE_INT", "RANGE_FLOAT"):
        numeric = "FLOAT" if comfy_type.endswith("FLOAT") else "INT"
        return "STRING", {
            "default": "",
            "multiline": False,
            "taidal_widget": "NULLABLE_NUMERIC",
            "taidal_numeric": numeric,
        }

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
        elif comfy_type == "FLOAT" and isinstance(param.default, (int, float)):
            config["default"] = float(param.default)
        elif comfy_type == "BOOLEAN" and isinstance(param.default, bool):
            config["default"] = param.default
        elif comfy_type == "STRING":
            config["default"] = str(param.default)
    if comfy_type in ("INT", "FLOAT"):
        # PEP-593 range mark narrows the full-range default bounds.
        lo, hi = _interval_bounds(param.annotation)
        if lo is not None:
            config["min"] = int(lo) if comfy_type == "INT" else lo
        if hi is not None:
            config["max"] = int(hi) if comfy_type == "INT" else hi
    return comfy_type, config


def _resolved_param_hints(func: Callable) -> Dict[str, Any]:
    """``get_type_hints`` for a callable's params, keyed by name — the PROPER resolution of a
    ``from __future__ import annotations`` / forward-ref STRING annotation against the owner's own
    namespace (so the structural classifiers see a real type, not a string). ``{}`` on any failure
    (an unresolvable forward ref / an odd callable); the caller then keeps the raw annotation, which
    :func:`_resolve_annotation` best-effort resolves against the typing namespace.
    """
    target = func.__init__ if inspect.isclass(func) else func
    try:
        return get_type_hints(target, include_extras=True)
    except Exception:  # noqa: BLE001 — unresolvable forward ref / builtin → fall back to raw annotations
        return {}


def _build_config_inputs(
    func: Callable,
    skip: Optional[AbstractSet[str]] = None,
    strict_typing: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Tuple[str, str]]]:
    """Build ``(required_widgets, nullable_optional_sockets, split_map)`` for a callable's params.

    ``required_widgets`` — the normal ``required`` section of ``INPUT_TYPES``: typed widgets the user
    fills directly on the node (strings, dropdowns, INT/FLOAT sliders, …).

    ``nullable_optional_sockets`` — params typed ``Optional[int/float] = None``: rendered as FLOAT/INT
    *optional input sockets* so the user can wire a compatible output (e.g. a Float Math result) into
    them.  When the socket is left unconnected the param is absent from ``execute`` kwargs and the
    constructor default (``None``) applies — semantically correct for "disabled bound", "no limit", etc.

    ``split_map`` — when ``strict_typing=True``, maps each ``Union[int, str]`` param name to its two
    optional socket keys ``(int_key, str_key)`` so the generated node's execute can merge them back
    into the canonical param name before constructing the op.

    Each widget's ``tooltip`` is sourced from the callable's docstring ``Args:``
    block (via :func:`confluid.parse_param_docs`) — the same per-parameter help
    navigaitor surfaces in its form-spec. Document a parameter once in the class
    docstring and it appears as a hover tooltip here; an undocumented parameter
    simply gets no tooltip (fix it upstream, never hand-write one here).

    ``skip`` names parameters to omit from both sets — used by op nodes to
    exclude the op-list param (it becomes a ``SAMPLEFLUX_OP`` socket, not a widget).
    """
    try:
        sig = inspect.signature(func)
    except (
        ValueError,
        TypeError,
    ):  # pragma: no cover - defensive: inspect rejects rare builtins
        return {}, {}, {}
    param_docs = parse_param_docs(func)
    hints = _resolved_param_hints(func)  # resolve `from __future__ import annotations` string annotations
    required: Dict[str, Any] = {}
    nullable_optionals: Dict[str, Any] = {}
    split_map: Dict[str, Tuple[str, str]] = {}
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
        # Resolve a STRINGIZED annotation to the real type (proper structural inspection downstream).
        if isinstance(param.annotation, str) and name in hints:
            param = param.replace(annotation=hints[name])
        # A NESTED-container type (a list of boxes, a dict of lists) can't be typed into a single widget
        # → an OPTIONAL ``SAMPLEFLUX_VALUE`` input socket fed by the Value node. Optional so an unwired
        # socket keeps the constructor default (``boxes=[]``); the wired Python value passes through
        # ``_coerce_kwargs`` unchanged (it is not a string).
        if _needs_value_socket(param.annotation):
            doc = param_docs.get(name)
            nullable_optionals[name] = (VALUE_TYPE, {"tooltip": doc}) if doc else (VALUE_TYPE,)
            continue
        comfy_type, config = _widget_for(param, owner=func)
        is_nullable_opt = bool(config.pop("_nullable_optional", False))
        doc = param_docs.get(name)

        # Dual-socket override: Union[int, str] params get two optional sockets when
        # strict_typing=True — {name}_samples (INT) and {name}_duration (STRING).
        # The execute wrapper merges whichever is filled back into the canonical param name.
        if strict_typing:
            origin = get_origin(param.annotation)
            args = get_args(param.annotation)
            if origin is Union and int in args and str in args:
                int_key = f"{name}_samples"
                str_key = f"{name}_duration"
                int_cfg: Dict[str, Any] = {"default": 0, "min": 0, "max": 2**31 - 1, "step": 1}
                str_cfg: Dict[str, Any] = {"default": "", "multiline": False}
                if doc:
                    int_cfg["tooltip"] = doc
                    str_cfg["tooltip"] = doc
                nullable_optionals[int_key] = ("INT", int_cfg)
                nullable_optionals[str_key] = ("STRING", str_cfg)
                split_map[name] = (int_key, str_key)
                continue

        # Tuple[T, T] range params → two native FLOAT/INT widgets ({name}__lo / {name}__hi).
        # _coerce_kwargs reconstructs the tuple before passing to the constructor.
        if comfy_type in ("RANGE_FLOAT", "RANGE_INT"):
            native = "FLOAT" if comfy_type == "RANGE_FLOAT" else "INT"
            cast_ = float if comfy_type == "RANGE_FLOAT" else int
            default_lo: Any = cast_(0)
            default_hi: Any = cast_(1)
            if (
                param.default not in (inspect.Parameter.empty, None)
                and isinstance(param.default, (tuple, list))
                and len(param.default) == 2
            ):
                default_lo = cast_(param.default[0])
                default_hi = cast_(param.default[1])
            # Inherit the full-range min/max (+ step) — dB ranges are routinely negative
            # (TimeVaryingNoiseOp.noise_power_low = (-80, -60)) and frequency ranges exceed
            # the frontend's implicit 2048 cap.
            lo_cfg: Dict[str, Any] = {**DEFAULT_WIDGET_CONFIG[native], "default": default_lo}
            hi_cfg: Dict[str, Any] = {**DEFAULT_WIDGET_CONFIG[native], "default": default_hi}
            # A PEP-593 range mark on the tuple (Annotated[Tuple[float, float], Interval(...)])
            # bounds BOTH endpoint widgets.
            bound_lo, bound_hi = _interval_bounds(param.annotation)
            if bound_lo is not None:
                lo_cfg["min"] = hi_cfg["min"] = cast_(bound_lo)
            if bound_hi is not None:
                lo_cfg["max"] = hi_cfg["max"] = cast_(bound_hi)
            if doc:
                lo_cfg["tooltip"] = doc
                hi_cfg["tooltip"] = doc
            required[f"{name}__lo"] = (native, lo_cfg)
            required[f"{name}__hi"] = (native, hi_cfg)
            continue

        if doc:
            config = {**config, "tooltip": doc}
        entry = (comfy_type,) if not config else (comfy_type, config)
        (nullable_optionals if is_nullable_opt else required)[name] = entry
    return required, nullable_optionals, split_map


def _has_bare_scalar_union_arm(annotation: Any) -> bool:
    """True if ``annotation`` is a ``Union`` (or ``X | Y``) with a top-level ``str``/``int`` arm.

    A param like ``pytorch_lightning.Trainer.devices: list[int] | str | int`` accepts a BARE scalar
    (``"auto"``, ``1``) AND the library parses the string form itself (``"auto"``, the comma-string
    ``"1,2,3"``). So a STRING widget value is that scalar arm and must pass through ``_coerce_kwargs``
    VERBATIM — never be comma-split into a list (which yielded ``["auto"]``, a "sequence of str" that
    Lightning rejects, the canonical "the default graph won't even build" footgun). A pure
    ``List[str]`` / ``Optional[List[str]]`` has NO such arm (its only non-``None`` member is the list),
    so it still splits. Inspect the Union ARGS, not the annotation string — ``str(List[str])`` itself
    contains the substring ``"str"``.
    """
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    if get_origin(annotation) in (Union, _types.UnionType):
        return any(arg in (str, int) for arg in get_args(annotation))
    return False


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

    # Reconstruct Tuple[T, T] params from their __lo / __hi widget-pair kwargs.
    lo_map = {k[:-4]: v for k, v in kwargs.items() if k.endswith("__lo")}
    hi_map = {k[:-4]: v for k, v in kwargs.items() if k.endswith("__hi")}
    paired = set(lo_map) & set(hi_map)
    skip_keys = {f"{b}__lo" for b in paired} | {f"{b}__hi" for b in paired}
    for base in paired:
        p = sig.parameters.get(base)
        ann_low = str(p.annotation).lower() if p is not None else ""
        lo_v, hi_v = lo_map[base], hi_map[base]
        if "int" in ann_low and "float" not in ann_low:
            out[base] = (int(lo_v), int(hi_v))
        else:
            out[base] = (float(lo_v), float(hi_v))

    for key, value in kwargs.items():
        if key in skip_keys:
            continue
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
            # A JSON-encoded container arriving on a STRING input socket: a ``Dict[...]`` or
            # ``Tuple[str, ...]`` param wired from another node's container ``@output`` (which rides a
            # STRING socket, json.dumps-encoded by :func:`_coerce_output_value`) or typed by hand.
            # Decode it back to the real container. MUST precede the list/sequence comma-split below
            # (kept for ``List``/``Sequence`` params); disjoint from the range-tuple path
            # (``Tuple[float,float]``/``Tuple[int,int]`` use ``__lo``/``__hi`` widgets whose keys are in
            # ``skip_keys`` and never reach this loop, and a variable-length ``Tuple[int, ...]`` carries
            # "int" so it stays a RANGE pair — hence the ``not int / not float`` guard here).
            if "dict" in ann_low:
                if stripped.startswith("{"):
                    try:
                        out[key] = json.loads(stripped)
                        continue
                    except (ValueError, TypeError):
                        pass
                    # Not strict JSON — accept a Python dict LITERAL too: `{0:'BG',1:'5GNR'}` (int
                    # keys + single quotes) is natural to type for a `Dict[int, str]` param like
                    # YoloSink.class_names but is invalid JSON. ast.literal_eval is safe (literals
                    # only, no code execution) and yields the real int-keyed dict the ctor wants.
                    try:
                        parsed_dict = ast.literal_eval(stripped)
                        if isinstance(parsed_dict, dict):
                            out[key] = parsed_dict
                            continue
                    except (ValueError, SyntaxError, TypeError):
                        pass
                out[key] = value  # not JSON/literal — leave the raw string for the constructor to handle
                continue
            # A structured/COMPOUND container — a LIST whose elements are themselves tuples/pairs
            # (or a Union of those), e.g. ``Config.keys: str | list[str | tuple[str, str]]`` (a
            # rename map: a bare key is src==dst, a ``(src, dst)`` pair renames). ComfyUI can only
            # render it as a STRING, and the flat ``Tuple``/``List`` branches below can't reconstruct
            # a list OF tuples — so this generic branch (keyed off the annotation SHAPE — ``list`` AND
            # ``tuple`` — never a class name) accepts EITHER a Python/JSON literal (faithful to any
            # nesting) OR a friendly comma list with ``src:dst`` renames. It MUST precede the flat
            # ``Tuple`` branch (which also matches "tuple") and the flat ``List`` branch.
            if "list" in ann_low and "tuple" in ann_low and "float" not in ann_low and "int" not in ann_low:
                # 1) A literal container — ``[("a", "b"), "c"]`` (Python) or ``[["a", "b"], "c"]``
                #    (JSON). ``ast`` first (handles the tuples/parens JSON can't); a JSON inner list
                #    is normalised to a tuple so a pair stays a pair.
                if stripped[:1] in "[(":
                    for _parse in (ast.literal_eval, json.loads):
                        try:
                            parsed = _parse(stripped)
                        except (ValueError, SyntaxError, TypeError):
                            continue
                        if isinstance(parsed, (list, tuple)):
                            out[key] = [tuple(e) if isinstance(e, (list, tuple)) else e for e in parsed]
                            break
                    if key in out:
                        continue
                # 2) Friendly comma list: ``"snr_db, noise_power_db:nf"`` -> ``["snr_db",
                #    ("noise_power_db", "nf")]``. A bare item stays a string (src==dst); an item with
                #    ``:`` becomes a ``(src, dst)`` rename pair. (Also the fallback for a malformed literal.)
                items: List[Any] = []
                for raw_item in stripped.split(","):
                    item = raw_item.strip()
                    if not item:
                        continue
                    if ":" in item:
                        src, dst = (part.strip() for part in item.split(":", 1))
                        items.append((src, dst))
                    else:
                        items.append(item)
                out[key] = items
                continue
            # A NON-range NUMERIC tuple (``Color = Tuple[int, int, int]`` / ``Box``) — a STRING widget
            # since :func:`_map_type` ranges ONLY a 2-element/variable numeric tuple. Decode
            # ``"(255, 0, 0)"`` / ``"255,0,0"`` / ``"[255,0,0]"`` into a real tuple, casting each element.
            # Structural (``_fixed_numeric_tuple_cast``), so a ``List[Tuple[int,...]]`` (boxes) is NOT
            # caught here (top-level ``list`` → the list branch below). MUST precede the bare-``int``/
            # ``float`` casts, which would otherwise ``int("(255, 0, 0)")`` and crash.
            tuple_cast = _fixed_numeric_tuple_cast(param.annotation) if param is not None else None
            if tuple_cast is not None:
                try:
                    out[key] = tuple(tuple_cast(p.strip()) for p in stripped.strip("()[] ").split(",") if p.strip())
                except (ValueError, TypeError):
                    out[key] = value
                continue
            if "tuple" in ann_low and "float" not in ann_low and "int" not in ann_low:
                if stripped.startswith("["):
                    try:
                        parsed = json.loads(stripped)
                        out[key] = tuple(parsed) if isinstance(parsed, list) else (parsed,)
                        continue
                    except (ValueError, TypeError):
                        pass
                if stripped.startswith("("):
                    # A Python TUPLE LITERAL — the empty-tuple default ``()`` (the repr of an empty
                    # ``Tuple[str, ...]`` param) AND the docstring-documented ``("IQImbalanceOp",
                    # "QuantizeOp")`` form. Without this, ``"()"`` comma-splits to ``("()",)`` (a
                    # one-element tuple of the literal string) — TRUTHY, so a helios
                    # ``ImpairmentsChain.tx_disabled`` widget left at its ``()`` default would OVERRIDE
                    # the metadata config stamped by ``ImpairmentsTxConfig`` (``self.tx_disabled or
                    # metadata[...]``), silently disabling the whole tx/rx config mechanism. ``ast``
                    # is literals-only (safe). Falls through to the comma-split on a non-literal.
                    try:
                        parsed = ast.literal_eval(stripped)
                        if isinstance(parsed, (tuple, list)):
                            out[key] = tuple(parsed)
                            continue
                    except (ValueError, SyntaxError, TypeError):
                        pass
                # A hand-typed comma-separated value (e.g. ``QuantizeOp, SpursOp``) → a tuple of strings.
                out[key] = tuple(item.strip() for item in stripped.split(",") if item.strip())
                continue
            # A pure ``List``/``Sequence`` param: comma-split the STRING widget into a list. But a
            # Union with a bare ``str``/``int`` arm (``devices: list[int] | str | int``) takes the
            # scalar VERBATIM — the library parses ``"auto"`` / ``"1,2,3"`` itself; splitting it into
            # ``["auto"]`` is the "sequence of str" Lightning rejects (see _has_bare_scalar_union_arm).
            if ("list" in ann_low or "sequence" in ann_low) and not _has_bare_scalar_union_arm(
                param.annotation if param is not None else None
            ):
                # A Python/JSON list LITERAL first — the ``[]`` empty default (the repr of an empty
                # ``List`` param like ``OverlayBoxes.boxes``) and an explicit ``[(20, 10, 80, 50)]`` box
                # list; a comma-split of ``"[]"`` would wrongly yield ``["[]"]``. ``ast`` (tuples/parens)
                # then ``json``; falls back to the comma-split for a plain ``"a, b, c"``.
                if stripped[:1] in "[(":
                    for _parse in (ast.literal_eval, json.loads):
                        try:
                            parsed = _parse(stripped)
                        except (ValueError, SyntaxError, TypeError):
                            continue
                        if isinstance(parsed, (list, tuple)):
                            out[key] = [tuple(e) if isinstance(e, (list, tuple)) else e for e in parsed]
                            break
                    if key in out:
                        continue
                out[key] = [item.strip() for item in stripped.split(",") if item.strip()]
                continue
            # Cast a filled nullable-bool 3-state combo value (``true``/``false``) back to a real bool.
            if "bool" in ann_low:
                out[key] = stripped.lower() in ("true", "1", "yes", "on")
                continue
            # Cast a filled nullable-numeric STRING widget back to its number.
            # Skip for Union[int, str] (SampleSpec) — the value may be a duration like "1ms";
            # resolve_sample_count handles both forms at call time.
            if "int" in ann_low and "float" not in ann_low and "str" not in ann_low:
                out[key] = int(stripped)
                continue
            # Same guard for float: a ``Union[float, int, str]`` param (e.g. ThresholdOp's
            # ``low_level``) accepts call-time expression strings like "{meta_key}" — leave
            # the string for the constructor; only pure-numeric annotations cast here.
            if "float" in ann_low and "str" not in ann_low:
                out[key] = float(stripped)
                continue
        out[key] = value
    return out


def _is_record_annotation(annotation: Any) -> bool:
    """True if an annotation refers to a sample record (handles ``Iterator[Record]`` etc.).

    Accepts the runtime-erased alias (``Record`` == ``Dict[str, Any]``, matched structurally via
    :func:`_mentions_record`) AND the spelled-out ``Record`` / legacy ``Sample`` names — a
    stringized / unresolvable forward-ref annotation keeps its source text, so the token scan
    still catches it.
    """
    if _mentions_record(annotation):
        return True
    text = str(annotation)
    return "Record" in text or "Sample" in text


def _op_call_signature(obj: Any) -> Optional[inspect.Signature]:
    """Return ``obj.__call__``'s signature iff ``obj`` is a record-taking op class.

    Classification is the POSITIVE ``@configurable(category="op")`` tag — the same allowlist the
    palette admission uses — NOT an annotation sniff: the record carrier is a plain dict (there
    is no marker class to identity-match), and a kernel-only ``Transform`` subclass INHERITS its
    ``__call__`` from the base (nothing in ``vars(cls)``), which the old ``vars``-scan
    mis-classified as generic. The signature is resolved through the MRO (``inspect.signature``
    sees the inherited method). A tagged class whose ``__call__`` takes NO carrier param (an
    older ``category="op"`` + ``constant=True`` value producer with a no-arg ``__call__``)
    returns ``None`` so it still falls through to the value-node dispatch, as before.
    """
    if not inspect.isclass(obj) or getattr(obj, "__confluid_category__", None) != "op":
        return None
    call = getattr(obj, "__call__", None)  # MRO-resolved — an inherited __call__ counts
    if call is None:
        return None
    try:
        sig = inspect.signature(call)
    except (
        ValueError,
        TypeError,
    ):  # pragma: no cover - defensive: inspect rejects rare callables
        return None
    params = [p for n, p in sig.parameters.items() if n not in ("self", "cls")]
    return sig if params else None


def _value_call_signature(obj: Any) -> Optional[inspect.Signature]:
    """Return ``obj.__call__``'s signature iff ``obj`` is a class whose call takes NO inputs (beyond self).

    A no-arg ``__call__(self)`` marks a VALUE-PRODUCER node — a configurable class that produces values
    purely from its constructor config (surfaced as ``@output`` sockets), with NO record/data input. The
    canonical users are the ImpairmentsChain config utilities (``ImpairmentsTxConfig`` /
    ``ImpairmentsRxConfig``). Distinct from :func:`_op_call_signature` (a ``category="op"`` class whose
    ``__call__`` takes the record) and :func:`_is_runnable` (a no-arg ``run`` method, not ``__call__``);
    checked LAST in the dispatch so a source/op/object/runnable always wins.
    """
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
    return sig if not params else None


def _is_source(obj: Any) -> bool:
    """True if ``obj`` is a class whose indexing/iteration yields sample records."""
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
        if _is_record_annotation(ret):
            return True
    return bool(obj.__name__.endswith("Source"))


def _is_runnable(obj: Any) -> bool:
    """True if ``obj`` is a class implementing the run interface — a no-arg ``run(self)`` method.

    This is the structural signal (mirroring :func:`_is_source` / :func:`_op_call_signature`) that a
    class is a trainer / evaluator / processor: an orchestration entry point, not a per-record op or a
    record source. Such classes become buildable runnable nodes (members as wired sockets) regardless
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
    """True if ``obj`` is a runnable MEMBER that should become a ``SAMPLEFLUX_OBJECT`` node.

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


def _is_sink_class(obj: Any) -> bool:
    """True if ``obj`` is a SampleFlux storage sink tagged ``@configurable(category="sink")``.

    A sink (``HDF5Sink`` / ``YoloSink`` / ``JsonSink`` …) writes a whole Flux via
    ``write(record)`` / ``open`` / ``flush`` / ``close`` — it has NO ``run()`` (not a runnable), NO
    op ``category`` tag (not an op), and carries no ``role``/``lazy`` mark (not an object member by
    the role gate), so it matches none of the signature/role dispatch predicates. It is admitted via
    ``category="sink"`` in :data:`_NODE_CATEGORIES` and routed here: a sink builds as an OBJECT
    producer node (``_build_object_node``) whose output sockets as ``SAMPLEFLUX_OBJECT:sink`` (see
    :func:`_role_for_class`), docking into a ``DatasetProcessor`` runnable node's ``sink`` slot. The
    prediction sinks (``write(prediction, metadata)``) are deliberately UNtagged and excluded.
    """
    return inspect.isclass(obj) and getattr(obj, "__confluid_category__", None) == "sink"


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


def _op_list_params(cls: Any) -> Dict[str, str]:
    """Map each op-LIST constructor param → its dynamic socket-family prefix.

    A param accepts a LIST of ops when its NAME is in ``_OP_LIST_PARAMS`` with a ``list`` annotation
    (``Enable.ops`` / ``Pipeline.transforms`` / a helios ``*transforms`` variadic mirror) OR it is stamped
    ``__taidal_op_list_params__`` by the waivefront-helios bridge (a ``List[Transform]`` slot whose
    helios name — ``true_transforms`` / ``default_transforms`` — the sampleflux-tuned allowlist can't
    know). The family prefix is ``"op"`` for the original name-set params (so existing ``op_N``
    graphs/exports keep working unchanged) and the PARAM NAME otherwise — letting ONE node carry
    SEVERAL independent op-list families (``HeliosIfOp``'s ``true_transforms_N`` + ``false_transforms_N``),
    each grown by ``web/op_list.js``. Detected by name/stamp, NEVER per-class.
    """
    result: Dict[str, str] = {}
    try:
        sig = inspect.signature(cls)
    except (ValueError, TypeError):  # pragma: no cover - defensive
        return result
    stamped = set(getattr(cls, "__taidal_op_list_params__", ()) or ())
    for pname, param in sig.parameters.items():
        # ``list`` OR ``sequence``: ``Pipeline.transforms`` is ``Optional[Sequence[Any]]``, the
        # other op-lists are ``List``-annotated — both are op-list sockets.
        anno_low = str(param.annotation).lower()
        by_name = pname in _OP_LIST_PARAMS and ("list" in anno_low or "sequence" in anno_low)
        if by_name or pname in stamped:
            result[pname] = "op" if pname in _OP_LIST_PARAMS else pname
    return result


def _op_list_param(cls: Any) -> Optional[str]:
    """The FIRST op-list param name (single-family compat shim over :func:`_op_list_params`).

    Used where exactly one op-list family is expected (the engine ``_build_source_node`` — ``Flux.ops``
    — and the export embedder); the full multi-family map is :func:`_op_list_params`.
    """
    return next(iter(_op_list_params(cls)), None)


def _single_op_params(cls: Any) -> List[str]:
    """An op's SINGLE-op constructor params (each holds ONE op VALUE, not a list), in signature order.

    A param holds one op when its NAME is in ``_SINGLE_OP_PARAMS`` with a non-scalar annotation
    (``RandomApply.op`` / ``ConfigureOp.target``) OR it is stamped ``__taidal_op_params__`` by the
    waivefront-helios bridge (a single ``Transform`` slot — ``Configure.compute``/``.target``,
    ``Config.delegate`` — whose helios name the allowlist can't know). Each rides ONE optional
    ``SAMPLEFLUX_OP`` input socket instead of a STRING widget. Op-LIST params (``_op_list_params``) are
    excluded (a list wins). Returning SEVERAL is what surfaces BOTH ``HeliosConfigureOp.compute`` and
    ``.target`` — the old single-name helper exposed only the first.
    """
    try:
        sig = inspect.signature(cls)
    except (ValueError, TypeError):  # pragma: no cover - defensive
        return []
    stamped = set(getattr(cls, "__taidal_op_params__", ()) or ())
    list_params = set(_op_list_params(cls))
    result: List[str] = []
    for pname, param in sig.parameters.items():
        if pname in list_params:
            continue
        by_name = pname in _SINGLE_OP_PARAMS and not _is_scalar_anno(param.annotation)
        if by_name or pname in stamped:
            result.append(pname)
    return result


def _model_params(cls: Any) -> List[str]:
    """A MODEL node's constructor params that hold a WIRED model object, in signature order.

    Read from the ``__taidal_model_params__`` stamp the waivefront-helios ``bridge_model`` sets on a
    ``ModelInference``-style class (a ``model``-named, non-scalar ctor param — the wrapped type is
    deliberately open, so it can't be inferred from the annotation). Each rides ONE optional
    ``SAMPLEFLUX_OBJECT:model`` INPUT socket (a Loader model node's model output wires straight in)
    instead of a STRING widget — the model-node analogue of :func:`_single_op_params`. A self-contained
    model (no ``model`` slot) returns ``[]``, so it keeps the existing widget-only surface.
    """
    stamped = getattr(cls, "__taidal_model_params__", ()) or ()
    try:
        sig_params = set(inspect.signature(cls).parameters)
    except (ValueError, TypeError):  # pragma: no cover - defensive
        return list(stamped)
    return [p for p in stamped if p in sig_params]


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

    Such a param holds ANOTHER source/engine, so it rides a single ``SAMPLEFLUX_SOURCE`` input socket
    instead of becoming a STRING widget — e.g. ``DatasetSplit(source=…)`` / ``Flux(source=…)``.
    """
    return _SOURCE_PARAM if _has_param(cls, _SOURCE_PARAM) else None


def _source_list_param(cls: Any) -> Optional[str]:
    """Name of a source-list constructor param (``fluxes`` / ``sources``), or ``None``.

    Detected by name (``_SOURCE_LIST_PARAMS``) + a ``List``/``list`` annotation — e.g.
    ``JointFlux(fluxes: List[Flux])``. Such a param holds OTHER sources, so it rides dynamic
    ``SAMPLEFLUX_SOURCE`` sockets (``source_0`` / ``source_1`` / …) instead of a STRING widget.
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


def _validate_inputs_with_nullable_numerics(cls: Any, input_types: Dict[str, Any]) -> Any:
    """ComfyUI ``VALIDATE_INPUTS`` for generated nodes that expose a connectable nullable-numeric param.

    A nullable numeric (``Optional[int/float]=None``) renders as a blank STRING widget (so blank can mean
    None / metadata-fallback) tagged ``NULLABLE_NUMERIC``, and ``web/nullable_numeric_socket.js`` retypes
    its converted CANVAS socket to FLOAT/INT so a numeric wire draws. But the BACKEND ``INPUT_TYPES`` still
    declares it ``STRING``, so ComfyUI's built-in ``validate_node_input`` would reject the FLOAT/INT→STRING
    link at Queue ("Return type mismatch") — which is exactly why a wired ``noise_power_db`` validated only
    when it wasn't in the executed output's cone. Declaring an ``input_types`` parameter makes ComfyUI SKIP
    that built-in check and defer to us (execution.py): we ACCEPT a FLOAT/INT wire into a NULLABLE_NUMERIC
    param and otherwise reuse ComfyUI's OWN ``validate_node_input`` (no divergence — every other socket is
    still type-checked). ``input_types`` maps each LINKED input name → the upstream output's type string.
    """
    try:  # ComfyUI's real matcher (only importable inside ComfyUI) — keeps our rules identical to its.
        from comfy_execution.validation import validate_node_input as _compatible
    except Exception:  # pragma: no cover - fluxstudio's own tests run without ComfyUI installed

        def _compatible(received: Any, expected: Any, strict: bool = False) -> bool:
            return received == expected or "*" in (str(received), str(expected))

    declared = {**cls.INPUT_TYPES().get("required", {}), **cls.INPUT_TYPES().get("optional", {})}
    for name, received in (input_types or {}).items():
        entry = declared.get(name)
        expected = entry[0] if isinstance(entry, (list, tuple)) and entry else entry
        opts = entry[1] if isinstance(entry, (list, tuple)) and len(entry) > 1 and isinstance(entry[1], dict) else {}
        if opts.get("taidal_widget") == "NULLABLE_NUMERIC" and str(received) in ("FLOAT", "INT"):
            continue  # the sanctioned numeric-wire-into-nullable-param coercion
        if not isinstance(expected, str):
            continue  # a combo options-list (or unknown) — ComfyUI handles those; don't reject here
        if _compatible(received, expected):
            continue
        return f"{name}: received type {received!r} is not compatible with the declared {expected!r}"
    return True


def _has_nullable_numeric_input(*input_dicts: Optional[Dict[str, Any]]) -> bool:
    """True if any input entry is a ``NULLABLE_NUMERIC``-tagged widget (needs the VALIDATE_INPUTS skip)."""
    for inputs in input_dicts:
        for entry in (inputs or {}).values():
            if isinstance(entry, (list, tuple)) and len(entry) > 1 and isinstance(entry[1], dict):
                if entry[1].get("taidal_widget") == "NULLABLE_NUMERIC":
                    return True
    return False


def _node_class(
    name: str,
    category: str,
    inputs: Dict[str, Any],
    return_types: Tuple[str, ...],
    execute: Callable,
    return_names: Optional[Tuple[str, ...]] = None,
    kind: Optional[str] = None,
    callable_path: Optional[str] = None,
    optional_inputs: Optional[Dict[str, Any]] = None,
    output_node: bool = False,
    hidden: Optional[Dict[str, Any]] = None,
    is_random: bool = False,
    is_constant: bool = False,
    split_map: Optional[Dict[str, Tuple[str, str]]] = None,
) -> Type:
    """Assemble a ComfyUI node class from its parts.

    ``output_node=True`` marks the node a terminal ``OUTPUT_NODE`` (ComfyUI always executes it on
    Queue) — used by runnable nodes, which RUN on execute (:func:`_build_runnable_node`). ``hidden``
    is folded into ``INPUT_TYPES`` (e.g. ``{"unique_id": "UNIQUE_ID"}`` so a runnable node can address
    its own live-output widget). Most generated nodes pass neither.

    ``kind`` (``"op"`` / ``"source"`` / ``"generic"``) and ``callable_path`` (importable
    ``module:qualname``) ride on the class as ``TAIDAL_NODE_KIND`` / ``TAIDAL_CALLABLE_PATH`` so the
    ops-export compiler (:mod:`fluxstudio.export`) can pick out the op nodes from a saved graph and
    re-emit each as a Confluid ``!class:`` marker. They have no effect on canvas execution.
    ``is_constant`` mirrors the wrapped callable's ``@configurable(constant=True)`` mark as a
    ``TAIDAL_CONSTANT`` class attr — the ops-export reads it to HOIST a wired value node into the
    exported YAML (top-level ``!class:`` + dotted ``!ref:``) instead of dropping the wired values.

    ``split_map`` maps each ``Union[int, str]`` param name (when ``strict_typing=True`` on the
    configurable) to its ``({name}_samples, {name}_duration)`` socket pair.  The timed execute
    wrapper merges whichever socket is filled back into the canonical name before forwarding to the
    inner execute.
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
    _split_map = split_map or {}

    def _timed_execute(self: Any, **kwargs: Any) -> Any:
        # Merge dual sockets ({name}_samples / {name}_duration) back into the
        # canonical param name before the inner execute sees them.
        for _orig, (_int_key, _str_key) in _split_map.items():
            _str_val = kwargs.pop(_str_key, "")
            _int_val = kwargs.pop(_int_key, None)
            if _str_val:
                kwargs[_orig] = _str_val
            elif _int_val is not None and _int_val > 0:
                kwargs[_orig] = _int_val
            # else: omit → constructor uses its own default
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
        "TAIDAL_NODE_KIND": kind,
        "TAIDAL_CALLABLE_PATH": callable_path,
        "TAIDAL_CONSTANT": is_constant,
    }
    if output_node:
        attrs["OUTPUT_NODE"] = True
    if _has_nullable_numeric_input(inputs, optional_inputs):
        # A connectable nullable-numeric param is a blank STRING widget retyped to FLOAT/INT on the
        # canvas; this lets ComfyUI accept the numeric wire at Queue (it skips its built-in type check
        # for nodes whose VALIDATE_INPUTS takes `input_types`). Without it the wire draws but Queue
        # fails with "Return type mismatch" (the backend still sees STRING).
        attrs["VALIDATE_INPUTS"] = classmethod(_validate_inputs_with_nullable_numerics)
    if return_names is not None:
        attrs["RETURN_NAMES"] = return_names
    if is_random:
        # float("nan") != float("nan") always → ComfyUI always considers the output stale
        # and re-executes the full upstream chain on every queue prompt.
        attrs["IS_CHANGED"] = classmethod(lambda cls, **kw: float("nan"))
    return type(name, (object,), attrs)


def _build_source_node(cls: Callable, name: str, category: str) -> Type:
    """Build a source/engine node: constructor config -> a ``SAMPLEFLUX_SOURCE`` output (a ``Flux``).

    Source-typed constructor params ride WIRED sockets instead of STRING widgets, so the engine /
    view sources (``DatasetSplit`` / ``Flux`` / ``JointFlux``) compose on the canvas from other
    Source/Op nodes (which already expose themselves as ``SAMPLEFLUX_SOURCE`` / ``SAMPLEFLUX_OP``):
      * a single ``source`` param   -> one ``SAMPLEFLUX_SOURCE`` input (``DatasetSplit.source`` /
        ``Flux.source``); a required socket when the ctor param has no default, else optional;
      * a source-list param (``fluxes``) -> dynamic ``source_N`` ``SAMPLEFLUX_SOURCE`` inputs grown by
        ``web/source_list.js`` (``JointFlux.fluxes``);
      * an op-list param (``ops``)        -> dynamic ``op_N`` ``SAMPLEFLUX_OP`` inputs grown by
        ``web/op_list.js`` (``Flux.ops``).
    Everything else stays a constructor-config widget. A plain leaf source (``HuggingFaceSource`` …)
    has none of these params, so it is unchanged (all-widget).
    """
    src_param = _source_param(cls)
    src_list_param = _source_list_param(cls)
    op_list_param = _op_list_param(cls)
    wired = {p for p in (src_param, src_list_param, op_list_param) if p is not None}
    _strict = getattr(cls, "__confluid_strict_typing__", False)
    required, nullable_opts, _split_map = _build_config_inputs(cls, skip=wired or None, strict_typing=_strict)

    optional: Dict[str, Any] = {**nullable_opts}
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
        from sampleflux.core import Flux

        if src_list_param is not None:
            sources = _collect_sources(kwargs)
            kwargs = {k: v for k, v in kwargs.items() if not (k.startswith("source_") and k[7:].isdigit())}
            kwargs[src_list_param] = sources
        if op_list_param is not None:
            ops = _collect_ops(kwargs)
            kwargs = {k: v for k, v in kwargs.items() if not (k.startswith("op_") and k[3:].isdigit())}
            kwargs[op_list_param] = ops
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
        kind="source",
        callable_path=_safe_callable_path(cls),
        is_random=getattr(cls, "__confluid_random__", False),
        split_map=_split_map or None,
    )


def _build_op_node(cls: Callable, name: str, category: str) -> Type:
    """Build a dual-mode op node: ``(SAMPLE?, op_*) -> (SAMPLE, OP[, @output…])``.

    The op node both APPLIES the op (the ``sample`` input chained to the ``sample`` output, today's
    linear-pipeline UX) AND exposes the constructed op on the ``op`` output (``SAMPLEFLUX_OP``), so it
    can feed a higher-order op. ``sample`` is OPTIONAL: leave it unconnected to use the node purely
    as an op-value. Op-VALUED constructor params ride ``SAMPLEFLUX_OP`` sockets instead of STRING
    widgets: each single-op param (``_single_op_params``) is one static socket, and each op-LIST param
    (``_op_list_params``) a dynamic ``<prefix>_N`` family. A node may carry SEVERAL of each — e.g.
    ``HeliosConfigureOp`` sockets BOTH ``compute`` and ``target``; ``HeliosIfOp`` carries the
    ``true_transforms_N`` and ``false_transforms_N`` families.

    Any ``@output`` properties declared on the op class (e.g. ``applied_impairments``) become extra
    output sockets appended after ``(sample, op)`` — read from the live op instance after
    ``__call__`` completes (or their pre-call default when ``sample`` is unconnected).
    """
    op_list_params = _op_list_params(cls)  # {param: socket-family prefix}
    single_op_params = _single_op_params(cls)  # [param, …] — each ONE op VALUE
    list_prefixes = sorted(set(op_list_params.values()))
    # Config widgets EXCLUDING the op-typed params (they ride SAMPLEFLUX_OP sockets, not widgets).
    _strict = getattr(cls, "__confluid_strict_typing__", False)
    _socketed = set(op_list_params) | set(single_op_params)
    required, nullable_opts, _split_map = _build_config_inputs(cls, skip=_socketed or None, strict_typing=_strict)
    optional: Dict[str, Any] = {"sample": (SAMPLE_TYPE,), **nullable_opts}
    for prefix in list_prefixes:
        optional[f"{prefix}_0"] = (OP_TYPE,)  # seed; web/op_list.js grows <prefix>_1, _2, … per family
    for pname in single_op_params:
        # one op VALUE each (RandomApply.op / ConfigureOp.target / helios compute/target/delegate/…)
        optional[pname] = (OP_TYPE,)

    # Collect any @output properties declared on this op class.
    out_specs = list(output_specs(cast(type, cls)))
    out_sockets = [_output_socket_type(s["name"], _output_return_anno(cls, s["name"])) for s in out_specs]

    def execute(self: Any, sample: Any = None, **kwargs: Any) -> Tuple[Any, ...]:
        if op_list_params:
            list_values: Dict[str, Any] = {}
            for pname, prefix in op_list_params.items():
                ops = _collect_indexed(kwargs, prefix)
                list_values[pname] = ops
            kwargs = {k: v for k, v in kwargs.items() if not _is_indexed_key(k, set(list_prefixes))}
            kwargs.update(list_values)
        # Post-construction injection (the confluid paradigm) of each wired single-op SAMPLEFLUX_OP value.
        wired_singles = {p: kwargs.pop(p, None) for p in single_op_params}
        op = cls(**_coerce_kwargs(cls, kwargs))
        for pname, wired in wired_singles.items():
            if wired is not None:
                setattr(op, pname, wired)
        applied: Any = None
        if sample is not None:
            # The record's values are typed items (each carries its own type/metadata), so an
            # incompatible op raises naturally when applied.
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
        kind="op",
        callable_path=_safe_callable_path(cls),
        is_random=getattr(cls, "__confluid_random__", False),
        split_map=_split_map or None,
    )


def _build_model_node(cls: Callable, name: str, category: str) -> Type:
    """Build a MODEL node: ``(SAMPLE?, config) -> (SAMPLE, OBJECT:model[, @output…])``.

    A model node is "like an op, but a bit different": it RUNS inference rather than applying a
    deterministic transform. Like :func:`_build_op_node` it takes an optional ``sample`` and config
    widgets and outputs the applied ``sample``, but its SECOND output is the constructed model on a
    role-qualified ``SAMPLEFLUX_OBJECT:model`` socket (it outputs ITSELF as the model object — so it can
    also dock into a downstream evaluator's ``model`` slot) rather than a ``SAMPLEFLUX_OP``. The wrapped
    class must be callable as ``model(record) -> record`` (the waivefront-helios bridge generates such a
    wrapper around a helios ``infer``). ``@output`` properties, if any, are appended as extra sockets.

    A ``model``-typed ctor param (``_model_params`` — the ``ModelInference.model`` loader→runner
    decoupling, stamped ``__taidal_model_params__`` by the waivefront-helios bridge) rides a wired
    ``SAMPLEFLUX_OBJECT:model`` INPUT socket instead of a STRING widget, so a Loader model node's model
    output wires straight in. ``execute`` ``setattr``s each wired model post-construction (the confluid
    paradigm) — mirroring how :func:`_build_op_node` injects single-op ``SAMPLEFLUX_OP`` values.
    """
    model_socket = _object_socket("model")
    model_in_params = _model_params(cls)  # ctor params that take a WIRED model (SAMPLEFLUX_OBJECT:model)
    # Config widgets EXCLUDING the wired model params (they ride SAMPLEFLUX_OBJECT:model input sockets).
    required, nullable_opts, _split_map = _build_config_inputs(
        cls, skip=set(model_in_params) or None, strict_typing=getattr(cls, "__confluid_strict_typing__", False)
    )
    optional: Dict[str, Any] = {"sample": (SAMPLE_TYPE,), **nullable_opts}
    for pname in model_in_params:
        optional[pname] = (model_socket,)  # a wired model input (a Loader node's model output docks here)
    out_specs = list(output_specs(cast(type, cls)))
    out_sockets = [_output_socket_type(s["name"], _output_return_anno(cls, s["name"])) for s in out_specs]

    def execute(self: Any, sample: Any = None, **kwargs: Any) -> Tuple[Any, ...]:
        # Post-construction injection (the confluid paradigm) of each wired SAMPLEFLUX_OBJECT:model value.
        wired_models = {p: kwargs.pop(p, None) for p in model_in_params}
        model = cls(**_coerce_kwargs(cls, kwargs))
        for pname, wired in wired_models.items():
            if wired is not None:
                setattr(model, pname, wired)
        applied: Any = None
        if sample is not None:
            applied = model(sample)
            if applied is None:
                raise RuntimeError(
                    f"{getattr(cls, '__name__', 'model')} returned None for the sample; nothing to " "pass downstream."
                )
        extra_outs: List[Any] = []
        for spec, socket in zip(out_specs, out_sockets):
            try:
                value = getattr(model, spec["name"])
            except Exception as exc:
                logger.debug("{}: output {!r} unavailable ({}); emitting None.", name, spec["name"], exc)
                value = None
            extra_outs.append(_coerce_output_value(value, socket))
        return (applied, model, *extra_outs)

    return _node_class(
        name,
        category,
        required,
        (SAMPLE_TYPE, model_socket, *out_sockets),
        execute,
        return_names=("sample", "model", *[s["name"] for s in out_specs]),
        optional_inputs=optional,
        kind="model",
        callable_path=_safe_callable_path(cls),
        is_random=getattr(cls, "__confluid_random__", False),
        split_map=_split_map or None,
    )


def _build_value_node(cls: Callable, name: str, category: str) -> Type:
    """Build a VALUE-producer node: ``(config widgets) -> (@output sockets…)``.

    For a configurable class with a no-arg ``__call__(self)`` (:func:`_value_call_signature`) — a pure
    value producer with NO record input. ``execute`` builds the object from its config widgets, invokes
    it (``obj()`` — honouring the producer contract), and surfaces each declared ``@output`` property as
    a typed output socket (containers → JSON ``STRING``, via the same machinery as op/runnable nodes).
    Unlike an op node there are NO ``sample`` input or ``sample``/``op`` outputs — the only outputs are
    the declared ``@output``s. Canonical users: the ImpairmentsChain ``ImpairmentsTxConfig`` /
    ``ImpairmentsRxConfig`` config utilities.
    """
    _strict = getattr(cls, "__confluid_strict_typing__", False)
    required, nullable_opts, _split_map = _build_config_inputs(cls, strict_typing=_strict)
    optional: Dict[str, Any] = dict(nullable_opts)

    out_specs = list(output_specs(cast(type, cls)))
    out_sockets = [_output_socket_type(s["name"], _output_return_anno(cls, s["name"])) for s in out_specs]

    def execute(self: Any, **kwargs: Any) -> Tuple[Any, ...]:
        obj = cls(**_coerce_kwargs(cls, kwargs))
        obj()  # run the producer (no-arg; @output values may depend on it — harmless for pure-config ones)
        outs: List[Any] = []
        for spec, socket in zip(out_specs, out_sockets):
            try:
                value = getattr(obj, spec["name"])
            except Exception as exc:
                logger.debug("{}: output {!r} unavailable ({}); emitting None.", name, spec["name"], exc)
                value = None
            outs.append(_coerce_output_value(value, socket))
        return tuple(outs)

    return _node_class(
        name,
        category,
        required,
        tuple(out_sockets),
        execute,
        return_names=tuple(s["name"] for s in out_specs),
        optional_inputs=optional or None,
        kind="value",
        callable_path=_safe_callable_path(cls),
        is_random=getattr(cls, "__confluid_random__", False),
        is_constant=getattr(cls, "__confluid_constant__", False),
        split_map=_split_map or None,
    )


def _build_generic_node(func: Callable, name: str, category: str) -> Type:
    inputs, nullable_opts, _ = _build_config_inputs(func)
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
        optional_inputs=nullable_opts or None,
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
    """True when a constructor param should be a wired ``SAMPLEFLUX_OBJECT`` socket (not a widget).

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

    * a dataset-named output rides ``SAMPLEFLUX_SOURCE``;
    * a scalar output a typed widget string (via :func:`_map_type` — ``INT`` / ``FLOAT`` / ``STRING`` / ``BOOLEAN``);
    * a **container** output (``Dict`` / ``List`` / ``Set`` / ``Tuple`` — e.g. ``computed_metrics`` a
      ``Dict[str, float]``, ``label_names`` a ``List[str]``) rides a plain ``STRING`` socket (the runnable
      node JSON-encodes the value), so it wires straight into ComfyUI's native *Show Text* node. This branch
      is BEFORE the role inference on purpose: it stops ``computed_metrics`` from mis-mapping to a bogus
      ``SAMPLEFLUX_OBJECT:metric`` via the ``"metric"`` substring in :func:`_infer_member_role`;
    * a role-bearing object output (``trained_model`` → ``SAMPLEFLUX_OBJECT:model``, task-agnostic) docks
      straight into a downstream runnable's likewise-qualified object slot (an evaluator's ``model``);
    * any other object rides the bare ``SAMPLEFLUX_OBJECT``.
    """
    if prop_name in _DATASET_MEMBER_PARAMS:
        return SOURCE_TYPE
    if _is_scalar_anno(anno):
        socket = _map_type(prop_name, anno)
        # An OUTPUT is never a directory picker: a path-named scalar output (e.g. a value
        # producer's ``directory``) rides a plain ``STRING`` socket so it wires into a reader's
        # path/directory widget (which is itself STRING-backed, converted to a STRING input).
        return "STRING" if socket == "PATH" else socket
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
    """Build an OBJECT node: constructor config widgets -> TWO ``SAMPLEFLUX_OBJECT`` outputs.

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
    # str/int/float config arm; see :func:`_is_wireable_object_param`) rides a WIRED ``SAMPLEFLUX_OBJECT``
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

    inputs, nullable_opts, _ = _build_config_inputs(
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
    # model both ride ``SAMPLEFLUX_OBJECT:model``. The role is the explicit ``role=`` tag or inferred
    # from the type (``_role_for_class``); a role-less member (logger / lazy infra) keeps the bare
    # ``SAMPLEFLUX_OBJECT``. Mirrors the runnable-SLOT side. See :func:`_object_socket_for_class`.
    obj_socket = _object_socket_for_class(cls)

    return _node_class(
        name,
        category,
        inputs,
        (obj_socket, obj_socket),
        execute,
        return_names=("instance", "class"),
        optional_inputs={**object_optional, **nullable_opts} or None,
        kind="object",
        callable_path=_safe_callable_path(cls),
    )


def _runnable_consumed_as_instance(
    prompt: Optional[Dict[str, Any]],
    unique_id: Optional[Any],
    node_mappings: Optional[Dict[str, Any]] = None,
) -> bool:
    """True if this runnable node's slot-0 ``SAMPLEFLUX_RUNNABLE`` output is wired into ANOTHER node's
    ``SAMPLEFLUX_RUNNABLE`` input — i.e. it is a BRANCH of a workflow combinator (``Conditional`` /
    ``Sequence`` / ``Switch``) that will run it, so it must NOT run standalone (no double-run).

    This is the dual-mode switch read inside a runnable node's ``execute``: consumed-as-instance ⇒
    build the runnable and hand it over (the combinator runs the selected one); NOT consumed ⇒ the
    node is terminal and runs on execute (the unchanged default). Reads ComfyUI's hidden ``PROMPT``
    (the flat API graph ``{node_id: {class_type, inputs}}``); a missing prompt / id ⇒ NOT consumed
    (terminal), so behaviour is identical when the hidden input is absent (older ComfyUI, fluxstudio's
    own tests). Robust to a dynamic ``<prefix>_N`` runnable slot (``Sequence.steps_3``) — the class
    only declares the ``<prefix>_0`` seed, so the base seed's type is consulted.
    """
    if not prompt or unique_id is None:
        return False
    if node_mappings is None:
        from fluxstudio import NODE_CLASS_MAPPINGS as node_mappings  # the live registry
    uid = str(unique_id)
    for node in prompt.values():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        consumer = node_mappings.get(class_type) if (node_mappings and isinstance(class_type, str)) else None
        if consumer is None or not hasattr(consumer, "INPUT_TYPES"):
            continue
        try:
            spec = consumer.INPUT_TYPES()
        except Exception:  # pragma: no cover - a node whose INPUT_TYPES raises
            continue
        declared = {**spec.get("required", {}), **spec.get("optional", {})}
        for iname, link in (node.get("inputs", {}) or {}).items():
            if not (isinstance(link, (list, tuple)) and len(link) == 2):
                continue  # a literal widget value, not a link
            if str(link[0]) != uid or link[1] != 0:
                continue  # not a link to OUR slot-0 (the runnable) output
            entry = declared.get(iname)
            if entry is None and "_" in iname:  # a dynamic `<prefix>_N` slot only seeded at `<prefix>_0`
                entry = declared.get(f"{iname.rsplit('_', 1)[0]}_0")
            itype = entry[0] if isinstance(entry, (list, tuple)) and entry else entry
            if _base_socket_type(itype) == RUNNABLE_TYPE:
                return True
    return False


def _build_runnable_node(cls: Callable, name: str, category: str) -> Type:
    """Build a runnable (trainer / evaluator / processor) node: members as wired INPUT sockets.

    Every configurable member is enumerated from ``to_pydantic(cls)`` (which surfaces both ctor params
    AND ``__init__``-body slots, and skips ``@ignore_config`` members). Each member is rendered as:

    * a ``SAMPLEFLUX_SOURCE`` socket — dataset/source members (:data:`_DATASET_MEMBER_PARAMS` + the
      ``source`` param), fed by Source/dataset nodes;
    * a ``SAMPLEFLUX_OBJECT`` socket — any other non-scalar member (model / loss / optimizer / loaders /
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

    OUTPUTS: slot 0 is the (post-run) runnable on a ``SAMPLEFLUX_RUNNABLE`` wire; then EVERY declared
    ``@output`` property (:func:`confluid.output_specs`) becomes a wireable socket via
    :func:`_output_socket_type` — ``trained_model`` rides ``SAMPLEFLUX_OBJECT:model`` into a downstream
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
    # ``model`` slot accepts ``SAMPLEFLUX_OBJECT:model`` — so ComfyUI rejects a loss wire but a
    # classification model docks into a segmentation trainer's model slot just the same (TASK-
    # AGNOSTIC; task survives only as palette/navigaitor guidance). Role-less slots (optimizer /
    # loader / lightning / logger) stay generic ``SAMPLEFLUX_OBJECT``. See :data:`_STRICT_SLOT_ROLES`
    # / :func:`_object_socket`. Mirrors the object-NODE output side (:func:`_object_socket_for_class`).
    socket_members: Dict[str, str] = {}  # member name -> SOURCE_TYPE | OBJECT_TYPE[:role] | RUNNABLE_TYPE
    runnable_list_members: List[str] = []  # member name -> a dynamic <name>_N SAMPLEFLUX_RUNNABLE family
    for fname, finfo in fields.items():
        anno = getattr(finfo, "annotation", Any)
        if fname in _RUNNABLE_LIST_PARAMS:
            runnable_list_members.append(fname)  # seeded as <name>_0 below, collected in execute
        elif fname in _RUNNABLE_PARAMS:
            socket_members[fname] = RUNNABLE_TYPE  # a combinator branch — wired runnable INSTANCE
        elif fname in _DATASET_MEMBER_PARAMS or fname == _SOURCE_PARAM:
            socket_members[fname] = SOURCE_TYPE
        elif _is_scalar_anno(anno):
            continue  # scalar -> widget (handled by _build_config_inputs below)
        else:
            role = _STRICT_SLOT_ROLES.get(fname)
            socket_members[fname] = _object_socket(role) if role else OBJECT_TYPE

    # Scalars stay widgets; skip the socket members + runnable-list members (the dynamic <name>_N
    # families are seeded below, never a widget) — any non-ctor body slot _build_config_inputs
    # wouldn't see anyway.
    required, nullable_opts, _ = _build_config_inputs(
        cls, skip=set(socket_members) | set(runnable_list_members) or None
    )
    optional: Dict[str, Any] = {**nullable_opts}
    for member, socket in socket_members.items():
        if member in required_params:
            required[member] = (socket,)
        else:
            optional[member] = (socket,)
    # A runnable-LIST member (Sequence.steps) seeds ONE optional SAMPLEFLUX_RUNNABLE socket
    # (``<name>_0``); web/runnable_list.js grows ``<name>_1`` … as branches are wired, and
    # execute collects the whole family via _collect_indexed.
    for member in runnable_list_members:
        optional[f"{member}_0"] = (RUNNABLE_TYPE,)

    # EVERY declared @output -> a typed output socket (mapped by _output_socket_type; containers ride a
    # STRING/JSON socket). The node runs before reading them, so they carry live values.
    out_specs = list(output_specs(runnable_cls))
    out_sockets = [_output_socket_type(s["name"], _output_return_anno(cls, s["name"])) for s in out_specs]
    extra_types = tuple(out_sockets)
    extra_names = tuple(s["name"] for s in out_specs)

    def execute(self: Any, unique_id: Optional[str] = None, prompt: Any = None, **kwargs: Any) -> Dict[str, Any]:
        from fluxstudio.nodes import run_runnable  # lazy: nodes.py imports bridge (avoid an import cycle)

        # Gather each dynamic runnable-LIST family (steps_0/steps_1/…) into its ctor kwarg, dropping the
        # indexed keys so they don't reach the ctor split as bogus params.
        for member in runnable_list_members:
            items = _collect_indexed(kwargs, member)
            for ik in [k for k in kwargs if _is_indexed_key(k, {member})]:
                kwargs.pop(ik)
            if items:
                kwargs[member] = items

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

        # DUAL-MODE: if this runnable's slot-0 output feeds a workflow combinator's SAMPLEFLUX_RUNNABLE
        # input, it is a BRANCH — hand over the constructed-but-UN-RUN instance and let the combinator
        # run it (no double-run; only the SELECTED branch ever runs). Otherwise it is the terminal
        # OUTPUT_NODE and runs on execute (the default, unchanged when not consumed).
        if _runnable_consumed_as_instance(prompt, unique_id):
            note = f"{type(obj).__name__}: built as an instance — the consuming workflow runs it."
            return {"ui": {"text": [note]}, "result": (obj, *([None] * len(out_sockets)))}

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
        # PROMPT lets execute detect when this node is a combinator BRANCH (dual-mode); UNIQUE_ID
        # addresses its own live-output widget.
        hidden={"unique_id": "UNIQUE_ID", "prompt": "PROMPT"},
    )


def create_comfy_node(
    func: Callable,
    name: str,
    category: str = "Taidal",
    display_name: Optional[str] = None,
) -> Type:
    """Dynamically construct a ComfyUI node class from a Python callable.

    Dispatches on the callable's SampleFlux role: a ``run()``-implementing class (trainer / evaluator /
    processor) becomes a buildable RUNNABLE node (members as wired sockets); ``category="op"``
    classes (record-taking ops) become ``SAMPLE -> SAMPLE`` nodes; record-yielding sources (and task
    ``role="dataset"`` classes) become ``-> SOURCE`` nodes; runnable MEMBER classes (model / loss /
    metric / optimizer / loader / lightning) AND ``category="sink"`` storage sinks become ``-> OBJECT``
    nodes (a sink outputs ``SAMPLEFLUX_OBJECT:sink``, docking into a ``DatasetProcessor`` node's
    ``sink`` slot); a no-arg ``__call__(self)`` class becomes a VALUE node (config widgets ->
    ``@output`` sockets, no record I/O); everything else falls back to generic.
    """
    if _is_runnable(func):
        return _build_runnable_node(func, name, category)
    # A MODEL node (``category="model"``) also has a record-taking ``__call__`` — it is
    # dispatched BEFORE the op check by its own category tag (a model RUNS inference and
    # additionally exposes its model object; see :func:`_build_model_node`).
    if getattr(func, "__confluid_category__", None) == "model":
        return _build_model_node(func, name, category)
    if _op_call_signature(func) is not None:
        return _build_op_node(func, name, category)
    if _is_source(func) or _is_dataset_member_class(func):
        return _build_source_node(func, name, category)
    if _is_object_member_class(func) or _is_sink_class(func):
        # A sink is an object-member PRODUCER (like a model / loss / logger): config widgets ->
        # ``instance`` + ``class`` outputs on a ``SAMPLEFLUX_OBJECT:sink`` wire, docking into a
        # ``DatasetProcessor`` node's ``sink`` slot. Reuses ``_build_object_node`` verbatim.
        return _build_object_node(func, name, category)
    if _value_call_signature(func) is not None:
        return _build_value_node(func, name, category)
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
    modules expose a same-named class (e.g. ``sampleflux.ops.numpy:RescaleOp`` vs
    ``sampleflux.ops.torch:RescaleOp``): the second registration silently overwrites the first and
    one node vanishes from the palette. We qualify the key with the top-level package + confluid
    category + group, e.g. ``Taidal_sampleflux_op_numpy_RescaleOp`` vs ``Taidal_sampleflux_op_torch_RescaleOp``.

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
    render as wired ``SAMPLEFLUX_SOURCE`` / ``SAMPLEFLUX_OP`` sockets (see :func:`_build_source_node`).
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
       datasets (``sonair`` re-exports ``LightningClassifier`` from ``sonair.classifier``)
       AND registered third-party members (``marainer.lightning.configurables``'s ``torch`` optimizers /
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
        # The palette LABEL is normally the verbatim callable name; a producer may override it with a
        # ``__taidal_display_name__`` stamp (the waivefront-helios bridge sets it to the class name minus
        # the leading ``Helios`` — the class NAME keeps the prefix as its flat confluid registry key, only
        # the label drops it since the ``[PKG]`` tag already disambiguates). The registration KEY
        # (``node_name`` / ``_node_key``) is unaffected, so saved workflows still resolve.
        display_label = getattr(target_func, "__taidal_display_name__", None) or name
        display_name_mappings[node_name] = (
            f"[{_package_tag(module_path, dist_name)}] {_humanize_node_name(display_label)}"
        )
        logger.debug(f"ComfyFlux: Registered node {node_name} for {module_path}.{name}")
