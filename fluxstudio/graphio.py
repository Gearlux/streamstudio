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
  output shape, e.g. ``waivefront-rfuav/config/train_efficientvit_b0.yaml``), and serialises
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

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union, cast

from confluid import Class, Instance, LazyClass, Reference
from confluid import dump as _confluid_dump
from confluid import get_configurable_attrs
from confluid import load as _confluid_load
from confluid import resolve as _confluid_resolve
from confluid.registry import resolve_class
from dataflux.discovery import resolve_callable
from logflow import get_logger

from fluxstudio.bridge import _OP_LIST_PARAMS, _SOURCE_LIST_PARAMS, OBJECT_TYPE, OP_TYPE, SOURCE_TYPE, _coerce_kwargs
from fluxstudio.export import UnsupportedTopologyError, _is_link

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

_NODE_INDEX_CACHE: Dict[int, Dict[type, str]] = {}


def _node_index(node_mappings: Dict[str, Any]) -> Dict[type, str]:
    """Reverse index ``{configurable class -> ComfyUI node_key}`` built from the node mappings.

    Each generated node class stamps ``TAIDAL_CALLABLE_PATH`` (an importable ``module:qualname``);
    ``resolve_callable`` turns it back into the class, giving us the class→key direction needed to
    emit a workflow node for a Fluid marker. Cached per ``node_mappings`` identity.
    """
    cache_key = id(node_mappings)
    cached = _NODE_INDEX_CACHE.get(cache_key)
    if cached is not None:
        return cached
    index: Dict[type, str] = {}
    for key, node_cls in node_mappings.items():
        path = getattr(node_cls, "TAIDAL_CALLABLE_PATH", None)
        if not path:
            continue
        try:
            target = resolve_callable(path)
        except (ImportError, AttributeError, ValueError):  # pragma: no cover - defensive
            continue
        if isinstance(target, type):
            index[target] = key
    _NODE_INDEX_CACHE[cache_key] = index
    return index


def _node_key_for_class(cls: type, node_mappings: Dict[str, Any]) -> Optional[str]:
    """The ComfyUI node_key registered for ``cls``, or ``None`` if it has no FluxStudio node."""
    return _node_index(node_mappings).get(cls)


def _target_and_kwargs(obj: Any) -> Optional[Tuple[type, bool, Dict[str, Any]]]:
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

    Handles ``Flux.ops`` / ``JointFlux.fluxes`` (list of nodes) and ``Tee.branches``
    (list of single-op lists — flattened one level for v1, matching the bridge convention).
    """
    if not isinstance(value, (list, tuple)) or not value:
        return None
    out: List[Any] = []
    for item in value:
        if _is_node_value(item):
            out.append(item)
        elif isinstance(item, (list, tuple)) and len(item) == 1 and _is_node_value(item[0]):
            out.append(item[0])  # Tee.branches: single-op branch → the op
        else:
            return None  # not a uniform list-of-nodes
    return out


def _widget_and_socket_params(node_cls: Any) -> Tuple[List[str], Dict[str, str]]:
    """Split a node's ``INPUT_TYPES`` into ordered widget names and ``{socket_name: socket_type}``.

    Widget order is the declaration order (required then optional) — the same order the
    positional ``widgets_values`` array uses. Socket entries are those whose ComfyUI type is a
    wire type (``DATAFLUX_*`` / ``HF_DATASET``); everything else (scalars, ``Literal`` combos) is
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
            # (``DATAFLUX_OBJECT:classification_model``) is still recognised as a wired socket,
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
# Export: workflow.json → YAML (hoisted-!ref: shape)
# =======================================================================================


def _inline_subgraphs(graph: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten ``definitions.subgraphs`` instances into the top-level node set for export.

    Each subgraph instance node is replaced by the subgraph's internal nodes (ids remapped so they
    can't collide with top-level nodes) and internal links, dropping the ``inputNode``/``outputNode``
    (id ``-10`` / ``-20``) boundary links. So a workflow saved as a subgraph round-trips back to the
    same flat hoisted YAML. (Standalone-reusable ``include:`` files are a documented follow-up.)
    """
    subgraphs = {sg["id"]: sg for sg in graph.get("definitions", {}).get("subgraphs", [])}
    if not subgraphs:
        return graph
    plain = [n for n in graph.get("nodes", []) if n["type"] not in subgraphs]
    next_id = max((n["id"] for n in plain), default=0)
    out_nodes: List[Dict[str, Any]] = list(plain)
    out_links: List[List[Any]] = list(graph.get("links", []))
    for node in graph.get("nodes", []):
        sg = subgraphs.get(node["type"])
        if sg is None:
            continue
        idmap: Dict[int, int] = {}
        for inode in sg.get("nodes", []):
            next_id += 1
            idmap[inode["id"]] = next_id
            out_nodes.append({**inode, "id": next_id})
        for link in sg.get("links", []):
            if link[1] == -10 or link[3] == -20:  # boundary link — drop
                continue
            out_links.append(
                [link[0], idmap.get(link[1], link[1]), link[2], idmap.get(link[3], link[3]), link[4], link[5]]
            )
    return {**graph, "nodes": out_nodes, "links": out_links, "definitions": {"subgraphs": []}}


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
    __slots__ = ("class_type", "widgets", "links")

    def __init__(self, class_type: str) -> None:
        self.class_type = class_type
        self.widgets: Dict[str, Any] = {}
        self.links: Dict[str, Tuple[str, int]] = {}  # input_name -> (src_node_id, src_slot)


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
        node_cls = node_mappings.get(class_type)
        if node_cls is not None:
            widget_names, _ = _widget_and_socket_params(node_cls)
            for name, value in zip(widget_names, node.get("widgets_values", []) or []):
                pnode.widgets[name] = value
        for inp in node.get("inputs", []) or []:
            link_id = inp.get("link")
            if link_id is not None and link_id in link_src:
                pnode.links[inp["name"]] = link_src[link_id]
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


def workflow_to_yaml(graph: Dict[str, Any], node_mappings: Dict[str, Any]) -> str:
    """Convert a ComfyUI workflow (UI or prompt form) into a hoisted-``!ref:`` Confluid YAML.

    Every node becomes a top-level YAML key (named by the socket/param it feeds, path-prefixed
    on collision); a wire becomes a ``!ref:<name>`` in the consumer's kwargs. ``num_classes`` /
    ``image_size`` are hoisted to top-level vars. The result is serialised with
    :func:`confluid.dump` and matches the navigaitor / reference config shape.
    """
    pnodes = _normalize_workflow(graph, node_mappings)
    if not pnodes:
        return _confluid_dump({})

    # Recover a Fluid spec per node + which output slot each consumer wired (for object lazy/instance).
    specs: Dict[str, Tuple[type, bool, Dict[str, Any]]] = {}
    for nid, pnode in pnodes.items():
        if getattr(node_mappings.get(pnode.class_type), "TAIDAL_CALLABLE_PATH", None):
            specs[nid] = _build_fluid(pnode, node_mappings)

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

    names = _assign_ref_names(pnodes, specs, node_mappings)

    # Build a Fluid marker per node, wiring links as References and list-params as Reference lists.
    fluids: Dict[str, Any] = {}
    referenced: set = set()
    for nid, (cls, is_lazy, kwargs) in specs.items():
        pnode = pnodes[nid]
        wired: Dict[str, List[Tuple[str, str]]] = {}
        for input_name, (src_id, _slot) in pnode.links.items():
            if src_id not in specs:
                continue
            referenced.add(src_id)
            base = _strip_index(input_name)
            wired.setdefault(base, []).append((input_name, src_id))
        for base, items in wired.items():
            if len(items) == 1 and items[0][0] == base:
                kwargs[base] = Reference(names[items[0][1]])
            else:  # dynamic op_N / source_N → a list, ordered by slot index
                ordered = sorted(items, key=lambda it: _index_of(it[0]))
                kwargs[base] = [Reference(names[src]) for _n, src in ordered]
        fluids[nid] = LazyClass(cls, **kwargs) if is_lazy else Class(cls, **kwargs)

    document = _hoist_document(fluids, names, referenced, specs)
    return _confluid_dump(document)


def _assign_ref_names(
    pnodes: Dict[str, "_PNode"],
    specs: Dict[str, Tuple[type, bool, Dict[str, Any]]],
    node_mappings: Dict[str, Any],
) -> Dict[str, str]:
    """Assign each node a stable top-level YAML key: the param it feeds, else its class name.

    On collision, disambiguate with a numeric suffix (``model_2``). (Subgraph path-prefixing is
    applied by :mod:`fluxstudio.subgraphio` when nodes are nested.)
    """
    # Preferred base name = the consumer input socket this node feeds (model / train_set / …).
    fed_as: Dict[str, str] = {}
    for pnode in pnodes.values():
        for input_name, (src_id, _slot) in pnode.links.items():
            if src_id in specs and src_id not in fed_as:
                fed_as[src_id] = _strip_index(input_name)
    names: Dict[str, str] = {}
    used: Dict[str, int] = {}
    for nid in specs:
        cls = specs[nid][0]
        # A runnable root is named by its confluid role (trainer / evaluator) — the marainer CLI
        # bind key, so the exported YAML is directly runnable (`marainer train -c …`).
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


def _snake(class_name: str) -> str:
    out = []
    for i, ch in enumerate(class_name):
        if ch.isupper() and i > 0:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def _scalar_of(value: Any) -> Any:
    return value


__all__ = ["yaml_to_workflow", "workflow_to_yaml"]
