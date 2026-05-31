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

Scope (v1): a single linear op chain, plus fan-out (one op output → ≥2 op nodes) compiled to a
:class:`dataflux.ops.tee.Tee`. The compiler is a **faithful translator, not an optimizer**: it
does NOT auto-insert ``Copy``/``Stash`` for branch isolation — the user places those nodes
explicitly (they exist on the canvas). True multi-input merges (fan-in) and widget-driven-by-link
inputs raise :class:`UnsupportedTopologyError`.

Input format — the ComfyUI prompt / API graph (what ``app.graphToPrompt()`` emits and ``/prompt``
consumes), NOT the richer UI ``workflow.json``::

    {"<node_id>": {"class_type": "Taidal_dataflux_op_numpy_RescaleOp",
                   "inputs": {"sample": ["3", 0], "factor": 2.0}}, ...}

``inputs`` values are either literals (widget values) or ``["<upstream_id>", <slot>]`` links.
"""

from typing import Any, Dict, List, Optional, cast

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
    """The canvas op-graph can't be expressed as a (possibly Tee-nested) op-list.

    Raised for fan-in / multi-input merges (use the ``Stash``/``Unstash`` metadata-bus
    pattern instead), multiple disconnected op chains, widget-inputs driven by a link,
    or an op node whose wrapped callable can't be resolved.
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


def compile_ops(prompt: Dict[str, Any], node_mappings: Dict[str, Any]) -> List[Any]:
    """Compile a ComfyUI prompt graph into an ordered (Tee-nested) list of op ``Instance``\\ s.

    Args:
        prompt: The ComfyUI prompt / API-format graph (``{node_id: {class_type, inputs}}``).
        node_mappings: FluxStudio's ``NODE_CLASS_MAPPINGS`` (maps ``class_type`` → node class,
            carrying ``TAIDAL_NODE_KIND`` / ``TAIDAL_CALLABLE_PATH``).

    Returns:
        A list suitable for ``Flux(ops=...)`` after a dump/load round-trip — each element is a
        Confluid ``Instance`` (an op), with a synthesized ``Tee`` ``Instance`` at any fan-out tail.
        Empty when the graph has no op nodes.

    Raises:
        UnsupportedTopologyError: fan-in, multiple disconnected op chains, a cycle, a
            widget-input driven by a link, or an unresolvable op node.
    """
    op_ids = [nid for nid, node in prompt.items() if _node_kind(node.get("class_type", ""), node_mappings) == "op"]
    if not op_ids:
        logger.debug("compile_ops: no op nodes in graph — returning empty op-list.")
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
    if len(roots) > 1:
        raise UnsupportedTopologyError(
            f"Found {len(roots)} independent op-chain heads ({sorted(roots)}); ops export needs a "
            "single connected chain. Export one chain at a time, or route via Stash/Unstash."
        )

    # Deterministic branch order: sort successor ids (numeric-aware so '2' < '10').
    def _sort_key(nid: str) -> Any:
        return (0, int(nid)) if nid.isdigit() else (1, nid)

    visited: set = set()

    def linearize(nid: str) -> List[Any]:
        if nid in visited:  # pragma: no cover - defensive: single-socket op nodes form a forest, never a re-entrant DAG
            raise UnsupportedTopologyError(f"Cycle detected at node {nid!r}; op-graphs must be acyclic.")
        visited.add(nid)
        instance = _op_to_instance(nid, prompt[nid], node_mappings)
        children = sorted(successors[nid], key=_sort_key)
        if not children:
            return [instance]
        if len(children) == 1:
            return [instance, *linearize(children[0])]
        # Fan-out → Tee. Each branch is its own op sub-list. Tee runs them sequentially on the
        # same Sample/metadata; branches don't reconverge via a sample wire (that would be fan-in,
        # which is impossible through the single ``sample`` socket and rejected above).
        branches = [linearize(child) for child in children]
        return [instance, Instance(Tee, branches=branches)]

    ops = linearize(roots[0])

    unvisited = op_id_set - visited
    if unvisited:
        raise UnsupportedTopologyError(
            f"Op nodes {sorted(unvisited)} are not reachable from the chain head {roots[0]!r} "
            "(disconnected op subgraph); ops export needs a single connected chain."
        )
    return ops


def export_ops_yaml(prompt: Dict[str, Any], node_mappings: Dict[str, Any]) -> str:
    """Compile a prompt graph and serialize its ops to a Confluid YAML document.

    The result is a mapping with a single ``ops:`` key holding the ``!class:...()`` op-list,
    so the consumer side is ``confluid.load(path)["ops"]`` → ``Flux(source=..., ops=...)``.
    """
    return dump({"ops": compile_ops(prompt, node_mappings)})


__all__ = ["UnsupportedTopologyError", "compile_ops", "export_ops_yaml"]
