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
  NOT reproduced, and the inserted ``__taidal_stash_*`` keys remain in metadata.
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
from dataflux.ops.configure import ConfigureOp
from dataflux.ops.formula import FormulaOp
from dataflux.ops.stash import StashInputOp, StashTargetOp, UnstashInputOp, UnstashTargetOp
from logflow import get_logger

from fluxstudio.bridge import _SINGLE_OP_PARAMS, _coerce_kwargs, _op_list_param

logger = get_logger(__name__)

# The single ``DATAFLUX_SAMPLE`` input socket every generated op node declares
# (see ``bridge._build_op_node``) — the one edge that defines op-to-op data flow.
_SAMPLE_INPUT = "sample"

# The canvas fan-in adapter (``fluxstudio.nodes.MixSamplesNode``): input from arg 1,
# target from arg 2, metadata = the shallow union of all three (arg 3 wins).
_MIX_CLASS_TYPE = "Taidal_MixSamples"
_MIX_ARGS = ("input_sample", "target_sample", "metadata_sample")

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

# The canvas-only mark: a node whose ``properties["taidal_export"] == "skip"`` (toggled by
# ``web/export_skip.js``) runs on the canvas but is EXCLUDED from exports — it and
# everything data-downstream of it (sample wires + Mix args). The canvas export button
# collects the marked ids and posts them as ``skip_ids``; the CLI takes ``--skip``.
# (ComfyUI's native mute/bypass need no mark: the frontend drops/reroutes those nodes
# before the API prompt ever reaches the exporter.)
_EXPORT_SKIP_PROPERTY = "taidal_export"
_EXPORT_SKIP_VALUE = "skip"

# Virtual node id for "the component's incoming sample" (whatever the Flux source feeds).
_ROOT = "__taidal_root__"

# Dynamic ``DATAFLUX_OP`` value sockets a higher-order op node grows (``web/op_list.js``).
_OP_SOCKET_RE = re.compile(r"^op_(\d+)$")

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
    ) -> None:
        self.prompt = prompt
        self.node_mappings = node_mappings
        self.notes = notes
        self.configure_specs = configure_specs or {}
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
        for key, value in inputs.items():
            if key == _SAMPLE_INPUT or not _is_link(value):
                continue
            if _OP_SOCKET_RE.match(key) or key in _SINGLE_OP_PARAMS:
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


def _expand_skip_ids(skip_ids: Any, prompt: Dict[str, Any]) -> Set[str]:
    """Resolve user-marked skip ids to the full excluded node set.

    Two expansions: a marked id also matches its SUBGRAPH-inlined inner nodes
    (``"125"`` excludes ``"125:121"`` — marking an instance excludes the whole instance),
    and the set closes over DATA edges (``sample`` wires + Mix arguments) — everything
    downstream of a marked node has no exportable feed, so it is canvas-only too.
    Widget/op-value wires from an excluded node do NOT exclude the consumer (it just
    loses that value and keeps its constructor default, with a note).
    """
    marked = {str(sid) for sid in (skip_ids or [])}
    if not marked:
        return set()
    excluded = {nid for nid in prompt if nid in marked or any(nid.startswith(f"{sid}:") for sid in marked)}
    while True:
        grew = False
        for nid, node in prompt.items():
            if nid in excluded:
                continue
            inputs = node.get("inputs", {})
            data_args = _MIX_ARGS if str(node.get("class_type", "")) == _MIX_CLASS_TYPE else (_SAMPLE_INPUT,)
            for arg in data_args:
                value = inputs.get(arg)
                if _is_link(value) and str(value[0]) in excluded:
                    excluded.add(nid)
                    grew = True
                    break
        if not grew:
            return excluded


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

    if class_type in _PRIMITIVE_VALUE_NODES:
        value = upstream.get("inputs", {}).get("value")
        if _is_link(value):  # a primitive fed by another node — follow the wire
            return _resolve_widget_link(target, key, value, fold)
        return ("literal", value)

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
    target: Any,
    class_type: str,
    node_id: str,
    op_links: List[Tuple[int, str, Any]],
    fold: _FoldContext,
) -> Optional[Tuple[str, Any]]:
    """Compile ``op_N`` links into the higher-order op's op-list constructor kwarg.

    Each wired producer must be a generated op node; it embeds as a nested ``!class:``
    Instance built from its own widgets (its wired widgets fold through the same pass).
    ``branches`` params get each op wrapped as its own 1-op branch, mirroring the live
    node (``bridge._build_op_node``). Returns ``(param_name, value)`` or ``None``.
    """
    param = _op_list_param(target)
    if param is None:
        fold.notes.append(
            f"{class_type} (node {node_id}): has wired op_N inputs but no op-list constructor " "param — omitted"
        )
        return None
    instances: List[Any] = []
    for _idx, key, link in sorted(op_links, key=lambda item: item[0]):
        src = str(link[0])
        upstream = fold.prompt.get(src)
        upstream_cls = fold.node_mappings.get(str(upstream.get("class_type", ""))) if upstream else None
        if upstream is None or getattr(upstream_cls, "TAIDAL_NODE_KIND", None) != "op":
            fold.notes.append(
                f"{class_type} (node {node_id}): {key!r} is wired from node {link[0]} — omitted, "
                "not a generated op node"
            )
            continue
        instances.append(_op_to_instance(src, upstream, fold.node_mappings, dropped_notes=fold.notes, fold=fold))
    if not instances:
        return None
    return param, ([[op] for op in instances] if param == "branches" else instances)


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

    inputs = node.get("inputs", {})
    literal_inputs: Dict[str, Any] = {}
    ref_inputs: Dict[str, Any] = {}
    op_links: List[Tuple[int, str, Any]] = []
    for key, value in inputs.items():
        if key == _SAMPLE_INPUT or key in configured_params:
            continue  # a configured param is injected per sample by the ConfigureOp wrap below
        if not _is_link(value):
            literal_inputs[key] = value
            continue
        socket_match = _OP_SOCKET_RE.match(key)
        if fold is not None and socket_match:
            op_links.append((int(socket_match.group(1)), key, value))
            continue
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
        embedded = _embed_op_values(target, class_type, node_id, op_links, fold)
        if embedded is not None:
            coerced[embedded[0]] = embedded[1]
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
    return instance


# --------------------------------------------------------------------------- DAG scheduling


def _build_graph(
    prompt: Dict[str, Any], node_mappings: Dict[str, Any], excluded: Optional[Set[str]] = None
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
        class_type = str(node.get("class_type", ""))
        if class_type == _MIX_CLASS_TYPE:
            universe[nid] = "mix"
        elif _node_kind(class_type, node_mappings) == "op":
            universe[nid] = "op"

    value_consumed: Set[str] = set()
    for node in prompt.values():
        for key, value in node.get("inputs", {}).items():
            if _is_link(value) and (_OP_SOCKET_RE.match(key) or key in _SINGLE_OP_PARAMS):
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
    if inserted:
        fold.notes.append(
            f"branching canvas wiring compiled to a sequential op-list — {inserted} stash/unstash "
            f"ops inserted (metadata keys '{_STASH_KEY_PREFIX}*' remain on the sample)"
        )
    return ops_out


# --------------------------------------------------------------------------- public compile API


def compile_op_chains(
    prompt: Dict[str, Any],
    node_mappings: Dict[str, Any],
    chain_notes: Optional[List[List[str]]] = None,
    chain_constants: Optional[List[Dict[str, Any]]] = None,
    skip_ids: Optional[Any] = None,
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
            everything data-downstream of it (sample wires + Mix args), with a note.

    Returns:
        One list per component, each suitable for ``Flux(ops=...)`` after a dump/load
        round-trip — op ``Instance`` markers plus any inserted ``Stash*``/``Unstash*``
        instances. Empty when the graph has no op nodes.

    Raises:
        UnsupportedTopologyError: a cycle, an unresolvable op node, or a component
            merging several distinct sample streams.
    """
    prompt = {str(nid): node for nid, node in prompt.items()}
    skipped_marks = _expand_skip_ids(skip_ids, prompt)
    skip_note: Optional[str] = None
    if skipped_marks:
        listed = ", ".join(
            f"node {nid} ({prompt[nid].get('class_type', '?')})" for nid in sorted(skipped_marks, key=_id_sort_key)
        )
        skip_note = f"excluded by export-skip mark (canvas-only, incl. data-downstream nodes): {listed}"
        prompt = {nid: node for nid, node in prompt.items() if nid not in skipped_marks}

    configure_specs, excluded = _collect_value_chains(prompt, node_mappings)
    universe, preds, boundary_feeds = _build_graph(prompt, node_mappings, excluded)
    if not universe:
        logger.debug("compile_op_chains: no op nodes in graph — returning no chains.")
        return []

    chains: List[List[Any]] = []
    for members in _components(universe, preds):
        _check_component(members, preds, boundary_feeds)
        notes: List[str] = [skip_note] if skip_note else []
        fold = _FoldContext(prompt, node_mappings, notes, configure_specs)
        chains.append(_compile_component(members, universe, preds, prompt, node_mappings, fold))
        if chain_notes is not None:
            chain_notes.append(notes)
        if chain_constants is not None:
            chain_constants.append(dict(fold.constants))
    return chains


def compile_ops(prompt: Dict[str, Any], node_mappings: Dict[str, Any], skip_ids: Optional[Any] = None) -> List[Any]:
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
    chains = compile_op_chains(prompt, node_mappings, skip_ids=skip_ids)
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


def export_ops_yaml(prompt: Dict[str, Any], node_mappings: Dict[str, Any], skip_ids: Optional[Any] = None) -> str:
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
        prompt, node_mappings, chain_notes=notes_per_chain, chain_constants=constants_per_chain, skip_ids=skip_ids
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
    prompt: Dict[str, Any], node_mappings: Dict[str, Any], skip_ids: Optional[Any] = None
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
        prompt, node_mappings, chain_notes=notes_per_chain, chain_constants=constants_per_chain, skip_ids=skip_ids
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
    "compile_op_chains",
    "compile_ops",
    "export_ops_yaml",
    "export_ops_yamls",
]
