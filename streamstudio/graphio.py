"""Bidirectional Confluid-YAML ↔ StreamStudio (ComfyUI) **whole-graph** converter.

This is the generalization of :mod:`streamstudio.export` (which handles only a linear op
chain). It converts a *whole* config — sources, ops, object members, and the runnable
that ties them together — between a Confluid YAML document and a ComfyUI ``workflow.json``
(the UI/litegraph form you save and load on the canvas), in BOTH directions:

* :func:`yaml_to_workflow` — YAML → ``workflow.json``. Broadcast-resolves the config to a
  Fluid marker graph with ``confluid.load(until="settled")`` (no instantiation — every node is a
  uniform ``Target`` / ``Target`` / ``Partial`` marker carrying ``.target`` + ``.kwargs``;
  ``!ref:`` targets are shared by identity, so a fan-out is one node with several
  out-links). Pass ``instantiate=True`` to instead walk live-but-inert objects from
  ``confluid.load(..., solidify=False)`` — same walk, via :func:`_target_and_kwargs`.
* :func:`workflow_to_yaml` — ``workflow.json`` (or the prompt/API form) → YAML. Builds
  Fluid markers per node (recovering the class from each node's ``TAIDAL_CALLABLE_PATH``),
  **hoists** every node to a top-level key wired by ``!ref:`` (matching the navigaitor
  output shape, e.g. ``waivefront/config/train_efficientvit_b0.yaml``), and serialises
  with the canonical :func:`confluid.dump`.

The **Fluid graph is the shared IR**: confluid owns YAML↔Fluid (``load(until="settled")``/``dump``);
this module owns Fluid↔``workflow.json``. Neither direction depends on ComfyUI being
importable — it only reads ``NODE_CLASS_MAPPINGS`` (the generated node classes).

Subgraph / group support rides the ``as_=`` argument of :func:`yaml_to_workflow`
(``"group"`` wraps the nodes in a visual ``groups[]`` box; ``"subgraph"`` packages them as a
reusable ``definitions.subgraphs[]`` blueprint with boundary sockets), and the export side
inlines any saved subgraph back into the flat hoisted YAML.
"""

from __future__ import annotations

import copy
import dataclasses
import math
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union, cast

from confluid import PartialClass, Reference, Target
from confluid import dump as _confluid_dump
from confluid import get_configurable_attrs
from confluid import load as _confluid_load
from confluid import marks, output_specs
from confluid.registry import resolve_class
from loggair import get_logger
from recordstream import Pipeline, Stream
from recordstream.discovery import resolve_callable
from recordstream.items import get_item_type
from recordstream.ops.configure import ConfigureOp
from recordstream.ops.formula import FormulaOp

from streamstudio.bridge import (
    _OP_LIST_PARAMS,
    _SOURCE_LIST_PARAMS,
    OBJECT_TYPE,
    OP_TYPE,
    PREVIEW_NODE_CLASS_TYPE,
    RECORD_TYPE,
    SELECTION_NODE_CLASS_TYPE,
    SOURCE_TYPE,
    _coerce_kwargs,
    _op_list_params,
    extract_source_for,
    item_socket_types,
)
from streamstudio.export import (
    _EXPORT_SKIP_PROPERTY,
    _EXPORT_SKIP_VALUE,
    _EXPORT_TARGET_VALUE,
    _MIX_ARGS,
    _MIX_CLASS_TYPE,
    _PRIMITIVE_VALUE_NODES,
    _RECORD_INPUT,
    _SUBGRAPH_OP_CLASS_TYPE,
    GATE_PARAM,
    JOIN_CLASS_TYPE,
    JOIN_INPUTS,
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
    check_op_chain,
    collect_export_marks,
    declared_outcomes,
    fold_branches,
)

# Distinct node colours for a reconstructed DEAD-END TAP — a record-chain op whose own record
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

#: Canvas-glue node keys the contract template places (see :func:`_contract_pipeline`).
# A DECLARATION the consuming workspace needs and only the graph author can answer, PLACED
# unfilled by the contract template. Coloured apart from every working node because it is the
# one thing on a freshly-seeded canvas that is not yet true -- it must read as an empty field,
# not as a finished step. The title says so too: colour alone is no affordance for a reader who
# cannot see it, and a field nobody knows to look for is indistinguishable from a missing one.
_DECLARE_COLOR = "#653a1c"
_DECLARE_BGCOLOR = "#47280f"
_DECLARE_TITLE = "{name} — graph OUTPUT, fill this in"

#: recordstream's dataset-level class-vocabulary declaration.
_CLASS_NAMES_OUTPUT_CLASS_TYPE = "Taidal_recordstream_value_contract_ClassNamesOutput"

_EXTRACT_CLASS_TYPE = "Taidal_ExtractRecord"
_RECORD_BUILDER_CLASS_TYPE = "Taidal_RecordBuilder"
_STREAM_CLASS_TYPE = "Taidal_recordstream_engine_Stream"
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
    from ``confluid.load(until="settled")`` (default) OR the live-but-inert graph from
    ``confluid.load(..., solidify=False)`` (``instantiate=True``).
    """
    if isinstance(obj, (Target, Target)):  # PartialClass is a Target subclass
        target = obj.target
        cls = target if isinstance(target, type) else resolve_class(target)
        if cls is None:
            return None
        return cls, isinstance(obj, PartialClass), dict(obj.kwargs)
    # A live @configurable instance (instantiate=True path). Read config values from the
    # instance __dict__ ONLY (never getattr — that would execute @property getters, some of
    # which raise, e.g. an unattached LightningModule.trainer).
    if not isinstance(obj, type) and marks(type(obj)).configurable:
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

    Handles ``Stream.ops`` / ``JointStream.streams`` (list of nodes); a nested single-op
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


class AppViewError(ValueError):
    """An ``app:`` block the importer cannot turn into a usable ComfyUI App view.

    Every case here would otherwise fail SILENTLY: the frontend's ``pruneLinearData`` drops a
    ``linearData`` entry whose node does not resolve, and its App-view renderer drops one whose
    widget does not, so a wrong name yields an empty form with no error anywhere. The message
    names what was asked for and what the canvas actually carries.
    """


def _widget_and_socket_params(node_cls: Any) -> Tuple[List[str], Dict[str, str]]:
    """Split a node's ``INPUT_TYPES`` into ordered widget names and ``{socket_name: socket_type}``.

    Widget order is the declaration order (required then optional) — the same order the
    positional ``widgets_values`` array uses. Socket entries are those whose ComfyUI type is a
    wire type (``RECORDSTREAM_*`` / ``HF_DATASET``); everything else (scalars, ``Literal`` combos) is
    a widget.
    """
    from streamstudio.bridge import _is_socket_type

    spec = node_cls.INPUT_TYPES()
    widgets: List[str] = []
    sockets: Dict[str, str] = {}
    for section in ("required", "optional"):
        for name, decl in spec.get(section, {}).items():
            socket_type = decl[0] if isinstance(decl, (tuple, list)) else decl
            if isinstance(socket_type, list):  # a Literal combo (options list as the "type") → widget
                widgets.append(name)
            # ``_is_socket_type`` is BASE-type aware so a category-qualified object socket
            # (``RECORDSTREAM_OBJECT:classification_model``) is still recognised as a wired socket,
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
            if not widget_names:  # e.g. Taidal_MixRecords (sockets only) — nothing to complete
                continue
            current = node.get("inputs") or []
            by_name = {inp["name"]: inp for inp in current}
            sockets = [inp for inp in current if inp["name"] not in widget_names]  # record / op_N / … stay first
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
    """Inverse of :func:`streamstudio.bridge._coerce_kwargs`: a ctor value → a widget literal.

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

    source → 0 (the ``source`` output); op → 1 (the ``op`` value, slot 0 is the applied record);
    object → 1 (``lazy``) when the marker is a ``Partial``, else 0 (``instance``); runnable → 0.
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

        Returns ``None`` (and logs) when ``obj`` is not a node or its class has no StreamStudio
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
                "Skipping {}.{} — no StreamStudio node (not tagged op/source/engine/member, e.g. a "
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

        # Pre-declare the node's single sockets (member / source / record) in INPUT_TYPES order, so
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

        Used by the ops-doc importer to materialise a ``Taidal_MixRecords`` fan-in node — a
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
        ``_wire_into`` so hand-authored wiring (record chain, Mix, constant refs) lays out alike.
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


#: The top-level key a source document uses to declare its ComfyUI App view. Excluded from the
#: node walk everywhere a document's other top-level keys are read as node values.
APP_KEY = "app"


def _app_declaration(raw: Any) -> Optional[Dict[str, Any]]:
    """The document's validated ``app:`` block, or ``None`` when it declares no App view.

    Shape::

        app:
          inputs:                       # the widgets the form shows, in order
            - [ClassName, widget_name]
          outputs: [ClassName]          # what the Run button drives

    Nodes are addressed by CLASS NAME because it is the one spelling that reads the same in both
    document shapes a template can have — a ``flow:`` step graph keys its ops by step name, a
    whole-graph config nests them under arbitrary keys — and because a node ID is assigned by the
    importer, so regenerating would renumber it.
    """
    if not isinstance(raw, dict) or APP_KEY not in raw:
        return None
    block = raw[APP_KEY]
    if not isinstance(block, dict):
        raise AppViewError(f"app: must be a mapping with 'inputs' and 'outputs' keys, got {type(block).__name__}")
    if "outputs" not in block:
        raise AppViewError("app: needs an 'outputs' list naming the node(s) the Run button drives")
    inputs: List[Tuple[str, str]] = []
    for entry in block.get("inputs") or []:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise AppViewError(f"app.inputs takes a [ClassName, widget_name] pair per line, got {entry!r}")
        inputs.append((str(entry[0]), str(entry[1])))
    outputs = block["outputs"]
    if not isinstance(outputs, (list, tuple)):
        raise AppViewError(f"app.outputs takes a list of class names, got {outputs!r}")
    preview = block.get("preview", False)
    if not isinstance(preview, bool):
        raise AppViewError(f"app.preview is true or false, got {preview!r}")
    return {"inputs": inputs, "outputs": [str(value) for value in outputs], "preview": preview}


def _class_name_of(node: Dict[str, Any], node_mappings: Dict[str, Any]) -> str:
    """The Python class a placed node stands for — read off the generated node class's stamp."""
    node_cls = node_mappings.get(str(node.get("type", "")))
    path = str(getattr(node_cls, "TAIDAL_CALLABLE_PATH", "") or "")
    if not path:
        return str(node.get("type", "")).rsplit("_", 1)[-1]
    # The stamp is ``module.path:ClassName`` (a colon separates the callable from its module),
    # falling back to a dotted tail for any stamp written without one.
    return path.rsplit(":", 1)[-1].rsplit(".", 1)[-1]


def _resolve_app_node(name: str, workflow: Dict[str, Any], node_mappings: Dict[str, Any]) -> Dict[str, Any]:
    """The ONE placed node of class ``name`` — refusing when there is none, or more than one."""
    placed = [node for node in workflow.get("nodes", []) if _class_name_of(node, node_mappings) == name]
    if not placed:
        carried = sorted({_class_name_of(node, node_mappings) for node in workflow.get("nodes", [])})
        raise AppViewError(f"app: no node on this canvas is a {name} — it carries {', '.join(carried) or 'nothing'}")
    if len(placed) > 1:
        ids = ", ".join(str(node["id"]) for node in placed)
        raise AppViewError(
            f"app: {name} is on {len(placed)} nodes ({ids}) — a class name cannot say which one, "
            "so name a class the canvas carries exactly once"
        )
    return cast(Dict[str, Any], placed[0])


def _widget_defaults(node_cls: Any) -> List[Any]:
    """A node class's ``widgets_values`` in declaration order — its INPUT_TYPES defaults."""
    spec = node_cls.INPUT_TYPES() if hasattr(node_cls, "INPUT_TYPES") else {}
    values: List[Any] = []
    for name, entry in (spec.get("required") or {}).items():
        options = entry[1] if isinstance(entry, (list, tuple)) and len(entry) > 1 and isinstance(entry[1], dict) else {}
        if "default" in options:
            values.append(options["default"])
    return values


def _place_preview_nodes(workflow: Dict[str, Any], tail_name: str, node_mappings: Dict[str, Any]) -> List[str]:
    """Place ``Selection`` and ``Preview on Selection`` around the chain; return the App's output ids.

    The preview node hangs on the record output of the op ``tail_name`` names (a lazy wire —
    the editor never executes the ops, the workspace does); the selection node sits left of the
    whole chain. Both take the workspace's address as a widget the launcher fills in.
    """
    preview_cls = node_mappings.get(PREVIEW_NODE_CLASS_TYPE)
    selection_cls = node_mappings.get(SELECTION_NODE_CLASS_TYPE)
    if preview_cls is None or selection_cls is None:
        raise AppViewError(
            f"app.preview needs the editor's preview nodes ({PREVIEW_NODE_CLASS_TYPE}, {SELECTION_NODE_CLASS_TYPE})"
            " — this installation registers neither"
        )
    tail = _resolve_app_node(tail_name, workflow, node_mappings)
    nodes = workflow.setdefault("nodes", [])
    links = workflow.setdefault("links", [])
    next_node = int(workflow.get("last_node_id", max((int(n["id"]) for n in nodes), default=0)))
    next_link = int(workflow.get("last_link_id", max((int(link[0]) for link in links), default=0)))

    next_node += 1
    preview: Dict[str, Any] = {
        "id": next_node,
        "type": PREVIEW_NODE_CLASS_TYPE,
        "pos": [tail["pos"][0] + _DEFAULT_SIZE[0] + _MARGIN_X, tail["pos"][1]],
        "size": list(_DEFAULT_SIZE),
        "flags": {},
        "order": next_node,
        "mode": 0,
        "inputs": [{"name": "record", "type": RECORD_TYPE, "link": None}],
        "outputs": _WorkflowBuilder._build_outputs(preview_cls),
        "properties": {"Node name for S&R": PREVIEW_NODE_CLASS_TYPE},
        "widgets_values": _widget_defaults(preview_cls),
    }
    next_link += 1
    links.append([next_link, tail["id"], 0, preview["id"], 0, RECORD_TYPE])
    tail_record = tail["outputs"][0]
    tail_record["links"] = list(tail_record.get("links") or []) + [next_link]
    preview["inputs"][0]["link"] = next_link
    nodes.append(preview)

    next_node += 1
    leftmost = min(float(n["pos"][0]) for n in nodes)
    selection: Dict[str, Any] = {
        "id": next_node,
        "type": SELECTION_NODE_CLASS_TYPE,
        "pos": [leftmost - _DEFAULT_SIZE[0] - _MARGIN_X, tail["pos"][1]],
        "size": list(_DEFAULT_SIZE),
        "flags": {},
        "order": next_node,
        "mode": 0,
        "inputs": [],
        "outputs": _WorkflowBuilder._build_outputs(selection_cls),
        "properties": {"Node name for S&R": SELECTION_NODE_CLASS_TYPE},
        "widgets_values": _widget_defaults(selection_cls),
    }
    nodes.append(selection)
    workflow["last_node_id"] = next_node
    workflow["last_link_id"] = next_link
    return [str(preview["id"]), str(selection["id"])]


def _apply_app_view(
    workflow: Dict[str, Any], spec: Dict[str, Any], node_mappings: Dict[str, Any], as_: str
) -> Dict[str, Any]:
    """Write ComfyUI's ``extra.linearMode`` / ``linearData`` from a validated ``app:`` block."""
    if as_ == "subgraph":
        # Subgraph packaging moves every node inside ``definitions.subgraphs[]``. ComfyUI's App
        # view renders a subgraph-internal widget only when it has been PROMOTED onto the instance
        # node (``resolveNodeWidget`` takes its ``isPromotedWidgetView`` branch there), so ids that
        # point inside a definition are silently skipped and the form comes up empty. Refuse rather
        # than ship that: a template wanting an App view imports flat.
        raise AppViewError(
            "app: cannot be declared on a subgraph import — the App view only renders a "
            "subgraph-internal widget once it is promoted onto the instance node. Import the "
            "template flat (drop --as_ subgraph) to give it an App view."
        )
    inputs: List[List[Any]] = []
    for class_name, widget in spec["inputs"]:
        node = _resolve_app_node(class_name, workflow, node_mappings)
        node_cls = node_mappings.get(str(node.get("type", "")))
        widgets, sockets = _widget_and_socket_params(node_cls) if node_cls is not None else ([], {})
        if widget in sockets:
            raise AppViewError(
                f"app: {class_name}.{widget} is a wired socket, not a widget — an App view shows "
                f"fields, so expose one of: {', '.join(widgets) or 'none'}"
            )
        if widget not in widgets:
            raise AppViewError(f"app: {class_name} has no widget {widget!r} — it has {', '.join(widgets) or 'none'}")
        inputs.append([node["id"], widget])
    # ComfyUI stores output node ids as STRINGS and input ids numerically — matching the shape the
    # frontend writes itself when a user builds an app view by hand.
    outputs = [str(_resolve_app_node(name, workflow, node_mappings)["id"]) for name in spec["outputs"]]
    if spec.get("preview"):
        # A PANE graph has no record source, so its named output op can show nothing when the
        # App's Run button fires. ``preview: true`` places the editor's two workspace-preview
        # nodes — the selection the page shows, and the chain run over it with the form's
        # values — and THOSE become the App's outputs; the first named output is the op the
        # preview hangs on. Regeneration keeps them, which hand-placing never did.
        outputs = _place_preview_nodes(workflow, spec["outputs"][0], node_mappings)
    workflow["extra"] = {
        **workflow.get("extra", {}),
        "linearMode": True,
        "linearData": {
            "inputs": inputs,
            "outputs": outputs,
        },
    }
    return workflow


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
        node_mappings: StreamStudio's ``NODE_CLASS_MAPPINGS``.
        instantiate: ``False`` (default) walks the un-instantiated marker graph from
            ``confluid.load(until="settled")``; ``True`` walks live-but-inert objects from
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
    # A FLOW document (the export shape ``{…constants…, flow: {step: op}, outputs: …}``) is a step
    # GRAPH, not a mapping of node-valued top-level keys — its ``flow:`` mapping would be walked as
    # if each step name were a config key by the whole-graph path below. Detect it on a raw marker
    # parse (``until="document"`` keeps ``!ref`` links intact) and dispatch to the dedicated importer.
    # Restricted to path/text inputs (where flow docs actually arrive — the CLI passes a path, the
    # route raw YAML) so the probe never mutates a caller-owned dict; any parse failure or non-flow
    # shape falls through to the whole-graph path unchanged.
    raw: Any = None
    if isinstance(doc, (str, Path)):
        try:
            raw = _confluid_load(doc, until="document")
        except Exception:  # pragma: no cover - defensive: the whole-graph path re-parses and reports
            raw = None
        if _is_flow_document(raw):
            return flow_yaml_to_workflow(raw, node_mappings, as_=as_, name=name)

    # instantiate=True → live-but-inert objects (load parses the file/string THEN materialises with
    # the expensive solidify suppressed); else → the un-instantiated broadcast-resolved marker graph.
    graph = _confluid_load(doc, solidify=False) if instantiate else _confluid_load(doc, until="settled")
    builder = _WorkflowBuilder(node_mappings)
    entries = list(graph.values()) if isinstance(graph, dict) else [graph]
    for value in entries:
        if _is_node_value(value):
            builder.emit(value)
    workflow = builder.finalize()
    if as_ == "group":
        workflow = _wrap_in_group(workflow, name)
    elif as_ == "subgraph":
        workflow = _wrap_in_subgraph(workflow, builder.free_inputs, name)
    # The App-view declaration is read off the RAW document (``app:`` is plain data, so the node
    # walk above skipped it) and applied to the finished workflow, whatever the packaging.
    app_spec = _app_declaration(raw if isinstance(doc, (str, Path)) else doc)
    return _apply_app_view(workflow, app_spec, node_mappings, as_) if app_spec else workflow


# =======================================================================================
# Import: flow.yaml (the exported step-graph document) → workflow.json
# =======================================================================================


def _is_flow_document(doc: Any) -> bool:
    """True if ``doc`` is a parsed flow-export document: a dict with a ``flow`` step MAPPING."""
    return isinstance(doc, dict) and isinstance(doc.get("flow"), dict)


def _flow_document_to_replay(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Reshape a ``{…constants…, flow: {...}, outputs: …}`` document for the node replay.

    A flow document is already the graph, so this is a re-KEYING, not a lowering: the steps
    become an ordered ``__steps__`` list of ops the replay walks. Steps are parsed UNBUILT
    (``parse_flow(build=False)``) so a hoisted constant's dotted ``!ref:`` never resolves
    during a structural import.
    """
    from recordstream.flow import parse_flow as _parse_flow

    steps, outputs = _parse_flow(doc["flow"], str(doc.get("outputs", "") or ""), build=False)
    # ``app:`` is a declaration ABOUT the canvas, not a value on it — every other surviving
    # top-level key is replayed as a hoisted constant node, so it is dropped here and read
    # separately by :func:`_app_declaration`.
    replay: Dict[str, Any] = {k: v for k, v in doc.items() if k not in ("flow", "outputs", APP_KEY)}
    branchy = [s.name for s in steps if s.from_ is not None or s.merge_from]
    if branchy and any(s.merge_from for s in steps) or _forks(steps):
        raise UnsupportedTopologyError(
            "Importing a BRANCHY flow document back onto the canvas is not supported yet "
            f"(steps {branchy} fork or merge). The importer replays a sequence of ops; the "
            "step graph needs a node-per-step walk instead. Tracked in streamstudio TASKS.md. "
            "A linear flow document imports normally."
        )
    replay["__steps__"] = [step.op for step in steps if step.op is not None]
    replay["__step_names__"] = [step.name for step in steps if step.op is not None]
    return replay


def _forks(steps: Any) -> bool:
    """True when two steps read the same producer — a fan-out the sequential replay can't place."""
    read = [s.from_ for s in steps if s.from_ is not None]
    return len(read) != len(set(read))


def flow_yaml_to_workflow(
    doc: Union[str, Path, Dict[str, Any]],
    node_mappings: Dict[str, Any],
    *,
    as_: str = "graph",
    name: str = "Imported",
) -> Dict[str, Any]:
    """Convert a StreamStudio **flow-export** YAML into a workflow.json.

    The inverse of :func:`streamstudio.export.export_flow_yamls`: the document's steps are
    replayed into a connected canvas graph.

    * each step becomes a node, wired from the step before it;
    * a hoisted ``constant=True`` value node is placed and its dotted
      ``!ref:<name>.<output>`` consumers are wired from its output;
    * a **dead-end tap** — a record-chain op whose own record output feeds nothing, excluding the
      pipeline's final output — is given a distinct colour.

    The source is intentionally left unattached (the export contract — the user reattaches their
    own source).

    **A BRANCHY document does not import yet** — :func:`_flow_document_to_replay` raises, because
    the replay walks a SEQUENCE while a forking/merging step graph needs a node-per-step walk
    (tracked in ``TASKS.md``). A linear flow document imports normally.

    Args:
        doc: A flow-doc as a YAML path / string, or an already-parsed raw-marker dict.
        node_mappings: StreamStudio's ``NODE_CLASS_MAPPINGS``.
        as_: ``"graph"`` (default) lays the nodes out; ``"group"`` additionally wraps them in one
            visual box. ``"subgraph"`` wraps them with a declared boundary. ``"pipeline"`` reads the
            document's ``RecordContract`` steps as a contract and builds the typed boundary they
            describe, beside a pre-wired ``Stream`` (see :func:`_contract_pipeline`).
        name: Title for the group box.

    Returns:
        A ComfyUI UI-format workflow dict, JSON-safe (non-finite widget values nulled).
    """
    raw = doc if _is_flow_document(doc) else _confluid_load(doc, until="document")
    if not _is_flow_document(raw):  # pragma: no cover - caller detected the shape
        raise UnsupportedTopologyError(
            "flow_yaml_to_workflow expects a flow-export document (a top-level 'flow:' step mapping)."
        )
    app_spec = _app_declaration(raw)
    raw = _flow_document_to_replay(cast(Dict[str, Any], raw))

    # PIPELINE packaging reads the contract ops as the DECLARATION they are and builds the typed
    # boundary they describe — the contract ops themselves are never drawn. It replaces the replay
    # entirely, so it dispatches before any node is emitted.
    if as_ == "pipeline":
        contract = _contract_of(raw)
        if not contract:
            raise UnsupportedTopologyError(
                "pipeline packaging needs a contract to build from: the document declares no "
                "RecordContract entries (a 'flow:' whose steps are RecordContract ops)"
            )
        return _contract_pipeline(contract, name, node_mappings)

    builder = _WorkflowBuilder(node_mappings)

    # 1. Hoisted constants → value nodes (document key -> node id), for dotted-!ref wiring below.
    const_nodes: Dict[str, int] = {}
    for key, marker in raw.items():
        if key == "__steps__" or not _is_node_value(marker):
            continue
        res = builder.emit(marker)
        if res is not None:
            const_nodes[key] = res[0]

    # 2. Replay the steps as a chain — and DRAW its decisions (user design 2026-09-06): a step
    #    gated on an outcome (``requires: <flag>``, the document's spelling) hangs off the OUTCOME
    #    socket of the earlier step whose op decides that flag; consecutive steps on one outcome
    #    chain inside their branch; a step gated on an outcome decided INSIDE the open branch opens
    #    a nested decision (the radio chain's ``try_linear`` branch decides ``linear``); the first
    #    ungated step after a run of branches is fed through a ``Join branches`` node from every
    #    outermost tail. The gate itself is never written on the canvas — the wire is the
    #    condition, and ``export.fold_branches`` writes the gate back from it. A branchy DOCUMENT
    #    never reaches here (``_flow_document_to_replay`` raises): the branches drawn here come
    #    from the gates, which is how a document spells them.
    previous: Optional[int] = None
    op_node_ids: List[int] = []  # real op nodes, in schedule order (last = deepest sink = final output)
    placed: List[Tuple[Callable[..., Any], int]] = []  # (op class, node id) — what a gate may name
    # Open decisions, outermost first: {"test": node id, "branches": [[outcome, head, tails]]} —
    # a branch's tails are its own tail until a decision nested in it closes, then that one's.
    open_decisions: List[Dict[str, Any]] = []
    step_names = list(raw.get("__step_names__") or [])

    def _close_innermost() -> List[int]:
        """Close the innermost decision WITHOUT a join: its tails become the enclosing branch's."""
        inner = open_decisions.pop()
        tails = [tail for _o, _h, tails in inner["branches"] for tail in tails]
        if open_decisions:
            open_decisions[-1]["branches"][-1][2] = tails
        return tails

    for position, marker in enumerate(raw["__steps__"]):
        spec = _target_and_kwargs(marker)
        if spec is None:
            continue
        cls, _is_lazy, kwargs = spec
        res = builder.emit(marker)
        if res is None:
            continue
        nid = res[0]
        outcome = str(kwargs.get(GATE_PARAM) or "")
        step = step_names[position] if position < len(step_names) else str(position)
        if outcome:
            test = next((pid for pcls, pid in reversed(placed) if outcome in declared_outcomes(pcls)), None)
            if test is None:
                decided = "; ".join(
                    f"{', '.join(declared_outcomes(pcls))} ({pcls.__name__})"
                    for pcls, _pid in placed
                    if declared_outcomes(pcls)
                )
                raise UnsupportedTopologyError(
                    f"step {step!r} is gated on the outcome {outcome!r}, which no step before it decides — "
                    f"the outcomes decided so far: {decided or 'none'}"
                )
            # Pop nested decisions closed by returning to an outer test node.
            while (
                open_decisions and open_decisions[-1]["test"] != test and any(d["test"] == test for d in open_decisions)
            ):
                _close_innermost()
            current = open_decisions[-1] if open_decisions else None
            if current is not None and current["test"] != test:
                open_decisions.append({"test": test, "branches": []})  # nested inside the open branch
                current = open_decisions[-1]
            elif current is None:
                open_decisions.append({"test": test, "branches": []})
                current = open_decisions[-1]
            branches = current["branches"]
            if branches and branches[-1][0] == outcome:
                builder.connect(branches[-1][2][-1], 0, nid, _RECORD_INPUT, RECORD_TYPE)
                branches[-1][2] = [nid]
            else:
                if len(branches) >= len(JOIN_INPUTS):
                    raise UnsupportedTopologyError(
                        f"step {step!r} opens branch {len(branches) + 1} of one decision; Join branches "
                        f"takes at most {len(JOIN_INPUTS)}"
                    )
                slot = next(i for i, out in enumerate(builder.by_id[test]["outputs"]) if out["name"] == outcome)
                builder.connect(test, slot, nid, _RECORD_INPUT, RECORD_TYPE)
                branches.append([outcome, nid, [nid]])
        else:
            if open_decisions:
                while len(open_decisions) > 1:
                    _close_innermost()
                tails = _close_innermost()
                if len(tails) > len(JOIN_INPUTS):
                    raise UnsupportedTopologyError(
                        f"the decision before step {step!r} ends in {len(tails)} branches; Join branches "
                        f"takes at most {len(JOIN_INPUTS)}"
                    )
                join = builder.spawn(JOIN_CLASS_TYPE)
                for index, tail in enumerate(tails):
                    builder.connect(tail, 0, join, JOIN_INPUTS[index], RECORD_TYPE)
                builder.connect(join, 0, nid, _RECORD_INPUT, RECORD_TYPE)
            elif previous is not None:
                builder.connect(previous, 0, nid, _RECORD_INPUT, RECORD_TYPE)
            previous = nid
        placed.append((cls, nid))
        op_node_ids.append(nid)

    # 3. Wire dotted-!ref free inputs (constant.output -> consumer param) to the constant nodes.
    _wire_constant_refs(builder, const_nodes)

    # 4. Colour the dead-end taps (record-chain leaves that are not the final output).
    _color_dead_end_taps(builder, op_node_ids)

    # 5. JSON-safety: a non-finite widget value (e.g. ThresholdOp.high_level=NaN) would emit a bare
    #    ``NaN`` token that the browser's JSON.parse rejects — null it.
    _sanitize_widget_values(builder)

    workflow = builder.finalize()
    # 6. Restore each node's FULL widget-input-slot list so a value wired into a widget (a hoisted
    #    constant → ImpairmentsChain.tx_disabled) lands at the slot index ComfyUI rebuilds from the class.
    _complete_widget_input_slots(workflow, node_mappings)
    if as_ == "group":
        workflow = _wrap_in_group(workflow, name)
    elif as_ == "subgraph" and op_node_ids:
        # The flow document DECLARES its own boundary (head step's unfed record / ``outputs:`` step),
        # so the !ref: convention _wrap_in_subgraph infers from is not needed here.
        workflow = _wrap_flow_in_subgraph(workflow, op_node_ids[0], op_node_ids[-1], name, node_mappings)
    # 7. The App-view declaration the document carried (``app:``), resolved against the placed nodes.
    return _apply_app_view(workflow, app_spec, node_mappings, as_) if app_spec else workflow


def _declared_output_names(src_cls: Any) -> "Set[str]":
    """The ``@output`` property names a node's class declares — the ones that are ATTRIBUTES.

    Everything else a node emits is a STRUCTURAL socket standing for the node itself (a source's
    ``source``, an op's ``op``, a value producer's ``object``) or for data flow (``record``), and
    those are what a ``!ref:`` can name. Asking the class is what makes the distinction exact:
    a slot index cannot tell them apart, and a name list would go stale the next time a builder
    grows a socket.
    """
    path = getattr(src_cls, "TAIDAL_CALLABLE_PATH", None)
    if not path:
        return set()
    try:
        return {str(spec["name"]) for spec in output_specs(cast(type, resolve_callable(str(path))))}
    except Exception:  # pragma: no cover - a class we cannot resolve declares nothing we can fold
        return set()


def _contract_of(replay: Dict[str, Any]) -> "Dict[str, str]":
    """``{record entry: item type}`` declared by the ``RecordContract`` steps of a flow document.

    In pipeline packaging a contract op is a DECLARATION, not a node: it says what the record must
    carry, and the builder turns that into the typed boundary it was describing. Steps are read in
    document order and merged, so a chain of one-entry contracts (the readable way to write it —
    one node per requirement) states the same thing as a single multi-entry one.
    """
    contract: Dict[str, str] = {}
    for marker in replay.get("__steps__", []):
        parsed = _target_and_kwargs(marker)
        if parsed is None:
            continue
        cls, _lazy, kwargs = parsed
        if getattr(cls, "__name__", "") != "RecordContract":
            continue
        fields = kwargs.get("fields")
        if isinstance(fields, dict):
            contract.update({str(k): str(v) for k, v in fields.items()})
    return contract


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
    """Tint every dead-end tap — a record-chain op whose record output feeds nothing — distinctly.

    "Record-chain op" = has an incoming ``record`` wire (so a config-only node like a ``MaxOp`` feeding
    a ``ConfigureOp`` via its ``op`` output, with no record input, is NOT a tap). "Feeds nothing" = no
    outgoing ``RECORDSTREAM_RECORD`` wire from any output (so an op feeding a Mix or a later op is live).
    The single last-scheduled op (the deepest sink = the pipeline's final output) is exempt.
    """
    if not op_node_ids:
        return
    final = op_node_ids[-1]
    has_record_in: set = set()
    produces_record: set = set()
    for _lid, src_id, _src_slot, dst_id, dst_idx, ltype in builder.links:
        if ltype != RECORD_TYPE:
            continue
        produces_record.add(src_id)
        dst = builder.by_id[dst_id]
        if dst_idx < len(dst["inputs"]) and dst["inputs"][dst_idx].get("name") == _RECORD_INPUT:
            has_record_in.add(dst_id)
    for nid in op_node_ids:
        if nid != final and nid in has_record_in and nid not in produces_record:
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

    return str(uuid.uuid5(uuid.NAMESPACE_DNS, "taidal.streamstudio.subgraph:" + ":".join(parts)))


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
        input_defs.append(_boundary_slot(ref_name, stype, slot_i, link_ids, _sg_uuid(name, "in", ref_name)))

    # OUTPUT boundary: a pipeline root is a node whose OWN output nothing internal consumes (it is the
    # SOURCE of no internal link) → one output socket each.
    producers = {link[1] for link in internal_links if link[3] != -20}
    roots = [n for n in internal_nodes if n["id"] not in producers] or internal_nodes[-1:]
    output_defs: List[Dict[str, Any]] = []
    for slot_i, root in enumerate(roots):
        out_type = root["outputs"][0]["type"] if root["outputs"] else "*"
        next_link += 1
        internal_links.append([next_link, root["id"], 0, -20, slot_i, out_type])
        out_name = root["outputs"][0]["name"] if root["outputs"] else "output"
        output_defs.append(
            _boundary_slot(out_name, out_type, slot_i, [next_link], _sg_uuid(name, "out", str(root["id"])))
        )

    return _subgraph_envelope(
        name,
        sg_id,
        input_defs,
        output_defs,
        internal_nodes,
        internal_links,
        last_node_id=workflow["last_node_id"],
        last_link_id=next_link,
    )


def _wrap_flow_in_subgraph(
    workflow: Dict[str, Any],
    head_id: int,
    final_id: int,
    name: str,
    node_mappings: Dict[str, Any],
) -> Dict[str, Any]:
    """Package an imported flow document as a PIPELINE BLUEPRINT — a subgraph with a DECLARED boundary.

    A flow document already states its boundary, which is why this does not need the ``!ref:``
    convention :func:`_wrap_in_subgraph` infers from: the head step's ``record`` input is fed by
    nothing (that is where a source connects) and ``outputs:`` names the step whose record leaves.
    Both slots are emitted whether or not anything is connected — a boundary that renders only once
    it is wired cannot be aimed at, the same defect the ``Stream.source`` STRING widget had.

    The instance additionally carries an ``op`` output (``RECORDSTREAM_OP``): an *Extract as Op*
    terminator is placed on the final step and promoted, so the rectangle drops straight into a
    ``Stream``'s op slot and the user never has to know that node exists. The terminator compiles its
    own record-ANCESTOR cone, so taps drawn off the spine inside the blueprint stay out of the
    resulting ``Pipeline`` — that is what lets one shape be both a pipeline and a viewer.
    """
    internal_nodes: List[Dict[str, Any]] = workflow["nodes"]
    internal_links: List[Any] = list(workflow["links"])
    next_link: int = workflow["last_link_id"]
    next_node: int = workflow["last_node_id"]
    by_id = {n["id"]: n for n in internal_nodes}
    sg_id = _sg_uuid(name)

    # -- INPUT: the head step's unfed ``record`` socket, wired from the inputNode (-10).
    input_defs: List[Dict[str, Any]] = []
    head: Optional[Dict[str, Any]] = by_id.get(head_id)
    head_slot = (
        next((i for i, inp in enumerate(head["inputs"]) if inp.get("name") == _RECORD_INPUT), None)
        if head is not None
        else None
    )
    if head is not None and head_slot is not None:
        next_link += 1
        internal_links.append([next_link, -10, 0, head_id, head_slot, RECORD_TYPE])
        head["inputs"][head_slot]["link"] = next_link
        input_defs.append(
            _boundary_slot(_RECORD_INPUT, RECORD_TYPE, 0, [next_link], _sg_uuid(name, "in", _RECORD_INPUT))
        )

    # -- The terminator: placed on the final step, its ``op`` promoted to the boundary.
    output_defs: List[Dict[str, Any]] = []
    final = by_id.get(final_id)
    if final is not None:
        next_link += 1  # final step's record -> outputNode slot 0
        internal_links.append([next_link, final_id, 0, -20, 0, RECORD_TYPE])
        final["outputs"][0]["links"].append(next_link)
        output_defs.append(_boundary_slot("record", RECORD_TYPE, 0, [next_link], _sg_uuid(name, "out", "record")))

        terminator_cls = node_mappings.get(_SUBGRAPH_OP_CLASS_TYPE)
        if terminator_cls is not None:
            next_node += 1
            terminator: Dict[str, Any] = {
                "id": next_node,
                "type": _SUBGRAPH_OP_CLASS_TYPE,
                "pos": [final["pos"][0] + _MARGIN_X + _DEFAULT_SIZE[0], final["pos"][1] + _DEFAULT_SIZE[1]],
                "size": list(_DEFAULT_SIZE),
                "flags": {},
                "order": next_node,
                "mode": 0,
                "inputs": [{"name": _RECORD_INPUT, "type": RECORD_TYPE, "link": None}],
                "outputs": _WorkflowBuilder._build_outputs(terminator_cls),
                "properties": {"Node name for S&R": _SUBGRAPH_OP_CLASS_TYPE},
                "widgets_values": [],
            }
            next_link += 1  # final step's record -> the terminator
            internal_links.append([next_link, final_id, 0, terminator["id"], 0, RECORD_TYPE])
            final["outputs"][0]["links"].append(next_link)
            terminator["inputs"][0]["link"] = next_link
            next_link += 1  # the terminator's op -> outputNode slot 1
            internal_links.append([next_link, terminator["id"], 0, -20, 1, OP_TYPE])
            terminator["outputs"][0]["links"].append(next_link)
            internal_nodes.append(terminator)
            output_defs.append(_boundary_slot("op", OP_TYPE, 1, [next_link], _sg_uuid(name, "out", "op")))

    return _subgraph_envelope(
        name,
        sg_id,
        input_defs,
        output_defs,
        internal_nodes,
        internal_links,
        last_node_id=next_node,
        last_link_id=next_link,
    )


def _contract_pipeline(
    contract: "Dict[str, str]",
    name: str,
    node_mappings: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the canvas a workspace opens on: a CONTRACT subgraph beside a ``Stream``.

    ``contract`` maps a record entry to its item type (``{"image": "Image", "class": "Label"}``).
    Each entry becomes an *Extract from Record* on the incoming record, feeding the passthrough
    node for its item type, whose output is promoted to a boundary output NAMED AFTER THE ENTRY
    and TYPED by the item. The graph then states the contract structurally — record in, one typed
    entry out per requirement — with nothing to read out of a widget.

    The type nodes also feed a *Build Record*, closed by the *Extract as Op* terminator whose
    ``op`` is promoted: that is what makes the rectangle droppable into a ``Stream``'s op slot,
    and it is pre-wired here into the ``Stream`` placed beside it. Exactly ONE thing is left
    unconnected — the ``Stream``'s ``source`` — which is the one decision the workspace cannot
    make for the user.
    """
    extract_cls = node_mappings.get(_EXTRACT_CLASS_TYPE)
    builder_cls = node_mappings.get(_RECORD_BUILDER_CLASS_TYPE)
    terminator_cls = node_mappings.get(_SUBGRAPH_OP_CLASS_TYPE)
    stream_cls = node_mappings.get(_STREAM_CLASS_TYPE)
    missing = [
        key
        for key, cls in (
            (_EXTRACT_CLASS_TYPE, extract_cls),
            (_RECORD_BUILDER_CLASS_TYPE, builder_cls),
            (_SUBGRAPH_OP_CLASS_TYPE, terminator_cls),
            (_STREAM_CLASS_TYPE, stream_cls),
        )
        if cls is None
    ]
    if missing:
        raise UnsupportedTopologyError(f"the contract pipeline needs nodes that are not registered: {missing}")

    sockets = item_socket_types()
    unknown = {item for item in contract.values() if item not in sockets}
    if unknown:
        raise UnsupportedTopologyError(f"no item type registered as {sorted(unknown)} (known: {', '.join(sockets)})")

    nodes: List[Dict[str, Any]] = []
    links: List[Any] = []
    next_node = 0
    next_link = 0

    # -- Build Record: one entry socket per contract field, gathering the typed items.
    next_node += 1
    gather = _node(next_node, _RECORD_BUILDER_CLASS_TYPE, builder_cls, [_MARGIN_X + 2 * _COL_DX, _MARGIN_Y])
    gather_id = next_node

    output_defs: List[Dict[str, Any]] = []
    for slot, (field, item) in enumerate(contract.items()):
        socket = sockets[item]
        row = _MARGIN_Y + slot * _ROW_DY

        # The extract carrying THIS item type — the base node for the record engine's own,
        # a domain package's own node for its additions.
        extract_key, item_output = extract_source_for(item)
        item_extract_cls = node_mappings.get(extract_key)
        if item_extract_cls is None:
            raise UnsupportedTopologyError(f"no extract node registered for item type {item!r} ({extract_key})")
        next_node += 1
        widgets = [field, "viridis", 512] if extract_key == _EXTRACT_CLASS_TYPE else [field]
        extract = _node(next_node, extract_key, item_extract_cls, [_MARGIN_X, row], widgets)
        extract["inputs"].append({"name": _RECORD_INPUT, "type": RECORD_TYPE, "link": None})
        nodes.append(extract)

        next_node += 1  # the item's Type node — the contract, stated
        type_key = f"Taidal_{item}Type"
        type_cls = node_mappings.get(type_key)
        if type_cls is None:
            raise UnsupportedTopologyError(f"no passthrough node registered for item type {item!r} ({type_key})")
        declare = _node(next_node, type_key, type_cls, [_MARGIN_X + _COL_DX, row])
        declare["inputs"].append({"name": item.lower(), "type": socket, "link": None})
        nodes.append(declare)

        # the extract's item-typed output -> the type node, BY NAME (an Image leaves on the
        # native ``image`` socket, which several outputs could otherwise match by type)
        item_slot = next((i for i, out in enumerate(extract["outputs"]) if out["name"] == item_output), None)
        if item_slot is None:  # pragma: no cover - defensive: the extract lost that output
            raise UnsupportedTopologyError(f"{extract_key} has no {item_output!r} output for {field!r}")
        next_link += 1
        links.append([next_link, extract["id"], item_slot, declare["id"], 0, extract["outputs"][item_slot]["type"]])
        extract["outputs"][item_slot]["links"].append(next_link)
        declare["inputs"][0]["link"] = next_link

        # the type node -> the boundary output, named after the ENTRY and typed by the ITEM
        next_link += 1
        links.append([next_link, declare["id"], 0, -20, slot, socket])
        declare["outputs"][0]["links"].append(next_link)
        output_defs.append(_boundary_slot(field, socket, slot, [next_link], _sg_uuid(name, "out", field)))

        # ...and -> Build Record. The socket NAME is the record key the compiler renames to
        # (``RenameField(field -> label)``), so it must be the ENTRY name: seeded ``entry_0``
        # would build a record carrying ``entry_0`` instead of ``image``.
        gather["inputs"].append({"name": field, "type": socket, "link": None})
        next_link += 1
        links.append([next_link, declare["id"], 0, gather_id, slot, socket])
        declare["outputs"][0]["links"].append(next_link)
        gather["inputs"][slot]["link"] = next_link

    nodes.append(gather)

    # -- the incoming record feeds every extract (the default wiring; the user may rewire).
    input_defs: List[Dict[str, Any]] = []
    fed = [n for n in nodes if n["type"] == _EXTRACT_CLASS_TYPE]
    link_ids: List[int] = []
    for extract in fed:
        next_link += 1
        links.append([next_link, -10, 0, extract["id"], 0, RECORD_TYPE])
        extract["inputs"][0]["link"] = next_link
        link_ids.append(next_link)
    if link_ids:
        input_defs.append(_boundary_slot(_RECORD_INPUT, RECORD_TYPE, 0, link_ids, _sg_uuid(name, "in", _RECORD_INPUT)))

    # -- the terminator: Build Record -> Extract as Op -> the promoted ``op`` boundary.
    next_node += 1
    terminator = _node(next_node, _SUBGRAPH_OP_CLASS_TYPE, terminator_cls, [_MARGIN_X + 3 * _COL_DX, _MARGIN_Y])
    terminator["inputs"].append({"name": _RECORD_INPUT, "type": RECORD_TYPE, "link": None})
    next_link += 1
    links.append([next_link, gather_id, 0, terminator["id"], 0, RECORD_TYPE])
    gather["outputs"][0]["links"].append(next_link)
    terminator["inputs"][0]["link"] = next_link
    op_slot = len(output_defs)
    next_link += 1
    links.append([next_link, terminator["id"], 0, -20, op_slot, OP_TYPE])
    terminator["outputs"][0]["links"].append(next_link)
    output_defs.append(_boundary_slot("op", OP_TYPE, op_slot, [next_link], _sg_uuid(name, "out", "op")))
    nodes.append(terminator)

    envelope = _subgraph_envelope(
        name,
        _sg_uuid(name),
        input_defs,
        output_defs,
        nodes,
        links,
        last_node_id=next_node,
        last_link_id=next_link,
    )

    # -- the parent canvas: the blueprint and a ``Stream``, its ``op`` already wired.
    #    The blueprint's ``record`` input is deliberately NOT wired here: the subgraph is an OP
    #    of the Stream, so the Stream feeds it at RUN time. Wiring it on the canvas — from the
    #    Stream's own output, through a walker — would draw a loop that says the opposite.
    instance = envelope["nodes"][0]
    instance["pos"] = [_MARGIN_X + _COL_DX, _MARGIN_Y]
    op_out = next(i for i, out in enumerate(instance["outputs"]) if out["name"] == "op")

    # One seed value per WIDGET the class declares (chunk_size, class_names) -- a longer seed
    # assigns values to the wrong knobs when ComfyUI rebuilds the node from the class.
    stream_spec: Dict[str, Any] = getattr(stream_cls, "INPUT_TYPES", dict)() or {}
    stream_widgets = list(stream_spec.get("required") or {})
    stream = _node(
        2,
        _STREAM_CLASS_TYPE,
        stream_cls,
        [_MARGIN_X + 2 * _COL_DX, _MARGIN_Y],
        [0 if w == "chunk_size" else "" for w in stream_widgets],
    )
    stream["inputs"] = [
        {"name": "source", "type": SOURCE_TYPE, "link": None},  # THE one thing left to connect
        {"name": "op_0", "type": OP_TYPE, "link": 1},
    ]
    instance["outputs"][op_out]["links"] = [1]
    envelope["nodes"].append(stream)
    envelope["links"] = [[1, instance["id"], op_out, stream["id"], 1, OP_TYPE]]
    last_node = 2

    # -- the class vocabulary, when the contract declares an item type that HAS one.
    #    It sits on the PARENT canvas, not inside the blueprint: the blueprint compiles to a
    #    per-RECORD Pipeline, and a dataset-level constant threaded through a per-record
    #    boundary would be folded away or left dangling. Unwired is its FINISHED state -- it is
    #    a declaration the document carries, hoisted to its own top-level key because the class
    #    is ``constant=True``, and read from there by whatever opens the document.
    declaration_cls = node_mappings.get(_CLASS_NAMES_OUTPUT_CLASS_TYPE)
    if declaration_cls is not None and _declares_a_class_vocabulary(contract):
        last_node += 1
        declaration = _node(
            last_node,
            _CLASS_NAMES_OUTPUT_CLASS_TYPE,
            declaration_cls,
            [_MARGIN_X + 2 * _COL_DX, _MARGIN_Y + 2 * _ROW_DY],
            [""],  # EMPTY: the graph author is the only one who knows the answer
        )
        declaration["title"] = _DECLARE_TITLE.format(name="class_names")
        declaration["color"] = _DECLARE_COLOR
        declaration["bgcolor"] = _DECLARE_BGCOLOR
        envelope["nodes"].append(declaration)

    envelope["last_node_id"] = last_node
    envelope["last_link_id"] = 1
    return envelope


def _declares_a_class_vocabulary(contract: "Dict[str, str]") -> bool:
    """True when a declared item type MODELS a class vocabulary (a ``classes`` field).

    Structural, never a list of type names: ``Label`` and ``MultiLabel`` carry ``classes``,
    while ``Image`` / ``Mask`` / ``Boxes`` do not — so a segmentation contract is not asked
    for classes and a classification one is, decided by the record model rather than by
    anything hard-coded here. An item type that grows a vocabulary later starts qualifying
    with no change to this function.
    """
    for item in contract.values():
        try:
            item_type = get_item_type(item)
        except KeyError:
            continue
        if dataclasses.is_dataclass(item_type) and any(f.name == "classes" for f in dataclasses.fields(item_type)):
            return True
    return False


def _inner_link(link: Any) -> Optional[Dict[str, Any]]:
    """A subgraph definition's inner link, in the DICT form ComfyUI reads.

    Top-level links are 6-arrays; a subgraph definition's inner links are DICTS — the same
    asymmetry :func:`_link_six` normalises on the way IN. Built once, here, because both wrap
    modes feed :func:`_subgraph_envelope` and a second copy is a second chance to get it wrong.
    """
    six = _link_six(link)
    if six is None:
        return None
    return {
        "id": six[0],
        "origin_id": six[1],
        "origin_slot": six[2],
        "target_id": six[3],
        "target_slot": six[4],
        "type": six[5],
    }


def _node(node_id: int, key: str, node_cls: Any, pos: List[int], widgets: Optional[List[Any]] = None) -> Dict[str, Any]:
    """One workflow node for a registered class, with its declared outputs and no inputs yet.

    The caller appends the inputs it wires; outputs come from the class so a socket the node
    really has is never invented here.
    """
    outputs = [
        {"name": name, "type": rtype, "links": []}
        for name, rtype in zip(getattr(node_cls, "RETURN_NAMES", ()) or (), getattr(node_cls, "RETURN_TYPES", ()) or ())
    ]
    return {
        "id": node_id,
        "type": key,
        "pos": list(pos),
        "size": [300, 100],
        "flags": {},
        "order": node_id,
        "mode": 0,
        "inputs": [],
        "outputs": outputs,
        "properties": {"Node name for S&R": key},
        "widgets_values": list(widgets or []),
    }


def _boundary_slot(name: str, socket_type: str, slot_i: int, link_ids: List[int], uid: str) -> Dict[str, Any]:
    """One declared boundary slot of a subgraph definition (input or output).

    Declared means RENDERED — a slot exists in the definition whether or not the instance has
    anything wired to it. An unconnected boundary that does not render cannot be aimed at, which is
    the same defect the ``Stream.source`` STRING widget had.
    """
    return {
        "id": uid,
        "name": name,
        "type": socket_type,
        "linkIds": list(link_ids),
        "localized_name": name,
        "label": name,
        "pos": [0, 50 + slot_i * 40],
    }


def _subgraph_envelope(
    name: str,
    sg_id: str,
    input_defs: List[Dict[str, Any]],
    output_defs: List[Dict[str, Any]],
    internal_nodes: List[Dict[str, Any]],
    internal_links: List[Any],
    *,
    last_node_id: int,
    last_link_id: int,
) -> Dict[str, Any]:
    """The ``definitions.subgraphs[] + one instance node`` workflow both wrap modes return.

    Shared by :func:`_wrap_in_subgraph` (boundary INFERRED from unresolved ``!ref:`` + pipeline roots)
    and :func:`_wrap_flow_in_subgraph` (boundary DECLARED by the flow document's own structure), so
    the envelope shape is written once.
    """
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
        # A subgraph definition's inner links are DICTS (top-level links are 6-arrays -- the same
        # asymmetry _link_six normalises on the way IN). Emitting arrays here produced a definition
        # whose every wire was silently dropped on load: nodes appeared, sockets appeared, nothing
        # was connected. Built at this ONE chokepoint because both wrap modes come through here.
        "links": [d for d in (_inner_link(link) for link in internal_links) if d is not None],
        "groups": [],
        "extra": {},
        "state": {
            "lastNodeId": last_node_id,
            "lastLinkId": last_link_id,
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
    """An op-DAG member: an op-kind node OR the ``Taidal_MixRecords`` fan-in (the ops-export universe)."""
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


def ui_workflow_to_prompt(graph: Dict[str, Any], node_mappings: Dict[str, Any]) -> Dict[str, Any]:
    """A UI ``workflow.json`` -> the prompt/API-format graph the flow compiler consumes.

    The editor's frontend makes this conversion itself (``graphToPrompt``) before POSTing to
    ``/taidal/export_flow`` — but a graph FILE a consuming workspace owns (what the save
    mirror writes) is the UI form, and no frontend is around when it is converted headless.
    Subgraphs are inlined first and Reroutes collapsed (the frontend's own collapse,
    reproduced), so the result is what ``graphToPrompt`` would have produced:
    ``{node_id: {class_type, inputs}}`` with widget values by name and every wire as a
    ``[source_id, slot]`` pair. It lives HERE because graphio owns all workflow.json
    knowledge — ``export.py`` stays prompt-form only.
    """
    graph = _inline_subgraphs(graph)
    prompt: Dict[str, Any] = {}
    for node_id, pnode in _normalize_ui_workflow(graph, node_mappings).items():
        inputs: Dict[str, Any] = dict(pnode.widgets)
        inputs.update({name: [src, slot] for name, (src, slot) in pnode.links.items()})
        prompt[node_id] = {"class_type": pnode.class_type, "inputs": inputs}
    return _collapse_reroutes(prompt)


def extract_ops_workflow(
    workflow: Dict[str, Any], node_mappings: Dict[str, Any], *, flatten: bool = False
) -> Dict[str, Any]:
    """Extract the op-DAG of a UI ``workflow.json`` as a standalone, CONNECTED ``workflow.json``.

    The GRAPH counterpart of :func:`streamstudio.export.export_flow_yamls` — emitted as the RAW DAG
    *as drawn* (positions / widgets / wiring preserved, NO Stash/Unstash/Mix/constant compilation),
    but obeying the SAME `export-skip` / `export-target` marks (`properties["taidal_export"]`) the
    YAML ops-export uses (read via the shared :func:`streamstudio.export.collect_export_marks`): a
    **target** mark restricts the export to the union of the targets' data ancestor cones (so the
    dead-end viewer/analysis side-branches the user did NOT target are dropped); a **skip** mark
    removes the marked nodes + their downstream. With NO marks the whole op graph is kept. The
    config/value nodes (`@configurable(constant=True)` producers) are
    KEPT — they are ancestors of the ops they configure, so they ride along in the cone.

    Two modes, because a real pipeline routes the record THROUGH ComfyUI subgraph instances (which
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
        node_mappings: StreamStudio's ``NODE_CLASS_MAPPINGS`` (read for each node's ``TAIDAL_NODE_KIND``).
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


def _fold_drawn_branches(pnodes: Dict[str, "_PNode"], node_mappings: Dict[str, Any]) -> Dict[str, "_PNode"]:
    """The whole-graph exporter's face of :func:`streamstudio.export.fold_branches`.

    The fold speaks the prompt shape; this rebuilds it from the parsed nodes, folds, and parses
    back — carrying the UI-only export marks across. A graph that draws no decision (no join,
    no record wire off an outcome socket) is returned as it came.
    """
    drawn = any(p.class_type == JOIN_CLASS_TYPE for p in pnodes.values()) or any(
        slot >= 1 for p in pnodes.values() for name, (_src, slot) in p.links.items() if name == _RECORD_INPUT
    )
    if not drawn:
        return pnodes
    # A canvas naming Taidal nodes this installation does not register (a stale editor) gets
    # THAT refusal, by name, from the export proper — folding over unknown nodes would only
    # report that their branches "decide nothing".
    if any(p.class_type.startswith(_TAIDAL_NODE_PREFIX) and p.class_type not in node_mappings for p in pnodes.values()):
        return pnodes
    prompt = {
        nid: {
            "class_type": p.class_type,
            "inputs": {**p.widgets, **{k: [s, slot] for k, (s, slot) in p.links.items()}},
        }
        for nid, p in pnodes.items()
    }
    folded = _normalize_prompt(fold_branches(prompt, node_mappings), node_mappings)
    for nid, pnode in folded.items():
        pnode.export_skip = pnodes[nid].export_skip
        pnode.export_target = pnodes[nid].export_target
    return folded


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
    # Partial when the class is confluid-lazy-marked (optimizer / loader / lightning / logger) OR a
    # consumer wired its object node's `lazy` output (slot 1) — a deferred runtime-injection slot.
    is_lazy = marks(cls).lazy
    return cls, is_lazy, coerced


def _collapse_reroutes(prompt: Dict[str, Any]) -> Dict[str, Any]:
    """A prompt-view with ``Reroute`` (rgthree / native) pass-throughs collapsed out.

    ComfyUI's frontend treats a Reroute as a VIRTUAL node — the live API prompt the ops-export
    sees has them collapsed (a consumer wires straight to the Reroute's source). But the
    whole-graph export works on the saved WORKFLOW, where Reroutes are real nodes that sit in the
    record chain — and they break the Subgraph-Op cone compile's one-stream check (an op's
    ``record`` resolves to a Reroute, a separate "feed", so a fan-in looks like two streams). So
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


def _forward_record_closure(roots: Set[str], prompt: Dict[str, Any]) -> Set[str]:
    """Nodes reachable FORWARD from ``roots`` along record wires (Mix args included), EXCLUDING the
    roots themselves. Used to find a Subgraph-Op cone's dead-end viewer side-taps (RenderSignalPlot /
    preview ConvertToImage / Extract→MaskToImage): they consume a cone op's record but never reach
    the terminator, so the cone compile drops them — they must drop from the whole-graph surface too,
    or they fold into stray ``pipeline_N`` entries. Op-VALUE feeders of the Stream (no record wire) are
    NOT reached, so the real downstream ops are untouched."""
    consumers: Dict[str, List[str]] = {}
    for nid, node in prompt.items():
        args = _MIX_ARGS if str(node.get("class_type", "")) == _MIX_CLASS_TYPE else (_RECORD_INPUT,)
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


#: Every node the bridge discovers is keyed ``Taidal_<package>_<category>[_<group>]_<Class>``
#: (``bridge._node_key``), and the hand-written canvas adapters carry the prefix too. So a
#: class type that starts with it names a TAIDAL node — and one the running installation
#: does not register is an installation out of step with the canvas, not a ComfyUI node.
_TAIDAL_NODE_PREFIX = "Taidal_"


def _missing_node_refusal(missing: List[Tuple[str, str]]) -> str:
    """The message for a canvas naming Taidal nodes this installation does not register.

    Dropping them into a comment block is what this replaces: the node registry is a
    SNAPSHOT taken when the server started, so a canvas drawn after an op was added
    converts to a silently shortened chain, and the failure surfaces wherever the export
    is consumed — measured 2026-09-05, an editor started before eleven ops existed turned
    a twelve-node canvas into one node, and the workspace reported an empty chain three
    layers away. The remedy belongs in the first message, so it is in this one.
    """
    names = ", ".join(f"node {nid} ({class_type})" for nid, class_type in missing)
    return (
        f"this StreamStudio does not register {len(missing)} node(s) the canvas names: {names}. "
        "A Taidal node the canvas names but this installation has no class for is normally an op "
        "added AFTER the editor started — the node registry is read once at startup. Restart it "
        "(`streamstudio restart`) and convert again; if the op really is gone from this "
        "installation, the canvas is older than the code and needs redrawing."
    )


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
    ``warning``; the rest of the graph still exports). The ONE exception REFUSES: a
    ``Taidal_*`` class type this installation does not register at all is a canvas the
    running server is out of step with — normally an op added after it started — and a
    shortened chain there is worse than an error (:func:`_missing_node_refusal`). The canvas WIRING is preserved:
    record-chained ops fold into ``pipeline: !class:Stream`` entries (``source: !ref:<source>``
    traced through a Walk-style adapter, plus an ordered ``ops:`` list). What does NOT fold stays
    a standalone entry, so the YAML keeps every op either way: an op with no record wiring, an op
    consumed as a ``RECORDSTREAM_OP`` value, and a fan-out chain. The last is deliberate rather
    than unfinished — a branchy graph has no flat spelling, and ``export_flow_yamls`` is the
    export that carries it. A dropped wire into a real config member is noted in the comment block.
    """
    pnodes = _normalize_workflow(graph, node_mappings)
    pnodes = _fold_drawn_branches(pnodes, node_mappings)
    if not pnodes:
        return _confluid_dump({})

    # Export-target cones come FIRST: a target's data ancestor cone (record wires + Mix
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
            arg_names = _MIX_ARGS if pnode.class_type == _MIX_CLASS_TYPE else (_RECORD_INPUT,)
            stack.extend(pnode.links[arg][0] for arg in arg_names if arg in pnode.links)
        conflicts = sorted(marked & cones)
        if conflicts:
            raise UnsupportedTopologyError(
                f"Export-skip mark(s) on {conflicts} conflict with the export target(s) "
                f"{sorted(target_marks)} — the marked node(s) are inside a target's "
                "dependency cone (needed to compute it). Clear one of the two marks."
            )

    # The export-skip mark (canvas-only nodes) closes over record wires: everything fed
    # from a marked node has no exportable feed either, so the whole branch is left out
    # instead of degenerating into junk pipeline_N fragments. The closure is FORWARD-ONLY
    # on the record path — a skip mark means "this node and everything behind it; the
    # nodes before it still export" (marking the node after a Mix must never erase the
    # Mix). The only upstream pruning is of NON-RECORD feeders — a config value node /
    # op-value producer that fed excluded nodes exclusively through widget/op wires would
    # otherwise linger as a junk standalone entry. A node consumed via ANY record-flow
    # socket anywhere is part of the data path and never pruned. Pruning is scoped to
    # ancestors of the explicitly MARKED nodes, so feeders of collaterally-excluded
    # downstream nodes survive; target cones are immune to both rules.
    export_skipped = set(marked)
    consumers_all: Dict[str, List[str]] = {}
    record_feeders: set = set()
    for nid, pnode in pnodes.items():
        record_args = _MIX_ARGS if pnode.class_type == _MIX_CLASS_TYPE else (_RECORD_INPUT,)
        for name, (src_id, _slot) in pnode.links.items():
            consumers_all.setdefault(src_id, []).append(nid)
            if name in record_args:
                record_feeders.add(src_id)
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
            pred = pnode.links.get(_RECORD_INPUT)
            if pred is not None and pred[0] in export_skipped:
                export_skipped.add(nid)
                grew = True
                continue
            node_consumers = consumers_all.get(nid, [])
            if (
                nid in dependency_candidates
                and nid not in record_feeders
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
    # wrap even when it sits outside a target cone (the cone follows record wires only) —
    # listing it as "outside the cone" would contradict the doc that embeds it.
    for consumer, chain_list in configure_specs.items():
        if consumer not in target_excluded and consumer not in export_skipped:
            for _param, chain in chain_list:
                target_excluded.difference_update(chain.op_ids)
                target_excluded.difference_update(chain.adapter_ids)

    # op-@output → param captures (e.g. NoiseFloorOp.applied_snr_db → another op's param): the producer
    # is wrapped in the context op Capture (records the value into a Context cell) and the consumer in
    # Apply (reads the cell). Producer + consumer are record-chained (the ancestor constraint), so
    # both land inside the SAME folded Stream pipeline (see _chain_op_instance) and the per-record
    # Context carries the value between them. `captures_by_producer` is the reverse map.
    output_captures = _collect_output_captures(prompt_view, node_mappings)
    captures_by_producer: Dict[str, Dict[str, str]] = {}
    for _consumer, cap_list in output_captures.items():
        for _param, cap in cap_list:
            captures_by_producer.setdefault(cap.producer_id, {})[cap.output_name] = cap.stash_key

    # WHOLE-GRAPH "Subgraph as an Op" support: a ``Taidal_SubgraphOp`` terminator (Extract as Op)
    # has no ``TAIDAL_CALLABLE_PATH``, so on its own it would be skipped — but its ``op`` output
    # (consumed by e.g. a ``Stream``'s ``op_N``) IS the subgraph's inner cone compiled to ONE
    # ``Pipeline``. Reuse the SHARED ops-export compiler (:func:`export._subgraph_op_instance`)
    # so the whole-graph export EMBEDS that chain in the consumer's op-list instead of dropping the
    # wire and fragmenting the inner ops into stray ``pipeline_N`` entries. All terminators share ONE
    # ``fold`` so a ``constant=True`` config (``ImpairmentsTx/RxConfig``) hoists exactly once.
    sg_prompt = _collapse_reroutes(prompt_view)  # Reroutes break the one-stream check (see helper)
    sg_notes: List[str] = []  # the cone compiler's own omission notes — surfaced in the comment block
    sg_fold = _FoldContext(sg_prompt, node_mappings, sg_notes, configure_specs, vc_excluded, output_captures)
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
            # Also drop the cone's dead-end viewer side-taps (forward record-closure) so they don't
            # fold into stray pipeline_N entries (see _forward_record_closure).
            sg_cone_excluded |= _forward_record_closure(cone, sg_prompt)
        except Exception as e:  # pragma: no cover - defensive: fall back to skip-with-comment
            logger.warning(
                f"workflow_to_yaml: could not compile Subgraph-Op {tid} into a Pipeline "
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
                    compute.append(_marker(Target, mcls, mkwargs))
                compute += [_marker(Target, FormulaOp, {"formula": f, "var": v}) for f, v in chain.formulas]
                marker = _marker(
                    Target if eager else Target, ConfigureOp, {"ops": compute, "target": marker, "param": param}
                )
            except Exception as e:  # pragma: no cover - defensive: chain members are plain ops
                logger.warning(
                    f"workflow_to_yaml: could not compile the value chain for {nid}.{param} "
                    f"({type(e).__name__}: {e}); the param keeps its default."
                )
        for param, _cap in output_captures.get(nid, []):
            # An @output -> param wire is `bind:` grammar in a flow document; a flat ops list
            # has no way to carry it now that the per-record cell plane is gone.
            dropped_wires.append((f"{nid}", param, "@output capture", "export as a flow: document"))
        return marker

    # Recover a Fluid spec per node + which output slot each consumer wired (for object lazy/instance).
    # Non-exportable nodes are collected (not fatal) and surfaced as YAML comments below.
    specs: Dict[str, Tuple[type, bool, Dict[str, Any]]] = {}
    skipped: List[Tuple[str, str, str]] = []  # (node_id, class_type, reason)
    missing: List[Tuple[str, str]] = []  # (node_id, class_type) — a Taidal node THIS install lacks
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
            logger.debug(f"workflow_to_yaml: node {nid} ({pnode.class_type}) compiled into a Subgraph-Op Pipeline.")
        elif nid in subgraph_op_instances:
            logger.debug(f"workflow_to_yaml: Subgraph-Op {nid} compiled to a Pipeline (embedded in its consumer).")
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
        elif node_cls is None and pnode.class_type.startswith(_TAIDAL_NODE_PREFIX):
            # A Taidal node this installation does not register. See _missing_node_refusal.
            missing.append((nid, pnode.class_type))
        elif node_cls is None:
            logger.debug(f"workflow_to_yaml: skipping node {nid} ({pnode.class_type}) — native ComfyUI node.")
            skipped.append((nid, pnode.class_type, "native ComfyUI node"))
        else:
            logger.debug(f"workflow_to_yaml: skipping node {nid} ({pnode.class_type}) — canvas-only adapter.")
            skipped.append((nid, pnode.class_type, "canvas-only Taidal adapter (no Confluid form)"))

    if missing:
        raise UnsupportedTopologyError(_missing_node_refusal(missing))

    # The WIRED OUTPUT SLOT is authoritative for an object node's eager/lazy-ness: a consumer that
    # pulled the `lazy` output (slot 1) → !lazy:; the `instance` output (slot 0) → !class: (eager).
    # (An unconsumed object node keeps the class's own `lazy` mark as its default from _build_fluid.)
    for pnode in pnodes.values():
        for _name, (src_id, src_slot) in pnode.links.items():
            if src_id not in specs:
                continue
            src_node_cls = node_mappings.get(pnodes[src_id].class_type)
            if getattr(src_node_cls, "TAIDAL_NODE_KIND", None) == "object":
                cls, _is_lazy, kw = specs[src_id]
                specs[src_id] = (cls, src_slot == 1, kw)

    # Fold record-chained ops into Stream pipeline entries so the canvas WIRING survives in the
    # YAML: each chain becomes `pipeline: !class:Stream` with `source: !ref:<source>` (traced
    # through a skipped adapter like Walk) and an ordered `ops:` list. Fan-out is NOT folded: the
    # `Save`/`Use` cell plane it once used was deleted with the context ops, and a branchy graph
    # belongs in the flow export instead.
    chain_specs, chain_heads, op_succs = _collect_op_chains(pnodes, specs, node_mappings, output_captures)

    names = _assign_ref_names(pnodes, specs, node_mappings)

    def _node_reference(src_id: str, src_slot: int) -> Any:
        """The VALUE a wire from ``src_id``'s output ``src_slot`` becomes: a ``!ref:`` or a literal.

        Always the WHOLE node — ``!ref:<name>``. A dotted ``!ref:<name>.<output>`` used to be
        emitted for a value node's declared ``@output``s, and it no longer resolves: attribute
        references were REMOVED from the config layer (architecture record 19), so such a
        document raises ``ConfigurationError`` the moment anything loads it. The replacement,
        which that error itself names, is a selector parameter on the CONSUMER: it takes the
        whole object and reads the attribute it wants.

        A value node's ``object`` output IS the node, so it maps to the whole-node ref cleanly.
        A wire from any OTHER value output is asking for an attribute, which cannot be spelled —
        :func:`_refuse_attribute_wire` refuses it here, by name, rather than writing a document
        that cannot be opened.
        """
        src_cls = node_mappings.get(pnodes[src_id].class_type)
        return_names = tuple(getattr(src_cls, "RETURN_NAMES", ()) or ())
        wired_output = return_names[src_slot] if src_slot < len(return_names) else None
        # Decided by asking the CLASS, never by slot index: a declared ``@output`` is an
        # ATTRIBUTE of the node, which no reference can name, so its value is FOLDED into the
        # document as a literal. Every other socket stands for the node itself (`source`, `op`,
        # `object`) or for data flow (`record`), and keeps the whole-node reference. Indexing
        # got this wrong twice — a value node's slot 0 is an @output, and an op node's `op` is not.
        if wired_output is not None and wired_output in _declared_output_names(src_cls):
            return _fold_output_literal(src_id, wired_output, src_cls)
        return Reference(names[src_id])

    def _fold_output_literal(src_id: str, output_name: str, src_cls: Any) -> Any:
        """Evaluate a wired ``@output`` HERE and inline its value.

        A document references a NODE, never one of its attributes — attribute references were
        removed from the config layer, so ``!ref:src.class_names`` is refused the moment anything
        loads the file. But the wire the user drew is the obvious one (a source's class names into
        the field that names the classes), so it is honoured by reading the value at export and
        writing it as a literal: the document then carries ``names: ['0', '1', …]``, which loads
        anywhere and says plainly what the graph delivers.

        The producer is built with its OWN wires resolved, not from its widgets alone. That
        distinction is the whole correctness of this: a node whose value depends on an input —
        a class-name walker reading a source — evaluated from widgets alone returns its EMPTY
        default, and the document is written happily with the wrong answer. Silently. Building
        the graph behind it is what makes the folded value the one the canvas actually describes.
        """
        try:
            value = getattr(_build_node_object(src_id, set()), output_name)
        except Exception as exc:
            raise UnsupportedTopologyError(
                f"{names[src_id]!r}: its {output_name!r} output is wired into another node's "
                f"parameter, and its value could not be read to inline it ({type(exc).__name__}: "
                f"{exc}). A config document references a NODE, not one of its attributes — wire "
                f"the node's first output instead, into a parameter that takes the whole node."
            ) from exc
        return list(value) if isinstance(value, (list, tuple)) else value

    def _build_node_object(node_id: str, building: "Set[str]") -> Any:
        """The live object a node stands for, with its wired inputs resolved first.

        ``building`` carries the ids already under construction, so a cycle raises here instead
        of recursing until the stack gives out.
        """
        if node_id in building:
            raise UnsupportedTopologyError(f"{names.get(node_id, node_id)!r}: its inputs form a cycle")
        building = building | {node_id}
        pnode = pnodes[node_id]
        cls, _lazy, kwargs = _build_fluid(pnode, node_mappings)
        for input_name, (wired_id, wired_slot) in pnode.links.items():
            if input_name == _RECORD_INPUT:  # data flow, not configuration
                continue
            wired_cls = node_mappings.get(pnodes[wired_id].class_type)
            wired_names = tuple(getattr(wired_cls, "RETURN_NAMES", ()) or ())
            wired_output = wired_names[wired_slot] if wired_slot < len(wired_names) else None
            if wired_output is not None and wired_output in _declared_output_names(wired_cls):
                kwargs[input_name] = getattr(_build_node_object(wired_id, building), wired_output)
            else:
                kwargs[input_name] = _build_node_object(wired_id, building)
        return cls(**kwargs)

    def _op_value_for(src_id: str, src_slot: int) -> Any:
        """The value for a SINGLE op-slot wire (``op``/``target``): a Subgraph-Op terminator
        embeds its compiled ``Pipeline`` inline; every other source is a plain/dotted
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
            base = _strip_index_for(cls, input_name)
            if base == _RECORD_INPUT:
                # The op-chaining data-flow socket — never a constructor kwarg (the chain
                # structure is captured by the folded Stream pipeline entries below).
                continue
            if base in configured:
                continue  # injected per record by the ConfigureOp wrap
            if src_id not in specs:
                if src_id in subgraph_op_instances:
                    # A Subgraph-Op terminator feeding this op slot → embed its compiled
                    # Pipeline inline (not dropped); _op_value_for resolves it below.
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
                sequential = isinstance(cls, type) and (
                    (base == "ops" and issubclass(cls, Stream)) or (base == "transforms" and issubclass(cls, Pipeline))
                )
                value_list: List[Any] = []
                for _n, src, slot in ordered:
                    if sequential and src in subgraph_op_instances:
                        # A sequential op-list (Stream ``ops`` / Pipeline ``transforms``) makes a
                        # nested ``Pipeline`` equivalent to its transforms SPLICED inline — and
                        # inline Target ops materialize cleanly via ``DatasetProcessor.run()``'s
                        # per-op ``flow()``, whereas a nested deferred ``Pipeline`` marker left in
                        # ``stream.ops`` does NOT (the Stream rejects it when it iterates ``ops``).
                        # Fan-out containers (Parallel) keep the single embedded chain.
                        value_list.extend(subgraph_op_instances[src].kwargs.get("transforms", []))
                    else:
                        value_list.append(_op_value_for(src, slot))
                kwargs[base] = value_list
        # _marker: kwargs set post-construction — a param literally named `target` (ConfigureOp)
        # collides with the Fluid ctor's own first parameter.
        fluids[nid] = _wrap_configured(nid, _marker(PartialClass if is_lazy else Target, cls, kwargs), eager=False)

    def _chain_op_instance(nid: str, chain_key: str) -> Target:
        """An inline ``!class:Op()`` Target for a chained op, with non-record wires resolved."""
        cls, _lazy, kwargs = chain_specs[nid]
        kwargs = dict(kwargs)
        configured = {param for param, _chain in configure_specs.get(nid, [])}
        configured |= {param for param, _cap in output_captures.get(nid, [])}  # capture wraps inject these
        list_params = _op_list_params(cls)
        wired_lists: Dict[str, List[Tuple[str, str, int]]] = {}
        for input_name, (src_id, src_slot) in pnodes[nid].links.items():
            base = _strip_index_for(cls, input_name)
            if base == _RECORD_INPUT:
                continue  # the chain itself — expressed by the ops-list order
            if base in configured:
                continue  # injected per record by the ConfigureOp wrap
            if src_id in specs:
                referenced.add(src_id)
                if base in list_params and input_name != base:
                    # A dynamic op_N fan-in on a CHAINED op: every wire joins the list,
                    # ordered by slot index below — the standalone path already did this,
                    # but a per-wire kwargs assignment here kept only the LAST wire
                    # (measured: a chained ConfigureOp drawn with two compute ops exported
                    # `ops: {_ref_: <second>}`, orphaning the first and failing the load).
                    wired_lists.setdefault(base, []).append((input_name, src_id, src_slot))
                else:
                    kwargs[base] = _node_reference(src_id, src_slot)
            else:
                literal = _primitive_literal(src_id, pnodes)
                if literal is not _MISSING:
                    kwargs[base] = literal
                    continue
                src_type = pnodes[src_id].class_type if src_id in pnodes else "unknown"
                dropped_wires.append((f"{chain_key}[{cls.__name__}]", base, src_id, src_type))
        for base, items in wired_lists.items():
            ordered = sorted(items, key=lambda it: _index_of(it[0]))
            kwargs[base] = [_node_reference(src, slot) for _name, src, slot in ordered]
        instance = _wrap_configured(nid, _marker(Target, cls, kwargs), eager=True)
        return cast(Target, instance)

    def _fold_chain(nid: str, chain_key: str) -> List[Any]:
        """Linearize a STRAIGHT chain into inline op Instances.

        A `Stream`'s ``ops`` list is a sequence, so only a straight run folds into one. A
        fork used to become ``Save``/``Use`` context cells; those ops were deleted with the
        lowering pass (2026-07-30), so a forking chain simply stops folding here and its
        branches stay standalone entries — the whole-graph YAML keeps every op, and a user
        who wants the branch wiring in one document exports the canvas as a `flow:` document.
        """
        instance = _chain_op_instance(nid, chain_key)
        children = sorted(op_succs.get(nid, []), key=_id_sort_key)
        if len(children) == 1:
            return [instance, *_fold_chain(children[0], chain_key)]
        if len(children) > 1:
            logger.debug(
                f"graphio: node {nid} fans out to {children} — the pipeline fold stops here "
                "(an ops list cannot express a fork; export as a flow: document for the wiring)."
            )
        return [instance]

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
        stream_kwargs: Dict[str, Any] = {"ops": _fold_chain(head, key)}
        check_op_chain(stream_kwargs["ops"], where=key)  # the contract, where the chain is drawn
        if source_nid is not None:
            referenced.add(source_nid)
            stream_kwargs["source"] = Reference(names[source_nid])
        chain_entries.append((key, Target(Stream, **stream_kwargs)))

    document = _hoist_document(fluids, names, referenced, specs)
    # `constant=True` configs hoisted while compiling the Subgraph-Op Pipeline(s) — emit them
    # as top-level entries so the nested chain's dotted `!ref:<name>.<output>` consumers resolve
    # against the same document (the ops-export shape).
    for cname, cval in sg_fold.constants.items():
        document.setdefault(cname, cval)
    for key, value in chain_entries:  # pipelines last — they are the roots of the config
        document[key] = value
    return _skipped_comment_block(skipped, dropped_wires, sg_notes) + _confluid_dump(document)


def _collect_op_chains(
    pnodes: Dict[str, "_PNode"],
    specs: Dict[str, Tuple[type, bool, Dict[str, Any]]],
    node_mappings: Dict[str, Any],
    output_captures: Optional[Dict[str, List[Tuple[str, Any]]]] = None,
) -> Tuple[Dict[str, Tuple[type, bool, Dict[str, Any]]], List[Tuple[str, Optional[str]]], Dict[str, List[str]]]:
    """Identify record-chained op nodes and POP them out of ``specs`` for Stream folding.

    A chain is a maximal run of op-kind nodes connected by their ``record`` sockets. Each
    chain folds into ONE ``Stream`` pipeline entry, so its ops must not also appear as
    standalone top-level entries. Returns ``(chain_specs, chain_heads, op_succs)`` where
    ``chain_heads`` is ``[(head_id, source_id-or-None), …]`` in deterministic head order —
    the source is traced through ONE skipped boundary node (the Walk adapter pattern:
    op.record ← Walk ← source) or taken directly when the head's feed is a source node.

    NOT folded (stay standalone entries): an op with no record wiring at all (a configured
    op-value), and any chain containing an op consumed as a ``RECORDSTREAM_OP`` value by an
    exported node (an ``op_N`` list input — those wires reference top-level entries).
    """

    def _kind(nid: str) -> Optional[str]:
        return getattr(node_mappings.get(pnodes[nid].class_type), "TAIDAL_NODE_KIND", None)

    op_ids = {nid for nid in specs if _kind(nid) == "op"}
    op_succs: Dict[str, List[str]] = {nid: [] for nid in op_ids}
    heads: List[str] = []
    for nid in op_ids:
        pred = pnodes[nid].links.get(_RECORD_INPUT)
        if pred is not None and pred[0] in op_ids:
            op_succs[pred[0]].append(nid)
        else:
            heads.append(nid)

    # An op pulled by its RECORDSTREAM_OP value output into an exported node (op_N lists of a
    # higher-order op / engine) is referenced BY NAME — its chain cannot fold away.
    captures = output_captures or {}
    consumed_as_value: set = set()
    for cnid, pnode in pnodes.items():
        if cnid not in specs:
            continue
        captured_params = {param for param, _cap in captures.get(cnid, [])}
        consumer_cls = _op_class_of(node_mappings.get(pnode.class_type))
        for input_name, (src_id, _slot) in pnode.links.items():
            base = _strip_index_for(consumer_cls, input_name)
            if base == _RECORD_INPUT or src_id not in op_ids:
                continue
            # A capture wire (op-@output → param) is NOT an op-value reference: the producer stays
            # folded in the pipeline (the context Capture cell carries the value to the
            # consumer's Apply). Only true RECORDSTREAM_OP value wires force a standalone entry.
            if base in captured_params:
                continue
            consumed_as_value.add(src_id)

    def _members(head: str) -> List[str]:
        out, stack, seen = [], [head], set()
        while stack:
            nid = stack.pop()
            if nid in seen:  # pragma: no cover - defensive: single record socket → a forest
                continue
            seen.add(nid)
            out.append(nid)
            stack.extend(op_succs[nid])
        return out

    def _trace_source(head: str) -> Optional[str]:
        pred = pnodes[head].links.get(_RECORD_INPUT)
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
        has_record_edge = bool(op_succs[head]) or pnodes[head].links.get(_RECORD_INPUT) is not None
        if not has_record_edge or any(m in consumed_as_value for m in members):
            continue  # stays a standalone entry
        chain_heads.append((head, _trace_source(head)))
        for member in members:
            chain_specs[member] = specs.pop(member)
    return chain_specs, chain_heads, op_succs


def _skipped_comment_block(
    skipped: List[Tuple[str, str, str]],
    dropped_wires: List[Tuple[str, str, str, str]],
    fold_notes: Optional[List[str]] = None,
) -> str:
    """Render the skipped-node / dropped-wire summary as a leading ``#`` comment block.

    Comments are invisible to ``confluid.load`` / any YAML parser, so the document below
    stays a normal, runnable config — the block only tells the user what the export left out
    and why. Empty string when nothing was skipped (the all-Taidal export is byte-identical
    to before). ``fold_notes`` carries the ops-compiler's own omission notes (a wire inside a
    Subgraph-Op cone that could not fold) — already fully worded, so they render verbatim;
    without this the cone's drops were invisible in the whole-graph export, and a canvas that
    LOOKED wired exported an op silently missing a param.
    """
    if not skipped and not dropped_wires and not fold_notes:
        return ""
    lines: List[str] = []
    if skipped:
        lines.append("# NOTE: the canvas contained node(s) with no Confluid YAML form; they were left out:")
        for nid, class_type, reason in sorted(skipped, key=lambda s: _id_sort_key(s[0])):
            lines.append(f"#   - node {nid} ({class_type}): {reason}")
    if dropped_wires:
        lines.append("# Wires from skipped nodes into exported config members were dropped")
        lines.append("# (each member falls back to its default):")
        for consumer, input_name, src_id, src_type in dropped_wires:
            lines.append(f"#   - {consumer}.{input_name} <- node {src_id} ({src_type})")
    for note in fold_notes or []:
        lines.append(f"# NOTE: {note}")
    return "\n".join(lines) + "\n"


def _assign_ref_names(
    pnodes: Dict[str, "_PNode"],
    specs: Dict[str, Tuple[type, bool, Dict[str, Any]]],
    node_mappings: Dict[str, Any],
) -> Dict[str, str]:
    """Assign each node a stable top-level YAML key: the param it feeds, else its class name.

    On collision, disambiguate with a numeric suffix (``model_2``). (Subgraph path-prefixing is
    applied by :mod:`streamstudio.subgraphio` when nodes are nested.)
    """
    # Preferred base name = the CONFIG input socket this node feeds (model / train_set / …) —
    # only on an exported consumer: a skipped node's sockets (a YamlDump `object`, a PreviewAny
    # `source`, a Math `a`) are UI plumbing, not config members, and would produce junk names.
    # The op-chaining ``record`` socket is data-flow, not a config member — naming an op
    # ``record`` after it would be meaningless, so those wires don't contribute a name either.
    # A VALUE node keeps its class name too: its entry is the CONFIG OBJECT, not the @output
    # value it feeds (``tx_disabled: !class:ImpairmentsTxConfig`` would read as the tuple).
    fed_as: Dict[str, str] = {}
    for nid, pnode in pnodes.items():
        if nid not in specs:
            continue
        consumer_op_cls = _op_class_of(node_mappings.get(pnode.class_type))
        for input_name, (src_id, _slot) in pnode.links.items():
            base = _strip_index_for(consumer_op_cls, input_name)
            if base == _RECORD_INPUT:
                continue
            src_cls = node_mappings.get(pnodes[src_id].class_type) if src_id in pnodes else None
            if getattr(src_cls, "TAIDAL_NODE_KIND", None) == "value":
                continue
            if (
                base in _OP_LIST_PARAMS
                or base in _SOURCE_LIST_PARAMS
                or (consumer_op_cls is not None and base in _op_list_params(consumer_op_cls))
            ):
                # A node feeding a LIST param (ops / branches / transforms / streams / sources, OR a
                # stamped op-list like ``true_transforms``) must NOT be NAMED after it: confluid
                # broadcasts a top-level key back INTO any class param of the same name, so a top-level
                # ``ops:`` (one op) would clobber the Stream's explicitly-wired ``ops:`` list — the whole
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
        # bind key, so the exported YAML is directly runnable (`recordstream run …`).
        role = marks(cls).role
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
    """Map a dynamic list-socket name back to its underlying list PARAM name (fixed name map).

    ``op_0`` → ``ops`` / ``source_3`` → ``streams`` (the socket prefix differs from the param name);
    a generic ``<name>_<digit>`` (object-list members ``logger_0`` / ``callbacks_0``, where the socket
    prefix IS the param name) → ``<name>``; a non-indexed socket (``model`` / ``train_set``) is
    returned unchanged. So the exporter collapses ``logger_0`` / ``logger_1`` back into one ``logger``
    list member (the inverse of the importer's object-list wiring). Prefer the CLASS-AWARE
    :func:`_strip_index_for` wherever the consuming class is at hand — the fixed ``op_N → ops`` map
    here is only the fallback (a ``Pipeline``'s op list is named ``transforms``, not ``ops``).
    """
    if name.startswith("op_") and name[3:].isdigit():
        return "ops"
    if name.startswith("source_") and name[7:].isdigit():
        return "streams"
    head, _, tail = name.rpartition("_")
    if head and tail.isdigit():
        return head
    return name


def _strip_index_for(cls: Any, name: str) -> str:
    """Target-aware :func:`_strip_index`: collapse a dynamic op-list socket into the CONSUMING class's
    REAL list param.

    Resolves the socket-family prefix through the same authority the bridge uses for op-list
    params (``bridge._op_list_params`` — a reverse prefix→param lookup): a ``Pipeline`` node's
    ``op_N`` collapses into ``transforms``, a ``Stream``'s into ``ops``, a stamped op-list family
    (``true_transforms_N``) into its own param. Falls back to the fixed name map
    (:func:`_strip_index`) when ``cls`` is unknown (a non-Taidal node) or the prefix is not one
    of its op-list families (``source_N`` → ``streams``, object lists, plain sockets).
    """
    head, _, tail = name.rpartition("_")
    if cls is not None and head and tail.isdigit():
        for param, prefix in _op_list_params(cls).items():
            if head == prefix:
                return param
    return _strip_index(name)


def _index_of(name: str) -> int:
    tail = name.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def _scalar_of(value: Any) -> Any:
    return value


__all__ = [
    "yaml_to_workflow",
    "flow_yaml_to_workflow",
    "workflow_to_yaml",
    "extract_ops_workflow",
    "ui_workflow_to_prompt",
]
