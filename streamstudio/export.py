"""Compile a StreamStudio canvas graph's **ops** into a Confluid YAML op-list.

A StreamStudio canvas is a :class:`recordstream.core.stream.Stream` turned inside-out — the source
is a ``-> RECORDSTREAM_SOURCE`` node, each transform a ``RECORDSTREAM_RECORD -> RECORDSTREAM_RECORD``
op node, and the lazy ``__iter__`` is replaced by the Walk/Extract adapters. This module
reverses the *ops half*: given a ComfyUI **prompt / API-format** graph it compiles the op
DAG into ONE SEQUENTIAL Confluid ``!class:...()`` op-list per weakly-connected component
(dropping the source + adapter nodes), so a user can reattach it to their own source::

    import confluid
    from recordstream import Stream
    ops = confluid.load("ops.yaml")["ops"]
    stream = Stream(source=my_own_source, ops=ops)

**DAG → sequential conversion** (the compiler is no longer limited to linear chains):

* **fan-out** (one op output → several op nodes) schedules the branches one after the
  other; the fork-point record is snapshotted into a per-record **Context cell**
  (:class:`recordstream.ops.context.Save`) and each later branch restarts from it
  (:class:`~recordstream.ops.context.Use`), so the branchy canvas wiring becomes one flat
  op sequence. Branches are ISOLATED (``Use`` deep-copies the fork; each branch carries
  its own metadata) — record.metadata is never used for graph wiring, so nothing
  compiler-inserted lingers into a sink. The auto-cells are ``__taidal_cell_<id>``,
  freed by their last reader (``drop`` flags compiled by a liveness pass).
* **fan-in** via the canvas ``Mix Records`` adapter (``Taidal_MixRecords``: input from A,
  target from B, metadata = union of all three) compiles to ONE
  :class:`recordstream.ops.context.Mix` reading the three branch cells
  (``input_from``/``target_from``/``metadata_from`` — the slot merge order reproduces the
  ``{**m1, **m2, **m3}`` arg-3-wins priority) and the adapter node itself disappears.
* the **deepest sink is scheduled last**, so the document's final record is the longest
  path's output; viewer-only side branches run earlier (delete them from the YAML if
  unwanted).

**Wired widget values** (a non-``record`` input driven by a link) no longer always drop:

* a native ComfyUI primitive pass-through (``PrimitiveInt`` / ``PrimitiveFloat`` /
  ``PrimitiveString`` / ``PrimitiveStringMultiline`` / ``PrimitiveBoolean``) folds INLINE
  as its literal ``value`` widget;
* a Taidal VALUE node marked ``@configurable(constant=True)`` is HOISTED as a top-level
  deferred ``!class:`` document entry carrying its widget settings verbatim, and the
  consumer kwarg becomes a dotted ``!ref:<name>.<output>`` (confluid attribute access).
  Shape: a config node's ``disabled`` output feeding a downstream op's ``tx_disabled`` param.
  (The concrete classes this was built for were removed upstream; nothing in the workspace
  carries ``constant=True`` today, so the coverage is the committed workflow fixture.);
* anything else is omitted with a note (the op keeps its constructor default) — a
  per-record runtime value should ride **metadata** instead (expression-capable params
  like ``ThresholdOp.low_level`` accept ``"{meta_key}"`` resolved from the record's
  metadata at call time).

``op_N`` links (an op consumed as a ``RECORDSTREAM_OP`` value by a higher-order op such as
``Pipeline`` / ``Enable`` / ``Parallel``) compile too: each wired producer embeds as a nested
``!class:`` instance in the consumer's op-list kwarg (mirroring the live node).

Input format — the ComfyUI prompt / API graph (what ``app.graphToPrompt()`` emits and
``/prompt`` consumes), NOT the richer UI ``workflow.json``::

    {"<node_id>": {"class_type": "Taidal_recordstream_op_numpy_RescaleOp",
                   "inputs": {"record": ["3", 0], "factor": 2.0}}, ...}

``inputs`` values are either literals (widget values) or ``["<upstream_id>", <slot>]`` links.

Still unsupported (:class:`UnsupportedTopologyError`): cycles, an unresolvable op node,
and a component that merges DISTINCT record streams (e.g. two different sources fanning
into one Mix) — a sequential op-list carries exactly one stream; use ``JointStream`` by hand.
"""

import inspect
import re
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Set, Tuple, Union, cast

from confluid import dump
from confluid.fluid import Reference, Target
from loggair import get_logger
from recordstream import Pipeline
from recordstream.discovery import resolve_callable
from recordstream.ops.configure import ConfigureOp
from recordstream.ops.contract import ChainContractError, check_chain
from recordstream.ops.formula import FormulaOp

from streamstudio.bridge import (
    _SINGLE_OP_PARAMS,
    GATE_PARAM,
    _coerce_kwargs,
    _op_list_params,
    _single_op_params,
    declared_outcomes,
)

logger = get_logger(__name__)

# The single ``RECORDSTREAM_RECORD`` input socket every generated op node declares
# (see ``bridge._build_op_node``) — the one edge that defines op-to-op data flow.
_RECORD_INPUT = "record"

# The canvas fan-in adapter (``streamstudio.nodes.MixRecordsNode``): input from arg 1,
# target from arg 2, metadata = the shallow union of all three (arg 3 wins).
_MIX_CLASS_TYPE = "Taidal_MixRecords"
_MIX_ARGS = ("input_record", "target_record", "metadata_record")
#: The canvas node where the branches of a decision rejoin (``nodes.JoinBranchesNode``): a
#: DRAWING the fold removes, never a step in the exported chain.
JOIN_CLASS_TYPE = "Taidal_JoinBranches"
JOIN_INPUTS = tuple(f"branch_{i}" for i in range(1, 7))

# The "subgraph as an Op" terminator (``streamstudio.nodes.SubgraphOpExtractNode``): its ``op``
# output (slot 0) is the upstream op DAG compiled to ONE ``Pipeline``. When that output feeds a
# consumer's op slot, the export embeds ``Pipeline(transforms=<compiled ancestor cone>)``
# (DAG→sequential, the same compiler) and the cone's inner ops drop from the standalone schedule.
_SUBGRAPH_OP_CLASS_TYPE = "Taidal_SubgraphOp"
# The hand-written ``Value`` node (``nodes.ValueNode``): a STRING ``value`` widget echoed to a
# ``RECORDSTREAM_VALUE`` output as a parsed Python literal — the producer for a NESTED-container param
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

# Legacy metadata-key prefix of the RETIRED stash/unstash lowering — kept ONLY so graphio's
# importer still recognises documents exported before the context-ops compiler (2026-07).

# Namespace for compiler-internal names. Retained for the graph-export/import paths that
# still recognise ids the compiler assigned (the per-record cell plane itself is gone).
# (was: per-record Context cells used by the inserted Save/Use/Mix context ops — the graph data
# plane — record.metadata is never used for wiring). Namespaced so they can't collide with
# user-named cells; each auto-cell is freed by its LAST reader (compiled drop flags).
_CELL_PREFIX = "__taidal_cell_"

# Op nodes expose their declared @output properties as output sockets at slot >= 2 (slot 0 =
# ``record``, slot 1 = ``op`` — see bridge._build_op_node). A wire from such a slot into a LATER
# op's PARAM is an op-@output capture: in a flow document it becomes ``bind: {param: "step.attr"}``
# (records the ACTUAL @output value into a Context cell) and the consumer in ``Apply`` (reads
# the cell into the param per record). The cell may be read by several consumers, so it is
# never dropped — it dies with the per-record Context.
_OP_OUTPUT_FIRST_SLOT = 2
_OUTPUT_CAPTURE_PREFIX = "__taidal_output_"

# The canvas-only mark: a node whose ``properties["taidal_export"] == "skip"`` (toggled by
# ``web/export_skip.js``) runs on the canvas but is EXCLUDED from exports — it, everything
# data-downstream of it (record wires + Mix args), AND every upstream dependency that
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

# Virtual node id for "the component's incoming record" (whatever the Stream source feeds).
_ROOT = "__taidal_root__"

# Dynamic ``RECORDSTREAM_OP`` value sockets a higher-order op node grows (``web/op_list.js``). The recordstream
# higher-order ops use the ``op_N`` family; a bridged composite from a companion package can carry SEVERAL
# families under its own stamped param names (``true_transforms_N`` …) — recognised class-aware via
# :func:`_op_list_socket_param` rather than this fixed pattern (kept as the unstamped fallback).
_OP_SOCKET_RE = re.compile(r"^op_(\d+)$")


def _op_class_of(node_cls: Any) -> Any:
    """The op CLASS wrapped by a generated op-node class (via ``TAIDAL_CALLABLE_PATH``), or ``None``.

    The op class is the authority on which constructor params accept op VALUES — ``bridge._single_op_params``
    / ``_op_list_params`` introspect IT (single-op params + the dynamic op-list families, incl. the
    stamped slots ``compute`` / ``true_transforms``). ``None`` when the path is missing/unresolvable
    (a non-op / native node), so the callers fall back to the recordstream ``op_N`` / ``op`` / ``target`` names.
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
    (a bridged If composite → ``true_transforms_N`` + ``false_transforms_N``); the recordstream ``op_N`` family maps
    to its single op-list param. ``None`` ``op_cls`` (unstamped/native) recognises nothing here.
    """
    if op_cls is not None:
        for param, prefix in _op_list_params(op_cls).items():
            if key.startswith(prefix + "_") and key[len(prefix) + 1 :].isdigit():
                return param
    return None


def _is_op_value_key(op_cls: Any, key: str) -> bool:
    """True if ``key`` is an op-VALUE input socket on ``op_cls`` — a single-op param (``ConfigureOp.target``
    / a stamped ``compute``) or an indexed op-list socket (``op_0`` / ``true_transforms_0``).

    These wires embed through the op-value path (nested ``!class:`` / list kwarg), NOT the widget/value-chain
    pre-passes — so the pre-passes SKIP them. Falls back to the recordstream name conventions when ``op_cls`` is
    unavailable.
    """
    if op_cls is None:
        return bool(_OP_SOCKET_RE.match(key)) or key in _SINGLE_OP_PARAMS
    return key in _single_op_params(op_cls) or _op_list_socket_param(op_cls, key) is not None


# The canvas record→value boundary + scalar-math adapters a VALUE CHAIN walks through
# (``streamstudio.nodes.RecordExtractorNode`` / ``MathNode``): an op widget wired from
# ``… op-chain → Extract(field=input) → Math* → widget`` compiles to a ``ConfigureOp``.
_EXTRACT_CLASS_TYPE = "Taidal_ExtractRecord"
_MATH_CLASS_TYPE = "Taidal_Math"
_MATH_VARS = ("a", "b", "c", "d", "e")


class _ValueChain(NamedTuple):
    """A compiled per-record value derivation feeding an op widget.

    ``op_ids`` are the record ops between the consumer's own record predecessor and the
    Extract boundary (in flow order); ``formulas`` the Math steps as ``(formula, var)``;
    ``adapter_ids`` the consumed Extract/Math canvas nodes (so the whole-graph export can
    suppress their "left out" listing — their semantics live on in the ConfigureOp).
    """

    op_ids: Tuple[str, ...]
    formulas: Tuple[Tuple[str, str], ...]
    adapter_ids: Tuple[str, ...]


class _OutputCapture(NamedTuple):
    """A compiled op-``@output`` → param wire (e.g. ``NoiseFloorOp.applied_snr_db`` → another op's param).

    ``producer_id`` is the op node whose ``@output`` is captured (a record-ancestor of the consumer),
    ``output_name`` the ``@output`` property (``RETURN_NAMES[slot]``), and ``stash_key`` the namespaced
    Context cell the captured value lands in (historically a metadata stash key — the field name
    survives). In the flow form the consumer step carries ``bind: {param: "<producer step>.<attr>"}``.
    """

    producer_id: str
    output_name: str
    stash_key: str


class UnsupportedTopologyError(ValueError):
    """The canvas op-graph can't be expressed as a sequential op-list document.

    Raised for cycles, an op node whose wrapped callable can't be resolved, a component
    merging several DISTINCT record streams (two different sources fanning into one
    ``Mix Records`` — a sequential op-list carries one stream), and, from the strict
    single-component internal flattener (:func:`_linear_chain_ops`) only, multiple
    disconnected op components (the per-component form :func:`compile_flow_documents` /
    :func:`export_flow_yamls` accepts those). A direct :func:`_op_to_instance` call with no
    note accumulator also raises on any link-driven widget input.
    """


def _is_link(value: Any) -> bool:
    """True if a prompt ``inputs`` value is a wire ``[upstream_node_id, output_slot]``.

    In the API format a link is a 2-list whose first element is the upstream node id
    (a string) and whose second is an integer output slot. Widget values arrive as
    scalars/strings (StreamStudio renders ``List`` params as comma-separated STRINGs, not
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
    isn't in StreamStudio's ``node_mappings`` at all).
    """
    cls = node_mappings.get(class_type)
    return getattr(cls, "TAIDAL_NODE_KIND", None) if cls is not None else None


def _record_predecessor(node: Dict[str, Any]) -> Optional[str]:
    """Upstream node id feeding this node's ``record`` socket, or ``None`` if unconnected."""
    record_in = node.get("inputs", {}).get(_RECORD_INPUT)
    return str(record_in[0]) if _is_link(record_in) else None


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

    ``Target(cls, **kwargs)`` collides when an op has a constructor param literally named
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
    ``notes``, the HOISTED constant entries (``name -> Target marker``, insertion
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
        # `bind:` grammar in the flow form — `captures_by_producer`
        # is the reverse map (producer id -> {output_name: cell}) so a producer feeding several
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

    The chain must root at the CONSUMER's own record predecessor (the shared fork), so the
    derivation can re-run on the consumer's incoming record inside a ``ConfigureOp``.
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
    record_link = extract.get("inputs", {}).get(_RECORD_INPUT)
    if not _is_link(record_link):
        return None

    op_ids: List[str] = []
    cur = record_link
    while True:
        src = str(cur[0])
        if src == consumer_pred:
            break
        if _node_kind(str(prompt.get(src, {}).get("class_type", "")), node_mappings) != "op":
            return None
        op_ids.insert(0, src)
        nxt = prompt[src].get("inputs", {}).get(_RECORD_INPUT)
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
    chain-member op ids whose record output feeds ONLY the chain (pure value branches) —
    those leave the record schedule entirely (they re-run inside the ConfigureOp instead
    of dangling as a dead-end branch).
    """
    specs: Dict[str, List[Tuple[str, _ValueChain]]] = {}
    chain_members: Set[str] = set()
    for nid, node in prompt.items():
        if _node_kind(str(node.get("class_type", "")), node_mappings) != "op":
            continue
        inputs = node.get("inputs", {})
        pred = inputs.get(_RECORD_INPUT)
        consumer_pred = str(pred[0]) if _is_link(pred) else None
        op_cls = _op_class_of(node_mappings.get(str(node.get("class_type", ""))))
        for key, value in inputs.items():
            if key == _RECORD_INPUT or not _is_link(value):
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
    producer is a record-ANCESTOR of the consumer (so it runs earlier in the schedule and its
    captured value is on the metadata bus when the consumer reads it). Returns ``None`` otherwise
    (slot 0/1 = ``record``/``op``, a non-op or self producer, or a cross-branch producer) — the
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
        return None  # slot 0/1 are record/op (handled elsewhere); out-of-range = not an @output
    if producer_id not in _ancestor_cone(consumer_id, prompt):
        return None  # cross-branch: the captured value wouldn't be on the bus — fall back to a note
    output_name = str(return_names[slot])
    return _OutputCapture(producer_id, output_name, f"{_OUTPUT_CAPTURE_PREFIX}{producer_id}_{output_name}")


def _collect_output_captures(
    prompt: Dict[str, Any], node_mappings: Dict[str, Any]
) -> Dict[str, List[Tuple[str, _OutputCapture]]]:
    """Pre-trace every op-param wire that captures another op's ``@output`` (output slot >= 2).

    Maps a consumer node id to its ``(param, capture)`` list — each compiles to a context ``Capture``
    on the producer + an ``Apply`` (cell read) on the consumer. Disjoint from
    value chains by construction (a value chain's immediate upstream is a Math/Extract adapter, not an
    op-kind node), so this runs alongside :func:`_collect_value_chains`. Both the producer and the
    consumer stay in the schedule (the producer also transforms the record; nothing is excluded).
    """
    specs: Dict[str, List[Tuple[str, _OutputCapture]]] = {}
    for nid, node in prompt.items():
        if _node_kind(str(node.get("class_type", "")), node_mappings) != "op":
            continue
        op_cls = _op_class_of(node_mappings.get(str(node.get("class_type", ""))))
        for key, value in node.get("inputs", {}).items():
            if key == _RECORD_INPUT or not _is_link(value):
                continue
            if _is_op_value_key(op_cls, key):
                continue  # op-value wires embed through their own path
            capture = _trace_output_capture(value, str(nid), prompt, node_mappings)
            if capture is not None:
                specs.setdefault(str(nid), []).append((key, capture))
    return specs


def _data_preds(node: Dict[str, Any]) -> List[str]:
    """Upstream node ids on this node's DATA edges (the ``record`` wire, or Mix args)."""
    inputs = node.get("inputs", {})
    data_args = _MIX_ARGS if str(node.get("class_type", "")) == _MIX_CLASS_TYPE else (_RECORD_INPUT,)
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
    as string node ids — pass straight to :func:`export_flow_yamls` / :func:`compile_flow_documents`.

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
    and the set closes DOWNSTREAM over data edges (``record`` wires + Mix arguments) —
    everything fed from a marked node has no exportable feed, so it is canvas-only too.

    The closure NEVER walks upstream: a skip mark means "this node and everything BEHIND
    it (data-downstream); the nodes before it still export" — pruning the marked node's
    record feeders would eat the pipeline that computes the data (marking the node after
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


#: The canvas node that gathers labelled values into a record (``streamstudio.nodes.RecordBuilderNode``).
_RECORD_BUILDER_CLASS_TYPE = "Taidal_RecordBuilder"
_RENAME_FIELD_CLASS_TYPE = "Taidal_recordstream_op_structure_RenameField"
_SELECT_FIELDS_CLASS_TYPE = "Taidal_recordstream_op_structure_SelectFields"


def expand_record_builders(prompt: Dict[str, Any], node_mappings: Dict[str, Any]) -> Dict[str, Any]:
    """Rewrite every *Build Record* into the record ops it means, returning a NEW prompt.

    ``Extract → Build Record → Extract as Op`` is how a subgraph states its record contract on the
    canvas: each builder slot NAME is a record key, and the extract feeding it says, through its
    ``field`` widget, where the value came from. Neither node can appear in a compiled ``Pipeline``
    — both are canvas glue with no recordstream counterpart — so the pair is TRANSLATED here:

    * ``field`` → slot name becomes ``RenameField(src=field, dst=name)`` (skipped when they already
      agree — the entry is under the right key already);
    * a closing ``SelectFields`` over the slot names, so the record then carries exactly what the
      boundary promised and nothing else;
    * the chain is rooted at whatever fed the extracts, so it runs over the incoming record.

    Doing it HERE rather than by placing those nodes on the canvas is the whole point: loose
    ``RenameField`` nodes beside the extracts were equivalent and unreadable, because nothing
    connected them to the outputs they described. The canvas keeps the shape the user drew; the
    compiler is what knows how to run it.

    A builder whose entries are NOT fed by extracts is left untouched — only that shape carries a
    ``field`` to derive a rename from, and inventing one would be a guess. Callers therefore still
    see it in the prompt and treat it as they would any other unresolvable node.
    """
    builders = {
        nid: node
        for nid, node in prompt.items()
        if isinstance(node, dict) and node.get("class_type") == _RECORD_BUILDER_CLASS_TYPE
    }
    if not builders:
        return prompt

    expanded: Dict[str, Any] = {k: v for k, v in prompt.items()}
    replacements: Dict[str, List[Any]] = {}  # builder id -> the link that now stands in for it
    consumed: Set[str] = set()

    for builder_id, builder in builders.items():
        entries = [
            (name, link)
            for name, link in (builder.get("inputs") or {}).items()
            if _is_link(link) and prompt.get(link[0], {}).get("class_type") == _EXTRACT_CLASS_TYPE
        ]
        if not entries or len(entries) != len([1 for v in (builder.get("inputs") or {}).values() if _is_link(v)]):
            continue  # not the extract shape (or only partly) — leave it for the caller to report

        source = prompt[entries[0][1][0]]["inputs"].get("record")
        previous: Any = source
        for index, (name, link) in enumerate(entries):
            extract = prompt[link[0]]
            consumed.add(link[0])
            field = str(extract["inputs"].get("field") or "")
            if field and field != name:
                new_id = f"{builder_id}__rename_{index}"
                expanded[new_id] = {
                    "class_type": _RENAME_FIELD_CLASS_TYPE,
                    "inputs": {"record": previous, "src": field, "dst": name},
                }
                previous = [new_id, 0]
        close_id = f"{builder_id}__select"
        expanded[close_id] = {
            "class_type": _SELECT_FIELDS_CLASS_TYPE,
            "inputs": {"record": previous, "keys": ",".join(name for name, _ in entries)},
        }
        replacements[builder_id] = [close_id, 0]

    if not replacements:
        return prompt

    # Re-point every consumer of a translated builder at its closing op, then drop the glue.
    for node in expanded.values():
        for key, value in list((node.get("inputs") or {}).items()):
            if _is_link(value) and value[0] in replacements:
                node["inputs"][key] = replacements[value[0]]
    for dead in {*replacements, *consumed}:
        expanded.pop(dead, None)
    return expanded


def _ancestor_cone(target_id: str, prompt: Dict[str, Any]) -> Set[str]:
    """The export-target's dependency cone: the target + every data ancestor.

    These are exactly the ops that COMPUTE the target — record wires and Mix arguments
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
    """The per-record-value note suffix: the sanctioned runtime mechanisms.

    Always points at ``recordstream.ops.configure.ConfigureOp`` (compute the value from the
    record and inject it as the param — works for ANY op attribute); additionally mentions
    the metadata-expression form when the param's annotation admits ``str``.
    """
    hint = (
        f"; for a per-record value wrap the op in recordstream ConfigureOp " f"(compute ops + target + param={key!r})"
    )
    if _param_admits_str(target, key):
        hint += " or set the widget to a '{meta_key}' expression resolved from the record's metadata at call time"
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
        # nested ``Pipeline`` instance, the "subgraph as an Op" landing in that slot.
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
        # (a list of boxes, a dict). Partial import keeps export.py ↔ nodes.py acyclic.
        from streamstudio.nodes import parse_value_literal

        value = upstream.get("inputs", {}).get("value")
        if _is_link(value):
            return _resolve_widget_link(target, key, value, fold)
        return ("literal", parse_value_literal(value if isinstance(value, str) else ""))

    node_cls = fold.node_mappings.get(class_type)
    kind = getattr(node_cls, "TAIDAL_NODE_KIND", None)
    if kind == "value":
        return_names = tuple(getattr(node_cls, "RETURN_NAMES", ()) or ())
        if slot < len(return_names) and return_names[slot] == "object":
            # The trailing ``object`` output (bridge appends it to every value node) IS the
            # configured node itself: the consumer wants the LIVE instance — a ``random=True``
            # producer keeps drawing per call — so hoist the node and hand back a WHOLE-NODE
            # ``!ref:``. The ``constant`` gate below guards only the declared-@output slots,
            # whose fold EVALUATES the property into a frozen literal; referencing the
            # instance evaluates nothing, so every value node qualifies.
            name = _hoist_constant(upstream_id, upstream, fold)
            return ("ref", Reference(name))
        if not getattr(node_cls, "TAIDAL_CONSTANT", False):
            return (
                "drop",
                f"; upstream {class_type} is a value node not marked constant — tag the class "
                "@configurable(constant=True) to fold its settings into the export",
            )
        if slot >= len(return_names):
            return ("drop", f"; upstream {class_type} output slot {slot} has no declared @output")
        name = _hoist_constant(upstream_id, upstream, fold)
        return ("ref", Reference(f"{name}.{return_names[slot]}"))

    if kind == "object" and slot == 0:
        # An OBJECT node's ``instance`` output (slot 0) wired into an op's param — a model into a
        # ``PredictClass.model``: hoist the node as a top-level ``!class:`` entry and hand the
        # consumer a WHOLE-NODE ref, the same shape as a value node's ``object`` output above.
        # Slot 1 (the ``lazy`` output) keeps the drop below: the hoist emits an EAGER entry, and
        # silently building what the author marked deferred would change when the cost is paid.
        name = _hoist_constant(upstream_id, upstream, fold)
        return ("ref", Reference(name))

    if kind == "op":
        # A wire from an op node's ``op`` VALUE output (a RECORDSTREAM_OP wire into a single-op
        # param like ``RandomApply.op`` / ``ConfigureOp.target``) embeds as a nested instance.
        return_names = tuple(getattr(node_cls, "RETURN_NAMES", ()) or ())
        op_slot = return_names.index("op") if "op" in return_names else 1
        if slot == op_slot:
            nested = _op_to_instance(upstream_id, upstream, fold.node_mappings, dropped_notes=fold.notes, fold=fold)
            return ("literal", nested)
        if slot >= _OP_OUTPUT_FIRST_SLOT:
            # An op-@output wire that wasn't captured (handled by _collect_output_captures) — the
            # producer isn't a record-ANCESTOR of this consumer, so its per-record value can't reach
            # here through the metadata bus. Drop with a note pointing at the requirement.
            out_name = return_names[slot] if slot < len(return_names) else f"slot {slot}"
            return (
                "drop",
                f"; to wire {class_type}.{out_name} (an @output) into a param, the producing op must "
                "be a record-ancestor of this op (same chain) so its captured value is on the bus",
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
        # A deferred ``Target`` marker (``!class:Foo`` — NO parens): the dotted ``!ref:`` reader
        # instantiates it to read the @output property; nothing eager happens at dump time.
        fold.constants[name] = _marker(Target, cast(type, target), coerced)
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
    (a bridged If composite → ``true_transforms`` + ``false_transforms``), so links are grouped by param
    and ordered by socket index within each. Each wired producer embeds as a nested ``!class:`` Target
    (its wired widgets fold through the same pass), or — for a Subgraph-Op terminator — its compiled cone
    as a nested ``Pipeline``, mirroring
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
                # → embed its compiled cone as a nested Pipeline (the subgraph as one Op).
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
            result[param] = instances
    return result


def _op_to_instance(
    node_id: str,
    node: Dict[str, Any],
    node_mappings: Dict[str, Any],
    dropped_notes: Optional[List[str]] = None,
    fold: Optional[_FoldContext] = None,
    bind_captures: bool = False,
) -> Target:
    """Build a Confluid ``Target`` marker (``!class:X()`` on dump) for one op node.

    Recovers the wrapped class from ``TAIDAL_CALLABLE_PATH``, takes the literal (non-link)
    widget inputs — minus the ``record`` wire — and runs them through the SAME coercion the
    live node uses (``bridge._coerce_kwargs``), so blank/optional/List widgets serialize the
    way they'd construct. Explicit kwargs on the ``Target`` make the dump robust regardless
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
        if key == _RECORD_INPUT or key in configured_params or key in captured_params:
            continue  # a configured/captured param is injected per record by the ConfigureOp wrap below
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
    instance = cast(Target, _marker(Target, cast(type, target), coerced))
    if cfg_specs and fold is not None:
        for param, chain in cfg_specs:
            # The value chain re-runs on the op's incoming record inside a ConfigureOp: the
            # chain ops compute, the Math steps apply as FormulaOps, and the result is set
            # as the wrapped op's ``param`` (the Configure pattern, serialized).
            compute: List[Any] = [
                _op_to_instance(mid, fold.prompt[mid], node_mappings, dropped_notes=dropped_notes, fold=fold)
                for mid in chain.op_ids
            ]
            compute += [_marker(Target, FormulaOp, {"formula": f, "var": v}) for f, v in chain.formulas]
            instance = cast(Target, _marker(Target, ConfigureOp, {"ops": compute, "target": instance, "param": param}))
    if capture_specs and not bind_captures:
        # An op-@output -> param wire is `bind: {param: "step.attr"}` in a flow document. The
        # flat form used to thread it through a per-record Context cell (Capture on the
        # producer, Apply on the consumer); those ops were deleted with the lowering pass, so
        # a flat `ops:` document cannot carry the wire — note it and keep the constructor
        # default, the same convention every other unfoldable wire follows.
        if dropped_notes is not None:
            params = ", ".join(param for param, _cap in capture_specs)
            dropped_notes.append(
                f"node {node_id} ({class_type}): @output-driven param(s) {params} dropped — an "
                "ops: list cannot carry an @output wire; export as a flow: document to keep it"
            )
    return instance


# --------------------------------------------------------------------------- subgraph-as-op


def _subgraph_op_instance(
    terminator_id: str, prompt: Dict[str, Any], node_mappings: Dict[str, Any], fold: _FoldContext
) -> Target:
    """Compile a Subgraph-Op terminator's ancestor cone into a ``Pipeline`` Target.

    The cone (the ops computing the record wired into the terminator) is flattened with the
    SAME DAG→sequential machinery the top level uses — fan-out and ``Mix`` fan-in become
    context ``Save``/``Use`` sequences — so the whole drawn sub-pipeline lands as one
    ``!class:recordstream.transform.Pipeline(transforms=[…])``. The shared ``fold`` means a
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
        ops.extend(_linear_chain_ops(members, universe, preds, prompt, node_mappings, fold))
    return cast(Target, _marker(Target, Pipeline, {"transforms": ops}))


def _op_value_consumed(node_id: str, prompt: Dict[str, Any], node_mappings: Dict[str, Any]) -> bool:
    """True if some node consumes ``node_id``'s output via an op-value slot (``op_N`` / ``op`` / ``target``,
    or a stamped slot like ``compute`` / ``true_transforms_0``)."""
    for cnode in prompt.values():
        op_cls = _op_class_of(node_mappings.get(str(cnode.get("class_type", ""))))
        for key, value in cnode.get("inputs", {}).items():
            if _is_link(value) and str(value[0]) == node_id and _is_op_value_key(op_cls, key):
                return True
    return False


def _record_consumed_outside(node_id: str, cone: Set[str], prompt: Dict[str, Any]) -> bool:
    """True if a node OUTSIDE ``cone`` consumes ``node_id``'s record output (record wire or Mix arg)."""
    for cnid, cnode in prompt.items():
        if cnid not in cone and node_id in _data_preds(cnode):
            return True
    return False


def _subgraph_cone_excluded(
    prompt: Dict[str, Any], node_mappings: Dict[str, Any], exempt: Optional[Set[str]] = None
) -> Set[str]:
    """Inner ops of each CONSUMED Subgraph-Op terminator → dropped from the standalone schedule.

    When a terminator's ``op`` output feeds an op slot, its ancestor cone is embedded as a
    nested ``Pipeline`` in that consumer, so those inner ops must not ALSO compile to a
    standalone pipeline. A cone op whose record is consumed OUTSIDE the cone too (a node reused
    in graph mode) stays scheduled — it is then both embedded and scheduled (the shared-node
    case). A terminator whose ``op`` output is unwired excludes nothing (its inner ops, if any,
    schedule as an ordinary pipeline — the "use the subgraph as a graph" path).

    ``exempt`` lists terminator ids whose cone is itself the COMPILE TARGET (``_compile_components``
    passes the resolved export-target ids). Such a terminator must NOT be excluded: when
    ``SubgraphOpExtractNode`` compiles its own cone via ``export_flow_yamls(target_ids=[tid])``, the
    same prompt also has that terminator's ``op`` output consumed (by the Stream it feeds), so
    WITHOUT this exemption the terminator would exclude its OWN cone — leaving only the few ops
    whose record is reused by an off-cone viewer branch, which then fragment into several
    components and trip the single-component ``_linear_chain_ops`` flattener ("Found N independent op
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
            if not _record_consumed_outside(member, cone, prompt):
                excluded.add(member)
    return excluded


# --------------------------------------------------------------------------- DAG scheduling


def _build_graph(
    prompt: Dict[str, Any],
    node_mappings: Dict[str, Any],
    excluded: Optional[Set[str]] = None,
    restrict: Optional[Set[str]] = None,
) -> Tuple[Dict[str, str], Dict[str, List[Tuple[str, str]]], Dict[str, Set[str]]]:
    """The record-flow universe: generated op nodes + ``Mix Records`` adapters.

    Returns ``(universe, preds, boundary_feeds)`` where ``universe`` maps node id →
    ``"op"``/``"mix"``, ``preds`` maps node id → its data-flow predecessors in
    deterministic argument order (``_ROOT`` for a boundary/unconnected feed), and
    ``boundary_feeds`` maps node id → the out-of-universe feed markers it touches
    (boundary node ids, or ``"<unconnected>"``) for the one-stream check.

    An op consumed ONLY as a ``RECORDSTREAM_OP`` value (an ``op_N`` link) with no record
    wiring is config, not record flow — it is excluded here and embeds as a nested
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
        arg_names = _MIX_ARGS if kind == "mix" else (_RECORD_INPUT,)
        for arg in arg_names:
            pred, feed = _resolve_pred(inputs.get(arg))
            plist.append((arg, pred))
            if feed is not None:
                feeds.add(feed)
        preds[nid] = plist
        boundary_feeds[nid] = feeds

    # Drop op-value-only nodes (no record in, nothing consumes their record out).
    record_consumed = {pred for plist in preds.values() for _arg, pred in plist}
    for nid in list(universe):
        if universe[nid] != "op" or nid not in value_consumed or nid in record_consumed:
            continue
        if not _is_link(prompt[nid].get("inputs", {}).get(_RECORD_INPUT)):
            logger.debug(f"_build_graph: node {nid} is an op-value producer — embedded, not scheduled.")
            del universe[nid], preds[nid], boundary_feeds[nid]

    return universe, preds, boundary_feeds


def _components(universe: Dict[str, str], preds: Dict[str, List[Tuple[str, str]]]) -> List[List[str]]:
    """Weakly-connected components of the record-flow DAG, ordered by smallest node id."""
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
            f"Op component {members} merges {len(feeds)} distinct record streams "
            f"({sorted(feeds)}); a sequential op-list carries exactly one stream — "
            "feed every head from the same source (or export the pieces separately)."
        )
    # Kahn pass for cycle detection (a single ``record`` socket cannot fan-in, but a
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


def _linear_chain_ops(
    members: List[str],
    universe: Dict[str, str],
    preds: Dict[str, List[Tuple[str, str]]],
    prompt: Dict[str, Any],
    node_mappings: Dict[str, Any],
    fold: _FoldContext,
) -> List[Any]:
    """Flatten a LINEAR component into an ordered op list — for a ``Pipeline``, NOT an export.

    This is deliberately not an export format: the flat ``ops:`` document was removed
    2026-08-01 and ``flow:`` is the only interchange shape. What survives is the fact that a
    ``recordstream.Pipeline`` HOLDS a straight op list, so the "subgraph as an Op" terminator
    (which compiles its cone into one ``Pipeline``) needs the cone flattened.

    A BRANCHY component has no such spelling — the per-record context ops that used to
    manufacture one were deleted with the lowering pass — so it raises, naming the flow export.
    """
    fan_out = sorted(
        {pred for nid in members for _arg, pred in preds[nid]}
        & {pred for pred, count in Counter(pred for nid in members for _arg, pred in preds[nid]).items() if count >= 2},
        key=_id_sort_key,
    )
    fan_in = sorted((nid for nid in members if universe[nid] == "mix"), key=_id_sort_key)
    if fan_out or fan_in:
        detail = []
        if fan_out:
            detail.append(f"fan-out at node(s) {fan_out}")
        if fan_in:
            detail.append(f"fan-in at Mix node(s) {fan_in}")
        raise UnsupportedTopologyError(
            f"This op component is branchy ({'; '.join(detail)}), and a Pipeline holds a straight "
            "op LIST — it cannot express a fork or a Mix fan-in. Export the canvas as a `flow:` "
            "document (canvas: 'Export Taidal Ops → Flow YAML'; CLI: `streamstudio export`), where "
            "the branch wiring is step grammar (`from:` / `merge_from:` / `bind:`)."
        )

    consumer_count: Dict[str, int] = {}
    for nid in members:
        for _arg, pred in preds[nid]:
            consumer_count[pred] = consumer_count.get(pred, 0) + 1
    ops_out: List[Any] = []
    emitted: Set[str] = set()

    def _emit(nid: str) -> None:
        if nid == _ROOT or nid in emitted:
            return
        emitted.add(nid)
        for _arg, pred in preds[nid]:
            _emit(pred)
        ops_out.append(_op_to_instance(nid, prompt[nid], node_mappings, dropped_notes=fold.notes, fold=fold))

    for sink in sorted((nid for nid in members if consumer_count.get(nid, 0) == 0), key=_id_sort_key):
        _emit(sink)

    leftover = set(members) - emitted
    if leftover:  # pragma: no cover - defensive: every DAG node reaches some sink
        raise UnsupportedTopologyError(f"Op nodes {sorted(leftover, key=_id_sort_key)} are unreachable from any sink.")
    return ops_out


def _flow_step_name(nid: str) -> str:
    """The step name for a canvas node id — the readable identity a flow document is FOR.

    Subgraph-path ids join with ``:`` (``378:368:371``), which a flow step name may not
    carry (``.``/``:`` are reserved), so the separator becomes ``_``.
    """
    return "source" if nid == _ROOT else f"node_{str(nid).replace(':', '_')}"


def _compile_component_flow(
    members: List[str],
    universe: Dict[str, str],
    preds: Dict[str, List[Tuple[str, str]]],
    prompt: Dict[str, Any],
    node_mappings: Dict[str, Any],
    fold: _FoldContext,
) -> Tuple["OrderedDict[str, Any]", str]:
    """Compile one component of the op DAG into a ``flow:`` mapping of NAMED STEPS.

    The graph-native twin of :func:`_compile_component`, and the reason the canvas no longer
    round-trips through a linearized form: the canvas IS a graph, so each op node becomes one
    step whose ``from:``/``merge_from:``/``bind:`` keys ARE the wires the user drew. Nothing
    is snapshotted into a per-record cell and nothing is lifted back afterwards.

    The mapping to step grammar, wire for wire:

    * a node's record predecessor  -> ``from:`` (the virtual ROOT is an identity ``source: {}``
      step, so a branch restarting from the source names it like any other step)
    * a ``Mix Records`` fan-in     -> ``from:`` arg-1 + ``merge_from: [arg-2, arg-3]`` (union in
      listed order, last-write-wins — arg-3 last reproduces the canvas
      ``{**m1, **m2, **m3}`` priority the flat form got from reading arg-3 as ``metadata_from``)
    * an op-``@output`` -> param wire -> ``bind: {param: "<producer step>.<output>"}``

    Emission order is the same deterministic DFS from the sinks (deepest sink LAST), which is
    what makes every ``from:`` reference an EARLIER step — the flow grammar's hard rule.
    """
    consumer_count: Dict[str, int] = {}
    for nid in members:
        for _arg, pred in preds[nid]:
            consumer_count[pred] = consumer_count.get(pred, 0) + 1

    depth: Dict[str, int] = {}

    def _depth(nid: str) -> int:
        if nid == _ROOT:
            return 0
        if nid not in depth:
            depth[nid] = 1 + max((_depth(pred) for _arg, pred in preds[nid]), default=0)
        return depth[nid]

    sinks = [nid for nid in members if consumer_count.get(nid, 0) == 0]
    sinks.sort(key=lambda nid: (_depth(nid), _id_sort_key(nid)))

    flow_map: "OrderedDict[str, Any]" = OrderedDict()
    emitted: Set[str] = set()
    # The source needs a referable name whenever a step reads it — a first step may omit
    # ``from:`` (it reads the source implicitly), but a LATER branch restarting from the
    # source must name it, and one identity step is simpler than two spellings.
    if any(pred == _ROOT for nid in members for _arg, pred in preds[nid]):
        flow_map[_flow_step_name(_ROOT)] = {}

    def _emit(nid: str) -> None:
        if nid == _ROOT or nid in emitted:
            return
        emitted.add(nid)  # acyclicity established by _check_component
        for _arg, pred in preds[nid]:
            _emit(pred)

        entry: Dict[str, Any] = {}
        if universe[nid] == "mix":
            (_, x), (_, y), (_, z) = preds[nid]
            entry["from"] = _flow_step_name(x)
            entry["merge_from"] = [_flow_step_name(y), _flow_step_name(z)]
        else:
            entry["op"] = _op_to_instance(
                nid, prompt[nid], node_mappings, dropped_notes=fold.notes, fold=fold, bind_captures=True
            )
            entry["from"] = _flow_step_name(preds[nid][0][1])

        binds = {
            param: f"{_flow_step_name(cap.producer_id)}.{cap.output_name}"
            for param, cap in fold.output_captures.get(str(nid), [])
        }
        if binds:
            entry["bind"] = binds
        flow_map[_flow_step_name(nid)] = entry

    for sink in sinks:
        _emit(sink)

    leftover = set(members) - emitted
    if leftover:  # pragma: no cover - defensive: every DAG node reaches some sink
        raise UnsupportedTopologyError(f"Op nodes {sorted(leftover, key=_id_sort_key)} are unreachable from any sink.")

    fan_out = Counter(pred for nid in members for _arg, pred in preds[nid] if pred != _ROOT)
    if flow_map and all(universe[nid] == "op" for nid in members) and all(count <= 1 for count in fan_out.values()):
        check_op_chain([entry["op"] for entry in flow_map.values() if "op" in entry], where="the drawn chain")
    outputs = next(reversed(flow_map)) if flow_map else ""
    return flow_map, outputs


# --------------------------------------------------------------------------- decision branches


def _class_name(class_type: str, node_mappings: Dict[str, Any]) -> str:
    """The wrapped op class's bare name — the word a user sees on the canvas — else the node type."""
    op_cls = _op_class_of(node_mappings.get(class_type))
    return str(getattr(op_cls, "__name__", class_type))


def _outcome_slots(class_type: str, node_mappings: Dict[str, Any]) -> Dict[int, str]:
    """``{output slot: outcome}`` of a TEST node — its ``flags`` as the bridge laid them out."""
    node_cls = node_mappings.get(class_type)
    outcomes = set(declared_outcomes(_op_class_of(node_cls)))
    names = list(getattr(node_cls, "RETURN_NAMES", ()) or ())
    return {slot: name for slot, name in enumerate(names) if slot >= 2 and name in outcomes}


def _can_be_gated(class_type: str, node_mappings: Dict[str, Any]) -> bool:
    op_cls = _op_class_of(node_mappings.get(class_type))
    try:
        return op_cls is not None and GATE_PARAM in inspect.signature(op_cls).parameters
    except (ValueError, TypeError):  # pragma: no cover - defensive
        return False


def _walk_branch(head: str, prompt: Dict[str, Any], successors: Dict[str, List[str]]) -> List[str]:
    """The straight run of nodes from a branch head along pass-through record wires, up to a join."""
    members = [head]
    current = head
    while True:
        following = [c for c in successors.get(current, []) if str(prompt[c].get("class_type", "")) != JOIN_CLASS_TYPE]
        if len(following) > 1:
            return members + [_FORK]
        if not following:
            return members
        current = following[0]
        members.append(current)


_FORK = "__fork__"


def fold_branches(prompt: Dict[str, Any], node_mappings: Dict[str, Any]) -> Dict[str, Any]:
    """Fold the DRAWN decision branches of a prompt graph into the linear, gated chain that runs.

    What the user draws (design 2026-09-06 — "the graph is the truth and should visually
    represent what is executed with branches and test"): a TEST node offers one record output
    per outcome (its ``flags``), each branch hangs off an outcome, and the branches rejoin in a
    ``Join branches`` node. What the record engine runs is a straight chain in which each branch
    node carries the gate that opens it — a closed gate passes the record through at the cost of
    one flag check, so no branch is computed "just in case". This is the ONLY place the two
    meet: the canvas never shows the gate, the document never shows the join.

    Per test node ``P``: every member of a branch gets the gate of that branch's outcome —
    unless it already carries one, which is how a decision NESTED in a branch keeps its own
    gate (the radio chain's ``try_linear`` branch decides ``linear`` inside); the branches are
    chained in ``P``'s own outcome order (branch 1's head from ``P``'s record output, branch k's
    head from branch k-1's tail); whatever the join fed — and whatever hung off ``P``'s
    pass-through record output — now follows the last tail; the join is dropped. Nested
    decisions fold INNERMOST first, so an outer branch's tail is its inner decision's last tail.

    Refused, each naming the nodes in the user's words: a join fed by nodes that are not the
    outcomes of ONE test node; a node on a branch that cannot be gated (it runs on every record);
    a branch that forks; a join wired to the middle of a branch; two branches on one outcome; a
    second join for one decision; more branches than the join has inputs. A prompt that draws
    no decision comes back unchanged (a plain fan-out keeps the exporters' own answer).
    """
    prompt = {nid: {**node, "inputs": dict(node.get("inputs", {}) or {})} for nid, node in prompt.items()}

    def class_type(nid: str) -> str:
        return str(prompt[nid].get("class_type", ""))

    def name_of(nid: str) -> str:
        return _class_name(class_type(nid), node_mappings)

    def survey() -> Tuple[Dict[str, Tuple[str, str]], Dict[str, List[str]]]:
        """``(heads, successors)``: branch heads → (test node, outcome); pass-through consumers."""
        heads: Dict[str, Tuple[str, str]] = {}
        successors: Dict[str, List[str]] = {}
        for nid, node in prompt.items():
            link = node["inputs"].get(_RECORD_INPUT)
            if not _is_link(link) or str(link[0]) not in prompt:
                continue
            src, slot = str(link[0]), int(link[1])
            if slot == 0:
                successors.setdefault(src, []).append(nid)
                continue
            outcome = _outcome_slots(class_type(src), node_mappings).get(slot)
            if outcome is not None:
                heads[nid] = (src, outcome)
        return heads, successors

    heads, successors = survey()
    if not heads and not any(class_type(nid) == JOIN_CLASS_TYPE for nid in prompt):
        return prompt

    while heads:
        # Decisions and their branches, as drawn right now.
        decisions: Dict[str, List[Tuple[str, List[str]]]] = {}
        for head, (decider, outcome) in heads.items():
            decisions.setdefault(decider, []).append((outcome, _walk_branch(head, prompt, successors)))
        # Innermost first: a decision none of whose branch members decides a drawn branch itself.
        innermost: Optional[str] = next(
            (t for t, drawn in decisions.items() if not any(m in decisions for _o, ms in drawn for m in ms)),
            None,
        )
        if innermost is None:  # pragma: no cover - defensive: a cycle of decisions cannot be drawn on a DAG
            raise UnsupportedTopologyError("the drawn decisions contain each other — redraw them as a chain")
        test = innermost
        drawn = decisions[test]
        order = list(declared_outcomes(_op_class_of(node_mappings.get(class_type(test)))))
        drawn.sort(key=lambda item: order.index(item[0]))
        seen: Set[str] = set()
        for outcome, members in drawn:
            if outcome in seen:
                raise UnsupportedTopologyError(
                    f"two branches hang off the {outcome!r} outcome of {name_of(test)} — wire one branch per outcome"
                )
            seen.add(outcome)
            if members[-1] == _FORK:
                raise UnsupportedTopologyError(
                    f"the {outcome!r} branch of {name_of(test)} forks at {name_of(members[-2])} — a branch is a "
                    "straight run into Join branches"
                )
            for member in members:
                gated = bool(prompt[member]["inputs"].get(GATE_PARAM))
                if not gated and not _can_be_gated(class_type(member), node_mappings):
                    raise UnsupportedTopologyError(
                        f"{name_of(member)} sits on the {outcome!r} branch of {name_of(test)} but runs on every "
                        "record — it cannot be gated, so it cannot follow an outcome; place it before the "
                        "decision or after Join branches"
                    )
        if len(drawn) > len(JOIN_INPUTS):
            raise UnsupportedTopologyError(
                f"{name_of(test)} has {len(drawn)} branches drawn; Join branches takes at most {len(JOIN_INPUTS)}"
            )
        tails = {members[-1]: outcome for outcome, members in drawn}
        member_outcome = {m: outcome for outcome, members in drawn for m in members}

        # The join this decision owns: every feeder a tail of one of ITS branches.
        owned: Optional[str] = None
        for jid in [nid for nid in prompt if class_type(nid) == JOIN_CLASS_TYPE]:
            inputs = prompt[jid]["inputs"]
            feeders = [str(inputs[key][0]) for key in JOIN_INPUTS if _is_link(inputs.get(key))]
            if not any(f in tails for f in feeders):
                continue
            middle = [f for f in feeders if f in member_outcome and f not in tails]
            if middle:
                raise UnsupportedTopologyError(
                    f"Join branches is wired to {name_of(middle[0])} in the middle of the "
                    f"{member_outcome[middle[0]]!r} branch of {name_of(test)} — wire the branch's last node"
                )
            strangers = [f for f in feeders if f not in tails]
            if strangers:
                others = sorted(
                    {t for t, ds in decisions.items() for _o, ms in ds for m in ms if m in strangers and t != test}
                )
                enclosing = [t for t in others if any(test in ms for _o, ms in decisions[t])]
                if others and len(enclosing) == len(others):
                    # The join closes an OUTER decision this one is nested in (the radio chain's
                    # `linear` decision inside the `try_linear` branch): once this decision has
                    # folded into its branch, its tail is that branch's tail and the outer fold
                    # owns the join. Leave it.
                    continue
                if others:
                    named = " and of ".join(name_of(t) for t in [test, *others])
                    raise UnsupportedTopologyError(
                        f"Join branches joins the outcomes of ONE decision node — its inputs come from branches of "
                        f"{named}; join each decision separately"
                    )
                raise UnsupportedTopologyError(
                    f"Join branches joins the outcomes of ONE decision node — its inputs come from "
                    f"{', '.join(name_of(f) for f in strangers)}, which decide nothing"
                )
            if owned is not None:
                raise UnsupportedTopologyError(
                    f"two Join branches nodes close the decision of {name_of(test)} — keep one"
                )
            owned = jid

        # Rewire into the chain: branches in outcome order, gates written, the continuation after the last tail.
        continuing = [nid for nid in successors.get(test, []) if nid not in heads]
        previous = test
        for outcome, members in drawn:
            prompt[members[0]]["inputs"][_RECORD_INPUT] = [previous, 0]
            for member in members:
                if not prompt[member]["inputs"].get(GATE_PARAM):
                    prompt[member]["inputs"][GATE_PARAM] = outcome
            previous = members[-1]
        if owned is not None:
            for node in prompt.values():
                for key, value in list(node["inputs"].items()):
                    if _is_link(value) and str(value[0]) == owned:
                        node["inputs"][key] = [previous, 0]
            del prompt[owned]
        for nid in continuing:
            prompt[nid]["inputs"][_RECORD_INPUT] = [previous, 0]
        heads, successors = survey()

    leftover = [nid for nid in prompt if class_type(nid) == JOIN_CLASS_TYPE]
    for jid in leftover:
        inputs = prompt[jid]["inputs"]
        feeders = [str(inputs[key][0]) for key in JOIN_INPUTS if _is_link(inputs.get(key))]
        named = ", ".join(name_of(f) for f in feeders if f in prompt) or "nothing"
        raise UnsupportedTopologyError(
            f"Join branches joins the outcomes of ONE decision node — its inputs come from {named}, "
            "which decide nothing"
        )
    return prompt


def check_op_chain(ops: Sequence[Any], where: str = "") -> None:
    """The record engine's per-node contract check over a STRAIGHT chain of op markers.

    The editor honours each node's input/output contract (user instruction 2026-09-06): a chain
    that reads an entry nothing before it writes is refused where it is drawn, with the engine's
    own located message, instead of surfacing later as an empty pane. The ops are constructed
    from their literal kwargs (a constructor does no work, by mandate); a chain whose kwargs
    embed other nodes or references is not checked here — it is checked where it runs. The
    first op's needs are the chain's INPUT contract, so only what a LATER op needs and nothing
    before it produces is a refusal.
    """
    live: List[Any] = []
    for marker in ops:
        target = getattr(marker, "target", None)
        kwargs = getattr(marker, "kwargs", None)
        if target is None or kwargs is None:
            return
        if any(_is_node_value(v) or isinstance(v, Reference) for v in kwargs.values()):
            return
        cls = target if isinstance(target, type) else None
        if cls is None:
            return
        try:
            live.append(cls(**kwargs))
        except Exception as exc:  # noqa: BLE001 - an op that will not build here is checked where it runs
            name = getattr(cls, "__name__", cls)
            logger.debug(f"check_op_chain: {name} not constructed ({exc}); chain not checked")
            return
    provided = set(getattr(live[0], "consumes", None) or {}) if live else set()
    try:
        check_chain(live, provided=provided, where=where)
    except ChainContractError as exc:
        raise UnsupportedTopologyError(str(exc)) from None


def _is_node_value(value: Any) -> bool:
    if isinstance(value, Target):  # PartialClass is a Target subclass
        return True
    if isinstance(value, (list, tuple)):
        return any(_is_node_value(item) for item in value)
    return False


# --------------------------------------------------------------------------- public compile API


def _compile_components(
    prompt: Dict[str, Any],
    node_mappings: Dict[str, Any],
    chain_notes: Optional[List[List[str]]] = None,
    chain_constants: Optional[List[Dict[str, Any]]] = None,
    skip_ids: Optional[Any] = None,
    target_ids: Optional[Any] = None,
) -> List[Any]:
    """Compile a ComfyUI prompt graph — ONE ``flow:`` document per connected op component.

    The shared front half of the canvas compiler: export-skip / export-target marks,
    value-chain and ``@output``-capture collection, constant hoisting, and component
    splitting. Each weakly-connected component is emitted as named steps by
    :func:`_compile_component_flow`; components are returned in deterministic
    smallest-node-id order.

    A widget input driven by a link is folded where possible (primitive inline,
    ``constant=True`` value node hoisted + dotted ``!ref:``, ``op_N`` producers embedded);
    what remains is omitted (the op keeps its constructor default) with a note.

    Args:
        prompt: The ComfyUI prompt / API-format graph (``{node_id: {class_type, inputs}}``).
        node_mappings: StreamStudio's ``NODE_CLASS_MAPPINGS`` (maps ``class_type`` → node class,
            carrying ``TAIDAL_NODE_KIND`` / ``TAIDAL_CALLABLE_PATH`` / ``TAIDAL_CONSTANT``).
        chain_notes: Optional accumulator; one ``List[str]`` of notes is appended PER
            returned component (parallel lists), for the exporter's comment block.
        chain_constants: Optional accumulator; one ``{name: Target}`` dict of HOISTED
            constant entries is appended PER component — the YAML exporters emit them as
            top-level keys above ``ops:`` (a bare compile drops them, so pass this when
            the result will be dumped).
        skip_ids: Node ids marked CANVAS-ONLY (``properties["taidal_export"] == "skip"``,
            collected by the canvas export button / the CLI ``--skip`` flag). Each id
            excludes the node, its subgraph-inlined inner nodes (prefix ``<id>:``), and
            everything data-downstream of it (record wires + Mix args), with a note. The
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
        One list per component, each suitable for ``Stream(ops=...)`` after a dump/load
        round-trip — op ``Target`` markers plus any inserted ``Stash*``/``Unstash*``
        instances. Empty when the graph has no op nodes.

    Raises:
        UnsupportedTopologyError: a cycle, an unresolvable op node, a component merging
            several distinct record streams, an unknown export target, or a skip mark
            directly on a node inside a target's dependency cone.
    """
    prompt = fold_branches(prompt, node_mappings)  # drawn decisions -> the gated chain (see fold_branches)
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
    # op-@output → param captures: a context Capture on the producer + an Apply on the consumer.
    # Both stay scheduled (the producer also transforms the record), so captures add NOTHING to
    # schedule_excluded.
    output_captures = _collect_output_captures(prompt, node_mappings)
    # A consumed Subgraph-Op terminator's inner ops embed as a nested Pipeline in the
    # consumer (see _subgraph_op_instance), so they drop from the STANDALONE schedule — but NOT
    # from the cone compile (fold.excluded carries only the value-chain set). EXEMPT the resolved
    # targets: when SubgraphOpExtractNode compiles its OWN cone (target_ids=[terminator]), that
    # terminator's op output is also consumed (by the Stream), so without the exemption it would
    # exclude its own cone and the remnant fragments into several components (UnsupportedTopology).
    sg_excluded = _subgraph_cone_excluded(prompt, node_mappings, exempt=set(resolved_targets))
    schedule_excluded = vc_excluded | sg_excluded

    def _compile(restrict: Optional[Set[str]], extra_notes: List[str]) -> List[Any]:
        universe, preds, boundary_feeds = _build_graph(prompt, node_mappings, schedule_excluded, restrict)
        if not universe:
            logger.debug("compile_flow_documents: no op nodes in graph — returning no documents.")
            return []
        compiled: List[Any] = []
        for members in _components(universe, preds):
            _check_component(members, preds, boundary_feeds)
            notes: List[str] = [*base_notes, *extra_notes]
            fold = _FoldContext(
                prompt, node_mappings, notes, configure_specs, excluded=vc_excluded, output_captures=output_captures
            )
            compiled.append(_compile_component_flow(members, universe, preds, prompt, node_mappings, fold))
            if chain_notes is not None:
                chain_notes.append(notes)
            if chain_constants is not None:
                chain_constants.append(dict(fold.constants))
        return compiled

    if not target_cones:
        return _compile(restrict=None, extra_notes=[])

    chains: List[Any] = []
    for target, cone in target_cones.items():
        note = f"compiled for export target: node {target} ({prompt[target].get('class_type', '?')})"
        chains.extend(_compile(restrict=cone, extra_notes=[note]))
    return chains


def compile_flow_documents(
    prompt: Dict[str, Any],
    node_mappings: Dict[str, Any],
    chain_notes: Optional[List[List[str]]] = None,
    chain_constants: Optional[List[Dict[str, Any]]] = None,
    skip_ids: Optional[Any] = None,
    target_ids: Optional[Any] = None,
) -> List[Tuple["OrderedDict[str, Any]", str]]:
    """Compile a prompt graph — ONE ``(flow_mapping, outputs)`` pair per connected component.

    The canvas IS a graph, so this compiles it AS one: :func:`_compile_component_flow` emits
    the wires the user drew as step grammar (``from:`` / ``merge_from:`` / ``bind:``). This is
    the ONLY canvas compiler — the flat ``ops:`` emitter was deleted 2026-08-01 (see the module
    docstring), so there is no second form to keep in parity.
    """
    return cast(
        List[Tuple["OrderedDict[str, Any]", str]],
        _compile_components(
            prompt,
            node_mappings,
            chain_notes=chain_notes,
            chain_constants=chain_constants,
            skip_ids=skip_ids,
            target_ids=target_ids,
        ),
    )


def _comment_block(notes: List[str]) -> str:
    """The leading ``#`` comment block for one document (empty when there are no notes)."""
    if not notes:
        return ""
    lines = ["# NOTE: flow-export compilation notes:"]
    lines += [f"#   - {note}" for note in notes]
    return "\n".join(lines) + "\n"


def export_flow_yamls(
    prompt: Dict[str, Any],
    node_mappings: Dict[str, Any],
    skip_ids: Optional[Any] = None,
    target_ids: Optional[Any] = None,
) -> List[str]:
    """Compile a prompt graph and serialize EACH op component as a ``flow:`` document.

    The canvas IS a graph, so this compiles it AS one — :func:`compile_flow_documents` emits
    named steps directly into ``{…constants…, flow: {step: …}, outputs: …}``. It does NOT
    linearize and lift back (the shape until 2026-07-30: compile to context ops, then
    reconstruct the graph with ``from_ops`` purely for readability — a round trip through a
    form that destroyed exactly the structure being rebuilt). Step names are the canvas node
    ids (``node_<id>``), so a step in the document points at the node that produced it.

    The consumer is ``FlowGraph.from_yaml(path)`` — or any engine entry point, since a flow
    document and an ``ops:`` list are two spellings of one step graph.
    """
    notes_per_chain: List[List[str]] = []
    constants_per_chain: List[Dict[str, Any]] = []
    flows = compile_flow_documents(
        prompt,
        node_mappings,
        chain_notes=notes_per_chain,
        chain_constants=constants_per_chain,
        skip_ids=skip_ids,
        target_ids=target_ids,
    )
    if not flows:
        return [dump({"flow": {}})]
    docs: List[str] = []
    for (flow_map, outputs), notes, constants in zip(flows, notes_per_chain, constants_per_chain):
        document: Dict[str, Any] = dict(constants)
        document["flow"] = dict(flow_map)
        document["outputs"] = outputs
        docs.append(_comment_block(notes) + dump(document))
    return docs


def chain_output_paths(base: Union[str, Path], count: int) -> List[Path]:
    """Output file paths for ``count`` component documents derived from one user-given ``base``.

    One component keeps the base path verbatim (``flow.yaml``); several get a 1-based stem
    suffix (``flow_1.yaml`` … ``flow_N.yaml``). Shared by the ``/taidal/export_flow`` route and
    the ``streamstudio export`` CLI so the file-naming policy can't fork.
    """
    base = Path(base)
    if count <= 1:
        return [base]
    return [base.with_name(f"{base.stem}_{i}{base.suffix}") for i in range(1, count + 1)]


__all__ = [
    "UnsupportedTopologyError",
    "chain_output_paths",
    "collect_export_marks",
    "compile_flow_documents",
    "export_flow_yamls",
]
