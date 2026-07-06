"""Compile a FluxStudio canvas graph's **ops** into a Confluid YAML op-list.

A FluxStudio canvas is a :class:`dataflux.core.Flux` turned inside-out — the source
is a ``-> DATAFLUX_SOURCE`` node, each transform a ``DATAFLUX_SAMPLE -> DATAFLUX_SAMPLE``
op node, and the lazy ``__iter__`` is replaced by the Walk/Extract adapters. This module
reverses the *ops half*: given a ComfyUI **prompt / API-format** graph it compiles the op
DAG into ONE SEQUENTIAL Confluid ``!class:...()`` op-list per weakly-connected component
(dropping the source + adapter nodes), so a user can reattach it to their own source::

    import confluid
    from dataflux import Flux
    ops = confluid.load("ops.yaml")["ops"]
    flux = Flux(source=my_own_source, ops=ops)

**DAG → sequential conversion** (the compiler is no longer limited to linear chains):

* **fan-out** (one op output → several op nodes) schedules the branches one after the
  other; the fork-point sample is snapshotted with :class:`dataflux.ops.stash.StashInputOp`
  / ``StashTargetOp`` and restored (``Unstash*Op``) before each later branch, so the
  branchy canvas wiring becomes one flat op sequence. ``metadata`` deliberately accumulates
  across branches (the Tee / metadata-bus convention) — per-branch metadata isolation is
  NOT reproduced. The inserted ``__taidal_stash_*`` snapshot is CLEANED UP by its final
  unstash (``Unstash*Op`` defaults to ``remove=True``; a fan-out's non-final unstashes carry
  ``remove=False`` so the key survives until the last branch), so it does not linger into a sink.
* **fan-in** via the canvas ``Mix Samples`` adapter (``Taidal_MixSamples``: input from A,
  target from B, metadata = union of all three) compiles to the equivalent unstashes —
  branches run in Mix-argument order so the metadata-sample branch writes last (matching
  Mix's ``{**m1, **m2, **m3}`` priority) and the merge node itself disappears.
* the **deepest sink is scheduled last**, so the document's final sample is the longest
  path's output; viewer-only side branches run earlier (delete them from the YAML if
  unwanted).

**Wired widget values** (a non-``sample`` input driven by a link) no longer always drop:

* a native ComfyUI primitive pass-through (``PrimitiveInt`` / ``PrimitiveFloat`` /
  ``PrimitiveString`` / ``PrimitiveStringMultiline`` / ``PrimitiveBoolean``) folds INLINE
  as its literal ``value`` widget;
* a Taidal VALUE node marked ``@configurable(constant=True)`` is HOISTED as a top-level
  deferred ``!class:`` document entry carrying its widget settings verbatim, and the
  consumer kwarg becomes a dotted ``!ref:<name>.<output>`` (confluid attribute access).
  Canonical case: ``ImpairmentsTxConfig.disabled`` feeding ``ImpairmentsChain.tx_disabled``;
* anything else is omitted with a note (the op keeps its constructor default) — a
  per-sample runtime value should ride **metadata** instead (expression-capable params
  like ``ThresholdOp.low_level`` accept ``"{meta_key}"`` resolved from ``sample.meta``
  at call time).

``op_N`` links (an op consumed as a ``DATAFLUX_OP`` value by a higher-order op such as
``Tee`` / ``Enable`` / ``Parallel``) compile too: each wired producer embeds as a nested
``!class:`` instance in the consumer's op-list kwarg (``branches`` entries are wrapped as
1-op branches, mirroring the live node).

Input format — the ComfyUI prompt / API graph (what ``app.graphToPrompt()`` emits and
``/prompt`` consumes), NOT the richer UI ``workflow.json``::

    {"<node_id>": {"class_type": "Taidal_dataflux_op_numpy_RescaleOp",
                   "inputs": {"sample": ["3", 0], "factor": 2.0}}, ...}

``inputs`` values are either literals (widget values) or ``["<upstream_id>", <slot>]`` links.

Still unsupported (:class:`UnsupportedTopologyError`): cycles, an unresolvable op node,
and a component that merges DISTINCT sample streams (e.g. two different sources fanning
into one Mix) — a sequential op-list carries exactly one stream; use ``JointFlux`` by hand.
"""

import inspect
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Set, Tuple, Union, cast

from confluid import dump
from confluid.fluid import Class, Instance, Reference
from dataflux.discovery import resolve_callable
from dataflux.ops.capture import CaptureOutputOp
from dataflux.ops.configure import ConfigureOp
from dataflux.ops.formula import FormulaOp
from dataflux.ops.stash import StashInputOp, StashTargetOp, UnstashInputOp, UnstashTargetOp
from dataflux.ops.transform_chain import TransformChain
from loggair import get_logger

from fluxstudio.bridge import _SINGLE_OP_PARAMS, _coerce_kwargs, _op_list_params, _single_op_params

logger = get_logger(__name__)

# The single ``DATAFLUX_SAMPLE`` input socket every generated op node declares
# (see ``bridge._build_op_node``) — the one edge that defines op-to-op data flow.
_SAMPLE_INPUT = "sample"

# The canvas fan-in adapter (``fluxstudio.nodes.MixSamplesNode``): input from arg 1,
# target from arg 2, metadata = the shallow union of all three (arg 3 wins).
_MIX_CLASS_TYPE = "Taidal_MixSamples"
_MIX_ARGS = ("input_sample", "target_sample", "metadata_sample")

# The "subgraph as an Op" terminator (``fluxstudio.nodes.SubgraphOpExtractNode``): its ``op``
# output (slot 0) is the upstream op DAG compiled to ONE ``TransformChain``. When that output
# feeds a consumer's op slot, the export embeds ``TransformChain(ops=<compiled ancestor cone>)``
# (DAG→sequential, the same compiler) and the cone's inner ops drop from the standalone schedule.
_SUBGRAPH_OP_CLASS_TYPE = "Taidal_SubgraphOp"
# The hand-written ``Value`` node (``nodes.ValueNode``): a STRING ``value`` widget echoed to a
# ``DATAFLUX_VALUE`` output as a parsed Python literal — the producer for a NESTED-container param
# (``OverlayBoxes.boxes``) that rides a value socket. Folds INLINE as its parsed literal.
_VALUE_NODE_CLASS_TYPE = "Taidal_Value"

# Native ComfyUI pass-through primitives (``comfy_extras/nodes_primitive.py``): a single
# ``value`` widget echoed to the output — foldable as that literal. The legacy virtual
# ``PrimitiveNode`` never reaches the API prompt (the frontend folds it), but it DOES
# appear in saved UI ``workflow.json`` graphs — graphio's normalizer captures its first
# widget as ``value`` so those wires inline too.
_PRIMITIVE_VALUE_NODES = {
    "PrimitiveString",
    "PrimitiveStringMultiline",
    "PrimitiveInt",
    "PrimitiveFloat",
    "PrimitiveBoolean",
    "PrimitiveNode",
}

# Metadata keys used by the inserted stash/unstash ops. They stay in metadata after the
# run — namespaced so they never collide with real pipeline keys.
_STASH_KEY_PREFIX = "__taidal_stash_"

# Op nodes expose their declared @output properties as output sockets at slot >= 2 (slot 0 =
# ``sample``, slot 1 = ``op`` — see bridge._build_op_node). A wire from such a slot into a LATER
# op's PARAM is an op-@output capture: the producer is wrapped in CaptureOutputOp (records the
# ACTUAL @output value into metadata) and the consumer in ConfigureOp(ops=[UnstashInputOp(key)]).
# The captured value rides the metadata bus under a namespaced key (it persists after the run).
_OP_OUTPUT_FIRST_SLOT = 2
_OUTPUT_CAPTURE_PREFIX = "__taidal_output_"

# The canvas-only mark: a node whose ``properties["taidal_export"] == "skip"`` (toggled by
# ``web/export_skip.js``) runs on the canvas but is EXCLUDED from exports — it, everything
# data-downstream of it (sample wires + Mix args), AND every upstream dependency that
# serves ONLY excluded nodes (so a marked preview branch doesn't leave its private feeder
# ops behind). The canvas export button collects the marked ids and posts them as
# ``skip_ids``; the CLI takes ``--skip``. (ComfyUI's native mute/bypass need no mark: the
# frontend drops/reroutes those nodes before the API prompt ever reaches the exporter.)
# The INVERSE selection is the TARGET mark (``"target"``): when any node is marked a
# target, the export contains exactly the ops that COMPUTE the target(s) — its data
# ancestor cone — one document per target.
_EXPORT_SKIP_PROPERTY = "taidal_export"
_EXPORT_SKIP_VALUE = "skip"
_EXPORT_TARGET_VALUE = "target"

# Virtual node id for "the component's incoming sample" (whatever the Flux source feeds).
_ROOT = "__taidal_root__"

# Dynamic ``DATAFLUX_OP`` value sockets a higher-order op node grows (``web/op_list.js``). The dataflux
# higher-order ops use the ``op_N`` family; a waivefront-helios composite can carry SEVERAL families
# under its own helios param names (``true_transforms_N`` …) — recognised class-aware via
# :func:`_op_list_socket_param` rather than this fixed pattern (kept as the unstamped fallback).
_OP_SOCKET_RE = re.compile(r"^op_(\d+)$")


def _op_class_of(node_cls: Any) -> Any:
    """The op CLASS wrapped by a generated op-node class (via ``TAIDAL_CALLABLE_PATH``), or ``None``.

    The op class is the authority on which constructor params accept op VALUES — ``bridge._single_op_params``
    / ``_op_list_params`` introspect IT (single-op params + the dynamic op-list families, incl. the
    stamped helios slots ``compute`` / ``true_transforms``). ``None`` when the path is missing/unresolvable
    (a non-op / native node), so the callers fall back to the dataflux ``op_N`` / ``op`` / ``target`` names.
    """
    path = getattr(node_cls, "TAIDAL_CALLABLE_PATH", None)
    if not path:
        return None
    try:
        return resolve_callable(path)
    except Exception:  # noqa: BLE001 - an unresolvable path simply means "no op-value introspection"
        return None


def _op_list_socket_param(op_cls: Any, key: str) -> Optional[str]:
    """The op-LIST constructor param whose dynamic ``<prefix>_N`` socket family ``key`` belongs to, else None.

    Routes each wired op-list socket to the right kwarg even when a node has SEVERAL families
    (``HeliosIfOp`` → ``true_transforms_N`` + ``false_transforms_N``); the dataflux ``op_N`` family maps
    to its single op-list param. ``None`` ``op_cls`` (unstamped/native) recognises nothing here.
    """
    if op_cls is not None:
        for param, prefix in _op_list_params(op_cls).items():
            if key.startswith(prefix + "_") and key[len(prefix) + 1 :].isdigit():
                return param
    return None


def _is_op_value_key(op_cls: Any, key: str) -> bool:
    """True if ``key`` is an op-VALUE input socket on ``op_cls`` — a single-op param (``ConfigureOp.target``
    / a stamped helios ``compute``) or an indexed op-list socket (``op_0`` / ``true_transforms_0``).

    These wires embed through the op-value path (nested ``!class:`` / list kwarg), NOT the widget/value-chain
    pre-passes — so the pre-passes SKIP them. Falls back to the dataflux name conventions when ``op_cls`` is
    unavailable.
    """
    if op_cls is None:
        return bool(_OP_SOCKET_RE.match(key)) or key in _SINGLE_OP_PARAMS
    return key in _single_op_params(op_cls) or _op_list_socket_param(op_cls, key) is not None


# The canvas sample→value boundary + scalar-math adapters a VALUE CHAIN walks through
# (``fluxstudio.nodes.SampleExtractorNode`` / ``MathNode``): an op widget wired from
# ``… op-chain → Extract(field=input) → Math* → widget`` compiles to a ``ConfigureOp``.
_EXTRACT_CLASS_TYPE = "Taidal_ExtractSample"
_MATH_CLASS_TYPE = "Taidal_Math"
_MATH_VARS = ("a", "b", "c", "d", "e")


class _ValueChain(NamedTuple):
    """A compiled per-sample value derivation feeding an op widget.

    ``op_ids`` are the sample ops between the consumer's own sample predecessor and the
    Extract boundary (in flow order); ``formulas`` the Math steps as ``(formula, var)``;
    ``adapter_ids`` the consumed Extract/Math canvas nodes (so the whole-graph export can
    suppress their "left out" listing — their semantics live on in the ConfigureOp).
    """

    op_ids: Tuple[str, ...]
    formulas: Tuple[Tuple[str, str], ...]
    adapter_ids: Tuple[str, ...]


class _OutputCapture(NamedTuple):
    """A compiled op-``@output`` → param wire (e.g. ``NoiseFloorOp.applied_snr_db`` → another op's param).

    ``producer_id`` is the op node whose ``@output`` is captured (a sample-ancestor of the consumer),
    ``output_name`` the ``@output`` property (``RETURN_NAMES[slot]``), and ``stash_key`` the namespaced
    metadata key the captured value lands in. The producer is wrapped in ``CaptureOutputOp`` and the
    consumer in ``ConfigureOp(ops=[UnstashInputOp(stash_key)], target=…, param=…)``.
    """

    producer_id: str
    output_name: str
    stash_key: str


class UnsupportedTopologyError(ValueError):
    """The canvas op-graph can't be expressed as a sequential op-list document.

    Raised for cycles, an op node whose wrapped callable can't be resolved, a component
    merging several DISTINCT sample streams (two different sources fanning into one
    ``Mix Samples`` — a sequential op-list carries one stream), and, from the strict
    single-chain forms (:func:`compile_ops` / :func:`export_ops_yaml`) only, multiple
    disconnected op components (the per-component forms :func:`compile_op_chains` /
    :func:`export_ops_yamls` accept those). A direct :func:`_op_to_instance` call with no
    note accumulator also raises on any link-driven widget input.
    """


def _is_link(value: Any) -> bool:
    """True if a prompt ``inputs`` value is a wire ``[upstream_node_id, output_slot]``.

    In the API format a link is a 2-list whose first element is the upstream node id
    (a string) and whose second is an integer output slot. Widget values arrive as
    scalars/strings (FluxStudio renders ``List`` params as comma-separated STRINGs, not
    lists — see ``bridge._coerce_kwargs``), so this heuristic doesn't collide with them.
    """
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and isinstance(value[0], (str, int))
        and not isinstance(value[0], bool)
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
    )


def _node_kind(class_type: str, node_mappings: Dict[str, Any]) -> Optional[str]:
    """The ``TAIDAL_NODE_KIND`` of a node's class, or ``None`` for boundary nodes.

    Boundary = anything that is not a generated op node: sources, generic value nodes,
    the hand-written Walk/Extract adapters, and native ComfyUI nodes (whose ``class_type``
    isn't in FluxStudio's ``node_mappings`` at all).
    """
    cls = node_mappings.get(class_type)
    return getattr(cls, "TAIDAL_NODE_KIND", None) if cls is not None else None


def _sample_predecessor(node: Dict[str, Any]) -> Optional[str]:
    """Upstream node id feeding this node's ``sample`` socket, or ``None`` if unconnected."""
    sample_in = node.get("inputs", {}).get(_SAMPLE_INPUT)
    return str(sample_in[0]) if _is_link(sample_in) else None


def _id_sort_key(nid: str) -> Any:
    """Deterministic node-id ordering: numeric-aware so ``'2' < '10'``, strings after."""
    return (0, int(nid)) if nid.isdigit() else (1, nid)


def _snake(class_name: str) -> str:
    """``ImpairmentsTxConfig`` → ``impairments_tx_config`` (document-entry naming)."""
    out = []
    for i, ch in enumerate(class_name):
        if ch.isupper() and i > 0:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def _marker(fluid_cls: Any, target: Any, kwargs: Dict[str, Any]) -> Any:
    """Build a Confluid Fluid marker with kwargs set AFTER construction.

    ``Instance(cls, **kwargs)`` collides when an op has a constructor param literally named
    ``target`` (e.g. ``ConfigureOp.target``) — the marker ctor's own first parameter. The
    marker stores ``self.kwargs = kwargs`` verbatim, so a post-construction update is
    exactly equivalent and collision-proof.
    """
    marker = fluid_cls(target)
    marker.kwargs.update(kwargs)
    return marker


def _param_admits_str(target: Any, key: str) -> bool:
    """True when the constructor param's annotation admits ``str`` (expression-capable hint)."""
    try:
        anno = inspect.signature(target).parameters[key].annotation
    except (KeyError, TypeError, ValueError):
        return False
    if anno is inspect.Parameter.empty:
        return False
    return "str" in str(anno)


class _FoldContext:
    """Per-document accumulator for the widget-wire folding pass.

    Holds the prompt + node mappings the resolvers need, the document's omission
    ``notes``, the HOISTED constant entries (``name -> Class marker``, insertion
    ordered — they dump above ``ops:``), and the pre-traced per-consumer VALUE CHAINS
    (``consumer id -> [(param, _ValueChain)]`` — compiled to ``ConfigureOp`` wraps).
    One context per exported document, so a constant wired into two ops in the same
    component hoists exactly once.
    """

    def __init__(
        self,
        prompt: Dict[str, Any],
        node_mappings: Dict[str, Any],
        notes: List[str],
        configure_specs: Optional[Dict[str, List[Tuple[str, "_ValueChain"]]]] = None,
        excluded: Optional[Set[str]] = None,
        output_captures: Optional[Dict[str, List[Tuple[str, "_OutputCapture"]]]] = None,
    ) -> None:
        self.prompt = prompt
        self.node_mappings = node_mappings
        self.notes = notes
        self.configure_specs = configure_specs or {}
        # op-@output → param captures (consumer id -> [(param, capture)]). The consumer is wrapped in
        # ConfigureOp(UnstashInputOp); the producer is wrapped in CaptureOutputOp — `captures_by_producer`
        # is the reverse map (producer id -> {output_name: stash_key}) so a producer feeding several
        # consumers captures each @output exactly once.
        self.output_captures = output_captures or {}
        self.captures_by_producer: Dict[str, Dict[str, str]] = {}
        for _consumer, cap_list in self.output_captures.items():
            for _param, cap in cap_list:
                self.captures_by_producer.setdefault(cap.producer_id, {})[cap.output_name] = cap.stash_key
        # Value-chain members (re-run inside a ConfigureOp). Honoured when a Subgraph-Op
        # terminator's cone is compiled via :func:`_subgraph_op_instance` (which restricts to
        # the cone but must still drop these); distinct from the standalone-schedule exclusion.
        self.excluded: Set[str] = excluded or set()
        self.constants: "OrderedDict[str, Any]" = OrderedDict()
        self._hoisted: Dict[str, str] = {}  # upstream node id -> assigned document name
        self._hoisting: Set[str] = set()  # cycle guard for constant→constant recursion

    def claim_name(self, base: str) -> str:
        """A document key for a hoisted constant: ``base``, then ``base_2``, ``base_3``, …"""
        name, i = base, 1
        while name in self.constants or name == "ops":
            i += 1
            name = f"{base}_{i}"
        return name


def _trace_value_chain(
    link: Any, consumer_pred: Optional[str], prompt: Dict[str, Any], node_mappings: Dict[str, Any]
) -> Optional[_ValueChain]:
    """Trace a widget wire backwards through ``Math* ← Extract(field=input) ← op-chain``.

    The chain must root at the CONSUMER's own sample predecessor (the shared fork), so the
    derivation can re-run on the consumer's incoming sample inside a ``ConfigureOp``.
    Returns ``None`` whenever the wire isn't that shape (multi-var Math, a target-field
    Extract, a different origin, …) — the caller falls back to the omit-with-note path.
    """
    formulas: List[Tuple[str, str]] = []
    adapter_ids: List[str] = []
    cur = link
    while True:  # Math steps, innermost-first
        node = prompt.get(str(cur[0]))
        if node is None:
            return None
        if str(node.get("class_type", "")) != _MATH_CLASS_TYPE:
            break
        inputs = node.get("inputs", {})
        wired = [(var, inputs[var]) for var in _MATH_VARS if _is_link(inputs.get(var))]
        formula = inputs.get("formula")
        if len(wired) != 1 or not isinstance(formula, str) or not formula.strip():
            return None
        formulas.insert(0, (formula, wired[0][0]))
        adapter_ids.append(str(cur[0]))
        cur = wired[0][1]

    extract = prompt.get(str(cur[0]))
    if extract is None or str(extract.get("class_type", "")) != _EXTRACT_CLASS_TYPE:
        return None
    if str(extract.get("inputs", {}).get("field", "input")) != "input":
        return None  # only the input-field view is the op-chain's flowing value
    adapter_ids.append(str(cur[0]))
    sample_link = extract.get("inputs", {}).get(_SAMPLE_INPUT)
    if not _is_link(sample_link):
        return None

    op_ids: List[str] = []
    cur = sample_link
    while True:
        src = str(cur[0])
        if src == consumer_pred:
            break
        if _node_kind(str(prompt.get(src, {}).get("class_type", "")), node_mappings) != "op":
            return None
        op_ids.insert(0, src)
        nxt = prompt[src].get("inputs", {}).get(_SAMPLE_INPUT)
        if not _is_link(nxt):
            if consumer_pred is None:  # both the chain and the consumer hang off the raw stream
                break
            return None
        cur = nxt
    return _ValueChain(tuple(op_ids), tuple(formulas), tuple(adapter_ids))


def _collect_value_chains(
    prompt: Dict[str, Any], node_mappings: Dict[str, Any]
) -> Tuple[Dict[str, List[Tuple[str, _ValueChain]]], Set[str]]:
    """Pre-trace every op widget wire that is a compilable VALUE CHAIN.

    Returns ``(specs, excluded)``: ``specs`` maps a consumer node id to its
    ``(param, chain)`` list (each becomes a ``ConfigureOp`` wrap), and ``excluded`` the
    chain-member op ids whose sample output feeds ONLY the chain (pure value branches) —
    those leave the sample schedule entirely (they re-run inside the ConfigureOp instead
    of dangling as a dead-end branch).
    """
    specs: Dict[str, List[Tuple[str, _ValueChain]]] = {}
    chain_members: Set[str] = set()
    for nid, node in prompt.items():
        if _node_kind(str(node.get("class_type", "")), node_mappings) != "op":
            continue
        inputs = node.get("inputs", {})
        pred = inputs.get(_SAMPLE_INPUT)
        consumer_pred = str(pred[0]) if _is_link(pred) else None
        op_cls = _op_class_of(node_mappings.get(str(node.get("class_type", ""))))
        for key, value in inputs.items():
            if key == _SAMPLE_INPUT or not _is_link(value):
                continue
            if _is_op_value_key(op_cls, key):
                continue  # op-value wires embed through their own path
            chain = _trace_value_chain(value, consumer_pred, prompt, node_mappings)
            if chain is None:
                continue
            specs.setdefault(str(nid), []).append((key, chain))
            chain_members.update(chain.op_ids)

    excluded: Set[str] = set()
    for member in chain_members:
        consumers_ok = True
        for cnid, cnode in prompt.items():
            for value in cnode.get("inputs", {}).values():
                if _is_link(value) and str(value[0]) == member:
                    in_chain = str(cnid) in chain_members
                    is_boundary = str(cnode.get("class_type", "")) == _EXTRACT_CLASS_TYPE
                    if not (in_chain or is_boundary):
                        consumers_ok = False
        if consumers_ok:
            excluded.add(member)
    return specs, excluded


def _trace_output_capture(
    link: Any, consumer_id: str, prompt: Dict[str, Any], node_mappings: Dict[str, Any]
) -> Optional[_OutputCapture]:
    """Trace a widget wire to an op node's ``@output`` slot (>= 2) — an op-``@output`` capture.

    Returns the capture iff the wire comes from an op-kind node's ``@output`` output socket AND that
    producer is a sample-ANCESTOR of the consumer (so it runs earlier in the schedule and its
    captured value is on the metadata bus when the consumer reads it). Returns ``None`` otherwise
    (slot 0/1 = ``sample``/``op``, a non-op or self producer, or a cross-branch producer) — the
    caller then falls back to the omit-with-note path.
    """
    if not _is_link(link):
        return None
    producer_id, slot = str(link[0]), int(link[1])
    producer = prompt.get(producer_id)
    if producer is None or producer_id == consumer_id:
        return None  # self-wire (a cycle) is not a capture
    class_type = str(producer.get("class_type", ""))
    if _node_kind(class_type, node_mappings) != "op":
        return None
    return_names = tuple(getattr(node_mappings.get(class_type), "RETURN_NAMES", ()) or ())
    if slot < _OP_OUTPUT_FIRST_SLOT or slot >= len(return_names):
        return None  # slot 0/1 are sample/op (handled elsewhere); out-of-range = not an @output
    if producer_id not in _ancestor_cone(consumer_id, prompt):
        return None  # cross-branch: the captured value wouldn't be on the bus — fall back to a note
    output_name = str(return_names[slot])
    return _OutputCapture(producer_id, output_name, f"{_OUTPUT_CAPTURE_PREFIX}{producer_id}_{output_name}")


def _collect_output_captures(
    prompt: Dict[str, Any], node_mappings: Dict[str, Any]
) -> Dict[str, List[Tuple[str, _OutputCapture]]]:
    """Pre-trace every op-param wire that captures another op's ``@output`` (output slot >= 2).

    Maps a consumer node id to its ``(param, capture)`` list — each compiles to a ``CaptureOutputOp``
    on the producer + a ``ConfigureOp(ops=[UnstashInputOp(key)])`` on the consumer. Disjoint from
    value chains by construction (a value chain's immediate upstream is a Math/Extract adapter, not an
    op-kind node), so this runs alongside :func:`_collect_value_chains`. Both the producer and the
    consumer stay in the schedule (the producer also transforms the sample; nothing is excluded).
    """
    specs: Dict[str, List[Tuple[str, _OutputCapture]]] = {}
    for nid, node in prompt.items():
        if _node_kind(str(node.get("class_type", "")), node_mappings) != "op":
            continue
        op_cls = _op_class_of(node_mappings.get(str(node.get("class_type", ""))))
        for key, value in node.get("inputs", {}).items():
            if key == _SAMPLE_INPUT or not _is_link(value):
                continue
            if _is_op_value_key(op_cls, key):
                continue  # op-value wires embed through their own path
            capture = _trace_output_capture(value, str(nid), prompt, node_mappings)
            if capture is not None:
                specs.setdefault(str(nid), []).append((key, capture))
    return specs


def _data_preds(node: Dict[str, Any]) -> List[str]:
    """Upstream node ids on this node's DATA edges (the ``sample`` wire, or Mix args)."""
    inputs = node.get("inputs", {})
    data_args = _MIX_ARGS if str(node.get("class_type", "")) == _MIX_CLASS_TYPE else (_SAMPLE_INPUT,)
    return [str(inputs[arg][0]) for arg in data_args if _is_link(inputs.get(arg))]


def _id_segments(node_id: Any) -> Set[str]:
    """The original node ids a (possibly subgraph-expanded) PROMPT id is built from.

    ComfyUI expands a subgraph instance into prompt ids that PATH-JOIN the original node ids with
    ``:`` — instance ``378`` of a definition holding node ``371`` → ``"378:371"``, nested →
    ``"378:368:371"``. A plain top-level node keeps its own id. So the segments of a prompt id are
    exactly the original ids on the instance path; node ids are globally unique across the workflow
    and its subgraph definitions, so matching a mark against these segments is unambiguous.
    """
    return set(str(node_id).split(":"))


def _resolve_marked(marked: Set[str], prompt: Dict[str, Any]) -> Set[str]:
    """Every PROMPT id whose ``:``-path contains a marked id as a SEGMENT.

    This resolves a ``taidal_export`` mark to the expanded prompt node(s) it names, regardless of
    where it sits: a top-level node (exact), a subgraph INSTANCE (matches every inner node ``id:*``),
    or a node INSIDE a definition (that specific node ``*:id`` in each instance). It generalizes the
    old prefix-only match (instance → inner), which could not reach a mark placed inside a subgraph —
    the case the ops-export now honours via :func:`collect_export_marks`.
    """
    return {nid for nid in prompt if marked & _id_segments(nid)}


def _marked_ids(skip_ids: Any, prompt: Dict[str, Any]) -> Set[str]:
    """The DIRECTLY marked node set: each given id resolved to the expanded prompt ids that name it.

    Marking a subgraph INSTANCE (``"378"``) matches its whole expanded subtree (``"378:371"`` …);
    marking a node INSIDE a definition (``"371"``) matches that node in each instance (``"378:368:371"``);
    a top-level id matches itself. See :func:`_resolve_marked` / :func:`_id_segments`.
    """
    marked = {str(sid) for sid in (skip_ids or [])}
    if not marked:
        return set()
    return _resolve_marked(marked, prompt)


def collect_export_marks(workflow: Any) -> Tuple[List[str], List[str]]:
    """Read ``taidal_export`` skip/target node ids from a UI ``workflow.json`` (subgraphs included).

    The ops-export consumes the ComfyUI PROMPT form, which carries NO node ``properties`` — so the
    export marks (``node.properties["taidal_export"]`` ∈ ``"skip"`` / ``"target"``), which the canvas
    stores on the live graph, must be read from the UI workflow instead. This scans BOTH the
    top-level nodes AND every subgraph DEFINITION's inner nodes: a mark placed inside a subgraph
    lives on a definition node, which the prompt expands to ``"<instance>:<inner>"`` and the segment
    match in :func:`_marked_ids` / the target resolver maps back. Returns ``(skip_ids, target_ids)``
    as string node ids — pass straight to :func:`export_ops_yamls` / :func:`compile_op_chains`.

    Collecting the marks HERE (Python) rather than in the browser is the single, subgraph-aware
    source of truth — mirroring how the whole-graph export already reads the same property from the
    workflow (``graphio._normalize_ui_workflow``). A non-dict / property-less input yields ``([], [])``.
    """
    skip: List[str] = []
    target: List[str] = []
    if not isinstance(workflow, dict):
        return skip, target

    def _scan(nodes: Any) -> None:
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            mark = (node.get("properties") or {}).get(_EXPORT_SKIP_PROPERTY)
            if mark == _EXPORT_SKIP_VALUE:
                skip.append(str(node.get("id")))
            elif mark == _EXPORT_TARGET_VALUE:
                target.append(str(node.get("id")))

    _scan(workflow.get("nodes"))
    for subgraph in (workflow.get("definitions") or {}).get("subgraphs") or []:
        if isinstance(subgraph, dict):
            _scan(subgraph.get("nodes"))
    return skip, target


def _expand_skip_ids(skip_ids: Any, prompt: Dict[str, Any]) -> Set[str]:
    """Resolve user-marked skip ids to the full excluded node set.

    Two expansions: a marked id also matches its SUBGRAPH-inlined inner nodes
    (``"125"`` excludes ``"125:121"`` — marking an instance excludes the whole instance);
    and the set closes DOWNSTREAM over data edges (``sample`` wires + Mix arguments) —
    everything fed from a marked node has no exportable feed, so it is canvas-only too.

    The closure NEVER walks upstream: a skip mark means "this node and everything BEHIND
    it (data-downstream); the nodes before it still export" — pruning the marked node's
    sample feeders would eat the pipeline that computes the data (marking the node after
    a Mix must not erase the Mix). Ops the marked branch no longer consumes simply stay
    in the schedule; value/config feeders need no pruning here because the ops-export
    folds them CONSUMER-driven (a constant hoists only when a surviving consumer
    references it). Widget/op-value wires from an excluded node do NOT exclude the
    consumer (it just loses that value and keeps its constructor default, with a note).
    """
    excluded = _marked_ids(skip_ids, prompt)
    if not excluded:
        return set()
    while True:
        grew = False
        for nid, node in prompt.items():
            if nid in excluded:
                continue
            # Downstream: any data predecessor excluded → this node has no exportable feed.
            if any(pred in excluded for pred in _data_preds(node)):
                excluded.add(nid)
                grew = True
        if not grew:
            return excluded


def _ancestor_cone(target_id: str, prompt: Dict[str, Any]) -> Set[str]:
    """The export-target's dependency cone: the target + every data ancestor.

    These are exactly the ops that COMPUTE the target — sample wires and Mix arguments
    followed transitively upstream. (Widget-folded inputs — constants, value chains,
    op-values — resolve by reference during compilation and need no membership here.)
    """
    cone: Set[str] = set()
    stack = [target_id]
    while stack:
        nid = stack.pop()
        if nid in cone or nid not in prompt:
            continue
        cone.add(nid)
        stack.extend(_data_preds(prompt[nid]))
    return cone


def _runtime_value_hint(target: Any, key: str) -> Optional[str]:
    """The per-sample-value note suffix: the sanctioned runtime mechanisms.

    Always points at ``dataflux.ops.configure.ConfigureOp`` (compute the value from the
    sample and inject it as the param — works for ANY op attribute); additionally mentions
    the metadata-expression form when the param's annotation admits ``str``.
    """
    hint = f"; for a per-sample value wrap the op in dataflux ConfigureOp " f"(compute ops + target + param={key!r})"
    if _param_admits_str(target, key):
        hint += " or set the widget to a '{meta_key}' expression resolved from sample.meta at call time"
    return hint


def _resolve_widget_link(target: Any, key: str, link: Any, fold: _FoldContext) -> Tuple[str, Any]:
    """Try to fold a link-driven widget value into something a static op-list can hold.

    Returns one of ``("literal", value)`` (a native primitive's widget, inlined),
    ``("ref", Reference)`` (a hoisted ``constant=True`` value node — dotted
    ``!ref:<name>.<output>``), or ``("drop", note_suffix_or_None)`` (not foldable; the
    caller appends the omission note, with the suffix explaining what WOULD make it
    exportable).
    """
    upstream_id, slot = str(link[0]), int(link[1])
    upstream = fold.prompt.get(upstream_id)
    if upstream is None:
        return ("drop", _runtime_value_hint(target, key))
    class_type = str(upstream.get("class_type", ""))

    if class_type == _SUBGRAPH_OP_CLASS_TYPE:
        # A Subgraph-Op terminator's ``op`` output (slot 0) wired into a single-op slot
        # (``ConfigureOp.target`` / ``RandomApply.op``): embed its compiled ancestor cone as a
        # nested ``TransformChain`` instance, the "subgraph as an Op" landing in that slot.
        if slot == 0:
            return ("literal", _subgraph_op_instance(upstream_id, fold.prompt, fold.node_mappings, fold))
        return ("drop", _runtime_value_hint(target, key))

    if class_type in _PRIMITIVE_VALUE_NODES:
        value = upstream.get("inputs", {}).get("value")
        if _is_link(value):  # a primitive fed by another node — follow the wire
            return _resolve_widget_link(target, key, value, fold)
        return ("literal", value)

    if class_type == _VALUE_NODE_CLASS_TYPE:
        # The Value node's STRING literal → the real Python object, inlined as the consumer's kwarg
        # (a list of boxes, a dict). Lazy import keeps export.py ↔ nodes.py acyclic.
        from fluxstudio.nodes import parse_value_literal

        value = upstream.get("inputs", {}).get("value")
        if _is_link(value):
            return _resolve_widget_link(target, key, value, fold)
        return ("literal", parse_value_literal(value if isinstance(value, str) else ""))

    node_cls = fold.node_mappings.get(class_type)
    kind = getattr(node_cls, "TAIDAL_NODE_KIND", None)
    if kind == "value":
        if not getattr(node_cls, "TAIDAL_CONSTANT", False):
            return (
                "drop",
                f"; upstream {class_type} is a value node not marked constant — tag the class "
                "@configurable(constant=True) to fold its settings into the export",
            )
        return_names = tuple(getattr(node_cls, "RETURN_NAMES", ()) or ())
        if slot >= len(return_names):
            return ("drop", f"; upstream {class_type} output slot {slot} has no declared @output")
        name = _hoist_constant(upstream_id, upstream, fold)
        return ("ref", Reference(f"{name}.{return_names[slot]}"))

    if kind == "op":
        # A wire from an op node's ``op`` VALUE output (a DATAFLUX_OP wire into a single-op
        # param like ``RandomApply.op`` / ``ConfigureOp.target``) embeds as a nested instance.
        return_names = tuple(getattr(node_cls, "RETURN_NAMES", ()) or ())
        op_slot = return_names.index("op") if "op" in return_names else 1
        if slot == op_slot:
            nested = _op_to_instance(upstream_id, upstream, fold.node_mappings, dropped_notes=fold.notes, fold=fold)
            return ("literal", nested)
        if slot >= _OP_OUTPUT_FIRST_SLOT:
            # An op-@output wire that wasn't captured (handled by _collect_output_captures) — the
            # producer isn't a sample-ANCESTOR of this consumer, so its per-sample value can't reach
            # here through the metadata bus. Drop with a note pointing at the requirement.
            out_name = return_names[slot] if slot < len(return_names) else f"slot {slot}"
            return (
                "drop",
                f"; to wire {class_type}.{out_name} (an @output) into a param, the producing op must "
                "be a sample-ancestor of this op (same chain) so its captured value is on the bus",
            )

    return ("drop", _runtime_value_hint(target, key))


def _hoist_constant(node_id: str, node: Dict[str, Any], fold: _FoldContext) -> str:
    """Hoist a ``constant=True`` value node as a deferred top-level ``!class:`` entry.

    The entry carries the node's literal widget settings (run through the SAME coercion
    the live node uses); its own link-driven widgets recurse through
    :func:`_resolve_widget_link` (a constant wired from another constant chains via
    dotted refs; an unfoldable wire is omitted with a note). Returns the document name
    the dotted ``!ref:`` uses — one hoist per upstream node id.
    """
    if node_id in fold._hoisted:
        return fold._hoisted[node_id]
    if node_id in fold._hoisting:  # pragma: no cover - defensive: prompt graphs are acyclic
        raise UnsupportedTopologyError(f"Cycle among constant value nodes at node {node_id!r}.")
    fold._hoisting.add(node_id)
    try:
        class_type = str(node.get("class_type", ""))
        node_cls = fold.node_mappings.get(class_type)
        path = getattr(node_cls, "TAIDAL_CALLABLE_PATH", None)
        if not path:  # pragma: no cover - defensive: generated value nodes always carry the path
            raise UnsupportedTopologyError(
                f"Constant value node {node_id!r} ({class_type}) has no resolvable TAIDAL_CALLABLE_PATH."
            )
        target = resolve_callable(path)

        literal_inputs: Dict[str, Any] = {}
        ref_inputs: Dict[str, Any] = {}
        for key, value in node.get("inputs", {}).items():
            if not _is_link(value):
                literal_inputs[key] = value
                continue
            outcome, payload = _resolve_widget_link(target, key, value, fold)
            if outcome == "literal":
                literal_inputs[key] = payload
            elif outcome == "ref":
                ref_inputs[key] = payload
            else:
                fold.notes.append(
                    f"{class_type} (node {node_id}, hoisted constant): {key!r} is wired from node "
                    f"{value[0]} — omitted, the constant keeps its constructor default{payload or ''}"
                )

        coerced = _coerce_kwargs(target, literal_inputs)
        coerced.update(ref_inputs)
        name = fold.claim_name(_snake(getattr(target, "__name__", class_type)))
        # A deferred ``Class`` marker (``!class:Foo`` — NO parens): the dotted ``!ref:`` reader
        # instantiates it to read the @output property; nothing eager happens at dump time.
        fold.constants[name] = _marker(Class, cast(type, target), coerced)
        fold._hoisted[node_id] = name
        logger.debug(f"_hoist_constant: node {node_id} ({class_type}) hoisted as {name!r}.")
        return name
    finally:
        fold._hoisting.discard(node_id)


def _embed_op_values(
    class_type: str,
    node_id: str,
    op_links: List[Tuple[str, int, Any]],
    fold: _FoldContext,
) -> Dict[str, Any]:
    """Compile each op-list socket family's links into its constructor kwarg → ``{param: value}``.

    ``op_links`` carries ``(param, index, link)`` — the consuming node may have SEVERAL op-list families
    (a helios ``HeliosIfOp`` → ``true_transforms`` + ``false_transforms``), so links are grouped by param
    and ordered by socket index within each. Each wired producer embeds as a nested ``!class:`` Instance
    (its wired widgets fold through the same pass), or — for a Subgraph-Op terminator — its compiled cone
    as a nested ``TransformChain``. A ``branches`` param wraps each op as its own 1-op branch, mirroring
    the live node (``bridge._build_op_node``). A family with no valid producer is omitted.
    """
    by_param: Dict[str, List[Tuple[int, Any]]] = {}
    for param, idx, link in op_links:
        by_param.setdefault(param, []).append((idx, link))
    result: Dict[str, Any] = {}
    for param, entries in by_param.items():
        instances: List[Any] = []
        for _idx, link in sorted(entries, key=lambda item: item[0]):
            src = str(link[0])
            upstream = fold.prompt.get(src)
            upstream_type = str(upstream.get("class_type", "")) if upstream else ""
            if upstream is not None and upstream_type == _SUBGRAPH_OP_CLASS_TYPE:
                # A Subgraph-Op terminator in this higher-order op's list (e.g. Parallel(ops=[…]))
                # → embed its compiled cone as a nested TransformChain (the subgraph as one Op).
                instances.append(_subgraph_op_instance(src, fold.prompt, fold.node_mappings, fold))
                continue
            upstream_cls = fold.node_mappings.get(upstream_type) if upstream else None
            if upstream is None or getattr(upstream_cls, "TAIDAL_NODE_KIND", None) != "op":
                fold.notes.append(
                    f"{class_type} (node {node_id}): {param!r} entry is wired from node {link[0]} — "
                    "omitted, not a generated op node"
                )
                continue
            instances.append(_op_to_instance(src, upstream, fold.node_mappings, dropped_notes=fold.notes, fold=fold))
        if instances:
            result[param] = [[op] for op in instances] if param == "branches" else instances
    return result


def _op_to_instance(
    node_id: str,
    node: Dict[str, Any],
    node_mappings: Dict[str, Any],
    dropped_notes: Optional[List[str]] = None,
    fold: Optional[_FoldContext] = None,
) -> Instance:
    """Build a Confluid ``Instance`` marker (``!class:X()`` on dump) for one op node.

    Recovers the wrapped class from ``TAIDAL_CALLABLE_PATH``, takes the literal (non-link)
    widget inputs — minus the ``sample`` wire — and runs them through the SAME coercion the
    live node uses (``bridge._coerce_kwargs``), so blank/optional/List widgets serialize the
    way they'd construct. Explicit kwargs on the ``Instance`` make the dump robust regardless
    of whether the op stores each arg as a same-named attribute.

    A widget input driven by a LINK first goes through the folding pass (``fold`` given):
    native primitives inline, ``constant=True`` value nodes hoist + dotted-``!ref:``, and
    ``op_N`` op-value links embed as nested instances. What remains unfoldable is OMITTED —
    the op keeps its constructor default — with a note appended to ``dropped_notes`` for
    the document's comment block. Without ``dropped_notes`` (a direct call), any link raises.
    """
    class_type = node["class_type"]
    cls = node_mappings.get(class_type)
    path = getattr(cls, "TAIDAL_CALLABLE_PATH", None) if cls is not None else None
    if not path:
        raise UnsupportedTopologyError(
            f"Node {node_id!r} ({class_type}) has no resolvable TAIDAL_CALLABLE_PATH; "
            "cannot serialize it as a Confluid !class: op (is it a generated op node?)."
        )
    target = resolve_callable(path)

    cfg_specs = fold.configure_specs.get(str(node_id), []) if fold is not None else []
    configured_params = {param for param, _chain in cfg_specs}
    capture_specs = fold.output_captures.get(str(node_id), []) if fold is not None else []
    captured_params = {param for param, _cap in capture_specs}

    inputs = node.get("inputs", {})
    literal_inputs: Dict[str, Any] = {}
    ref_inputs: Dict[str, Any] = {}
    op_links: List[Tuple[str, int, Any]] = []  # (op-list param, socket index, link)
    for key, value in inputs.items():
        if key == _SAMPLE_INPUT or key in configured_params or key in captured_params:
            continue  # a configured/captured param is injected per sample by the ConfigureOp wrap below
        if not _is_link(value):
            literal_inputs[key] = value
            continue
        list_param = _op_list_socket_param(target, key) if fold is not None else None
        if list_param is not None:
            op_links.append((list_param, int(key.rsplit("_", 1)[1]), value))
            continue
        # A single-op socket (compute/target/delegate/op) folds through _resolve_widget_link below
        # (it embeds an op-output wire as a nested instance, key-agnostically).
        note_suffix: Optional[str] = None
        if fold is not None:
            outcome, payload = _resolve_widget_link(target, key, value, fold)
            if outcome == "literal":
                literal_inputs[key] = payload
                continue
            if outcome == "ref":
                ref_inputs[key] = payload
                continue
            note_suffix = payload
        if dropped_notes is None:
            raise UnsupportedTopologyError(
                f"Input {key!r} on node {node_id!r} ({class_type}) is driven by a link, not a "
                "widget value. Ops export needs literal config values — set it inline on the node."
            )
        logger.debug(
            f"_op_to_instance: omitting {key!r} on node {node_id} ({class_type}) — wired from "
            f"node {value[0]} (runtime value); the op keeps its constructor default."
        )
        dropped_notes.append(
            f"{class_type} (node {node_id}): {key!r} is wired from node {value[0]} — "
            f"omitted, the op keeps its constructor default{note_suffix or ''}"
        )

    coerced = _coerce_kwargs(target, literal_inputs)
    coerced.update(ref_inputs)
    if op_links and fold is not None:
        coerced.update(_embed_op_values(class_type, node_id, op_links, fold))
    # ``target`` is the op's @configurable CLASS; pass the type (not the string path) so the
    # dumper emits the dotted ``!class:module.Qualname()`` form that ``confluid.load`` resolves.
    instance = cast(Instance, _marker(Instance, cast(type, target), coerced))
    if cfg_specs and fold is not None:
        for param, chain in cfg_specs:
            # The value chain re-runs on the op's incoming sample inside a ConfigureOp: the
            # chain ops compute, the Math steps apply as FormulaOps, and the result is set
            # as the wrapped op's ``param`` (the helios Configure pattern, serialized).
            compute: List[Any] = [
                _op_to_instance(mid, fold.prompt[mid], node_mappings, dropped_notes=dropped_notes, fold=fold)
                for mid in chain.op_ids
            ]
            compute += [_marker(Instance, FormulaOp, {"formula": f, "var": v}) for f, v in chain.formulas]
            instance = cast(
                Instance, _marker(Instance, ConfigureOp, {"ops": compute, "target": instance, "param": param})
            )
    # op-@output → param (consumer side): inject the producer's CAPTURED value (read back from the
    # metadata bus via UnstashInputOp) into this op's param per sample.
    for param, cap in capture_specs:
        instance = cast(
            Instance,
            _marker(
                Instance,
                ConfigureOp,
                {
                    # remove=False: the captured ``__taidal_output_*`` value may be read by SEVERAL
                    # consumers of the same producer output, so this unstash must NOT delete the key
                    # (distinct from the fan-out ``__taidal_stash_*`` keys cleaned up in _compile_component).
                    "ops": [_marker(Instance, UnstashInputOp, {"key": cap.stash_key, "remove": False})],
                    "target": instance,
                    "param": param,
                },
            ),
        )
    # op-@output → param (producer side): if THIS op's @output is captured downstream, wrap it so its
    # ACTUAL value lands in metadata after it runs (outermost — it applies the configured op above and
    # reads the @output off the innermost wrapped op via CaptureOutputOp's .target walk).
    produced = fold.captures_by_producer.get(str(node_id)) if fold is not None else None
    if produced:
        instance = cast(Instance, _marker(Instance, CaptureOutputOp, {"op": instance, "captures": dict(produced)}))
    return instance


# --------------------------------------------------------------------------- subgraph-as-op


def _subgraph_op_instance(
    terminator_id: str, prompt: Dict[str, Any], node_mappings: Dict[str, Any], fold: _FoldContext
) -> Instance:
    """Compile a Subgraph-Op terminator's ancestor cone into a ``TransformChain`` Instance.

    The cone (the ops computing the sample wired into the terminator) is flattened with the
    SAME DAG→sequential machinery the top level uses — fan-out and ``Mix`` fan-in become
    ``Stash``/``Unstash`` sequences — so the whole drawn sub-pipeline lands as one
    ``!class:dataflux.ops.transform_chain.TransformChain(ops=[…])``. The shared ``fold`` means a
    ``constant=True`` value node inside the subgraph still hoists ONCE to the document top
    level and the nested ops dotted-``!ref:`` it. ``fold.excluded`` (value-chain members) is
    honoured; the standalone-schedule exclusion (:func:`_subgraph_cone_excluded`) is NOT applied
    here — these cone ops are exactly what we compile.
    """
    cone = _ancestor_cone(terminator_id, prompt)
    universe, preds, boundary_feeds = _build_graph(prompt, node_mappings, fold.excluded, restrict=cone)
    ops: List[Any] = []
    for members in _components(universe, preds):
        _check_component(members, preds, boundary_feeds)
        ops.extend(_compile_component(members, universe, preds, prompt, node_mappings, fold))
    return cast(Instance, _marker(Instance, TransformChain, {"ops": ops}))


def _op_value_consumed(node_id: str, prompt: Dict[str, Any], node_mappings: Dict[str, Any]) -> bool:
    """True if some node consumes ``node_id``'s output via an op-value slot (``op_N`` / ``op`` / ``target``,
    or a stamped helios slot like ``compute`` / ``true_transforms_0``)."""
    for cnode in prompt.values():
        op_cls = _op_class_of(node_mappings.get(str(cnode.get("class_type", ""))))
        for key, value in cnode.get("inputs", {}).items():
            if _is_link(value) and str(value[0]) == node_id and _is_op_value_key(op_cls, key):
                return True
    return False


def _sample_consumed_outside(node_id: str, cone: Set[str], prompt: Dict[str, Any]) -> bool:
    """True if a node OUTSIDE ``cone`` consumes ``node_id``'s sample output (sample wire or Mix arg)."""
    for cnid, cnode in prompt.items():
        if cnid not in cone and node_id in _data_preds(cnode):
            return True
    return False


def _subgraph_cone_excluded(
    prompt: Dict[str, Any], node_mappings: Dict[str, Any], exempt: Optional[Set[str]] = None
) -> Set[str]:
    """Inner ops of each CONSUMED Subgraph-Op terminator → dropped from the standalone schedule.

    When a terminator's ``op`` output feeds an op slot, its ancestor cone is embedded as a
    nested ``TransformChain`` in that consumer, so those inner ops must not ALSO compile to a
    standalone pipeline. A cone op whose sample is consumed OUTSIDE the cone too (a node reused
    in graph mode) stays scheduled — it is then both embedded and scheduled (the shared-node
    case). A terminator whose ``op`` output is unwired excludes nothing (its inner ops, if any,
    schedule as an ordinary pipeline — the "use the subgraph as a graph" path).

    ``exempt`` lists terminator ids whose cone is itself the COMPILE TARGET (``compile_op_chains``
    passes the resolved export-target ids). Such a terminator must NOT be excluded: when
    ``SubgraphOpExtractNode`` compiles its own cone via ``export_ops_yaml(target_ids=[tid])``, the
    same prompt also has that terminator's ``op`` output consumed (by the Flux it feeds), so
    WITHOUT this exemption the terminator would exclude its OWN cone — leaving only the few ops
    whose sample is reused by an off-cone viewer branch, which then fragment into several
    components and trip the strict single-component ``export_ops_yaml`` ("Found N independent op
    components"). Exempting the target is the cone-compile analogue of ``_subgraph_op_instance``,
    which already skips this exclusion entirely ("these cone ops are exactly what we compile").
    """
    exempt = exempt or set()
    excluded: Set[str] = set()
    for tid, node in prompt.items():
        if str(node.get("class_type", "")) != _SUBGRAPH_OP_CLASS_TYPE:
            continue
        if tid in exempt:
            continue
        if not _op_value_consumed(tid, prompt, node_mappings):
            continue
        cone = _ancestor_cone(tid, prompt)
        for member in cone - {tid}:
            if not _sample_consumed_outside(member, cone, prompt):
                excluded.add(member)
    return excluded


# --------------------------------------------------------------------------- DAG scheduling


def _build_graph(
    prompt: Dict[str, Any],
    node_mappings: Dict[str, Any],
    excluded: Optional[Set[str]] = None,
    restrict: Optional[Set[str]] = None,
) -> Tuple[Dict[str, str], Dict[str, List[Tuple[str, str]]], Dict[str, Set[str]]]:
    """The sample-flow universe: generated op nodes + ``Mix Samples`` adapters.

    Returns ``(universe, preds, boundary_feeds)`` where ``universe`` maps node id →
    ``"op"``/``"mix"``, ``preds`` maps node id → its data-flow predecessors in
    deterministic argument order (``_ROOT`` for a boundary/unconnected feed), and
    ``boundary_feeds`` maps node id → the out-of-universe feed markers it touches
    (boundary node ids, or ``"<unconnected>"``) for the one-stream check.

    An op consumed ONLY as a ``DATAFLUX_OP`` value (an ``op_N`` link) with no sample
    wiring is config, not sample flow — it is excluded here and embeds as a nested
    instance in its consumer instead.
    """
    universe: Dict[str, str] = {}
    for nid, node in prompt.items():
        if excluded and nid in excluded:
            continue  # a pure value-chain member — it re-runs inside a ConfigureOp instead
        if restrict is not None and nid not in restrict:
            continue  # export-target mode: only the target's ancestor cone is scheduled
        class_type = str(node.get("class_type", ""))
        if class_type == _MIX_CLASS_TYPE:
            universe[nid] = "mix"
        elif _node_kind(class_type, node_mappings) == "op":
            universe[nid] = "op"

    value_consumed: Set[str] = set()
    for node in prompt.values():
        op_cls = _op_class_of(node_mappings.get(str(node.get("class_type", ""))))
        for key, value in node.get("inputs", {}).items():
            if _is_link(value) and _is_op_value_key(op_cls, key):
                value_consumed.add(str(value[0]))

    preds: Dict[str, List[Tuple[str, str]]] = {}
    boundary_feeds: Dict[str, Set[str]] = {}

    def _resolve_pred(value: Any) -> Tuple[str, Optional[str]]:
        """(pred-or-ROOT, boundary-feed-marker-or-None) for one data input."""
        src = str(value[0]) if _is_link(value) else None
        if src is not None and src in universe:
            return src, None
        return _ROOT, (src if src is not None else "<unconnected>")

    for nid, kind in universe.items():
        inputs = prompt[nid].get("inputs", {})
        feeds: Set[str] = set()
        plist: List[Tuple[str, str]] = []
        arg_names = _MIX_ARGS if kind == "mix" else (_SAMPLE_INPUT,)
        for arg in arg_names:
            pred, feed = _resolve_pred(inputs.get(arg))
            plist.append((arg, pred))
            if feed is not None:
                feeds.add(feed)
        preds[nid] = plist
        boundary_feeds[nid] = feeds

    # Drop op-value-only nodes (no sample in, nothing consumes their sample out).
    sample_consumed = {pred for plist in preds.values() for _arg, pred in plist}
    for nid in list(universe):
        if universe[nid] != "op" or nid not in value_consumed or nid in sample_consumed:
            continue
        if not _is_link(prompt[nid].get("inputs", {}).get(_SAMPLE_INPUT)):
            logger.debug(f"_build_graph: node {nid} is an op-value producer — embedded, not scheduled.")
            del universe[nid], preds[nid], boundary_feeds[nid]

    return universe, preds, boundary_feeds


def _components(universe: Dict[str, str], preds: Dict[str, List[Tuple[str, str]]]) -> List[List[str]]:
    """Weakly-connected components of the sample-flow DAG, ordered by smallest node id."""
    adjacency: Dict[str, Set[str]] = {nid: set() for nid in universe}
    for nid, plist in preds.items():
        for _arg, pred in plist:
            if pred != _ROOT:
                adjacency[nid].add(pred)
                adjacency[pred].add(nid)
    seen: Set[str] = set()
    components: List[List[str]] = []
    for nid in sorted(universe, key=_id_sort_key):
        if nid in seen:
            continue
        stack, members = [nid], []
        seen.add(nid)
        while stack:
            cur = stack.pop()
            members.append(cur)
            for nxt in adjacency[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        components.append(sorted(members, key=_id_sort_key))
    return components


def _check_component(
    members: List[str], preds: Dict[str, List[Tuple[str, str]]], boundary_feeds: Dict[str, Set[str]]
) -> None:
    """Reject components a sequential op-list cannot hold: several streams, or a cycle."""
    feeds: Set[str] = set()
    for nid in members:
        feeds |= boundary_feeds[nid]
    if len(feeds) > 1:
        raise UnsupportedTopologyError(
            f"Op component {members} merges {len(feeds)} distinct sample streams "
            f"({sorted(feeds)}); a sequential op-list carries exactly one stream — "
            "feed every head from the same source (or export the pieces separately)."
        )
    # Kahn pass for cycle detection (a single ``sample`` socket cannot fan-in, but a
    # malformed prompt could still loop; Mix args make real diamonds legal, not cycles).
    remaining = {nid: sum(1 for _a, p in preds[nid] if p != _ROOT) for nid in members}
    ready = [nid for nid, count in remaining.items() if count == 0]
    member_set = set(members)
    consumers: Dict[str, List[str]] = {nid: [] for nid in members}
    for nid in members:
        for _arg, pred in preds[nid]:
            if pred != _ROOT and pred in member_set:
                consumers[pred].append(nid)
    processed = 0
    while ready:
        cur = ready.pop()
        processed += 1
        for nxt in consumers[cur]:
            remaining[nxt] -= 1
            if remaining[nxt] == 0:
                ready.append(nxt)
    if processed != len(members):
        unprocessed = sorted((nid for nid, count in remaining.items() if count > 0), key=_id_sort_key)
        raise UnsupportedTopologyError(f"Op nodes {unprocessed} form a cycle; op-graphs must be acyclic.")


def _compile_component(
    members: List[str],
    universe: Dict[str, str],
    preds: Dict[str, List[Tuple[str, str]]],
    prompt: Dict[str, Any],
    node_mappings: Dict[str, Any],
    fold: _FoldContext,
) -> List[Any]:
    """Flatten one component of the op DAG into a sequential op-list.

    Deterministic DFS from the sinks (deepest sink LAST, so the final sample is the
    longest path's output); a Mix node visits its predecessors in argument order. The
    flowing sample's input/target lineage is tracked, with ``Stash*Op``/``Unstash*Op``
    instances inserted at forks and Mix merges (see the module docstring).
    """
    member_set = set(members)
    consumer_count: Dict[str, int] = {}
    for nid in members:
        for _arg, pred in preds[nid]:
            consumer_count[pred] = consumer_count.get(pred, 0) + 1

    # Nodes whose post-op sample state must be restorable later: every fork (≥2 consumers,
    # the virtual ROOT included) and every Mix input/target argument (restored at the merge).
    needs_stash: Set[str] = {nid for nid, count in consumer_count.items() if count >= 2}
    for nid in members:
        if universe[nid] == "mix":
            (_, x), (_, y), (_, _z) = preds[nid]
            needs_stash.update((x, y))

    # Longest-path depth orders the sinks: side branches first, the deepest tail last.
    depth: Dict[str, int] = {}

    def _depth(nid: str) -> int:
        if nid == _ROOT:
            return 0
        if nid not in depth:
            depth[nid] = 1 + max((_depth(pred) for _arg, pred in preds[nid]), default=0)
        return depth[nid]

    sinks = [nid for nid in members if consumer_count.get(nid, 0) == 0]
    sinks.sort(key=lambda nid: (_depth(nid), _id_sort_key(nid)))

    ops_out: List[Any] = []
    inserted = 0
    live = {"input": _ROOT, "target": _ROOT}
    emitted: Set[str] = set()

    def _stash_key(nid: str, field: str) -> str:
        # Input and target snapshots need DISTINCT metadata keys (both ops write
        # ``metadata[key]`` — one shared key would silently overwrite the other).
        return f"{_STASH_KEY_PREFIX}{'root' if nid == _ROOT else nid}_{field}"

    def _append_stash(nid: str) -> None:
        nonlocal inserted
        ops_out.append(Instance(StashInputOp, key=_stash_key(nid, "input")))
        ops_out.append(Instance(StashTargetOp, key=_stash_key(nid, "target")))
        inserted += 2

    def _restore(field: str, nid: str) -> None:
        nonlocal inserted
        if live[field] == nid:
            return
        op_cls = UnstashInputOp if field == "input" else UnstashTargetOp
        ops_out.append(Instance(op_cls, key=_stash_key(nid, field)))
        live[field] = nid
        inserted += 1

    if _ROOT in needs_stash:
        _append_stash(_ROOT)

    def _emit(nid: str) -> None:
        if nid == _ROOT or nid in emitted:
            return
        emitted.add(nid)  # acyclicity established by _check_component
        for _arg, pred in preds[nid]:
            _emit(pred)
        if universe[nid] == "mix":
            (_, x), (_, y), (_, _z) = preds[nid]
            # Mix(input← x, target← y, metadata = union): the branches already ran in
            # argument order, so metadata is merged with arg-3 priority; restore the
            # input/target lineages the merge selects, then the merge node is the state.
            _restore("input", x)
            _restore("target", y)
        else:
            pred = preds[nid][0][1]
            _restore("input", pred)
            _restore("target", pred)
            ops_out.append(_op_to_instance(nid, prompt[nid], node_mappings, dropped_notes=fold.notes, fold=fold))
        live["input"] = live["target"] = nid
        if nid in needs_stash:
            _append_stash(nid)

    for sink in sinks:
        _emit(sink)

    leftover = member_set - emitted
    if leftover:  # pragma: no cover - defensive: every DAG node reaches some sink
        raise UnsupportedTopologyError(f"Op nodes {sorted(leftover, key=_id_sort_key)} are unreachable from any sink.")

    # The Unstash*Op default is remove=True, so each ``__taidal_stash_*`` snapshot is DELETED from the
    # metadata bus once restored — it never lingers into a downstream sink. But a fan-out restores the
    # SAME key before each later branch, so EVERY unstash of a key EXCEPT the LAST must keep it
    # (remove=False) or the next branch can't restore the fork. (copy stays at its default — earlier
    # unstashes still deepcopy for branch independence.)
    unstash_indices: Dict[str, List[int]] = {}
    for i, op in enumerate(ops_out):
        if isinstance(op, Instance) and op.target in (UnstashInputOp, UnstashTargetOp):
            unstash_indices.setdefault(str(op.kwargs.get("key")), []).append(i)
    for indices in unstash_indices.values():
        for i in indices[:-1]:  # all but the LAST occurrence of each key keep it
            keep = ops_out[i]
            ops_out[i] = Instance(keep.target, **{**keep.kwargs, "remove": False})

    if inserted:
        fold.notes.append(
            f"branching canvas wiring compiled to a sequential op-list — {inserted} stash/unstash "
            f"ops inserted (each '{_STASH_KEY_PREFIX}*' snapshot is removed by its final unstash)"
        )
    return ops_out


# --------------------------------------------------------------------------- public compile API


def compile_op_chains(
    prompt: Dict[str, Any],
    node_mappings: Dict[str, Any],
    chain_notes: Optional[List[List[str]]] = None,
    chain_constants: Optional[List[Dict[str, Any]]] = None,
    skip_ids: Optional[Any] = None,
    target_ids: Optional[Any] = None,
) -> List[List[Any]]:
    """Compile a ComfyUI prompt graph — ONE sequential op-list per connected op component.

    The op DAG (generated op nodes + ``Mix Samples`` adapters) is split into
    weakly-connected components; each compiles to a flat op-list (fan-out/Mix fan-in
    sequentialized with stash/unstash ops — see the module docstring). Components are
    returned in deterministic smallest-node-id order.

    A widget input driven by a link is folded where possible (primitive inline,
    ``constant=True`` value node hoisted + dotted ``!ref:``, ``op_N`` producers embedded);
    what remains is omitted (the op keeps its constructor default) with a note.

    Args:
        prompt: The ComfyUI prompt / API-format graph (``{node_id: {class_type, inputs}}``).
        node_mappings: FluxStudio's ``NODE_CLASS_MAPPINGS`` (maps ``class_type`` → node class,
            carrying ``TAIDAL_NODE_KIND`` / ``TAIDAL_CALLABLE_PATH`` / ``TAIDAL_CONSTANT``).
        chain_notes: Optional accumulator; one ``List[str]`` of notes is appended PER
            returned component (parallel lists), for the exporter's comment block.
        chain_constants: Optional accumulator; one ``{name: Class}`` dict of HOISTED
            constant entries is appended PER component — the YAML exporters emit them as
            top-level keys above ``ops:`` (a bare compile drops them, so pass this when
            the result will be dumped).
        skip_ids: Node ids marked CANVAS-ONLY (``properties["taidal_export"] == "skip"``,
            collected by the canvas export button / the CLI ``--skip`` flag). Each id
            excludes the node, its subgraph-inlined inner nodes (prefix ``<id>:``), and
            everything data-downstream of it (sample wires + Mix args), with a note. The
            closure is FORWARD-ONLY: the nodes before the mark always export (a skip on
            the node after a Mix means "stop exporting here", never "erase the Mix").
        target_ids: Node ids marked EXPORT TARGETS (``properties["taidal_export"] ==
            "target"`` / the CLI ``--target`` flag). When given, the export inverts from
            opt-out to opt-in: ONE document per target, each containing exactly the ops
            that COMPUTE that target (its data ancestor cone — the target is the final
            op); everything outside the cones is simply not scheduled. A skip mark
            downstream of a target trims nodes the cone never contained; a DIRECT skip
            mark on a node inside a cone is a contradiction and raises.

    Returns:
        One list per component, each suitable for ``Flux(ops=...)`` after a dump/load
        round-trip — op ``Instance`` markers plus any inserted ``Stash*``/``Unstash*``
        instances. Empty when the graph has no op nodes.

    Raises:
        UnsupportedTopologyError: a cycle, an unresolvable op node, a component merging
            several distinct sample streams, an unknown export target, or a skip mark
            directly on a node inside a target's dependency cone.
    """
    prompt = {str(nid): node for nid, node in prompt.items()}

    # Targets resolve FIRST, against the raw graph. The forward-only skip closure cannot
    # reach a cone unless a node was marked INSIDE it (any upstream mark of a cone member
    # is itself a cone member) — that is a genuine contradiction and rejected here.
    # Each marked id resolves to the EXPANDED prompt node(s) it names (segment match), so a target
    # placed INSIDE a subgraph (``371`` → ``"378:368:371"``) cones correctly; a mark naming several
    # prompt nodes (a multi-instance subgraph) yields one cone per matched node.
    targets = [str(t) for t in (target_ids or []) if str(t).strip()]
    resolved_targets: List[str] = []
    missing: List[str] = []
    for t in targets:
        matches = sorted(_resolve_marked({t}, prompt), key=_id_sort_key)
        if matches:
            resolved_targets.extend(matches)
        else:
            missing.append(t)
    if missing:
        raise UnsupportedTopologyError(f"Export target(s) {missing} not found in the graph (unknown node id).")
    target_cones: Dict[str, Set[str]] = {
        t: _ancestor_cone(t, prompt) for t in sorted(set(resolved_targets), key=_id_sort_key)
    }
    protected: Set[str] = set().union(*target_cones.values()) if target_cones else set()

    direct_marks = _marked_ids(skip_ids, prompt)
    conflicts = sorted(direct_marks & protected, key=_id_sort_key)
    if conflicts:
        raise UnsupportedTopologyError(
            f"Export-skip mark(s) on {conflicts} conflict with the export target(s) "
            f"{sorted(target_cones)} — the marked node(s) are inside a target's dependency "
            "cone (needed to compute it). Clear one of the two marks."
        )

    skipped_marks = _expand_skip_ids(skip_ids, prompt)
    base_notes: List[str] = []
    if skipped_marks:
        listed = ", ".join(
            f"node {nid} ({prompt[nid].get('class_type', '?')})" for nid in sorted(skipped_marks, key=_id_sort_key)
        )
        base_notes.append(f"excluded by export-skip mark (canvas-only, incl. data-downstream nodes): {listed}")
        prompt = {nid: node for nid, node in prompt.items() if nid not in skipped_marks}

    configure_specs, vc_excluded = _collect_value_chains(prompt, node_mappings)
    # op-@output → param captures: a CaptureOutputOp on the producer + a ConfigureOp(UnstashInputOp)
    # on the consumer. Both stay scheduled (the producer also transforms the sample), so captures add
    # NOTHING to schedule_excluded.
    output_captures = _collect_output_captures(prompt, node_mappings)
    # A consumed Subgraph-Op terminator's inner ops embed as a nested TransformChain in the
    # consumer (see _subgraph_op_instance), so they drop from the STANDALONE schedule — but NOT
    # from the cone compile (fold.excluded carries only the value-chain set). EXEMPT the resolved
    # targets: when SubgraphOpExtractNode compiles its OWN cone (target_ids=[terminator]), that
    # terminator's op output is also consumed (by the Flux), so without the exemption it would
    # exclude its own cone and the remnant fragments into several components (UnsupportedTopology).
    sg_excluded = _subgraph_cone_excluded(prompt, node_mappings, exempt=set(resolved_targets))
    schedule_excluded = vc_excluded | sg_excluded

    def _compile(restrict: Optional[Set[str]], extra_notes: List[str]) -> List[List[Any]]:
        universe, preds, boundary_feeds = _build_graph(prompt, node_mappings, schedule_excluded, restrict)
        if not universe:
            logger.debug("compile_op_chains: no op nodes in graph — returning no chains.")
            return []
        compiled: List[List[Any]] = []
        for members in _components(universe, preds):
            _check_component(members, preds, boundary_feeds)
            notes: List[str] = [*base_notes, *extra_notes]
            fold = _FoldContext(
                prompt, node_mappings, notes, configure_specs, excluded=vc_excluded, output_captures=output_captures
            )
            compiled.append(_compile_component(members, universe, preds, prompt, node_mappings, fold))
            if chain_notes is not None:
                chain_notes.append(notes)
            if chain_constants is not None:
                chain_constants.append(dict(fold.constants))
        return compiled

    if not target_cones:
        return _compile(restrict=None, extra_notes=[])

    chains: List[List[Any]] = []
    for target, cone in target_cones.items():
        note = f"compiled for export target: node {target} ({prompt[target].get('class_type', '?')})"
        chains.extend(_compile(restrict=cone, extra_notes=[note]))
    return chains


def compile_ops(
    prompt: Dict[str, Any],
    node_mappings: Dict[str, Any],
    skip_ids: Optional[Any] = None,
    target_ids: Optional[Any] = None,
) -> List[Any]:
    """Compile a prompt graph into a SINGLE sequential op-list.

    The strict single-component variant of :func:`compile_op_chains` — kept for consumers
    that want the one-pipeline guarantee. Args/returns as there (hoisted constants are
    dropped — use ``compile_op_chains(chain_constants=...)`` or the YAML exporters when
    the result must resolve dotted ``!ref:``\\ s), except:

    Returns:
        The single component's op-list; empty when the graph has no op nodes.

    Raises:
        UnsupportedTopologyError: everything :func:`compile_op_chains` raises, PLUS a graph
            holding more than one independent op component.
    """
    chains = compile_op_chains(prompt, node_mappings, skip_ids=skip_ids, target_ids=target_ids)
    if len(chains) > 1:
        raise UnsupportedTopologyError(
            f"Found {len(chains)} independent op components; compile_ops/export_ops_yaml need a "
            "single connected component — use compile_op_chains/export_ops_yamls to compile each "
            "component individually."
        )
    return chains[0] if chains else []


def _comment_block(notes: List[str]) -> str:
    """The leading ``#`` comment block for one document (empty when there are no notes)."""
    if not notes:
        return ""
    lines = ["# NOTE: ops-export compilation notes:"]
    lines += [f"#   - {note}" for note in notes]
    return "\n".join(lines) + "\n"


def _document(constants: Dict[str, Any], ops: List[Any]) -> Dict[str, Any]:
    """The dumpable document mapping: hoisted constants first, then the ``ops:`` list."""
    payload: Dict[str, Any] = dict(constants)
    payload["ops"] = ops
    return payload


def export_ops_yaml(
    prompt: Dict[str, Any],
    node_mappings: Dict[str, Any],
    skip_ids: Optional[Any] = None,
    target_ids: Optional[Any] = None,
) -> str:
    """Compile a prompt graph and serialize its ops to a Confluid YAML document.

    The result is a mapping holding the hoisted ``constant=True`` entries (if any)
    followed by the ``ops:`` key with the ``!class:...()`` op-list, so the consumer side
    is ``confluid.load(path)["ops"]`` → ``Flux(source=..., ops=...)`` (dotted ``!ref:``\\ s
    resolve against the same document). Strict: a graph with several independent op
    components raises — use :func:`export_ops_yamls` to get one document per component.
    """
    notes_per_chain: List[List[str]] = []
    constants_per_chain: List[Dict[str, Any]] = []
    chains = compile_op_chains(
        prompt,
        node_mappings,
        chain_notes=notes_per_chain,
        chain_constants=constants_per_chain,
        skip_ids=skip_ids,
        target_ids=target_ids,
    )
    if len(chains) > 1:
        raise UnsupportedTopologyError(
            f"Found {len(chains)} independent op components; compile_ops/export_ops_yaml need a "
            "single connected component — use compile_op_chains/export_ops_yamls to compile each "
            "component individually."
        )
    if not chains:
        return dump({"ops": []})
    return _comment_block(notes_per_chain[0]) + dump(_document(constants_per_chain[0], chains[0]))


def export_ops_yamls(
    prompt: Dict[str, Any],
    node_mappings: Dict[str, Any],
    skip_ids: Optional[Any] = None,
    target_ids: Optional[Any] = None,
) -> List[str]:
    """Compile a prompt graph and serialize EACH op component to its own YAML document.

    Every document is complete and individually loadable — the hoisted ``constant=True``
    entries (if any) followed by ``ops:`` — the same consumer contract as
    :func:`export_ops_yaml` (``confluid.load(path)["ops"]``), one per component, in
    deterministic smallest-node-id order. A graph with no op nodes yields a single empty
    ``ops: []`` document (callers always have something to write/download).

    A component whose compilation dropped wired widget values, hoisted nothing for an
    untagged value node, or inserted stash/unstash ops carries a leading ``#`` comment
    block naming each case; comments are invisible to the loader.
    """
    notes_per_chain: List[List[str]] = []
    constants_per_chain: List[Dict[str, Any]] = []
    chains = compile_op_chains(
        prompt,
        node_mappings,
        chain_notes=notes_per_chain,
        chain_constants=constants_per_chain,
        skip_ids=skip_ids,
        target_ids=target_ids,
    )
    if not chains:
        return [dump({"ops": []})]
    docs: List[str] = []
    for chain, notes, constants in zip(chains, notes_per_chain, constants_per_chain):
        docs.append(_comment_block(notes) + dump(_document(constants, chain)))
    return docs


def chain_output_paths(base: Union[str, Path], count: int) -> List[Path]:
    """Output file paths for ``count`` component documents derived from one user-given ``base``.

    One component keeps the base path verbatim (``ops.yaml``); several get a 1-based stem
    suffix (``ops_1.yaml`` … ``ops_N.yaml``). Shared by the ``/taidal/export_ops`` route and
    the ``fluxstudio export`` CLI so the file-naming policy can't fork.
    """
    base = Path(base)
    if count <= 1:
        return [base]
    return [base.with_name(f"{base.stem}_{i}{base.suffix}") for i in range(1, count + 1)]


__all__ = [
    "UnsupportedTopologyError",
    "chain_output_paths",
    "collect_export_marks",
    "compile_op_chains",
    "compile_ops",
    "export_ops_yaml",
    "export_ops_yamls",
]
