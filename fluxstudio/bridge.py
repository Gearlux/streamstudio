"""Dynamic runtime generation of ComfyUI nodes from DataFlux / Waivefront / HF callables.

Discovered callables are classified into three node shapes so a real pipeline can be
wired on the ComfyUI canvas — instead of every socket collapsing to ``STRING``:

* **Source** — a class whose ``__getitem__`` / ``__iter__`` yields a
  :class:`dataflux.sample.Sample` (e.g. ``HuggingFaceSource``). The node exposes the
  constructor config as widgets and outputs a ``DATAFLUX_SOURCE`` (a
  :class:`dataflux.core.Flux` wrapping the constructed source).
* **Op** — a ``@configurable`` class with ``__call__(self, sample: Sample) ->
  Optional[Sample]`` (e.g. ``RescaleOp``, ``RenderSpectrogramImageOp``). The node is **dual-mode**:
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

import inspect
from typing import AbstractSet, Any, Callable, Dict, List, Optional, Tuple, Type

import datasets
from confluid import parse_param_docs
from dataflux.discovery import get_callable_path, resolve_callable, scan_module
from dataflux.typespec import infer_sample_type
from logflow import get_logger

logger = get_logger(__name__)


def _spec_to_dict(cls: Any, attr: str) -> Optional[Dict[str, Any]]:
    """JSON-serialize a callable's declared ``ACCEPTS`` / ``PRODUCES`` (:class:`SampleType`), or
    ``None`` when undeclared — the form the ``/taidal/node_specs`` route and the JS validator consume."""
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
_SOCKET_TYPES = {SAMPLE_TYPE, SOURCE_TYPE, OP_TYPE, "HF_DATASET"}

# Constructor params (on higher-order ops) that hold a LIST OF OPS — rendered as dynamic
# ``DATAFLUX_OP`` multi-inputs (``op_0``, ``op_1``, … via ``web/op_list.js``) instead of a STRING
# widget, so concrete op nodes wire into them. ``Enable``/``Parallel`` take ``ops``; ``Tee`` takes
# ``branches`` (a list of op-lists — v1 wraps each connected op as its own single-op branch).
_OP_LIST_PARAMS = {"ops", "branches"}

# Constructor params that name a HuggingFace dataset *identifier* (a hub repo id
# like ``mnist`` / ``kitofrank/RFUAV``, or optionally a local imagefolder path) —
# NOT a filesystem directory. Recognised only on HuggingFace dataset sources (see
# ``_is_hf_dataset_id_param``); ``dataflux.sources.HuggingFaceSource`` calls it
# ``path`` (the upstream ``datasets.load_dataset`` arg name) while sonair's
# ``HuggingFaceClassificationDataset`` calls it ``repo_id``.
_HF_DATASET_ID_PARAMS = {"path", "repo_id"}

# FluxStudio surfaces a node ONLY when its ``@configurable`` discovery category is one of these
# (a POSITIVE allowlist — see dataflux/confluid taxonomy). This is what a canvas can build:
#   * ``op``      — concrete Sample→Sample ops (``RescaleOp``, ``ComputeSpectrogramOp`` …).
#   * ``source``  — concrete data sources (``HuggingFaceSource``, ``RFUAVSource`` …).
#   * ``dataset`` — task datasets (``HuggingFaceClassificationDataset`` …; the task taxonomy
#                   uses ``category="dataset"`` + a ``task=`` tag).
# Everything else is excluded by construction: the generic ENGINES (``Flux`` / ``JointFlux`` /
# ``DatasetSplit`` = ``engine``) and the raw-callable op wrappers (``FilterOp`` / ``WrappedOp`` =
# ``engine``) — composition primitives wired in code, not GUI nodes — plus uncategorised leaks
# (module-level helper functions, raw sinks). Add a category here (e.g. ``"sink"``) to surface it.
_NODE_CATEGORIES = {"op", "source", "dataset"}


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


def _widget_for(param: inspect.Parameter, owner: Optional[Callable] = None) -> Tuple[str, Dict[str, Any]]:
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
    except (ValueError, TypeError):  # pragma: no cover - defensive: inspect rejects rare builtins
        return {}
    param_docs = parse_param_docs(func)
    required: Dict[str, Any] = {}
    for name, param in sig.parameters.items():
        if skip and name in skip:
            continue
        if name in ("self", "cls"):
            continue
        if param.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
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
    except (ValueError, TypeError):  # pragma: no cover - defensive: inspect rejects rare builtins
        return dict(kwargs)
    out: Dict[str, Any] = {}
    for key, value in kwargs.items():
        param = sig.parameters.get(key)
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
    except (ValueError, TypeError):  # pragma: no cover - defensive: inspect rejects rare callables
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
        except (ValueError, TypeError):  # pragma: no cover - defensive: inspect rejects rare callables
            continue
        if _is_sample_annotation(ret):
            return True
    return bool(obj.__name__.endswith("Source"))


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
    except (TypeError, ValueError):  # pragma: no cover - defensive: anonymous/odd callables
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
    unconnected (``None``) ones. The connected op nodes' ``op`` outputs arrive here as kwargs."""
    indexed = [(int(k[3:]), v) for k, v in kwargs.items() if k.startswith("op_") and k[3:].isdigit() and v is not None]
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
    inputs = _build_config_inputs(cls)

    def execute(self: Any, **kwargs: Any) -> Tuple[Any, ...]:
        from dataflux.core import Flux

        obj = cls(**_coerce_kwargs(cls, kwargs))
        flux = obj if isinstance(obj, Flux) else Flux.from_source(obj)
        return (flux,)

    return _node_class(
        name,
        category,
        inputs,
        (SOURCE_TYPE,),
        execute,
        return_names=("source",),
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
    except (ValueError, TypeError):  # pragma: no cover - defensive: inspect rejects rare builtins
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


def create_comfy_node(
    func: Callable,
    name: str,
    category: str = "Taidal",
    display_name: Optional[str] = None,
) -> Type:
    """Dynamically construct a ComfyUI node class from a Python callable.

    Dispatches on the callable's DataFlux role: ``Sample``-taking op classes become
    ``SAMPLE -> SAMPLE`` nodes, ``Sample``-yielding source classes become ``-> SOURCE``
    nodes, and everything else falls back to a generic scalar / STRING node.
    """
    if _op_call_signature(func) is not None:
        return _build_op_node(func, name, category)
    if _is_source(func):
        return _build_source_node(func, name, category)
    return _build_generic_node(func, name, category)


def _humanize_node_name(name: str) -> str:
    """Palette label for a discovered callable: the identifier verbatim, ``_`` → space.

    Deliberately does NOT use ``str.title()`` — that lowercases the tail of every
    word and destroys the workspace's CamelCase class names (``HuggingFaceSource``
    → ``Huggingfacesource``). Capitals are preserved as written: ``HuggingFaceSource``
    stays ``HuggingFaceSource``; ``rescale_op`` becomes ``rescale op``.
    """
    return name.replace("_", " ")


def register_module_nodes(
    module_path: str,
    target_mappings: Dict[str, Type],
    display_name_mappings: Dict[str, str],
    category: str = "Taidal",
) -> None:
    """Scan a module and register its buildable callables as ComfyUI nodes.

    Registration is a POSITIVE allowlist: a discovered class becomes a node ONLY if its
    ``@configurable`` discovery category is in :data:`_NODE_CATEGORIES` (``op`` / ``source`` /
    ``dataset``). Everything else is excluded by construction — the generic engines
    (``Flux`` / ``JointFlux`` / ``DatasetSplit``) and raw-callable op wrappers
    (``FilterOp`` / ``WrappedOp``), both ``category="engine"``; uncategorised module-level
    helper functions and raw sinks; and ``_``-private callables (skipped explicitly so a
    private op that *was* tagged still never surfaces). So a class only appears once it is
    deliberately tagged as a buildable op/source/dataset.
    """
    logger.info(f"ComfyFlux: Scanning module {module_path} for nodes...")
    discovered = scan_module(module_path)

    for item in discovered:
        if item["name"].startswith("_"):
            logger.debug(f"ComfyFlux: Skipping private callable {item['path']}")
            continue

        target_func = resolve_callable(item["path"])
        if not callable(target_func):
            continue

        if getattr(target_func, "__confluid_category__", None) not in _NODE_CATEGORIES:
            logger.debug(f"ComfyFlux: Skipping {item['path']} (category not in {_NODE_CATEGORIES})")
            continue

        node_name = f"Taidal_{item['name']}"
        node_class = create_comfy_node(
            func=target_func,
            name=node_name,
            category=category,
        )
        target_mappings[node_name] = node_class
        display_name_mappings[node_name] = _humanize_node_name(item["name"])
        logger.debug(f"ComfyFlux: Registered node {node_name} for {item['path']}")
