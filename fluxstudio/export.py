"""Compile a FluxStudio canvas graph's **ops** into a Confluid YAML op-list.

A FluxStudio canvas is a :class:`dataflux.core.Flux` turned inside-out — the source
is a ``-> DATAFLUX_SOURCE`` node, each transform a ``DATAFLUX_SAMPLE -> DATAFLUX_SAMPLE``
op node, and the lazy ``__iter__`` is replaced by the Walk/Extract adapters. This module
reverses the *ops half*: given a ComfyUI **prompt / API-format** graph it emits the ordered
op chain as Confluid ``!class:...()`` markers (dropping the source + adapter nodes), so a
user can reattach it to their own source::

    import confluid
    from dataflux import Flux
    ops = confluid.load("ops.yaml")["ops"]
    flux = Flux(source=my_own_source, ops=ops)

Why this is possible: every op node wraps a ``@configurable`` class, and
:func:`confluid.dump` / :func:`confluid.load` round-trip those — including the **recursive**
``Tee(branches=[[...],[...]])``, which is how a branchy graph folds into one (nested) op-list.

Scope (v1): linear op chains, plus fan-out (one op output → ≥2 op nodes) compiled to a
:class:`dataflux.ops.tee.Tee`. A graph holding SEVERAL disconnected op chains exports one
op-list per chain (:func:`compile_op_chains` / :func:`export_ops_yamls` — what the
``/taidal/export_ops`` route and the ``fluxstudio export`` CLI use, writing one YAML doc/file
per chain); the strict single-chain forms (:func:`compile_ops` / :func:`export_ops_yaml`)
still raise on a multi-chain graph. The compiler is a **faithful translator, not an
optimizer**: it does NOT auto-insert ``Copy``/``Stash`` for branch isolation — the user places
those nodes explicitly (they exist on the canvas). True multi-input merges (fan-in), cycles,
and widget-driven-by-link inputs raise :class:`UnsupportedTopologyError`.

Input format — the ComfyUI prompt / API graph (what ``app.graphToPrompt()`` emits and ``/prompt``
consumes), NOT the richer UI ``workflow.json``::

    {"<node_id>": {"class_type": "Taidal_dataflux_op_numpy_RescaleOp",
                   "inputs": {"sample": ["3", 0], "factor": 2.0}}, ...}

``inputs`` values are either literals (widget values) or ``["<upstream_id>", <slot>]`` links.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Union, cast

from confluid import dump
from confluid.fluid import Instance
from dataflux.discovery import resolve_callable
from dataflux.ops.tee import Tee
from logflow import get_logger

from fluxstudio.bridge import _coerce_kwargs

logger = get_logger(__name__)

# The single ``DATAFLUX_SAMPLE`` input socket every generated op node declares
# (see ``bridge._build_op_node``) — the one edge that defines op-to-op data flow.
_SAMPLE_INPUT = "sample"


class UnsupportedTopologyError(ValueError):
    """The canvas op-graph can't be expressed as (possibly Tee-nested) op-lists.

    Raised for fan-in / multi-input merges (use the ``Stash``/``Unstash`` metadata-bus
    pattern instead), cycles, widget-inputs driven by a link, an op node whose wrapped
    callable can't be resolved — and, from the strict single-chain forms
    (:func:`compile_ops` / :func:`export_ops_yaml`) only, multiple disconnected op chains
    (the per-chain forms :func:`compile_op_chains` / :func:`export_ops_yamls` accept those).
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


def _op_to_instance(node_id: str, node: Dict[str, Any], node_mappings: Dict[str, Any]) -> Instance:
    """Build a Confluid ``Instance`` marker (``!class:X()`` on dump) for one op node.

    Recovers the wrapped class from ``TAIDAL_CALLABLE_PATH``, takes the literal (non-link)
    widget inputs — minus the ``sample`` wire — and runs them through the SAME coercion the
    live node uses (``bridge._coerce_kwargs``), so blank/optional/List widgets serialize the
    way they'd construct. Explicit kwargs on the ``Instance`` make the dump robust regardless
    of whether the op stores each arg as a same-named attribute.
    """
    class_type = node["class_type"]
    cls = node_mappings.get(class_type)
    path = getattr(cls, "TAIDAL_CALLABLE_PATH", None) if cls is not None else None
    if not path:
        raise UnsupportedTopologyError(
            f"Node {node_id!r} ({class_type}) has no resolvable TAIDAL_CALLABLE_PATH; "
            "cannot serialize it as a Confluid !class: op (is it a generated op node?)."
        )

    inputs = node.get("inputs", {})
    literal_inputs: Dict[str, Any] = {}
    for key, value in inputs.items():
        if key == _SAMPLE_INPUT:
            continue
        if _is_link(value):
            raise UnsupportedTopologyError(
                f"Input {key!r} on node {node_id!r} ({class_type}) is driven by a link, not a "
                "widget value. Ops export needs literal config values — set it inline on the node."
            )
        literal_inputs[key] = value

    target = resolve_callable(path)
    coerced = _coerce_kwargs(target, literal_inputs)
    # ``target`` is the op's @configurable CLASS; pass the type (not the string path) so the
    # dumper emits the dotted ``!class:module.Qualname()`` form that ``confluid.load`` resolves.
    return Instance(cast(type, target), **coerced)


def _id_sort_key(nid: str) -> Any:
    """Deterministic node-id ordering: numeric-aware so ``'2' < '10'``, strings after."""
    return (0, int(nid)) if nid.isdigit() else (1, nid)


def compile_op_chains(prompt: Dict[str, Any], node_mappings: Dict[str, Any]) -> List[List[Any]]:
    """Compile a ComfyUI prompt graph into op-lists — ONE per independent op chain.

    Unlike :func:`compile_ops`, disconnected op chains are legal: each chain head (an op
    whose ``sample`` input comes from a boundary node or is unconnected) starts its own
    ordered (Tee-nested) op-list. Chains are returned in deterministic head-node-id order
    (numeric-aware, so node ``'2'`` sorts before ``'10'``).

    Args:
        prompt: The ComfyUI prompt / API-format graph (``{node_id: {class_type, inputs}}``).
        node_mappings: FluxStudio's ``NODE_CLASS_MAPPINGS`` (maps ``class_type`` → node class,
            carrying ``TAIDAL_NODE_KIND`` / ``TAIDAL_CALLABLE_PATH``).

    Returns:
        One list per chain, each suitable for ``Flux(ops=...)`` after a dump/load round-trip —
        every element a Confluid ``Instance`` (an op), with a synthesized ``Tee`` ``Instance``
        at any fan-out tail. Empty when the graph has no op nodes.

    Raises:
        UnsupportedTopologyError: fan-in, a cycle, a widget-input driven by a link, or an
            unresolvable op node.
    """
    op_ids = [nid for nid, node in prompt.items() if _node_kind(node.get("class_type", ""), node_mappings) == "op"]
    if not op_ids:
        logger.debug("compile_op_chains: no op nodes in graph — returning no chains.")
        return []

    op_id_set = set(op_ids)

    # Successors: op nodes whose ``sample`` socket is fed by a given op node.
    successors: Dict[str, List[str]] = {nid: [] for nid in op_ids}
    roots: List[str] = []
    for nid in op_ids:
        pred = _sample_predecessor(prompt[nid])
        if pred is not None and pred in op_id_set:
            successors[pred].append(nid)
        else:
            # sample comes from a boundary node (Walk / source) or is unconnected → chain head.
            roots.append(nid)

    if not roots:
        # Op nodes exist but none is a chain head — every op's ``sample`` is fed by another op,
        # i.e. the op-graph contains a cycle. ComfyUI graphs are acyclic in practice; guard anyway.
        raise UnsupportedTopologyError(
            "Op-graph has no chain head (every op's sample input is fed by another op) — "
            "it contains a cycle; op-graphs must be acyclic."
        )

    visited: set = set()

    def linearize(nid: str) -> List[Any]:
        if nid in visited:  # pragma: no cover - defensive: single-socket op nodes form a forest, never a re-entrant DAG
            raise UnsupportedTopologyError(f"Cycle detected at node {nid!r}; op-graphs must be acyclic.")
        visited.add(nid)
        instance = _op_to_instance(nid, prompt[nid], node_mappings)
        children = sorted(successors[nid], key=_id_sort_key)
        if not children:
            return [instance]
        if len(children) == 1:
            return [instance, *linearize(children[0])]
        # Fan-out → Tee. Each branch is its own op sub-list. Tee runs them sequentially on the
        # same Sample/metadata; branches don't reconverge via a sample wire (that would be fan-in,
        # which is impossible through the single ``sample`` socket and rejected above).
        branches = [linearize(child) for child in children]
        return [instance, Instance(Tee, branches=branches)]

    chains = [linearize(root) for root in sorted(roots, key=_id_sort_key)]

    # Every op node has at most one sample predecessor (single ``sample`` socket), so the
    # op-graph is a forest: any node not reached from some chain head sits in a component
    # where EVERY node has an op predecessor — a cycle.
    unvisited = op_id_set - visited
    if unvisited:
        raise UnsupportedTopologyError(
            f"Op nodes {sorted(unvisited, key=_id_sort_key)} are not reachable from any chain "
            "head — they form a cycle; op-graphs must be acyclic."
        )
    return chains


def compile_ops(prompt: Dict[str, Any], node_mappings: Dict[str, Any]) -> List[Any]:
    """Compile a prompt graph into a SINGLE ordered (Tee-nested) list of op ``Instance``\\ s.

    The strict single-chain variant of :func:`compile_op_chains` — kept for consumers that
    want the one-pipeline guarantee. Args/returns as there, except:

    Returns:
        The single chain's op-list; empty when the graph has no op nodes.

    Raises:
        UnsupportedTopologyError: everything :func:`compile_op_chains` raises, PLUS a graph
            holding more than one independent op chain.
    """
    chains = compile_op_chains(prompt, node_mappings)
    if len(chains) > 1:
        raise UnsupportedTopologyError(
            f"Found {len(chains)} independent op chains; compile_ops/export_ops_yaml need a "
            "single connected chain — use compile_op_chains/export_ops_yamls to compile each "
            "chain individually."
        )
    return chains[0] if chains else []


def export_ops_yaml(prompt: Dict[str, Any], node_mappings: Dict[str, Any]) -> str:
    """Compile a prompt graph and serialize its ops to a Confluid YAML document.

    The result is a mapping with a single ``ops:`` key holding the ``!class:...()`` op-list,
    so the consumer side is ``confluid.load(path)["ops"]`` → ``Flux(source=..., ops=...)``.
    Strict: a graph with several independent op chains raises (via :func:`compile_ops`) —
    use :func:`export_ops_yamls` to get one document per chain instead.
    """
    return dump({"ops": compile_ops(prompt, node_mappings)})


def export_ops_yamls(prompt: Dict[str, Any], node_mappings: Dict[str, Any]) -> List[str]:
    """Compile a prompt graph and serialize EACH independent op chain to its own YAML document.

    Every document is a complete, individually-loadable ``{ops: [!class:...()]}`` mapping —
    the same consumer contract as :func:`export_ops_yaml` (``confluid.load(path)["ops"]``),
    one per chain, in deterministic head-node-id order. A graph with no op nodes yields a
    single empty ``ops: []`` document (callers always have something to write/download).
    """
    chains = compile_op_chains(prompt, node_mappings) or [[]]
    return [dump({"ops": chain}) for chain in chains]


def chain_output_paths(base: Union[str, Path], count: int) -> List[Path]:
    """Output file paths for ``count`` chain documents derived from one user-given ``base``.

    One chain keeps the base path verbatim (``ops.yaml``); several get a 1-based stem suffix
    (``ops_1.yaml`` … ``ops_N.yaml``). Shared by the ``/taidal/export_ops`` route and the
    ``fluxstudio export`` CLI so the file-naming policy can't fork.
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
