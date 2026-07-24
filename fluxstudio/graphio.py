"""Bidirectional Confluid-YAML ↔ FluxStudio (ComfyUI) **whole-graph** converter.

This is the generalization of :mod:`fluxstudio.export` (which handles only a linear op
chain). It converts a *whole* config — sources, ops, object members, and the runnable
that ties them together — between a Confluid YAML document and a ComfyUI ``workflow.json``
(the UI/litegraph form you save and load on the canvas), in BOTH directions:

* :func:`yaml_to_workflow` — YAML → ``workflow.json``. Broadcast-resolves the config to a
  Fluid marker graph with :func:`confluid.resolve` (no instantiation — every node is a
  uniform ``Class`` / ``Instance`` / ``Lazy`` marker carrying ``.target`` + ``.kwargs``;
  ``!ref:`` targets are shared by identity, so a fan-out is one node with several
  out-links). Pass ``instantiate=True`` to instead walk live-but-inert objects from
  ``confluid.materialize(..., solidify=False)`` — same walk, via :func:`_target_and_kwargs`.
* :func:`workflow_to_yaml` — ``workflow.json`` (or the prompt/API form) → YAML. Builds
  Fluid markers per node (recovering the class from each node's ``TAIDAL_CALLABLE_PATH``),
  **hoists** every node to a top-level key wired by ``!ref:`` (matching the navigaitor
  output shape, e.g. ``waivefront/config/train_efficientvit_b0.yaml``), and serialises
  with the canonical :func:`confluid.dump`.

The **Fluid graph is the shared IR**: confluid owns YAML↔Fluid (``resolve``/``dump``);
this module owns Fluid↔``workflow.json``. Neither direction depends on ComfyUI being
importable — it only reads ``NODE_CLASS_MAPPINGS`` (the generated node classes).

Subgraph / group support rides the ``as_=`` argument of :func:`yaml_to_workflow`
(``"group"`` wraps the nodes in a visual ``groups[]`` box; ``"subgraph"`` packages them as a
reusable ``definitions.subgraphs[]`` blueprint with boundary sockets), and the export side
inlines any saved subgraph back into the flat hoisted YAML.
"""

from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union, cast

from confluid import Class, Fluid, Instance, LazyClass, Reference
from confluid import dump as _confluid_dump
from confluid import get_configurable_attrs
from confluid import load as _confluid_load
from confluid import resolve as _confluid_resolve
from confluid.registry import resolve_class
from loggair import get_logger
from sampleflux import Flux
from sampleflux.discovery import resolve_callable
from sampleflux.ops.configure import ConfigureOp
from sampleflux.ops.context import Apply as CtxApply
from sampleflux.ops.context import Capture as CtxCapture
from sampleflux.ops.context import MergeFields as CtxMerge
from sampleflux.ops.context import Save as CtxSave
from sampleflux.ops.context import Use as CtxUse
from sampleflux.ops.formula import FormulaOp
from sampleflux.ops.transform_chain import TransformChain

from fluxstudio.bridge import (
    _OP_LIST_PARAMS,
    _SOURCE_LIST_PARAMS,
    OBJECT_TYPE,
    OP_TYPE,
    SAMPLE_TYPE,
    SOURCE_TYPE,
    _coerce_kwargs,
    _op_list_params,
)
from fluxstudio.export import (
    _CELL_PREFIX,
    _EXPORT_SKIP_PROPERTY,
    _EXPORT_SKIP_VALUE,
    _EXPORT_TARGET_VALUE,
    _MIX_ARGS,
    _MIX_CLASS_TYPE,
    _PRIMITIVE_VALUE_NODES,
    _SAMPLE_INPUT,
    _SUBGRAPH_OP_CLASS_TYPE,
    UnsupportedTopologyError,
    _ancestor_cone,
    _collect_output_captures,
    _collect_value_chains,
    _FoldContext,
    _id_sort_key,
    _is_link,
    _marker,
    _node_kind,
    _op_class_of,
    _op_value_consumed,
    _snake,
    _subgraph_op_instance,
    collect_export_marks,
)

# Distinct node colours for a reconstructed DEAD-END TAP — a sample-chain op whose own sample
# output feeds nothing (a viewer/analysis branch that forks off and is never reused), excluding
# the pipeline's final output. Title bar + body, in ComfyUI's ``color``/``bgcolor`` convention.
_DEAD_END_COLOR = "#4a234a"
_DEAD_END_BGCOLOR = "#331833"

logger = get_logger(__name__)

# --- workflow.json envelope + layout constants -----------------------------------------
_WORKFLOW_VERSION = 0.4
_COL_DX = 340  # horizontal spacing between dependency layers (sources left → runnable right)
_ROW_DY = 200  # vertical spacing between siblings in a layer
_MARGIN_X = 40
_MARGIN_Y = 40
_DEFAULT_SIZE = [280, 130]

# Scalar dimension fields hoisted to top-level YAML vars + referenced via !ref: on export,
# mirroring navigaitor's HOISTED_TOP_LEVEL_VARS so the output matches the reference configs.
_HOISTED_VARS: Tuple[str, ...] = ("num_classes", "image_size")


# =======================================================================================
# Shared: class ↔ node-key index, node introspection, value coercion
# =======================================================================================

# Cache: {id(node_mappings): (node_mappings, index)}.  The stored reference prevents Python
# from reusing the same memory address for a *different* dict (id-reuse after GC would cause
# a stale cache hit — the `is` guard below is the fallback for any entry that slipped through).
_NODE_INDEX_CACHE: Dict[int, Tuple[Dict[str, Any], Dict[Callable[..., Any], str]]] = {}


def _node_index(node_mappings: Dict[str, Any]) -> Dict[Callable[..., Any], str]:
    """Reverse index ``{configurable target -> ComfyUI node_key}`` built from the node mappings.

    Each generated node class stamps ``TAIDAL_CALLABLE_PATH`` (an importable ``module:qualname``);
    ``resolve_callable`` turns it back into the target, giving us the target→key direction needed to
    emit a workflow node for a Fluid marker. The target may be a class OR a builder FUNCTION (a
    registered ``role="model"`` torchvision detection builder), so the index keys any callable —
    matching the bridge's callable-aware object nodes. Cached per ``node_mappings`` identity; the
    cache entry holds a strong reference to the dict so its ``id`` is never recycled while cached.
    """
    cache_key = id(node_mappings)
    entry = _NODE_INDEX_CACHE.get(cache_key)
    if entry is not None:
        cached_nm, index = entry
        if cached_nm is node_mappings:
            return index
    index = {}
    for key, node_cls in node_mappings.items():
        path = getattr(node_cls, "TAIDAL_CALLABLE_PATH", None)
        if not path:
            continue
        try:
            target = resolve_callable(path)
        except (ImportError, AttributeError, ValueError):  # pragma: no cover - defensive
            continue
        if callable(target):
            index[target] = key
    _NODE_INDEX_CACHE[cache_key] = (node_mappings, index)
    return index


def _node_key_for_class(cls: Callable[..., Any], node_mappings: Dict[str, Any]) -> Optional[str]:
    """The ComfyUI node_key registered for ``cls`` (a class OR builder function), or ``None``."""
    return _node_index(node_mappings).get(cls)


def _target_and_kwargs(obj: Any) -> Optional[Tuple[Callable[..., Any], bool, Dict[str, Any]]]:
    """``(class, is_lazy, kwargs)`` for a Fluid marker OR a live ``@configurable`` instance.

    Returns ``None`` for anything that is not a node (scalars, ``None``, plain lists/dicts).
    This is the single abstraction that lets the import walk consume EITHER the marker graph
    from :func:`confluid.resolve` (default) OR the live-but-inert graph from
    ``confluid.materialize(..., solidify=False)`` (``instantiate=True``).
    """
    if isinstance(obj, (Class, Instance)):  # LazyClass is a Class subclass
        target = obj.target
        cls = target if isinstance(target, type) else resolve_class(target)
        if cls is None:
            return None
        return cls, isinstance(obj, LazyClass), dict(obj.kwargs)
    # A live @configurable instance (instantiate=True path). Read config values from the
    # instance __dict__ ONLY (never getattr — that would execute @property getters, some of
    # which raise, e.g. an unattached LightningModule.trainer).
    if not isinstance(obj, type) and getattr(type(obj), "__confluid_configurable__", False):
        cls = type(obj)
        data = vars(obj)
        names = [n for n in get_configurable_attrs(obj) if n in data]
        return cls, False, {n: data[n] for n in names}
    return None


def _is_node_value(value: Any) -> bool:
    """True if ``value`` is a graph node (a Fluid marker or a live ``@configurable``)."""
    return _target_and_kwargs(value) is not None


def _node_list_value(value: Any) -> Optional[List[Any]]:
    """If ``value`` is a list whose items are nodes (or nested 1-lists of a node), return them.

    Handles ``Flux.ops`` / ``JointFlux.fluxes`` (list of nodes); a nested single-op
    1-list is flattened one level (tolerated legacy shape).
    """
    if not isinstance(value, (list, tuple)) or not value:
        return None
    out: List[Any] = []
    for item in value:
        if _is_node_value(item):
            out.append(item)
        elif isinstance(item, (list, tuple)) and len(item) == 1 and _is_node_value(item[0]):
            out.append(item[0])  # legacy single-op 1-list branch → the op
        else:
            return None  # not a uniform list-of-nodes
    return out


def _widget_and_socket_params(node_cls: Any) -> Tuple[List[str], Dict[str, str]]:
    """Split a node's ``INPUT_TYPES`` into ordered widget names and ``{socket_name: socket_type}``.

    Widget order is the declaration order (required then optional) — the same order the
    positional ``widgets_values`` array uses. Socket entries are those whose ComfyUI type is a
    wire type (``SAMPLEFLUX_*`` / ``HF_DATASET``); everything else (scalars, ``Literal`` combos) is
    a widget.
    """
    from fluxstudio.bridge import _is_socket_type

    spec = node_cls.INPUT_TYPES()
    widgets: List[str] = []
    sockets: Dict[str, str] = {}
    for section in ("required", "optional"):
        for name, decl in spec.get(section, {}).items():
            socket_type = decl[0] if isinstance(decl, (tuple, list)) else decl
            if isinstance(socket_type, list):  # a Literal combo (options list as the "type") → widget
                widgets.append(name)
            # ``_is_socket_type`` is BASE-type aware so a category-qualified object socket
            # (``SAMPLEFLUX_OBJECT:classification_model``) is still recognised as a wired socket,
            # not mistaken for a widget. The full qualified string is preserved in ``sockets``.
            elif isinstance(socket_type, str) and _is_socket_type(socket_type):
                sockets[name] = socket_type
            else:
                widgets.append(name)
    return widgets, sockets


def _widget_default(node_cls: Any, name: str) -> Any:
    """The declared default for a widget param (from its ``INPUT_TYPES`` config), else ``""``."""
    spec = node_cls.INPUT_TYPES()
    for section in ("required", "optional"):
        decl = spec.get(section, {}).get(name)
        if isinstance(decl, (tuple, list)) and len(decl) > 1 and isinstance(decl[1], dict):
            return decl[1].get("default", "")
        if isinstance(decl, (tuple, list)) and decl and isinstance(decl[0], list):
            return decl[0][0] if decl[0] else ""  # Literal combo → first option
    return ""


def _widget_socket_type(node_cls: Any, name: str) -> Any:
    """The socket type a widget param would carry as a (converted-to-)input slot — its ``INPUT_TYPES``
    type (``STRING``/``FLOAT``/``INT``/``BOOLEAN``, or a combo options-list). ``STRING`` if unknown."""
    spec = node_cls.INPUT_TYPES()
    for section in ("required", "optional"):
        decl = spec.get(section, {}).get(name)
        if decl is not None:
            return decl[0] if isinstance(decl, (tuple, list)) else decl
    return "STRING"


def _complete_widget_input_slots(workflow: Dict[str, Any], node_mappings: Dict[str, Any]) -> None:
    """Pad every node's ``inputs`` to the FULL ComfyUI-v0.22 slot list (sockets + EVERY widget param as
    a widget-input, in class order) and remap link target slots accordingly — in place, TOP-LEVEL and
    inside every subgraph definition.

    ComfyUI serialises every widget as an input slot (``"widget": {"name": …}``), but its in-memory
    ``graphToPrompt()`` (and the ops-doc importer) represent a CONVERTED widget as a single input slot and
    leave the unconverted widgets OFF the input list — so a value wired into a widget (a config's
    ``disabled`` → ``ImpairmentsChain.tx_disabled``) sits at e.g. slot 1, but ComfyUI rebuilds the node
    from the class with the widget at slot 3, and the link lands on the WRONG slot → it does not render
    ("the ImpairmentsXxConfig nodes are not connected"). Restoring the full slot list (matching a real
    ComfyUI save) fixes BOTH the graph export and the ops-yaml import. Sockets / dynamic ``op_N`` slots
    keep their leading order; widget slots follow in ``INPUT_TYPES`` order (= the ``widgets_values`` order).
    Handles both link encodings (top-level 6-arrays; subgraph-definition inner-link dicts).
    """

    def _target_id(link: Any) -> Any:
        return link[3] if isinstance(link, list) else link.get("target_id")

    def _target_slot(link: Any) -> Any:
        return link[4] if isinstance(link, list) else link.get("target_slot")

    def _set_target_slot(link: Any, value: int) -> None:
        if isinstance(link, list):
            link[4] = value
        else:
            link["target_slot"] = value

    def _complete(nodes: Any, links: Any) -> None:
        links_by_target: Dict[Any, List[Any]] = {}
        for link in links or []:
            links_by_target.setdefault(_target_id(link), []).append(link)
        for node in nodes or []:
            node_cls = node_mappings.get(node.get("type"))
            if node_cls is None or not hasattr(node_cls, "INPUT_TYPES"):  # native / nested-subgraph / non-node
                continue
            widget_names, _ = _widget_and_socket_params(node_cls)
            if not widget_names:  # e.g. Taidal_MixSamples (sockets only) — nothing to complete
                continue
            current = node.get("inputs") or []
            by_name = {inp["name"]: inp for inp in current}
            sockets = [inp for inp in current if inp["name"] not in widget_names]  # sample / op_N / … stay first
            widget_slots: List[Dict[str, Any]] = []
            for name in widget_names:
                slot = by_name.get(name)
                if slot is not None:  # already present (wired) → reuse, preserving its link
                    slot.setdefault("widget", {"name": name})
                    widget_slots.append(slot)
                else:
                    widget_slots.append(
                        {
                            "name": name,
                            "type": _widget_socket_type(node_cls, name),
                            "link": None,
                            "widget": {"name": name},
                        }
                    )
            new_inputs = sockets + widget_slots
            old_name_at = {idx: inp["name"] for idx, inp in enumerate(current)}
            new_index = {inp["name"]: idx for idx, inp in enumerate(new_inputs)}
            for link in links_by_target.get(node.get("id"), []):
                _set_target_slot(link, new_index[old_name_at[_target_slot(link)]])  # remap by name
            node["inputs"] = new_inputs

    _complete(workflow.get("nodes"), workflow.get("links"))
    for definition in (workflow.get("definitions") or {}).get("subgraphs") or []:
        _complete(definition.get("nodes"), definition.get("links"))


def _kwarg_to_widget(value: Any) -> Any:
    """Inverse of :func:`fluxstudio.bridge._coerce_kwargs`: a ctor value → a widget literal.

    ``None`` → ``""`` (a blank STRING the loader coerces back to ``None``); ``Path`` → str;
    a function/class → its importable dotted path (so e.g. ``collate_fn`` survives as a string
    the export side re-emits as ``!ref:``); a list/tuple → comma-joined STRING; scalars pass through.
    """
    if value is None:
        return ""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, type) or (callable(value) and hasattr(value, "__qualname__")):
        return f"{getattr(value, '__module__', '')}.{value.__qualname__}"
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    if isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _output_slot(node_cls: type, is_lazy: bool) -> int:
    """Which output slot of ``node_cls`` a consumer wires from, given the marker's lazy-ness.

    source → 0 (the ``source`` output); op → 1 (the ``op`` value, slot 0 is the applied sample);
    object → 1 (``lazy``) when the marker is a ``Lazy``, else 0 (``instance``); runnable → 0.
    """
    kind = getattr(node_cls, "TAIDAL_NODE_KIND", None)
    if kind == "op":
        return 1
    if kind == "object":
        return 1 if is_lazy else 0
    return 0  # source / runnable / generic


# =======================================================================================
# Import: YAML → workflow.json
# =======================================================================================


class _WorkflowBuilder:
    """Accumulates nodes + links while walking a Fluid/object graph, then lays them out."""

    def __init__(self, node_mappings: Dict[str, Any]) -> None:
        self.node_mappings = node_mappings
        self.nodes: List[Dict[str, Any]] = []
        self.by_id: Dict[int, Dict[str, Any]] = {}
        self.links: List[List[Any]] = []
        self.seen: Dict[int, Tuple[int, int]] = {}  # id(obj) -> (node_id, output_slot)
        # Unresolved !ref: targets reaching a node input — the subgraph INPUT boundary
        # (free inputs): (node_id, input_index, ref_name, socket_type).
        self.free_inputs: List[Tuple[int, int, str, str]] = []
        self._next_node = 0
        self._next_link = 0
        self._providers: Dict[int, set] = {}  # node_id -> set of provider node_ids (for layout)

    def emit(self, obj: Any) -> Optional[Tuple[int, int]]:
        """Emit ``obj`` (a node) and its dependencies; return ``(node_id, output_slot)``.

        Returns ``None`` (and logs) when ``obj`` is not a node or its class has no FluxStudio
        node (e.g. a logger / callback not registered as a configurable) — such members are
        dropped from the canvas rather than aborting the whole import.
        """
        oid = id(obj)
        if oid in self.seen:
            return self.seen[oid]
        spec = _target_and_kwargs(obj)
        if spec is None:
            return None
        cls, is_lazy, kwargs = spec
        node_key = _node_key_for_class(cls, self.node_mappings)
        if node_key is None:
            logger.warning(
                "Skipping {}.{} — no FluxStudio node (not tagged op/source/engine/member, e.g. a "
                "logger/callback). It is dropped from the canvas; wire it back by hand or register it.",
                cls.__module__,
                cls.__qualname__,
            )
            return None
        node_cls = self.node_mappings[node_key]
        node_id = self._alloc_node()
        self.seen[oid] = (node_id, _output_slot(node_cls, is_lazy))
        self._providers[node_id] = set()

        widget_names, socket_names = _widget_and_socket_params(node_cls)
        # Register the node dict EARLY (so children can wire back into it before this call returns).
        node: Dict[str, Any] = {
            "id": node_id,
            "type": node_key,
            "pos": [0, 0],  # set in _layout()
            "size": list(_DEFAULT_SIZE),
            "flags": {},
            "order": node_id,
            "mode": 0,
            "inputs": [],
            "outputs": self._build_outputs(node_cls),
            "properties": {"Node name for S&R": node_key},
            "widgets_values": [],
        }
        self.nodes.append(node)
        self.by_id[node_id] = node
        input_index: Dict[str, int] = {}

        def _add_input_socket(name: str, socket_type: str) -> int:
            input_index[name] = len(node["inputs"])
            node["inputs"].append({"name": name, "type": socket_type, "link": None})
            return input_index[name]

        def _wire_into(name: str, socket_type: str, child: Any) -> bool:
            res = self.emit(child)
            if res is None:
                return False
            src_id, src_slot = res
            if name not in input_index:
                _add_input_socket(name, socket_type)
            link_id = self._alloc_link()
            src_node = self.by_id[src_id]
            link_type = src_node["outputs"][src_slot]["type"] if src_slot < len(src_node["outputs"]) else "*"
            self.links.append([link_id, src_id, src_slot, node_id, input_index[name], link_type])
            src_node["outputs"][src_slot]["links"].append(link_id)
            node["inputs"][input_index[name]]["link"] = link_id
            self._providers[node_id].add(src_id)
            return True

        # Pre-declare the node's single sockets (member / source / sample) in INPUT_TYPES order, so
        # an unwired optional member still shows its socket.
        for sname, stype in socket_names.items():
            _add_input_socket(sname, stype)

        widget_values: Dict[str, Any] = {}
        for pname, value in kwargs.items():
            if isinstance(value, Reference):
                # An UNRESOLVED !ref: (a name not defined in this document) → a subgraph INPUT
                # boundary socket named after the reference target. In flat-graph mode it simply
                # stays an unconnected optional input.
                stype = socket_names.get(pname, SOURCE_TYPE)
                if pname not in input_index:
                    _add_input_socket(pname, stype)
                self.free_inputs.append((node_id, input_index[pname], value.target, stype))
                continue
            if pname in _OP_LIST_PARAMS or pname in _SOURCE_LIST_PARAMS:
                # higher-order op-list / source-list → dynamic op_N / source_N sockets
                base, stype = ("op", OP_TYPE) if pname in _OP_LIST_PARAMS else ("source", SOURCE_TYPE)
                idx = 0
                for child in _node_list_value(value) or []:
                    if _wire_into(f"{base}_{idx}", stype, child):
                        idx += 1
                continue
            if f"{pname}_0" in socket_names:
                # Object-LIST member (``logger`` / ``callbacks``): the node declares a ``<pname>_0``
                # seed socket (the bridge's ``_admits_object_list`` family — ``web/object_list.js``
                # grows the rest), so wire each child into ``<pname>_0`` / ``<pname>_1`` / … like
                # op_N/source_N — NOT a single by-name socket. Handles a list OR a single object value.
                stype = socket_names[f"{pname}_0"]
                children = _node_list_value(value)
                if children is None and _is_node_value(value):
                    children = [value]
                idx = 0
                for child in children or []:
                    if _wire_into(f"{pname}_{idx}", stype, child):
                        idx += 1
                continue
            child_nodes = _node_list_value(value)
            if child_nodes is not None:
                # A list-valued member on a node WITHOUT a ``<pname>_0`` seed (a single object socket,
                # e.g. a runnable's own ``logger`` slot): wire only the first, warn on the rest.
                stype = socket_names.get(pname, OBJECT_TYPE)
                if child_nodes:
                    _wire_into(pname, stype, child_nodes[0])
                    if len(child_nodes) > 1:
                        logger.warning(
                            "Member %r on %s is a list of %d; the canvas wires only the first "
                            "(single-socket list-gap — see graphio v1 limitations).",
                            pname,
                            node_key,
                            len(child_nodes),
                        )
                continue
            if _is_node_value(value):
                _wire_into(pname, socket_names.get(pname, SOURCE_TYPE), value)
                continue
            if pname in widget_names:
                widget_values[pname] = _kwarg_to_widget(value)

        node["widgets_values"] = [
            widget_values.get(name, _kwarg_to_widget(_widget_default(node_cls, name))) for name in widget_names
        ]
        return self.seen[oid]

    @staticmethod
    def _build_outputs(node_cls: type) -> List[Dict[str, Any]]:
        types = getattr(node_cls, "RETURN_TYPES", ())
        names = getattr(node_cls, "RETURN_NAMES", None) or types
        outputs = []
        for i, t in enumerate(types):
            nm = names[i] if i < len(names) else str(t)
            outputs.append({"name": nm, "type": t, "links": [], "slot_index": i})
        return outputs

    def _alloc_node(self) -> int:
        self._next_node += 1
        return self._next_node

    def _alloc_link(self) -> int:
        self._next_link += 1
        return self._next_link

    def spawn(self, node_key: str) -> int:
        """Create a bare hand-authored node (its wire sockets + outputs, no widget wiring).

        Used by the ops-doc importer to materialise a ``Taidal_MixSamples`` fan-in node — a
        node not present as a marker in the document but reconstructed from the unstash pattern.
        Mirrors the node-dict shape :meth:`emit` builds (so layout / finalize treat it alike).
        """
        node_cls = self.node_mappings[node_key]
        node_id = self._alloc_node()
        self._providers[node_id] = set()
        _widgets, socket_names = _widget_and_socket_params(node_cls)
        node: Dict[str, Any] = {
            "id": node_id,
            "type": node_key,
            "pos": [0, 0],
            "size": list(_DEFAULT_SIZE),
            "flags": {},
            "order": node_id,
            "mode": 0,
            "inputs": [{"name": n, "type": t, "link": None} for n, t in socket_names.items()],
            "outputs": self._build_outputs(node_cls),
            "properties": {"Node name for S&R": node_key},
            "widgets_values": [],
        }
        self.nodes.append(node)
        self.by_id[node_id] = node
        return node_id

    def connect(
        self, src_id: int, src_slot: int, dst_id: int, input_name: str, socket_type: str, *, as_widget: bool = False
    ) -> None:
        """Wire ``src_id``'s output ``src_slot`` into ``dst_id``'s ``input_name`` socket.

        Ensures the destination socket exists (creating it if the consumer declares the param
        only as a widget — ``as_widget`` marks it as a converted-from-widget input, the shape
        ComfyUI uses for a value wired into a widget-backed param). Updates the link table, both
        endpoints' link references, and the layout provider set. Mirrors :meth:`emit`'s internal
        ``_wire_into`` so hand-authored wiring (sample chain, Mix, constant refs) lays out alike.
        """
        dst = self.by_id[dst_id]
        idx = next((i for i, inp in enumerate(dst["inputs"]) if inp["name"] == input_name), None)
        if idx is None:
            idx = len(dst["inputs"])
            inp: Dict[str, Any] = {"name": input_name, "type": socket_type, "link": None}
            if as_widget:
                inp["widget"] = {"name": input_name}
            dst["inputs"].append(inp)
        elif as_widget:
            dst["inputs"][idx]["type"] = socket_type
            dst["inputs"][idx].setdefault("widget", {"name": input_name})
        link_id = self._alloc_link()
        src = self.by_id[src_id]
        self.links.append([link_id, src_id, src_slot, dst_id, idx, socket_type])
        if 0 <= src_slot < len(src["outputs"]):
            src["outputs"][src_slot]["links"].append(link_id)
        dst["inputs"][idx]["link"] = link_id
        self._providers[dst_id].add(src_id)

    def _layout(self) -> None:
        """Longest-path layering: depth = 1 + max(provider depth); sources left, runnable right."""
        depth: Dict[int, int] = {}

        def _depth(nid: int) -> int:
            if nid in depth:
                return depth[nid]
            depth[nid] = 0  # guard against cycles (canvas graphs are acyclic)
            providers = self._providers.get(nid, set())
            depth[nid] = 1 + max((_depth(p) for p in providers), default=-1)
            return depth[nid]

        for node in self.nodes:
            _depth(node["id"])
        by_depth: Dict[int, List[Dict[str, Any]]] = {}
        for node in self.nodes:
            by_depth.setdefault(depth[node["id"]], []).append(node)
        for d, column in by_depth.items():
            for row, node in enumerate(column):
                node["pos"] = [_MARGIN_X + d * _COL_DX, _MARGIN_Y + row * _ROW_DY]

    def finalize(self) -> Dict[str, Any]:
        self._layout()
        for node in self.nodes:
            node.pop("_input_index", None)
        return {
            "last_node_id": self._next_node,
            "last_link_id": self._next_link,
            "nodes": self.nodes,
            "links": self.links,
            "groups": [],
            "config": {},
            "extra": {},
            "version": _WORKFLOW_VERSION,
        }


def yaml_to_workflow(
    doc: Union[str, Path, Dict[str, Any]],
    node_mappings: Dict[str, Any],
    *,
    instantiate: bool = False,
    as_: str = "graph",
    name: str = "Imported",
) -> Dict[str, Any]:
    """Convert a Confluid YAML config into a ComfyUI ``workflow.json`` dict.

    Args:
        doc: A YAML path / string / already-parsed dict (anything ``confluid.load`` accepts).
        node_mappings: FluxStudio's ``NODE_CLASS_MAPPINGS``.
        instantiate: ``False`` (default) walks the un-instantiated marker graph from
            ``confluid.resolve``; ``True`` walks live-but-inert objects from
            ``confluid.load(..., solidify=False)`` (no model backbones / dataset loads).
        as_: ``"graph"`` (default) lays the nodes on the canvas; ``"group"`` additionally wraps
            them in one visual ``groups[]`` box titled ``name``; ``"subgraph"`` packages them as a
            reusable ``definitions.subgraphs[]`` blueprint + an instance node (boundary inputs from
            unresolved ``!ref:`` names, output = the pipeline root).
        name: Title for the group / subgraph (e.g. the source filename).

    Returns:
        A ComfyUI UI-format workflow dict (``nodes`` / ``links`` / ``groups`` / ``version`` …),
        ready to write to a ``.json`` and open on the canvas.
    """
    # An OPS document (the ops-export shape ``{…constants…, ops: [op markers]}``) is a SEQUENTIAL
    # op-list, not a graph of node-valued top-level keys — its ``ops:`` list would be silently
    # dropped by the whole-graph walk below (a list is not a node value). Detect it on a raw
    # marker parse (``flow=False`` keeps ``!ref`` links intact) and dispatch to the dedicated
    # importer. Restricted to path/text inputs (where the ops docs actually arrive — the CLI passes
    # a path, the route raw YAML) so the probe never mutates a caller-owned dict; any parse failure
    # or non-ops shape falls through to the whole-graph path unchanged.
    if isinstance(doc, (str, Path)):
        try:
            raw = _confluid_load(doc, flow=False)
        except Exception:  # pragma: no cover - defensive: the whole-graph path re-parses and reports
            raw = None
        if _is_ops_document(raw) or _is_flow_document(raw):
            return ops_yaml_to_workflow(raw, node_mappings, as_=as_, name=name)

    # instantiate=True → live-but-inert objects (load parses the file/string THEN materialises with
    # the expensive solidify suppressed); else → the un-instantiated broadcast-resolved marker graph.
    graph = _confluid_load(doc, solidify=False) if instantiate else _confluid_resolve(doc)
    builder = _WorkflowBuilder(node_mappings)
    entries = list(graph.values()) if isinstance(graph, dict) else [graph]
    for value in entries:
        if _is_node_value(value):
            builder.emit(value)
    workflow = builder.finalize()
    if as_ == "group":
        return _wrap_in_group(workflow, name)
    if as_ == "subgraph":
        return _wrap_in_subgraph(workflow, builder.free_inputs, name)
    return workflow


# =======================================================================================
# Import: ops.yaml (the sequential ops-export document) → workflow.json
# =======================================================================================


def _is_ops_document(doc: Any) -> bool:
    """True if ``doc`` is a parsed ops-export document: a dict with a top-level ``ops`` LIST.

    The ops-export emits ``{…hoisted constants…, ops: [!class:Op() …]}``; the whole-graph export
    hoists every node to its OWN top-level key and never carries a top-level ``ops`` list, so this
    is an unambiguous discriminator. A non-empty ``ops`` must hold Fluid markers (op instances).
    """
    if not isinstance(doc, dict) or not isinstance(doc.get("ops"), list):
        return False
    ops = doc["ops"]
    return all(isinstance(item, Fluid) for item in ops) if ops else True


def _is_flow_document(doc: Any) -> bool:
    """True if ``doc`` is a parsed flow-export document: a dict with a ``flow`` step MAPPING."""
    return isinstance(doc, dict) and isinstance(doc.get("flow"), dict)


def _lower_flow_document(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Lower a ``{…constants…, flow: {...}, outputs: …}`` document into the ops-doc form.

    Uses ``sampleflux.flow`` with UNBUILT step markers (``parse_flow(build=False)`` — a
    hoisted constant's dotted ``!ref:`` must not resolve during a structural import), so
    the replay sees live context ops around marker real-ops, exactly like a fresh export.
    """
    from sampleflux.flow import parse_flow as _parse_flow
    from sampleflux.flow import to_ops as _to_ops

    steps, outputs = _parse_flow(doc["flow"], str(doc.get("outputs", "") or ""), build=False)
    lowered: Dict[str, Any] = {k: v for k, v in doc.items() if k not in ("flow", "outputs")}
    lowered["ops"] = _to_ops(steps, outputs)
    return lowered


def ops_yaml_to_workflow(
    doc: Union[str, Path, Dict[str, Any]],
    node_mappings: Dict[str, Any],
    *,
    as_: str = "graph",
    name: str = "Imported",
) -> Dict[str, Any]:
    """Convert a FluxStudio **ops-export** YAML (``{…constants…, ops: [...]}``) into a workflow.json.

    The inverse of :func:`fluxstudio.export.export_ops_yamls`: the sequential op-list is replayed
    into a connected canvas graph. The AUTO-INSERTED context ops (``Save``/``Use``/``Mix`` over
    namespaced ``__taidal_cell_*`` cells, which the exporter adds to linearize a branchy DAG) are
    CONSUMED — not placed as nodes — and the fan-out / Mix fan-in they encode is reconstructed as
    real wiring:

    * each op's ``sample`` input is wired from the currently-live node; a ``Use`` that restores
      an earlier node's cell makes a later op FORK from it (fan-out);
    * a **Mix Samples** fan-in is REBUILT (the export erased the node) from a ``Mix`` marker's
      three argument cells, and ALSO when the three tracked lineages diverge at a real op
      (input≠target, or the input==target case with a one-shot metadata branch — a branch whose
      tail was itself SAVED is a plain fan-out restart, NOT a merge, so no spurious Mix);
    * a hoisted ``constant=True`` value node (e.g. ``ImpairmentsTxConfig``) is placed and its dotted
      ``!ref:<name>.<output>`` consumers are wired from its output;
    * a USER-named ``Save``/``Use`` (no ``__taidal_cell_`` namespace) stays an ordinary node — only
      the auto-inserted, namespaced cells are absorbed (the same rule for any real stash op);
    * a **dead-end tap** — a sample-chain op whose own sample output feeds nothing, excluding the
      pipeline's final output — is given a distinct colour. (With Mix-rebuild, re-joining branches feed
      a Mix and so are not dead-ends; colouring is the residual for a genuinely unconsumed branch.)

    The source is intentionally left unattached (the ops-doc contract — the user reattaches their own
    source). Lossy, documented point carried over from the export: a metadata-only Mix the exporter
    discarded (input==target==metadata, no divergence) is not recovered.

    Args:
        doc: An ops-doc as a YAML path / string, or an already-parsed raw-marker dict.
        node_mappings: FluxStudio's ``NODE_CLASS_MAPPINGS``.
        as_: ``"graph"`` (default) lays the nodes out; ``"group"`` additionally wraps them in one
            visual box. ``"subgraph"`` has no natural ops-doc boundary and degrades to ``"graph"``.
        name: Title for the group box.

    Returns:
        A ComfyUI UI-format workflow dict, JSON-safe (non-finite widget values nulled).
    """
    raw = doc if (_is_ops_document(doc) or _is_flow_document(doc)) else _confluid_load(doc, flow=False)
    if _is_flow_document(raw):
        # The lowered list holds LIVE context ops around marker real-ops (to_ops builds the
        # Save/Use/Mix), so it deliberately bypasses the all-markers ops-doc check below.
        raw = _lower_flow_document(cast(Dict[str, Any], raw))
    elif not isinstance(raw, dict) or not _is_ops_document(raw):  # pragma: no cover - caller detected the shape
        raise UnsupportedTopologyError(
            "ops_yaml_to_workflow expects an ops-export document (a top-level 'ops:' list) "
            "or a flow-export document (a 'flow:' step mapping)."
        )
    builder = _WorkflowBuilder(node_mappings)

    # 1. Hoisted constants → value nodes (document key -> node id), for dotted-!ref wiring below.
    const_nodes: Dict[str, int] = {}
    for key, marker in raw.items():
        if key == "ops" or not _is_node_value(marker):
            continue
        res = builder.emit(marker)
        if res is not None:
            const_nodes[key] = res[0]

    # 2. Replay the op-list, tracking THREE live lineages — the node currently feeding the sample's
    #    `input`, `target`, AND `metadata` — plus the cell snapshots. Tracking metadata as its own
    #    lineage (reset only by a real op) is what lets the importer rebuild a Mix even when
    #    input==target (both restored to one node K while the metadata came from a side branch).
    live: Dict[str, Optional[int]] = {"input": None, "target": None, "metadata": None}  # node id per lineage
    cells: Dict[str, Optional[int]] = {}  # context-ops export: __taidal_cell_* -> the node it snapshots
    stashed_nodes: set = set()  # every node ever auto-stashed (a saved FORK, reused later — see below)
    op_node_ids: List[int] = []  # real op nodes, in schedule order (last = deepest sink = final output)

    for marker in raw["ops"]:
        spec = _target_and_kwargs(marker)
        if spec is None:
            continue
        cls, _is_lazy, kwargs = spec

        # The context-ops export (2026-07): whole-sample Save/Use/Mix over namespaced
        # ``__taidal_cell_*`` cells. Save records the live node into a cell (no node
        # placed); Use restarts the stream from a cell; a Mix marker rebuilds the erased
        # ``Mix Samples`` canvas node wired from its three argument cells. A USER-named
        # cell (no namespace prefix) falls through and stays a real node.
        cell_name = kwargs.get("name")
        if cls is CtxSave and isinstance(cell_name, str) and cell_name.startswith(_CELL_PREFIX):
            cells[cell_name] = live["input"]
            if live["input"] is not None:
                stashed_nodes.add(live["input"])
            continue
        if cls is CtxUse and isinstance(cell_name, str) and cell_name.startswith(_CELL_PREFIX):
            restored = cells.get(cell_name)
            live["input"] = live["target"] = live["metadata"] = restored
            continue
        if cls is CtxMerge and any(
            str(kwargs.get(slot, "")).startswith(_CELL_PREFIX)
            for slot in ("input_from", "target_from", "metadata_from")
        ):
            mix_id = builder.spawn(_MIX_CLASS_TYPE)
            for arg, slot in zip(_MIX_ARGS, ("input_from", "target_from", "metadata_from")):
                slot_cell = kwargs.get(slot)
                src = cells.get(str(slot_cell)) if slot_cell else live["input"]
                if src is not None:
                    builder.connect(src, 0, mix_id, arg, SAMPLE_TYPE)
            live["input"] = live["target"] = live["metadata"] = mix_id
            continue

        # A real op node (incl. a user Stash*Op with a non-namespaced key).
        res = builder.emit(marker)
        if res is None:
            continue
        nid = res[0]
        i, t, m = live["input"], live["target"], live["metadata"]
        if i == t == m or (i == t and m in stashed_nodes):
            # Plain chain, OR a fan-out branch RESTART: input/target converged to a fork node and the
            # lingering metadata branch is itself a SAVED fork (a tap reused by a later Mix, not a
            # one-shot contributor) — so this is just a new branch off the fork, NOT a merge. (The
            # bus-carried metadata is an export artifact here; the original graph had no Mix.)
            if i is not None:
                builder.connect(i, 0, nid, _SAMPLE_INPUT, SAMPLE_TYPE)
        else:
            # A genuine Mix Samples fan-in (the export erased the node): input/target restored to the
            # stashed node(s), metadata from a ONE-SHOT branch the bus carried (e.g. CropSignalLengthOp's
            # input/target merged with the region-detection branch's `predicted_regions`).
            mix_id = builder.spawn(_MIX_CLASS_TYPE)
            for arg, lineage in zip(_MIX_ARGS, (i, t, m)):
                if lineage is not None:
                    builder.connect(lineage, 0, mix_id, arg, SAMPLE_TYPE)
            builder.connect(mix_id, 0, nid, _SAMPLE_INPUT, SAMPLE_TYPE)
        live["input"] = live["target"] = live["metadata"] = nid
        op_node_ids.append(nid)

    # 3. Wire dotted-!ref free inputs (constant.output -> consumer param) to the constant nodes.
    _wire_constant_refs(builder, const_nodes)

    # 4. Colour the dead-end taps (sample-chain leaves that are not the final output).
    _color_dead_end_taps(builder, op_node_ids)

    # 5. JSON-safety: a non-finite widget value (e.g. ThresholdOp.high_level=NaN) would emit a bare
    #    ``NaN`` token that the browser's JSON.parse rejects — null it.
    _sanitize_widget_values(builder)

    workflow = builder.finalize()
    # 6. Restore each node's FULL widget-input-slot list so a value wired into a widget (a hoisted
    #    constant → ImpairmentsChain.tx_disabled) lands at the slot index ComfyUI rebuilds from the class.
    _complete_widget_input_slots(workflow, node_mappings)
    if as_ == "group":
        return _wrap_in_group(workflow, name)
    # "subgraph" degrades to "graph" — an ops-doc has no natural !ref-boundary inputs to promote.
    return workflow


def _wire_constant_refs(builder: "_WorkflowBuilder", const_nodes: Dict[str, int]) -> None:
    """Connect each unresolved dotted ``!ref:<name>.<output>`` free input to its constant node.

    :meth:`_WorkflowBuilder.emit` records a ``Reference`` kwarg as a free input carrying the ref
    target string (``"impairments_tx_config.disabled"``). Here the ``<name>`` prefix is matched to a
    hoisted constant node and the ``<output>`` suffix to that node's output slot (by ``RETURN_NAMES``
    /output name), and the two are wired (the consumer param shown as a converted-from-widget input).
    A ref whose prefix names no constant is left free (a genuine subgraph boundary).
    """
    for node_id, _idx, ref_target, fallback_type in builder.free_inputs:
        head, _dot, tail = ref_target.partition(".")
        src_id = const_nodes.get(head)
        if src_id is None:
            continue
        src = builder.by_id[src_id]
        slot = next((i for i, out in enumerate(src["outputs"]) if out.get("name") == tail), 0)
        out_type = src["outputs"][slot]["type"] if slot < len(src["outputs"]) else fallback_type
        input_name = builder.by_id[node_id]["inputs"][_idx]["name"]
        builder.connect(src_id, slot, node_id, input_name, out_type, as_widget=True)


def _color_dead_end_taps(builder: "_WorkflowBuilder", op_node_ids: List[int]) -> None:
    """Tint every dead-end tap — a sample-chain op whose sample output feeds nothing — distinctly.

    "Sample-chain op" = has an incoming ``sample`` wire (so a config-only node like a ``MaxOp`` feeding
    a ``ConfigureOp`` via its ``op`` output, with no sample input, is NOT a tap). "Feeds nothing" = no
    outgoing ``SAMPLEFLUX_SAMPLE`` wire from any output (so an op feeding a Mix or a later op is live).
    The single last-scheduled op (the deepest sink = the pipeline's final output) is exempt.
    """
    if not op_node_ids:
        return
    final = op_node_ids[-1]
    has_sample_in: set = set()
    produces_sample: set = set()
    for _lid, src_id, _src_slot, dst_id, dst_idx, ltype in builder.links:
        if ltype != SAMPLE_TYPE:
            continue
        produces_sample.add(src_id)
        dst = builder.by_id[dst_id]
        if dst_idx < len(dst["inputs"]) and dst["inputs"][dst_idx].get("name") == _SAMPLE_INPUT:
            has_sample_in.add(dst_id)
    for nid in op_node_ids:
        if nid != final and nid in has_sample_in and nid not in produces_sample:
            node = builder.by_id[nid]
            node["color"] = _DEAD_END_COLOR
            node["bgcolor"] = _DEAD_END_BGCOLOR


def _sanitize_widget_values(builder: "_WorkflowBuilder") -> None:
    """Replace non-finite floats (``NaN`` / ``±inf``) in every node's ``widgets_values`` with ``None``.

    aiohttp / ``json.dumps`` serialise these as bare ``NaN`` / ``Infinity`` tokens that the browser's
    ``JSON.parse`` rejects, corrupting the whole import response (the same failure class the histogram
    payload guards against). ``None`` round-trips as ``null`` and the widget falls back to its default.
    """
    for node in builder.nodes:
        values = node.get("widgets_values")
        if isinstance(values, list):
            node["widgets_values"] = [None if isinstance(v, float) and not math.isfinite(v) else v for v in values]


# --- groups & subgraphs ----------------------------------------------------------------

_GROUP_PAD = 30
_GROUP_TITLE_H = 30


def _wrap_in_group(workflow: Dict[str, Any], title: str) -> Dict[str, Any]:
    """Draw one visual ``groups[]`` box around every node (presentation only — dropped on export)."""
    nodes = workflow.get("nodes", [])
    if nodes:
        x0 = min(n["pos"][0] for n in nodes) - _GROUP_PAD
        y0 = min(n["pos"][1] for n in nodes) - _GROUP_PAD - _GROUP_TITLE_H
        x1 = max(n["pos"][0] + n["size"][0] for n in nodes) + _GROUP_PAD
        y1 = max(n["pos"][1] + n["size"][1] for n in nodes) + _GROUP_PAD
        workflow["groups"] = [
            {"title": title, "bounding": [x0, y0, x1 - x0, y1 - y0], "color": "#3f789e", "font_size": 24}
        ]
    return workflow


def _sg_uuid(*parts: str) -> str:
    """Deterministic UUID for a subgraph artefact (so the same YAML → the same id, for round-trips)."""
    import uuid

    return str(uuid.uuid5(uuid.NAMESPACE_DNS, "taidal.fluxstudio.subgraph:" + ":".join(parts)))


def _wrap_in_subgraph(
    workflow: Dict[str, Any], free_inputs: List[Tuple[int, int, str, str]], name: str
) -> Dict[str, Any]:
    """Package a flat workflow as a reusable ``definitions.subgraphs[]`` blueprint + an instance node.

    Boundary inference (the approved convention): each distinct unresolved ``!ref:NAME`` becomes one
    INPUT socket named ``NAME`` (wired from the subgraph's internal ``inputNode`` id ``-10`` to every
    consumer); the pipeline root(s) — nodes nothing else consumes — feed the ``outputNode`` id ``-20``,
    one OUTPUT socket each. The parent canvas carries a single instance node typed by the subgraph id.
    """
    internal_nodes = workflow["nodes"]
    internal_links = list(workflow["links"])
    next_link = workflow["last_link_id"]
    sg_id = _sg_uuid(name)
    nodes_by_id = {n["id"]: n for n in internal_nodes}

    # INPUT boundary: group free-ref consumers by ref name → one input slot each.
    by_ref: Dict[str, List[Tuple[int, int, str]]] = {}
    for node_id, idx, ref_name, stype in free_inputs:
        by_ref.setdefault(ref_name, []).append((node_id, idx, stype))
    input_defs: List[Dict[str, Any]] = []
    for slot_i, (ref_name, consumers) in enumerate(by_ref.items()):
        stype = consumers[0][2]
        link_ids: List[int] = []
        for node_id, idx, _st in consumers:
            next_link += 1
            internal_links.append([next_link, -10, slot_i, node_id, idx, stype])
            link_ids.append(next_link)
            if node_id in nodes_by_id and idx < len(nodes_by_id[node_id]["inputs"]):
                nodes_by_id[node_id]["inputs"][idx]["link"] = next_link
        input_defs.append(
            {
                "id": _sg_uuid(name, "in", ref_name),
                "name": ref_name,
                "type": stype,
                "linkIds": link_ids,
                "localized_name": ref_name,
                "label": ref_name,
                "pos": [0, 50 + slot_i * 40],
            }
        )

    # OUTPUT boundary: a pipeline root is a node whose OWN output nothing internal consumes (it is the
    # SOURCE of no internal link) → one output socket each.
    producers = {link[1] for link in internal_links if link[3] != -20}
    roots = [n for n in internal_nodes if n["id"] not in producers] or internal_nodes[-1:]
    output_defs: List[Dict[str, Any]] = []
    for slot_i, root in enumerate(roots):
        out_type = root["outputs"][0]["type"] if root["outputs"] else "*"
        next_link += 1
        internal_links.append([next_link, root["id"], 0, -20, slot_i, out_type])
        output_defs.append(
            {
                "id": _sg_uuid(name, "out", str(root["id"])),
                "name": root["outputs"][0]["name"] if root["outputs"] else "output",
                "type": out_type,
                "linkIds": [next_link],
                "localized_name": "output",
                "label": "output",
                "pos": [0, 50 + slot_i * 40],
            }
        )

    subgraph_def = {
        "id": sg_id,
        "version": 1,
        "name": name,
        "inputs": input_defs,
        "outputs": output_defs,
        "inputNode": {"id": -10, "bounding": [0, 0, 120, 60]},
        "outputNode": {"id": -20, "bounding": [800, 0, 120, 60]},
        "widgets": [],
        "nodes": internal_nodes,
        "links": internal_links,
        "groups": [],
        "extra": {},
        "state": {
            "lastNodeId": workflow["last_node_id"],
            "lastLinkId": next_link,
            "lastGroupId": 0,
            "lastRerouteId": 0,
        },
    }
    instance = {
        "id": 1,
        "type": sg_id,
        "pos": [_MARGIN_X, _MARGIN_Y],
        "size": list(_DEFAULT_SIZE),
        "flags": {},
        "order": 0,
        "mode": 0,
        "inputs": [{"name": d["name"], "type": d["type"], "link": None} for d in input_defs],
        "outputs": [
            {"name": d["name"], "type": d["type"], "links": [], "slot_index": i} for i, d in enumerate(output_defs)
        ],
        "properties": {"Node name for S&R": name},
        "widgets_values": [],
    }
    return {
        "last_node_id": 1,
        "last_link_id": 0,
        "nodes": [instance],
        "links": [],
        "groups": [],
        "config": {},
        "extra": {},
        "version": _WORKFLOW_VERSION,
        "definitions": {"subgraphs": [subgraph_def]},
    }


# =======================================================================================
# Export: extract the op DAG as a standalone workflow.json (the raw graph, no compilation)
# =======================================================================================


def _is_op_node(class_type: str, node_mappings: Dict[str, Any]) -> bool:
    """An op-DAG member: an op-kind node OR the ``Taidal_MixSamples`` fan-in (the ops-export universe)."""
    return class_type == _MIX_CLASS_TYPE or _node_kind(class_type, node_mappings) == "op"


def _detach_dropped_link_refs(nodes: List[Dict[str, Any]], live_link_ids: "set") -> None:
    """Null out any ``inputs[].link`` / ``outputs[].links`` ref pointing at a dropped link, in place."""
    for node in nodes:
        for inp in node.get("inputs") or []:
            if inp.get("link") not in live_link_ids:
                inp["link"] = None
        for out in node.get("outputs") or []:
            if out.get("links") is not None:
                out["links"] = [lid for lid in out["links"] if lid in live_link_ids]


def _ops_workflow_skeleton(nodes: List[Dict[str, Any]], links: List[List[Any]]) -> Dict[str, Any]:
    """Wrap kept ``nodes`` + ``links`` in a minimal, loadable ``workflow.json`` envelope."""
    node_ids = [n.get("id") for n in nodes]
    last_node = max((i for i in node_ids if isinstance(i, int)), default=0)
    last_link = max((lk[0] for lk in links if isinstance(lk[0], int)), default=0)
    return {
        "last_node_id": last_node,
        "last_link_id": last_link,
        "nodes": nodes,
        "links": links,
        "groups": [],
        "config": {},
        "extra": {},
        "version": _WORKFLOW_VERSION,
    }


def _data_flow_adjacency(links: Any) -> Tuple[Dict[Any, set], Dict[Any, set]]:
    """Forward + reverse node-id adjacency over a workflow's links (handles 6-array AND dict links)."""
    fwd: Dict[Any, set] = {}
    rev: Dict[Any, set] = {}
    for raw in links or []:
        link = _link_six(raw)
        if link is not None:
            fwd.setdefault(link[1], set()).add(link[3])
            rev.setdefault(link[3], set()).add(link[1])
    return fwd, rev


def _reachable(starts: Any, adjacency: Dict[Any, set]) -> set:
    """All nodes reachable from ``starts`` (inclusive) following ``adjacency`` (fwd or rev)."""
    seen = set(starts)
    stack = list(starts)
    while stack:
        for nxt in adjacency.get(stack.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _marks_to_top_level(workflow: Dict[str, Any], marked_ids: set, def_by_id: Dict[Any, Any]) -> set:
    """Resolve marked ids (which may name a SUBGRAPH-INNER node) to TOP-LEVEL node ids.

    A mark on a node INSIDE a subgraph definition resolves to the top-level INSTANCE(s) of that
    definition — so the cone/skip is computed against the instance the user actually sees.
    """
    top_ids = {n.get("id") for n in workflow.get("nodes") or []}
    resolved: set = set()
    for mid in marked_ids:
        direct = next((t for t in top_ids if str(t) == str(mid)), None)
        if direct is not None:
            resolved.add(direct)
            continue
        for did, definition in def_by_id.items():  # an inner mark → the instance(s) of its definition
            if any(str(inner.get("id")) == str(mid) for inner in definition.get("nodes") or []):
                resolved |= {n.get("id") for n in workflow.get("nodes") or [] if str(n.get("type")) == str(did)}
    return resolved


def _keep_universe(all_ids: Any, fwd: Dict[Any, set], rev: Dict[Any, set], targets: set, skips: set) -> set:
    """The node-id set surviving the export marks: a TARGET restricts to the union of its data
    ancestor cones; a SKIP removes the marked nodes + their data-downstream closure; neither = all."""
    universe = _reachable(targets, rev) if targets else set(all_ids)
    if skips:
        universe -= _reachable(skips, fwd)
    return universe


def extract_ops_workflow(
    workflow: Dict[str, Any], node_mappings: Dict[str, Any], *, flatten: bool = False
) -> Dict[str, Any]:
    """Extract the op-DAG of a UI ``workflow.json`` as a standalone, CONNECTED ``workflow.json``.

    The GRAPH counterpart of :func:`fluxstudio.export.export_ops_yamls` — emitted as the RAW DAG
    *as drawn* (positions / widgets / wiring preserved, NO Stash/Unstash/Mix/constant compilation),
    but obeying the SAME `export-skip` / `export-target` marks (`properties["taidal_export"]`) the
    YAML ops-export uses (read via the shared :func:`fluxstudio.export.collect_export_marks`): a
    **target** mark restricts the export to the union of the targets' data ancestor cones (so the
    dead-end viewer/analysis side-branches the user did NOT target are dropped); a **skip** mark
    removes the marked nodes + their downstream. With NO marks the whole op graph is kept. The
    config/value nodes (`@configurable(constant=True)` producers like `ImpairmentsTxConfig`) are
    KEPT — they are ancestors of the ops they configure, so they ride along in the cone.

    Two modes, because a real pipeline routes the sample THROUGH ComfyUI subgraph instances (which
    are not op nodes), so a naive "keep op nodes + links between them" would sever the pipeline:

    * ``flatten=False`` (**subgraphs intact**): keep op + Mix + value/config nodes **AND the subgraph
      INSTANCE nodes** (plus the transitive ``definitions.subgraphs`` they reference) that fall in the
      kept universe; the pipeline stays connected because the instances (its real waypoints) survive
      as black boxes. Marks naming a subgraph-inner node resolve to the containing instance. Node ids
      + links are preserved VERBATIM (a subset, no remap).
    * ``flatten=True`` (**raw ops**): inline every subgraph (``_inline_subgraphs``) so the inner op
      nodes surface, keep op + Mix + value nodes, and **reconnect them through the dropped nodes** (a
      transitive contraction through chains of dropped pass-throughs — Reroute / Extract / boundaries
      — emitting a direct edge). Marks survive inlining as node properties, matched there directly.

    Args:
        workflow: the UI ``workflow.json`` dict (``app.graphToPrompt().workflow``) — carries the marks.
        node_mappings: FluxStudio's ``NODE_CLASS_MAPPINGS`` (read for each node's ``TAIDAL_NODE_KIND``).
        flatten: ``False`` keeps subgraph instances as black boxes; ``True`` inlines + contracts to raw ops.

    Returns:
        A connected ``workflow.json`` dict of the (marked) op DAG.
    """
    skip_marks, target_marks = collect_export_marks(workflow)
    if flatten:
        result = _extract_ops_flattened(workflow, node_mappings, set(skip_marks), set(target_marks))
    else:
        result = _extract_ops_keep_subgraphs(workflow, node_mappings, set(skip_marks), set(target_marks))
    # NORMALISE widget-input slots: ComfyUI's in-memory graphToPrompt() represents a converted widget as a
    # single input slot and omits unconverted widgets, so a value wired into a widget (config → tx_disabled)
    # would land at the wrong slot index when ComfyUI rebuilds the node from the class. Pad to the full slot
    # list so the re-imported graph renders the connection (the "ImpairmentsXxConfig not connected" fix).
    _complete_widget_input_slots(result, node_mappings)
    return result


def _extract_ops_keep_subgraphs(
    workflow: Dict[str, Any], node_mappings: Dict[str, Any], skip_marks: set, target_marks: set
) -> Dict[str, Any]:
    """``flatten=False``: keep op + Mix + value + subgraph-instance nodes in the marked universe."""
    defs = list((workflow.get("definitions") or {}).get("subgraphs") or [])
    def_by_id = {d.get("id"): d for d in defs}
    nodes = workflow.get("nodes") or []
    fwd, rev = _data_flow_adjacency(workflow.get("links"))
    targets = _marks_to_top_level(workflow, target_marks, def_by_id)
    skips = _marks_to_top_level(workflow, skip_marks, def_by_id)
    universe = _keep_universe([n.get("id") for n in nodes], fwd, rev, targets, skips)

    def _keep(node: Dict[str, Any]) -> bool:
        class_type = str(node.get("type", ""))
        # op / Mix, a subgraph instance (a real pipeline waypoint), or a value/config producer.
        return (
            _is_op_node(class_type, node_mappings)
            or class_type in def_by_id
            or _node_kind(class_type, node_mappings) == "value"
        )

    kept_nodes = [copy.deepcopy(n) for n in nodes if n.get("id") in universe and _keep(n)]
    kept_ids = {n.get("id") for n in kept_nodes}

    kept_links: List[List[Any]] = []
    kept_link_ids: set = set()
    for raw in workflow.get("links") or []:
        link = _link_six(raw)
        if link is not None and link[1] in kept_ids and link[3] in kept_ids:
            kept_links.append(link)
            kept_link_ids.add(link[0])
    _detach_dropped_link_refs(kept_nodes, kept_link_ids)

    # A kept subgraph instance is unloadable without its definition — keep the transitive closure of
    # referenced defs (a definition's inner nodes can themselves be nested subgraph instances).
    needed = {str(n.get("type", "")) for n in kept_nodes if str(n.get("type", "")) in def_by_id}
    frontier = list(needed)
    while frontier:
        for inner in def_by_id.get(frontier.pop(), {}).get("nodes", []) or []:
            inner_type = str(inner.get("type", ""))
            if inner_type in def_by_id and inner_type not in needed:
                needed.add(inner_type)
                frontier.append(inner_type)

    out = _ops_workflow_skeleton(kept_nodes, kept_links)
    kept_defs = [copy.deepcopy(d) for d in defs if d.get("id") in needed]
    if kept_defs:
        out["definitions"] = {"subgraphs": kept_defs}
    return out


def _extract_ops_flattened(
    workflow: Dict[str, Any], node_mappings: Dict[str, Any], skip_marks: set, target_marks: set
) -> Dict[str, Any]:
    """``flatten=True``: inline subgraphs, keep op + Mix + value in the marked universe, contract."""
    flat = _inline_subgraphs(workflow)
    nodes_by_id = {n.get("id"): n for n in flat.get("nodes") or []}
    # Marks survive inlining as node PROPERTIES (inner ids are renumbered, so match by property here
    # rather than by id — this is why the mark is read from each inlined node, not from collect()).
    targets = {
        i
        for i, n in nodes_by_id.items()
        if (n.get("properties") or {}).get(_EXPORT_SKIP_PROPERTY) == _EXPORT_TARGET_VALUE
    }
    skips = {
        i
        for i, n in nodes_by_id.items()
        if (n.get("properties") or {}).get(_EXPORT_SKIP_PROPERTY) == _EXPORT_SKIP_VALUE
    }
    fwd, rev = _data_flow_adjacency(flat.get("links"))
    universe = _keep_universe(nodes_by_id.keys(), fwd, rev, targets, skips)

    def _keep_op_or_value(node: Dict[str, Any]) -> bool:
        class_type = str(node.get("type", ""))
        return _is_op_node(class_type, node_mappings) or _node_kind(class_type, node_mappings) == "value"

    kept_ids = {nid for nid in universe if _keep_op_or_value(nodes_by_id[nid])}

    # Per-link out-adjacency (with slots) over ALL inlined nodes, for the contraction.
    adjacency: Dict[Any, List[Any]] = {}
    for raw in flat.get("links") or []:
        link = _link_six(raw)
        if link is not None:
            adjacency.setdefault(link[1], []).append((link[3], link[5], link[2], link[4]))

    kept_nodes = [copy.deepcopy(nodes_by_id[nid]) for nid in nodes_by_id if nid in kept_ids]
    by_id = {n.get("id"): n for n in kept_nodes}
    for node in kept_nodes:  # rebuild every link ref from the contracted edge set below
        for inp in node.get("inputs") or []:
            inp["link"] = None
        for out in node.get("outputs") or []:
            if "links" in out:
                out["links"] = []

    # Contraction: for each kept node, walk its data wires through chains of DROPPED nodes to the
    # next kept node and emit a direct edge (keeping the source's output slot + the final input slot).
    edges: set = set()
    for origin in kept_ids:
        seen: set = set()
        stack = list(adjacency.get(origin, []))
        while stack:
            target, link_type, origin_slot, target_slot = stack.pop()
            if (target, target_slot) in seen:
                continue
            seen.add((target, target_slot))
            if target in kept_ids:
                edges.add((origin, origin_slot, target, target_slot, link_type))
            else:  # pass through the dropped node, preserving the source's output slot + type
                for nxt_target, _t, _os, nxt_slot in adjacency.get(target, []):
                    stack.append((nxt_target, link_type, origin_slot, nxt_slot))

    links: List[List[Any]] = []
    for next_id, (origin, origin_slot, target, target_slot, link_type) in enumerate(sorted(edges), start=1):
        links.append([next_id, origin, origin_slot, target, target_slot, link_type])
        onode, tnode = by_id.get(origin), by_id.get(target)
        if onode is not None and isinstance(origin_slot, int) and 0 <= origin_slot < len(onode.get("outputs") or []):
            onode["outputs"][origin_slot].setdefault("links", []).append(next_id)
        if tnode is not None and isinstance(target_slot, int) and 0 <= target_slot < len(tnode.get("inputs") or []):
            tnode["inputs"][target_slot]["link"] = next_id
    return _ops_workflow_skeleton(kept_nodes, links)


# =======================================================================================
# Export: workflow.json → YAML (hoisted-!ref: shape)
# =======================================================================================


def _link_six(link: Any) -> Optional[List[Any]]:
    """Normalise a litegraph link to ``[id, origin_id, origin_slot, target_id, target_slot, type]``.

    Top-level links are usually 6-arrays, but ComfyUI saves a SUBGRAPH definition's inner links
    as dicts (``{"id": …, "origin_id": …}``) — indexing those positionally was the
    ``KeyError: 1`` that aborted every whole-graph export containing a subgraph instance.
    """
    if isinstance(link, dict):
        return [
            link.get("id"),
            link.get("origin_id"),
            link.get("origin_slot", 0),
            link.get("target_id"),
            link.get("target_slot", 0),
            link.get("type"),
        ]
    if isinstance(link, (list, tuple)) and len(link) >= 6:
        return list(link[:6])
    return None


def _inline_subgraphs_once(graph: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """One level of subgraph-instance flattening; returns ``(graph, changed)``.

    Replaces every instance node with remapped copies of its definition's inner nodes and
    RE-STITCHES the wires crossing the instance boundary: an outer link into instance input
    ``k`` reattaches to the inner node(s) the definition's ``inputNode`` (id ``-10``) slot ``k``
    feeds (a subgraph input may fan out to several inner targets), and an outer link from
    instance output ``k`` re-originates at the inner node feeding ``outputNode`` (id ``-20``)
    slot ``k``. The whole links array is then REBUILT with fresh sequential ids and every
    node's ``inputs[].link`` / ``outputs[].links`` references rewritten — inner link ids are
    only unique within their definition, so copying them verbatim collides across two
    instances of the same subgraph (and with top-level ids).
    """
    subgraphs = {sg["id"]: sg for sg in graph.get("definitions", {}).get("subgraphs", [])}
    instance_ids = {n["id"] for n in graph.get("nodes", []) if n.get("type") in subgraphs}
    if not instance_ids:
        return graph, False

    next_node_id = max((n["id"] for n in graph.get("nodes", []) if isinstance(n.get("id"), int)), default=0)
    out_nodes: List[Dict[str, Any]] = [copy.deepcopy(n) for n in graph.get("nodes", []) if n["id"] not in instance_ids]

    # Edges as [origin, origin_slot, target, target_slot, type]; ids re-assigned at the end.
    edges: List[List[Any]] = []
    for link in graph.get("links", []):
        six = _link_six(link)
        if six is not None:
            edges.append(six[1:])

    # Boundary maps per instance: input slot k → inner targets; output slot k → inner origin.
    in_map: Dict[Tuple[Any, Any], List[Tuple[Any, Any]]] = {}
    out_map: Dict[Tuple[Any, Any], Tuple[Any, Any]] = {}
    for node in graph.get("nodes", []):
        sg = subgraphs.get(node.get("type"))
        if sg is None:
            continue
        idmap: Dict[Any, int] = {}
        for inode in sg.get("nodes", []):
            next_node_id += 1
            idmap[inode["id"]] = next_node_id
            inode_copy = copy.deepcopy(inode)
            inode_copy["id"] = next_node_id
            out_nodes.append(inode_copy)
        for link in sg.get("links", []):
            six = _link_six(link)
            if six is None:
                continue
            _lid, origin, oslot, target, tslot, ltype = six
            if origin == -10:  # subgraph input slot `oslot` feeds inner input (target, tslot)
                in_map.setdefault((node["id"], oslot), []).append((idmap.get(target, target), tslot))
            elif target == -20:  # inner output (origin, oslot) feeds subgraph output slot `tslot`
                out_map[(node["id"], tslot)] = (idmap.get(origin, origin), oslot)
            else:
                edges.append([idmap.get(origin, origin), oslot, idmap.get(target, target), tslot, ltype])

    # Re-stitch edges that touch an instance node onto the inner nodes.
    resolved: List[List[Any]] = []
    for origin, oslot, target, tslot, ltype in edges:
        if origin in instance_ids:
            inner_origin = out_map.get((origin, oslot))
            if inner_origin is None:
                continue  # instance output unwired inside the definition — dangling, drop
            origin, oslot = inner_origin
        if target in instance_ids:
            for inner_target, inner_tslot in in_map.get((target, tslot), []):
                resolved.append([origin, oslot, inner_target, inner_tslot, ltype])
            continue  # unwired-inside instance input → outer link drops
        resolved.append([origin, oslot, target, tslot, ltype])

    # Rebuild the links array with fresh ids + rewrite every node's link references (the link id
    # is the join key _normalize_ui_workflow matches inputs against, so refs must follow).
    by_id = {n["id"]: n for n in out_nodes}
    for node in out_nodes:
        for inp in node.get("inputs", []) or []:
            if isinstance(inp, dict):
                inp["link"] = None
        for outp in node.get("outputs", []) or []:
            if isinstance(outp, dict):
                outp["links"] = []
    out_links: List[List[Any]] = []
    for origin, oslot, target, tslot, ltype in resolved:
        src, dst = by_id.get(origin), by_id.get(target)
        if src is None or dst is None:
            continue  # endpoint vanished (defensive)
        link_id = len(out_links) + 1
        out_links.append([link_id, origin, oslot, target, tslot, ltype])
        inputs = dst.get("inputs", []) or []
        if isinstance(tslot, int) and 0 <= tslot < len(inputs) and isinstance(inputs[tslot], dict):
            inputs[tslot]["link"] = link_id
        outputs = src.get("outputs", []) or []
        if isinstance(oslot, int) and 0 <= oslot < len(outputs) and isinstance(outputs[oslot], dict):
            outputs[oslot].setdefault("links", []).append(link_id)
    return {**graph, "nodes": out_nodes, "links": out_links}, True


def _inline_subgraphs(graph: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten ``definitions.subgraphs`` instances into the top-level node set for export.

    Repeats the single-level pass so an instance nested inside another definition also
    flattens (depth-capped — a self-referential definition cannot terminate). So a workflow
    saved with subgraphs exports like its flat equivalent.
    (Standalone-reusable ``include:`` files are a documented follow-up.)
    """
    if not graph.get("definitions", {}).get("subgraphs", []):
        return graph
    for _ in range(16):
        graph, changed = _inline_subgraphs_once(graph)
        if not changed:
            break
    else:
        logger.warning("_inline_subgraphs: subgraph nesting deeper than 16 levels (cyclic definition?) — stopping.")
    return {**graph, "definitions": {"subgraphs": []}}


def _normalize_workflow(graph: Dict[str, Any], node_mappings: Dict[str, Any]) -> Dict[str, "_PNode"]:
    """Normalise a UI ``workflow.json`` OR a prompt/API graph into ``{node_id: _PNode}``.

    A ``_PNode`` carries the node's class_type plus its inputs split into literal **widgets**
    (``name -> value``) and **links** (``name -> (src_id, src_slot)``). Subgraph instances are
    inlined first so a saved subgraph exports like its flat equivalent.
    """
    if "nodes" in graph and isinstance(graph["nodes"], list):
        return _normalize_ui_workflow(_inline_subgraphs(graph), node_mappings)
    return _normalize_prompt(graph, node_mappings)


class _PNode:
    __slots__ = ("class_type", "widgets", "links", "export_skip", "export_target")

    def __init__(self, class_type: str) -> None:
        self.class_type = class_type
        self.widgets: Dict[str, Any] = {}
        self.links: Dict[str, Tuple[str, int]] = {}  # input_name -> (src_node_id, src_slot)
        self.export_skip = False  # properties["taidal_export"] == "skip" (UI workflows only)
        self.export_target = False  # properties["taidal_export"] == "target" (UI workflows only)


def _normalize_ui_workflow(graph: Dict[str, Any], node_mappings: Dict[str, Any]) -> Dict[str, "_PNode"]:
    # link_id -> (src_id, src_slot); links may be 6-tuples or {id, origin_id, ...} dicts.
    link_src: Dict[int, Tuple[str, int]] = {}
    for link in graph.get("links", []):
        if isinstance(link, (list, tuple)) and len(link) >= 6:
            link_src[link[0]] = (str(link[1]), link[2])
        elif isinstance(link, dict):
            link_src[link["id"]] = (str(link["origin_id"]), link["origin_slot"])
    out: Dict[str, _PNode] = {}
    for node in graph["nodes"]:
        node_id = str(node["id"])
        class_type = node["type"]
        pnode = _PNode(class_type)
        mark = (node.get("properties") or {}).get(_EXPORT_SKIP_PROPERTY)
        pnode.export_skip = mark == _EXPORT_SKIP_VALUE
        pnode.export_target = mark == _EXPORT_TARGET_VALUE
        node_cls = node_mappings.get(class_type)
        if node_cls is not None:
            widget_names, _ = _widget_and_socket_params(node_cls)
            for name, value in zip(widget_names, node.get("widgets_values", []) or []):
                pnode.widgets[name] = value
        elif class_type in _PRIMITIVE_VALUE_NODES:
            # Native primitives aren't in the Taidal mappings, but their single widget IS
            # their output — capture it so wires from them can inline as literals.
            values = node.get("widgets_values", []) or []
            if values:
                pnode.widgets["value"] = values[0]
        for inp in node.get("inputs", []) or []:
            link_id = inp.get("link")
            if link_id is not None and link_id in link_src:
                pnode.links[inp["name"]] = link_src[link_id]
                if inp.get("widget"):
                    # A LINKED converted widget: ComfyUI keeps a stale placeholder in
                    # widgets_values (e.g. ThresholdOp.low_level showing '>') — the wire
                    # is authoritative, so the leftover widget value must not export.
                    pnode.widgets.pop(inp["widget"].get("name") or inp["name"], None)
        out[node_id] = pnode
    return out


def _normalize_prompt(graph: Dict[str, Any], node_mappings: Dict[str, Any]) -> Dict[str, "_PNode"]:
    out: Dict[str, _PNode] = {}
    for node_id, node in graph.items():
        if not isinstance(node, dict) or "class_type" not in node:
            continue
        pnode = _PNode(node["class_type"])
        for name, value in (node.get("inputs") or {}).items():
            if _is_link(value):
                pnode.links[name] = (str(value[0]), value[1])
            else:
                pnode.widgets[name] = value
        out[str(node_id)] = pnode
    return out


_MISSING = object()  # sentinel: "no primitive literal here" (``None`` is a legal widget value)


def _primitive_literal(src_id: str, pnodes: Dict[str, "_PNode"]) -> Any:
    """A native primitive pass-through node's literal ``value`` widget, else ``_MISSING``.

    The comfy_extras ``Primitive*`` nodes echo their single ``value`` widget to the
    output, so a wire from one is a CONSTANT the export can inline instead of dropping.
    """
    pnode = pnodes.get(src_id)
    if pnode is not None and pnode.class_type in _PRIMITIVE_VALUE_NODES:
        return pnode.widgets.get("value", _MISSING)
    return _MISSING


# Param names whose string value is an importable dotted path → emitted as ``!ref:<path>`` (a
# function reference, e.g. a DataLoader's ``collate_fn``), mirroring navigaitor's DOTTED_REF_FIELDS.
_REF_FIELDS = {"collate_fn"}


def _build_fluid(pnode: "_PNode", node_mappings: Dict[str, Any]) -> Tuple[type, bool, Dict[str, Any]]:
    """Recover ``(class, is_lazy, literal_kwargs)`` for one workflow node.

    Only widgets that DIFFER from the node's declared default are kept — the import fills every
    widget position (ComfyUI needs them positional), so a default-valued widget was not explicitly
    set and must not bloat the YAML (this also discards params the bridge can't faithfully render,
    e.g. tuple defaults, which revert to the class default on reload). Matches the brevity of the
    hand-written / navigaitor reference configs.
    """
    node_cls = node_mappings.get(pnode.class_type)
    path = getattr(node_cls, "TAIDAL_CALLABLE_PATH", None) if node_cls is not None else None
    if not path:
        raise UnsupportedTopologyError(
            f"Node {pnode.class_type!r} has no resolvable TAIDAL_CALLABLE_PATH "
            "(a native ComfyUI node or an adapter); cannot serialize it as a Confluid !class:."
        )
    cls = cast(type, resolve_callable(path))
    explicit = {k: v for k, v in pnode.widgets.items() if v != _widget_default(node_cls, k)}
    coerced = _coerce_kwargs(cls, explicit)
    for ref_field in _REF_FIELDS:
        value = coerced.get(ref_field)
        if isinstance(value, str) and value:
            coerced[ref_field] = Reference(value)
    # Lazy when the class is confluid-lazy-marked (optimizer / loader / lightning / logger) OR a
    # consumer wired its object node's `lazy` output (slot 1) — a deferred runtime-injection slot.
    is_lazy = bool(getattr(cls, "__confluid_lazy__", False))
    return cls, is_lazy, coerced


def _collapse_reroutes(prompt: Dict[str, Any]) -> Dict[str, Any]:
    """A prompt-view with ``Reroute`` (rgthree / native) pass-throughs collapsed out.

    ComfyUI's frontend treats a Reroute as a VIRTUAL node — the live API prompt the ops-export
    sees has them collapsed (a consumer wires straight to the Reroute's source). But the
    whole-graph export works on the saved WORKFLOW, where Reroutes are real nodes that sit in the
    sample chain — and they break the Subgraph-Op cone compile's one-stream check (an op's
    ``sample`` resolves to a Reroute, a separate "feed", so a fan-in looks like two streams). So
    for the Subgraph-Op compile we reproduce the frontend's collapse: drop every Reroute and
    re-point each surviving link THROUGH it to the real producer. Node ids are otherwise unchanged,
    so the resulting cone ids still match the pnodes.
    """

    def _is_reroute(nid: str) -> bool:
        node = prompt.get(nid)
        return node is not None and "reroute" in str(node.get("class_type", "")).lower()

    def _resolve(src_id: str, src_slot: int, seen: Tuple[str, ...] = ()) -> List[Any]:
        if not _is_reroute(src_id) or src_id in seen:  # real node, or a Reroute cycle — stop
            return [src_id, src_slot]
        for value in prompt[src_id].get("inputs", {}).values():  # a Reroute passes its single input
            if _is_link(value):
                return _resolve(str(value[0]), int(value[1]), seen + (src_id,))
        return [src_id, src_slot]  # unconnected Reroute — leave it (becomes a boundary feed)

    out: Dict[str, Any] = {}
    for nid, node in prompt.items():
        if _is_reroute(nid):
            continue
        inputs = {
            name: (_resolve(str(v[0]), int(v[1])) if _is_link(v) else v) for name, v in node.get("inputs", {}).items()
        }
        out[nid] = {**node, "inputs": inputs}
    return out


def _forward_sample_closure(roots: Set[str], prompt: Dict[str, Any]) -> Set[str]:
    """Nodes reachable FORWARD from ``roots`` along sample wires (Mix args included), EXCLUDING the
    roots themselves. Used to find a Subgraph-Op cone's dead-end viewer side-taps (RenderSignalPlot /
    preview ConvertToImage / Extract→MaskToImage): they consume a cone op's sample but never reach
    the terminator, so the cone compile drops them — they must drop from the whole-graph surface too,
    or they fold into stray ``pipeline_N`` entries. Op-VALUE feeders of the Flux (no sample wire) are
    NOT reached, so the real downstream ops are untouched."""
    consumers: Dict[str, List[str]] = {}
    for nid, node in prompt.items():
        args = _MIX_ARGS if str(node.get("class_type", "")) == _MIX_CLASS_TYPE else (_SAMPLE_INPUT,)
        for arg in args:
            value = node.get("inputs", {}).get(arg)
            if _is_link(value):
                consumers.setdefault(str(value[0]), []).append(nid)
    seen = set(roots)
    reached: Set[str] = set()
    stack = list(roots)
    while stack:
        for consumer in consumers.get(stack.pop(), []):
            if consumer not in seen:
                seen.add(consumer)
                reached.add(consumer)
                stack.append(consumer)
    return reached


def workflow_to_yaml(graph: Dict[str, Any], node_mappings: Dict[str, Any]) -> str:
    """Convert a ComfyUI workflow (UI or prompt form) into a hoisted-``!ref:`` Confluid YAML.

    Every node becomes a top-level YAML key (named by the socket/param it feeds, path-prefixed
    on collision); a wire becomes a ``!ref:<name>`` in the consumer's kwargs. ``num_classes`` /
    ``image_size`` are hoisted to top-level vars. The result is serialised with
    :func:`confluid.dump` and matches the navigaitor / reference config shape.

    Nodes with no Confluid YAML form do NOT fail the export — they are skipped and listed in
    a leading ``#`` comment block: native ComfyUI nodes (viewers / primitives — not in
    ``node_mappings``), the canvas-only Taidal adapters (Walk / Extract / Retag / … — no
    ``TAIDAL_CALLABLE_PATH``), and any Taidal node whose spec fails to build (logged at
    ``warning``; the rest of the graph still exports). The canvas WIRING is preserved:
    sample-chained ops fold into ``pipeline: !class:Flux`` entries (``source: !ref:<source>``
    traced through a Walk-style adapter + an ordered ``ops:`` list, fan-out as a nested
    a container) instead of unconnected standalone entries; a dropped wire into a real config
    member is noted in the comment block.
    """
    pnodes = _normalize_workflow(graph, node_mappings)
    if not pnodes:
        return _confluid_dump({})

    # Export-target cones come FIRST: a target's data ancestor cone (sample wires + Mix
    # args) is immune to the skip closures below — a skip mark downstream of a target
    # means "this node and everything after it"; the cone before it must survive, or the
    # target itself would be erased by the upstream-dependency pruning (its only consumer
    # being the marked node). A DIRECT skip mark inside a cone is a contradiction and
    # raises, mirroring the ops-export.
    marked = {nid for nid, pnode in pnodes.items() if pnode.export_skip}
    target_marks = {nid for nid, pnode in pnodes.items() if pnode.export_target}
    cones: set = set()
    if target_marks:
        stack = list(target_marks)
        while stack:
            cur = stack.pop()
            if cur in cones or cur not in pnodes:
                continue
            cones.add(cur)
            pnode = pnodes[cur]
            arg_names = _MIX_ARGS if pnode.class_type == _MIX_CLASS_TYPE else (_SAMPLE_INPUT,)
            stack.extend(pnode.links[arg][0] for arg in arg_names if arg in pnode.links)
        conflicts = sorted(marked & cones)
        if conflicts:
            raise UnsupportedTopologyError(
                f"Export-skip mark(s) on {conflicts} conflict with the export target(s) "
                f"{sorted(target_marks)} — the marked node(s) are inside a target's "
                "dependency cone (needed to compute it). Clear one of the two marks."
            )

    # The export-skip mark (canvas-only nodes) closes over sample wires: everything fed
    # from a marked node has no exportable feed either, so the whole branch is left out
    # instead of degenerating into junk pipeline_N fragments. The closure is FORWARD-ONLY
    # on the sample path — a skip mark means "this node and everything behind it; the
    # nodes before it still export" (marking the node after a Mix must never erase the
    # Mix). The only upstream pruning is of NON-SAMPLE feeders — a config value node /
    # op-value producer that fed excluded nodes exclusively through widget/op wires would
    # otherwise linger as a junk standalone entry. A node consumed via ANY sample-flow
    # socket anywhere is part of the data path and never pruned. Pruning is scoped to
    # ancestors of the explicitly MARKED nodes, so feeders of collaterally-excluded
    # downstream nodes survive; target cones are immune to both rules.
    export_skipped = set(marked)
    consumers_all: Dict[str, List[str]] = {}
    sample_feeders: set = set()
    for nid, pnode in pnodes.items():
        sample_args = _MIX_ARGS if pnode.class_type == _MIX_CLASS_TYPE else (_SAMPLE_INPUT,)
        for name, (src_id, _slot) in pnode.links.items():
            consumers_all.setdefault(src_id, []).append(nid)
            if name in sample_args:
                sample_feeders.add(src_id)
    dependency_candidates: Dict[str, bool] = {}
    stack = list(marked)
    while stack:
        cur = stack.pop()
        if cur in dependency_candidates or cur not in pnodes:
            continue
        dependency_candidates[cur] = True
        stack.extend(src for src, _slot in pnodes[cur].links.values())
    while True:
        grew = False
        for nid, pnode in pnodes.items():
            if nid in export_skipped or nid in cones:
                continue
            pred = pnode.links.get(_SAMPLE_INPUT)
            if pred is not None and pred[0] in export_skipped:
                export_skipped.add(nid)
                grew = True
                continue
            node_consumers = consumers_all.get(nid, [])
            if (
                nid in dependency_candidates
                and nid not in sample_feeders
                and node_consumers
                and all(consumer in export_skipped for consumer in node_consumers)
            ):
                export_skipped.add(nid)
                grew = True
        if not grew:
            break

    # Export-target marks invert the selection for the OP surface: only ops inside the
    # cone union stay; the rest are skipped-with-reason. Non-op entries (sources,
    # configs) are untouched — they export when referenced, like always.
    target_excluded: set = set()
    if target_marks:
        for nid, pnode in pnodes.items():
            kind = getattr(node_mappings.get(pnode.class_type), "TAIDAL_NODE_KIND", None)
            is_flow_node = kind == "op" or pnode.class_type == _MIX_CLASS_TYPE
            if is_flow_node and nid not in cones and nid not in export_skipped:
                target_excluded.add(nid)

    # Value chains (op-chain → Extract(field=input) → Math → widget) compile to ConfigureOp
    # wraps, mirroring the ops-export — the shared tracer consumes a prompt-shaped view of
    # the (non-skipped) pnodes. PURE chain members re-run inside the wrap's compute, so
    # they are dropped from the standalone/pipeline surface; the consumed Extract/Math
    # adapters aren't "left out" either and stay off the skipped list.
    prompt_view = {
        nid: {
            "class_type": p.class_type,
            "inputs": {**p.widgets, **{k: [s, slot] for k, (s, slot) in p.links.items()}},
        }
        for nid, p in pnodes.items()
        if nid not in export_skipped
    }
    configure_specs, vc_excluded = _collect_value_chains(prompt_view, node_mappings)
    compiled_adapters = {
        aid for chain_list in configure_specs.values() for _param, chain in chain_list for aid in chain.adapter_ids
    }
    # A value-chain member feeding a SURVIVING consumer rides along inside its ConfigureOp
    # wrap even when it sits outside a target cone (the cone follows sample wires only) —
    # listing it as "outside the cone" would contradict the doc that embeds it.
    for consumer, chain_list in configure_specs.items():
        if consumer not in target_excluded and consumer not in export_skipped:
            for _param, chain in chain_list:
                target_excluded.difference_update(chain.op_ids)
                target_excluded.difference_update(chain.adapter_ids)

    # op-@output → param captures (e.g. NoiseFloorOp.applied_snr_db → another op's param): the producer
    # is wrapped in the context op Capture (records the value into a Context cell) and the consumer in
    # Apply (reads the cell). Producer + consumer are sample-chained (the ancestor constraint), so
    # both land inside the SAME folded Flux pipeline (see _chain_op_instance) and the per-sample
    # Context carries the value between them. `captures_by_producer` is the reverse map.
    output_captures = _collect_output_captures(prompt_view, node_mappings)
    captures_by_producer: Dict[str, Dict[str, str]] = {}
    for _consumer, cap_list in output_captures.items():
        for _param, cap in cap_list:
            captures_by_producer.setdefault(cap.producer_id, {})[cap.output_name] = cap.stash_key

    # WHOLE-GRAPH "Subgraph as an Op" support: a ``Taidal_SubgraphOp`` terminator (Extract as Op)
    # has no ``TAIDAL_CALLABLE_PATH``, so on its own it would be skipped — but its ``op`` output
    # (consumed by e.g. a ``Flux``'s ``op_N``) IS the subgraph's inner cone compiled to ONE
    # ``TransformChain``. Reuse the SHARED ops-export compiler (:func:`export._subgraph_op_instance`)
    # so the whole-graph export EMBEDS that chain in the consumer's op-list instead of dropping the
    # wire and fragmenting the inner ops into stray ``pipeline_N`` entries. All terminators share ONE
    # ``fold`` so a ``constant=True`` config (``ImpairmentsTx/RxConfig``) hoists exactly once.
    sg_prompt = _collapse_reroutes(prompt_view)  # Reroutes break the one-stream check (see helper)
    sg_fold = _FoldContext(sg_prompt, node_mappings, [], configure_specs, vc_excluded, output_captures)
    subgraph_op_instances: Dict[str, Any] = {}
    sg_cone_excluded: set = set()
    for tid, pnode in pnodes.items():
        if pnode.class_type != _SUBGRAPH_OP_CLASS_TYPE or tid in export_skipped:
            continue
        if not _op_value_consumed(tid, sg_prompt, node_mappings):
            continue  # unconsumed terminator → its inner ops compile as an ordinary pipeline (graph mode)
        try:
            subgraph_op_instances[tid] = _subgraph_op_instance(tid, sg_prompt, node_mappings, sg_fold)
            cone = _ancestor_cone(tid, sg_prompt)
            sg_cone_excluded |= cone - {tid}
            # Also drop the cone's dead-end viewer side-taps (forward sample-closure) so they don't
            # fold into stray pipeline_N entries (see _forward_sample_closure).
            sg_cone_excluded |= _forward_sample_closure(cone, sg_prompt)
        except Exception as e:  # pragma: no cover - defensive: fall back to skip-with-comment
            logger.warning(
                f"workflow_to_yaml: could not compile Subgraph-Op {tid} into a TransformChain "
                f"({type(e).__name__}: {e}); it is skipped-with-comment (use the ops-export instead)."
            )
    # The hoisted constants' source nodes are emitted by the fold (top-level + dotted refs), so drop
    # them from graphio's own node surface to avoid double-emitting them.
    sg_cone_excluded |= set(sg_fold._hoisted)

    def _wrap_configured(nid: str, marker: Any, eager: bool) -> Any:
        """Wrap a consumer marker in ConfigureOp(s) for value chains and Apply(s) for @output captures."""
        for param, chain in configure_specs.get(nid, []):
            try:
                compute: List[Any] = []
                for mid in chain.op_ids:
                    mcls, _mlazy, mkwargs = _build_fluid(pnodes[mid], node_mappings)
                    compute.append(_marker(Instance, mcls, mkwargs))
                compute += [_marker(Instance, FormulaOp, {"formula": f, "var": v}) for f, v in chain.formulas]
                marker = _marker(
                    Instance if eager else Class, ConfigureOp, {"ops": compute, "target": marker, "param": param}
                )
            except Exception as e:  # pragma: no cover - defensive: chain members are plain ops
                logger.warning(
                    f"workflow_to_yaml: could not compile the value chain for {nid}.{param} "
                    f"({type(e).__name__}: {e}); the param keeps its default."
                )
        for param, cap in output_captures.get(nid, []):
            marker = _marker(
                Instance if eager else Class,
                CtxApply,
                {"op": marker, "param": param, "source": cap.stash_key},
            )
        return marker

    # Recover a Fluid spec per node + which output slot each consumer wired (for object lazy/instance).
    # Non-exportable nodes are collected (not fatal) and surfaced as YAML comments below.
    specs: Dict[str, Tuple[type, bool, Dict[str, Any]]] = {}
    skipped: List[Tuple[str, str, str]] = []  # (node_id, class_type, reason)
    for nid, pnode in pnodes.items():
        node_cls = node_mappings.get(pnode.class_type)
        if nid in export_skipped:
            logger.debug(f"workflow_to_yaml: skipping node {nid} ({pnode.class_type}) — export-skip mark.")
            skipped.append((nid, pnode.class_type, "excluded by export-skip mark (canvas-only)"))
        elif nid in target_excluded:
            logger.debug(f"workflow_to_yaml: skipping node {nid} ({pnode.class_type}) — outside the target cone.")
            skipped.append((nid, pnode.class_type, "outside the export-target cone"))
        elif nid in vc_excluded:
            logger.debug(f"workflow_to_yaml: node {nid} ({pnode.class_type}) re-runs inside a ConfigureOp compute.")
        elif nid in compiled_adapters:
            logger.debug(f"workflow_to_yaml: node {nid} ({pnode.class_type}) compiled into a ConfigureOp value chain.")
        elif nid in sg_cone_excluded:
            logger.debug(
                f"workflow_to_yaml: node {nid} ({pnode.class_type}) compiled into a Subgraph-Op TransformChain."
            )
        elif nid in subgraph_op_instances:
            logger.debug(
                f"workflow_to_yaml: Subgraph-Op {nid} compiled to a TransformChain (embedded in its consumer)."
            )
        elif getattr(node_cls, "TAIDAL_CALLABLE_PATH", None):
            try:
                specs[nid] = _build_fluid(pnode, node_mappings)
            except Exception as e:
                # A Taidal node that won't serialize must not sink the whole export —
                # actionable for the operator, so log at warning (unlike the expected skips).
                logger.warning(
                    f"workflow_to_yaml: skipping node {nid} ({pnode.class_type}) — "
                    f"could not serialize it: {type(e).__name__}: {e}"
                )
                skipped.append((nid, pnode.class_type, f"failed to serialize — {type(e).__name__}: {e}"))
        elif node_cls is None:
            logger.debug(f"workflow_to_yaml: skipping node {nid} ({pnode.class_type}) — native ComfyUI node.")
            skipped.append((nid, pnode.class_type, "native ComfyUI node"))
        else:
            logger.debug(f"workflow_to_yaml: skipping node {nid} ({pnode.class_type}) — canvas-only adapter.")
            skipped.append((nid, pnode.class_type, "canvas-only Taidal adapter (no Confluid form)"))

    # The WIRED OUTPUT SLOT is authoritative for an object node's eager/lazy-ness: a consumer that
    # pulled the `lazy` output (slot 1) → !lazy:; the `instance` output (slot 0) → !class: (eager).
    # (An unconsumed object node keeps the class's own __confluid_lazy__ default from _build_fluid.)
    for pnode in pnodes.values():
        for _name, (src_id, src_slot) in pnode.links.items():
            if src_id not in specs:
                continue
            src_node_cls = node_mappings.get(pnodes[src_id].class_type)
            if getattr(src_node_cls, "TAIDAL_NODE_KIND", None) == "object":
                cls, _is_lazy, kw = specs[src_id]
                specs[src_id] = (cls, src_slot == 1, kw)

    # Fold sample-chained ops into Flux pipeline entries so the canvas WIRING survives in the
    # YAML: each chain becomes `pipeline: !class:Flux` with `source: !ref:<source>` (traced
    # through a skipped adapter like Walk) and an ordered `ops:` list, fan-out via Save/Use cells.
    chain_specs, chain_heads, op_succs = _collect_op_chains(pnodes, specs, node_mappings, output_captures)

    names = _assign_ref_names(pnodes, specs, node_mappings)

    def _node_reference(src_id: str, src_slot: int) -> Reference:
        """The ``!ref:`` for a wire from ``src_id``'s output ``src_slot``.

        A VALUE node's outputs are its declared ``@output`` properties (slot order ==
        ``RETURN_NAMES``), so the ref is DOTTED — ``!ref:<name>.<output>`` resolves the
        property value (e.g. ``impairments_tx_config.disabled``), not the config INSTANCE
        a plain ``!ref:<name>`` would inject where a tuple/dict belongs. Every other kind
        keeps the plain whole-node ref (slot semantics live in ``_output_slot``).
        """
        src_cls = node_mappings.get(pnodes[src_id].class_type)
        if getattr(src_cls, "TAIDAL_NODE_KIND", None) == "value":
            return_names = tuple(getattr(src_cls, "RETURN_NAMES", ()) or ())
            if src_slot < len(return_names):
                return Reference(f"{names[src_id]}.{return_names[src_slot]}")
        return Reference(names[src_id])

    def _op_value_for(src_id: str, src_slot: int) -> Any:
        """The value for a SINGLE op-slot wire (``op``/``target``): a Subgraph-Op terminator
        embeds its compiled ``TransformChain`` inline; every other source is a plain/dotted
        ``!ref:`` to its node. (For sequential op-LISTs the cone ops are spliced inline instead
        — see the list branch below.)"""
        if src_id in subgraph_op_instances:
            return subgraph_op_instances[src_id]
        return _node_reference(src_id, src_slot)

    # Build a Fluid marker per node, wiring links as References and list-params as Reference lists.
    fluids: Dict[str, Any] = {}
    referenced: set = set()
    dropped_wires: List[Tuple[str, str, str, str]] = []  # (consumer_name, input, src_id, src_class_type)
    for nid, (cls, is_lazy, kwargs) in specs.items():
        pnode = pnodes[nid]
        configured = {param for param, _chain in configure_specs.get(nid, [])}
        configured |= {param for param, _cap in output_captures.get(nid, [])}  # capture wraps inject these
        wired: Dict[str, List[Tuple[str, str, int]]] = {}
        for input_name, (src_id, src_slot) in pnode.links.items():
            base = _strip_index(input_name)
            if base == _SAMPLE_INPUT:
                # The op-chaining data-flow socket — never a constructor kwarg (the chain
                # structure is captured by the folded Flux pipeline entries below).
                continue
            if base in configured:
                continue  # injected per sample by the ConfigureOp wrap
            if src_id not in specs:
                if src_id in subgraph_op_instances:
                    # A Subgraph-Op terminator feeding this op slot → embed its compiled
                    # TransformChain inline (not dropped); _op_value_for resolves it below.
                    wired.setdefault(base, []).append((input_name, src_id, src_slot))
                    continue
                literal = _primitive_literal(src_id, pnodes)
                if literal is not _MISSING:
                    kwargs[base] = literal  # a native primitive pass-through → inline its widget
                    continue
                src_type = pnodes[src_id].class_type if src_id in pnodes else "unknown"
                dropped_wires.append((names[nid], base, src_id, src_type))
                continue
            referenced.add(src_id)
            wired.setdefault(base, []).append((input_name, src_id, src_slot))
        for base, items in wired.items():
            if len(items) == 1 and items[0][0] == base:
                kwargs[base] = _op_value_for(items[0][1], items[0][2])
            else:  # dynamic op_N / source_N → a list, ordered by slot index
                ordered = sorted(items, key=lambda it: _index_of(it[0]))
                sequential = base == "ops" and isinstance(cls, type) and issubclass(cls, (Flux, TransformChain))
                value_list: List[Any] = []
                for _n, src, slot in ordered:
                    if sequential and src in subgraph_op_instances:
                        # A sequential op-list (Flux/TransformChain ``ops``) makes a nested
                        # ``TransformChain`` equivalent to its ops SPLICED inline — and inline
                        # Instance ops materialize cleanly via ``DatasetProcessor.run()``'s per-op
                        # ``flow()``, whereas a nested deferred ``TransformChain`` marker left in
                        # ``flux.ops`` does NOT (the Flux rejects it when it iterates ``ops``).
                        # Fan-out containers (Parallel) keep the single embedded chain.
                        value_list.extend(subgraph_op_instances[src].kwargs.get("ops", []))
                    else:
                        value_list.append(_op_value_for(src, slot))
                kwargs[base] = value_list
        # _marker: kwargs set post-construction — a param literally named `target` (ConfigureOp)
        # collides with the Fluid ctor's own first parameter.
        fluids[nid] = _wrap_configured(nid, _marker(LazyClass if is_lazy else Class, cls, kwargs), eager=False)

    def _chain_op_instance(nid: str, chain_key: str) -> Instance:
        """An inline ``!class:Op()`` Instance for a chained op, with non-sample wires resolved."""
        cls, _lazy, kwargs = chain_specs[nid]
        kwargs = dict(kwargs)
        configured = {param for param, _chain in configure_specs.get(nid, [])}
        configured |= {param for param, _cap in output_captures.get(nid, [])}  # capture wraps inject these
        for input_name, (src_id, src_slot) in pnodes[nid].links.items():
            base = _strip_index(input_name)
            if base == _SAMPLE_INPUT:
                continue  # the chain itself — expressed by the ops-list order
            if base in configured:
                continue  # injected per sample by the ConfigureOp wrap
            if src_id in specs:
                referenced.add(src_id)
                kwargs[base] = _node_reference(src_id, src_slot)
            else:
                literal = _primitive_literal(src_id, pnodes)
                if literal is not _MISSING:
                    kwargs[base] = literal
                    continue
                src_type = pnodes[src_id].class_type if src_id in pnodes else "unknown"
                dropped_wires.append((f"{chain_key}[{cls.__name__}]", base, src_id, src_type))
        instance = _wrap_configured(nid, _marker(Instance, cls, kwargs), eager=True)
        # If this chained op's @output is captured downstream, wrap it so its value lands in a cell.
        produced = captures_by_producer.get(nid)
        if produced:
            instance = _marker(Instance, CtxCapture, {"op": instance, "captures": dict(produced)})
        return cast(Instance, instance)

    def _fold_chain(nid: str, chain_key: str) -> List[Any]:
        """Linearize a chain into inline op Instances; fan-out becomes Save/Use context cells.

        Branches are ISOLATED (each restarts from the fork's cell, the ops-export shape) — the
        last branch reads with ``drop=True`` so the per-sample Context ends empty.
        """
        instance = _chain_op_instance(nid, chain_key)
        children = sorted(op_succs.get(nid, []), key=_id_sort_key)
        if not children:
            return [instance]
        if len(children) == 1:
            return [instance, *_fold_chain(children[0], chain_key)]
        cell = f"{_CELL_PREFIX}{nid}"
        ops: List[Any] = [instance, _marker(Instance, CtxSave, {"name": cell})]
        for i, child in enumerate(children):
            if i > 0:
                ops.append(_marker(Instance, CtxUse, {"name": cell, "drop": i == len(children) - 1}))
            ops.extend(_fold_chain(child, chain_key))
        return ops

    chain_entries: List[Tuple[str, Any]] = []
    used_keys = set(names.values())
    pipeline_count = 0
    for head, source_nid in chain_heads:
        pipeline_count += 1
        key = "pipeline" if pipeline_count == 1 else f"pipeline_{pipeline_count}"
        while key in used_keys:
            pipeline_count += 1
            key = f"pipeline_{pipeline_count}"
        used_keys.add(key)
        flux_kwargs: Dict[str, Any] = {"ops": _fold_chain(head, key)}
        if source_nid is not None:
            referenced.add(source_nid)
            flux_kwargs["source"] = Reference(names[source_nid])
        chain_entries.append((key, Class(Flux, **flux_kwargs)))

    document = _hoist_document(fluids, names, referenced, specs)
    # `constant=True` configs hoisted while compiling the Subgraph-Op TransformChain(s) — emit them
    # as top-level entries so the nested chain's dotted `!ref:<name>.<output>` consumers resolve
    # against the same document (the ops-export shape).
    for cname, cval in sg_fold.constants.items():
        document.setdefault(cname, cval)
    for key, value in chain_entries:  # pipelines last — they are the roots of the config
        document[key] = value
    return _skipped_comment_block(skipped, dropped_wires) + _confluid_dump(document)


def _collect_op_chains(
    pnodes: Dict[str, "_PNode"],
    specs: Dict[str, Tuple[type, bool, Dict[str, Any]]],
    node_mappings: Dict[str, Any],
    output_captures: Optional[Dict[str, List[Tuple[str, Any]]]] = None,
) -> Tuple[Dict[str, Tuple[type, bool, Dict[str, Any]]], List[Tuple[str, Optional[str]]], Dict[str, List[str]]]:
    """Identify sample-chained op nodes and POP them out of ``specs`` for Flux folding.

    A chain is a maximal run of op-kind nodes connected by their ``sample`` sockets. Each
    chain folds into ONE ``Flux`` pipeline entry, so its ops must not also appear as
    standalone top-level entries. Returns ``(chain_specs, chain_heads, op_succs)`` where
    ``chain_heads`` is ``[(head_id, source_id-or-None), …]`` in deterministic head order —
    the source is traced through ONE skipped boundary node (the Walk adapter pattern:
    op.sample ← Walk ← source) or taken directly when the head's feed is a source node.

    NOT folded (stay standalone entries): an op with no sample wiring at all (a configured
    op-value), and any chain containing an op consumed as a ``SAMPLEFLUX_OP`` value by an
    exported node (an ``op_N`` list input — those wires reference top-level entries).
    """

    def _kind(nid: str) -> Optional[str]:
        return getattr(node_mappings.get(pnodes[nid].class_type), "TAIDAL_NODE_KIND", None)

    op_ids = {nid for nid in specs if _kind(nid) == "op"}
    op_succs: Dict[str, List[str]] = {nid: [] for nid in op_ids}
    heads: List[str] = []
    for nid in op_ids:
        pred = pnodes[nid].links.get(_SAMPLE_INPUT)
        if pred is not None and pred[0] in op_ids:
            op_succs[pred[0]].append(nid)
        else:
            heads.append(nid)

    # An op pulled by its SAMPLEFLUX_OP value output into an exported node (op_N lists of a
    # higher-order op / engine) is referenced BY NAME — its chain cannot fold away.
    captures = output_captures or {}
    consumed_as_value: set = set()
    for cnid, pnode in pnodes.items():
        if cnid not in specs:
            continue
        captured_params = {param for param, _cap in captures.get(cnid, [])}
        for input_name, (src_id, _slot) in pnode.links.items():
            base = _strip_index(input_name)
            if base == _SAMPLE_INPUT or src_id not in op_ids:
                continue
            # A capture wire (op-@output → param) is NOT an op-value reference: the producer stays
            # folded in the pipeline (the context Capture cell carries the value to the
            # consumer's Apply). Only true SAMPLEFLUX_OP value wires force a standalone entry.
            if base in captured_params:
                continue
            consumed_as_value.add(src_id)

    def _members(head: str) -> List[str]:
        out, stack, seen = [], [head], set()
        while stack:
            nid = stack.pop()
            if nid in seen:  # pragma: no cover - defensive: single sample socket → a forest
                continue
            seen.add(nid)
            out.append(nid)
            stack.extend(op_succs[nid])
        return out

    def _trace_source(head: str) -> Optional[str]:
        pred = pnodes[head].links.get(_SAMPLE_INPUT)
        if pred is None:
            return None
        src_id = pred[0]
        if src_id in specs and _kind(src_id) == "source":
            return src_id
        if src_id in pnodes and src_id not in specs:  # one skipped boundary node (Walk)
            for _name, (up_id, _s) in pnodes[src_id].links.items():
                if up_id in specs and _kind(up_id) == "source":
                    return up_id
        return None

    chain_specs: Dict[str, Tuple[type, bool, Dict[str, Any]]] = {}
    chain_heads: List[Tuple[str, Optional[str]]] = []
    for head in sorted(heads, key=_id_sort_key):
        members = _members(head)
        has_sample_edge = bool(op_succs[head]) or pnodes[head].links.get(_SAMPLE_INPUT) is not None
        if not has_sample_edge or any(m in consumed_as_value for m in members):
            continue  # stays a standalone entry
        chain_heads.append((head, _trace_source(head)))
        for member in members:
            chain_specs[member] = specs.pop(member)
    return chain_specs, chain_heads, op_succs


def _skipped_comment_block(skipped: List[Tuple[str, str, str]], dropped_wires: List[Tuple[str, str, str, str]]) -> str:
    """Render the skipped-node / dropped-wire summary as a leading ``#`` comment block.

    Comments are invisible to ``confluid.load`` / any YAML parser, so the document below
    stays a normal, runnable config — the block only tells the user what the export left out
    and why. Empty string when nothing was skipped (the all-Taidal export is byte-identical
    to before).
    """
    if not skipped and not dropped_wires:
        return ""
    lines = ["# NOTE: the canvas contained node(s) with no Confluid YAML form; they were left out:"]
    for nid, class_type, reason in sorted(skipped, key=lambda s: _id_sort_key(s[0])):
        lines.append(f"#   - node {nid} ({class_type}): {reason}")
    if dropped_wires:
        lines.append("# Wires from skipped nodes into exported config members were dropped")
        lines.append("# (each member falls back to its default):")
        for consumer, input_name, src_id, src_type in dropped_wires:
            lines.append(f"#   - {consumer}.{input_name} <- node {src_id} ({src_type})")
    return "\n".join(lines) + "\n"


def _assign_ref_names(
    pnodes: Dict[str, "_PNode"],
    specs: Dict[str, Tuple[type, bool, Dict[str, Any]]],
    node_mappings: Dict[str, Any],
) -> Dict[str, str]:
    """Assign each node a stable top-level YAML key: the param it feeds, else its class name.

    On collision, disambiguate with a numeric suffix (``model_2``). (Subgraph path-prefixing is
    applied by :mod:`fluxstudio.subgraphio` when nodes are nested.)
    """
    # Preferred base name = the CONFIG input socket this node feeds (model / train_set / …) —
    # only on an exported consumer: a skipped node's sockets (a YamlDump `object`, a PreviewAny
    # `source`, a Math `a`) are UI plumbing, not config members, and would produce junk names.
    # The op-chaining ``sample`` socket is data-flow, not a config member — naming an op
    # ``sample`` after it would be meaningless, so those wires don't contribute a name either.
    # A VALUE node keeps its class name too: its entry is the CONFIG OBJECT, not the @output
    # value it feeds (``tx_disabled: !class:ImpairmentsTxConfig`` would read as the tuple).
    fed_as: Dict[str, str] = {}
    for nid, pnode in pnodes.items():
        if nid not in specs:
            continue
        for input_name, (src_id, _slot) in pnode.links.items():
            base = _strip_index(input_name)
            if base == _SAMPLE_INPUT:
                continue
            src_cls = node_mappings.get(pnodes[src_id].class_type) if src_id in pnodes else None
            if getattr(src_cls, "TAIDAL_NODE_KIND", None) == "value":
                continue
            consumer_op_cls = _op_class_of(node_mappings.get(pnode.class_type))
            if (
                base in _OP_LIST_PARAMS
                or base in _SOURCE_LIST_PARAMS
                or (consumer_op_cls is not None and base in _op_list_params(consumer_op_cls))
            ):
                # A node feeding a LIST param (ops / branches / transforms / fluxes / sources, OR a
                # stamped helios op-list like ``true_transforms``) must NOT be NAMED after it: confluid
                # broadcasts a top-level key back INTO any class param of the same name, so a top-level
                # ``ops:`` (one op) would clobber the Flux's explicitly-wired ``ops:`` list — the whole
                # op chain replaced by a single op (``'ConvertToImageOp' object is not iterable`` at
                # run()). Fall back to the class name so the ref stays collision-free (the shipped
                # convert_to_ultralytics config names its op list ``detection_ops``, never ``ops``).
                continue
            if src_id in specs and src_id not in fed_as:
                fed_as[src_id] = base
    names: Dict[str, str] = {}
    used: Dict[str, int] = {}
    for nid in specs:
        cls = specs[nid][0]
        # A runnable root is named by its confluid role (trainer / evaluator) — the config
        # bind key, so the exported YAML is directly runnable (`sampleflux run …`).
        role = getattr(cls, "__confluid_role__", None)
        if nid not in fed_as and role in ("trainer", "evaluator"):
            base = role
        else:
            base = fed_as.get(nid) or _snake(cls.__name__)
        n = used.get(base, 0)
        used[base] = n + 1
        names[nid] = base if n == 0 else f"{base}_{n + 1}"
    return names


def _hoist_document(
    fluids: Dict[str, Any],
    names: Dict[str, str],
    referenced: set,
    specs: Dict[str, Tuple[type, bool, Dict[str, Any]]],
) -> Dict[str, Any]:
    """Assemble the flat top-level YAML mapping: hoisted scalars, member nodes, then roots last."""
    # Hoist shared scalar dimensions (num_classes / image_size) that appear identically on ≥1 node.
    hoisted: Dict[str, Any] = {}
    for var in _HOISTED_VARS:
        values = {
            _scalar_of(kwargs[var])
            for _cls, _lazy, kwargs in specs.values()
            if var in kwargs and isinstance(kwargs.get(var), (int, float))
        }
        if len(values) == 1:
            val = values.pop()
            hoisted[var] = val
            for _cls, _lazy, kwargs in specs.values():
                if kwargs.get(var) == val:
                    kwargs[var] = Reference(var)

    roots = [nid for nid in specs if nid not in referenced]
    members = [nid for nid in specs if nid in referenced]

    document: Dict[str, Any] = {}
    document.update(hoisted)
    for nid in members:
        document[names[nid]] = fluids[nid]
    for nid in roots:
        document[names[nid]] = fluids[nid]
    return document


# --- small string helpers --------------------------------------------------------------


def _strip_index(name: str) -> str:
    """Map a dynamic list-socket name back to its underlying list PARAM name.

    ``op_0`` → ``ops`` / ``source_3`` → ``fluxes`` (the socket prefix differs from the param name);
    a generic ``<name>_<digit>`` (object-list members ``logger_0`` / ``callbacks_0``, where the socket
    prefix IS the param name) → ``<name>``; a non-indexed socket (``model`` / ``train_set``) is
    returned unchanged. So the exporter collapses ``logger_0`` / ``logger_1`` back into one ``logger``
    list member (the inverse of the importer's object-list wiring).
    """
    if name.startswith("op_") and name[3:].isdigit():
        return "ops"
    if name.startswith("source_") and name[7:].isdigit():
        return "fluxes"
    head, _, tail = name.rpartition("_")
    if head and tail.isdigit():
        return head
    return name


def _index_of(name: str) -> int:
    tail = name.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def _scalar_of(value: Any) -> Any:
    return value


__all__ = ["yaml_to_workflow", "ops_yaml_to_workflow", "workflow_to_yaml", "extract_ops_workflow"]
