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

import importlib
import inspect
from typing import (
    AbstractSet,
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
)

import datasets
from confluid import parse_param_docs
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

# Canonical palette display casing for workspace packages whose ``str.title()`` would mangle the
# intended CamelCase (``dataflux`` → ``Dataflux`` not ``DataFlux``). The top-level package of each
# discovered node's entry-point module is the palette's first level (``Taidal/<Package>/…``).
# Unlisted packages fall back to ``str.title()``.
_PACKAGE_DISPLAY_NAMES = {
    "dataflux": "DataFlux",
    "fluxstudio": "FluxStudio",
    "logflow": "LogFlow",
    "waivefront": "Waivefront",
    "sonair": "Sonair",
    "deltaid": "Deltaid",
    "raidar": "Raidar",
    "raidar_ultralytics": "Raidar Ultralytics",
    "marainer": "Marainer",
    "navigaitor": "Navigaitor",
}


def _package_display_name(package: str) -> str:
    """Palette label for a top-level package — canonical casing if known, else ``str.title()``."""
    return _PACKAGE_DISPLAY_NAMES.get(package, package.title())


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
    """True if ``obj`` is a runnable MEMBER class that should become a ``DATAFLUX_OBJECT`` node.

    A task-scoped model / loss / metric (``__confluid_role__`` in :data:`_MEMBER_ROLES`), OR a class
    tagged confluid ``lazy`` (``__confluid_lazy__`` — the role-less registered runtime members:
    ``torch.optim.*`` / ``DataLoader`` / ``pytorch_lightning.Trainer``). The ``lazy`` mark — not a
    category — is the signal for the runtime members so they stay out of navigaitor's category-driven
    form-spec pickers.
    """
    if not inspect.isclass(obj):
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
) -> Type:
    """Assemble a ComfyUI node class from its parts.

    Note: auto-generated nodes are never terminal/output nodes — the only OUTPUT_NODE
    (the sample viewer) is hand-written in ``fluxstudio/nodes.py``.

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
    attrs: Dict[str, Any] = {
        "INPUT_TYPES": classmethod(lambda cls, _spec=input_spec: _spec),
        "RETURN_TYPES": return_types,
        "FUNCTION": "execute",
        "CATEGORY": category,
        "execute": execute,
        "TAIDAL_ACCEPTS": accepts,
        "TAIDAL_PRODUCES": produces,
        "TAIDAL_NODE_KIND": kind,
        "TAIDAL_CALLABLE_PATH": callable_path,
    }
    if return_names is not None:
        attrs["RETURN_NAMES"] = return_names
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
    )


def _build_op_node(cls: Callable, name: str, category: str) -> Type:
    """Build a dual-mode op node: ``(SAMPLE?, op_*) -> (SAMPLE, OP)``.

    The op node both APPLIES the op (the ``sample`` input chained to the ``sample`` output, today's
    linear-pipeline UX) AND exposes the constructed op on the ``op`` output (``DATAFLUX_OP``), so it
    can feed a higher-order op. ``sample`` is OPTIONAL: leave it unconnected to use the node purely
    as an op-value. Higher-order ops (op-list constructor param) gain dynamic ``op_*`` inputs.
    """
    accepts = getattr(cls, "ACCEPTS", None)
    op_list_param = _op_list_param(cls)
    # Config widgets EXCLUDING the op-list param (it rides DATAFLUX_OP sockets, not a widget).
    required = _build_config_inputs(cls, skip={op_list_param} if op_list_param else None)
    optional: Dict[str, Any] = {"sample": (SAMPLE_TYPE,)}
    if op_list_param is not None:
        optional["op_0"] = (OP_TYPE,)  # seed; web/op_list.js grows op_1, op_2, … as they fill

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
                    raise RuntimeError(
                        f"{getattr(cls, '__name__', 'op')} cannot accept the upstream sample: "
                        f"expects {accepts} but got {got}."
                    )
            applied = op(sample)
            if applied is None:
                raise RuntimeError(
                    f"{getattr(cls, '__name__', 'op')} filtered out the sample (returned None); "
                    "nothing to pass downstream."
                )
        return (applied, op)

    return _node_class(
        name,
        category,
        required,
        (SAMPLE_TYPE, OP_TYPE),
        execute,
        return_names=("sample", "op"),
        optional_inputs=optional,
        accepts=_spec_to_dict(cls, "ACCEPTS"),
        produces=_spec_to_dict(cls, "PRODUCES"),
        kind="op",
        callable_path=_safe_callable_path(cls),
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
    inputs = _build_config_inputs(cls, skip=skip or None)
    # A member with runtime-injected required params (the skipped set) is deferred BY DESIGN — there
    # is no standalone live instance to build, so don't attempt it (and don't warn — that's expected).
    deferred_required = bool(skip)

    def execute(self: Any, **kwargs: Any) -> Tuple[Any, ...]:
        from confluid import LazyClass, flow

        coerced = _coerce_kwargs(cls, kwargs)
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

    return _node_class(
        name,
        category,
        inputs,
        (OBJECT_TYPE, OBJECT_TYPE),
        execute,
        return_names=("instance", "class"),
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

    A member that is a constructor param with NO default is a REQUIRED socket (``model`` / ``train_set``
    / ``dataset``); everything else is OPTIONAL — unwired ⇒ the runnable keeps its baked default
    ("true optional"). The node is **build-only**: ``execute`` constructs the runnable (ctor params to
    ``__init__``, body slots via post-construction ``setattr`` of the wired members) and outputs it on
    a ``DATAFLUX_RUNNABLE`` wire; a separate ``Run`` node calls ``.run()``.
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

    socket_members: Dict[str, str] = {}  # member name -> SOURCE_TYPE | OBJECT_TYPE
    for fname, finfo in fields.items():
        anno = getattr(finfo, "annotation", Any)
        if fname in _DATASET_MEMBER_PARAMS or fname == _SOURCE_PARAM:
            socket_members[fname] = SOURCE_TYPE
        elif _is_scalar_anno(anno):
            continue  # scalar -> widget (handled by _build_config_inputs below)
        else:
            socket_members[fname] = OBJECT_TYPE

    # Scalars stay widgets; skip the socket members (and any non-ctor body slot, which
    # _build_config_inputs wouldn't see anyway).
    required: Dict[str, Any] = _build_config_inputs(cls, skip=set(socket_members) or None)
    optional: Dict[str, Any] = {}
    for member, socket in socket_members.items():
        if member in ctor_required:
            required[member] = (socket,)
        else:
            optional[member] = (socket,)

    def execute(self: Any, **kwargs: Any) -> Tuple[Any, ...]:
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
        return (obj,)

    return _node_class(
        name,
        category,
        required,
        (RUNNABLE_TYPE,),
        execute,
        return_names=("runnable",),
        optional_inputs=optional or None,
        kind="runnable",
        callable_path=_safe_callable_path(cls),
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


def _node_category(module_path: str, confluid_category: str, group: Optional[str], root: str) -> str:
    """Build a node's ComfyUI palette path: ``<root>/<Package>/<Category>[/<group>]``.

    The first level is the entry-point module's TOP-LEVEL package (``dataflux.ops.numpy`` →
    ``DataFlux``) so every op/source of a package collects under one palette folder regardless
    of which submodule it lives in. Within that, nodes are split by ``@configurable`` category
    (``Op`` / ``Source``) and then by the optional path-like ``group`` (``numpy`` / ``targets`` /
    ``fft/numpy`` …). An untagged group leaves the node directly under ``<Package>/<Category>``.
    """
    package = _package_display_name(module_path.split(".")[0])
    path = f"{root}/{package}/{confluid_category.title()}"
    if group:
        path = f"{path}/{group}"
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
    root: str = "Taidal",
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
    :func:`_node_category`. ``root`` defaults to ``"Taidal"``.

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
        if inspect.isclass(member) and getattr(member, "__confluid_configurable__", False):
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
        node_class = create_comfy_node(
            func=target_func,
            name=node_name,
            category=_node_category(module_path, key_category, group, root),
        )
        target_mappings[node_name] = node_class
        display_name_mappings[node_name] = _humanize_node_name(name)
        logger.debug(f"ComfyFlux: Registered node {node_name} for {module_path}.{name}")
