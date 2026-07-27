"""Regression tests for the op-graph export, using the REAL ``impairments.json`` as the test-bed.

This is the exact workflow that exposed the export bugs, so it is the right fixture to pin them:

* it routes the record THROUGH 6 ComfyUI **subgraph instances** (a naive op-only filter severed it);
* it feeds the two ``ImpairmentsChain`` ops from ``value``-kind **config producers**
  (``ImpairmentsTxConfig`` / ``ImpairmentsRxConfig``) — which a value-blind filter dropped;
* it hangs **dead-end viewer/analysis taps** (``RenderSignalPlotOp`` / ``RegionPowerAnalysisOp``
  inside "Spectral Analysis" subgraphs) off the pipeline; and
* it carries an **export-target mark** on a ``RegionPowerAnalysisOp`` inside a subgraph — the same
  mark the YAML ops-export uses, which the graph export must honor (restricting to the cone).

The node *kinds* (op / value / source) are baked in ``_NODE_KINDS`` so the test is self-contained and
runs without the optional ``[waivefront]`` extra installed — it exercises ``graphio``'s extraction
logic, for which the kinds are a known input. ``test_baked_node_kinds_match_real_discovery`` guards
the baked map against drift whenever the real classes ARE importable.
"""

import json
from pathlib import Path

import pytest

from streamstudio.export import collect_export_marks
from streamstudio.graphio import extract_ops_workflow

_FIXTURE = Path(__file__).parent / "fixtures" / "impairments.json"

# The classification streamstudio's discovery assigns to each Taidal node type in the fixture. Baked so
# the test needs no optional packages; ``Taidal_MixRecords`` is handled by ``export._MIX_CLASS_TYPE``
# (not by kind), and native / third-party nodes (Reroute, PreviewImage, subgraph-instance UUIDs) are
# deliberately absent → classified as non-op and dropped/contracted.
_NODE_KINDS = {
    "Taidal_recordstream_op_image_ConvertToImageOp": "op",
    "Taidal_recordstream_op_numpy_ConnectedComponentsOp": "op",
    "Taidal_recordstream_op_numpy_MaxOp": "op",
    "Taidal_recordstream_op_numpy_ThresholdOp": "op",
    "Taidal_recordstream_op_structure_StashInputOp": "op",
    "Taidal_recordstream_op_torch_SqueezeOp": "op",
    "Taidal_waivefront_op_noise_NoiseFloorOp": "op",
    "Taidal_waivefront_op_regions_BboxesToRegionsOp": "op",
    "Taidal_waivefront_op_regions_RegionPowerAnalysisOp": "op",
    "Taidal_waivefront_op_signal_CropSignalLengthOp": "op",
    "Taidal_waivefront_op_signal_ImpairmentsChain": "op",
    "Taidal_waivefront_op_signal_ImpairmentsRxConfig": "value",
    "Taidal_waivefront_op_signal_ImpairmentsTxConfig": "value",
    "Taidal_waivefront_op_spectrogram_MagnitudeOp": "op",
    "Taidal_waivefront_op_spectrogram_SpectrogramOp": "op",
    "Taidal_waivefront_op_visualize_RenderOverlaysOp": "op",
    "Taidal_waivefront_op_visualize_RenderSignalPlotOp": "op",
    "Taidal_waivefront_source_HeliosFileSource": "source",
}


def _mappings() -> dict:
    """NODE_CLASS_MAPPINGS-shaped dict whose classes carry the baked ``TAIDAL_NODE_KIND``."""
    return {t: type("FakeNode", (), {"TAIDAL_NODE_KIND": k}) for t, k in _NODE_KINDS.items()}


def _load() -> dict:
    data: dict = json.loads(_FIXTURE.read_text())
    return data


def _strip_marks(workflow: dict) -> None:
    """Remove every ``taidal_export`` property (top-level AND subgraph-inner), in place."""
    for node in workflow.get("nodes") or []:
        (node.get("properties") or {}).pop("taidal_export", None)
    for definition in (workflow.get("definitions") or {}).get("subgraphs", []):
        for node in definition.get("nodes") or []:
            (node.get("properties") or {}).pop("taidal_export", None)


def _types(graph: dict) -> list:
    """The verbatim-callable tail of each kept node's type (e.g. ``ImpairmentsChain``)."""
    return [str(n["type"]).split("_")[-1] for n in graph["nodes"]]


def _connectivity(graph: dict) -> tuple:
    """Return (isolated_node_ids, dangling_input_link_count, weakly_connected_component_count).

    The component count is the strong check — ``isolated`` alone misses a graph SPLIT into two
    multi-node pieces (e.g. if a connector op were wrongly dropped, breaking the chain in the middle).
    """
    ids = {n["id"] for n in graph["nodes"]}
    degree = {i: 0 for i in ids}
    undirected: dict = {i: set() for i in ids}
    link_ids = {lk[0] for lk in graph["links"]}
    for lk in graph["links"]:
        degree[lk[1]] += 1
        degree[lk[3]] += 1
        if lk[1] in undirected and lk[3] in undirected:
            undirected[lk[1]].add(lk[3])
            undirected[lk[3]].add(lk[1])
    dangling = sum(
        1
        for n in graph["nodes"]
        for inp in (n.get("inputs") or [])
        if inp.get("link") is not None and inp["link"] not in link_ids
    )
    seen: set = set()
    components = 0
    for start in ids:
        if start in seen:
            continue
        components += 1
        stack = [start]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(undirected[node] - seen)
    return [i for i, d in degree.items() if d == 0], dangling, components


# --------------------------------------------------------------------------------------------------
# fixture sanity + baked-kinds drift guard
# --------------------------------------------------------------------------------------------------


def test_fixture_carries_the_export_target_mark() -> None:
    # The whole fix hinges on honoring the target mark, so the test-bed must actually carry one
    # (on a RegionPowerAnalysisOp INSIDE a subgraph definition — collect_export_marks is subgraph-aware).
    skip_marks, target_marks = collect_export_marks(_load())
    assert target_marks, "the impairments fixture must carry an export-target mark"


def test_baked_node_kinds_match_real_discovery() -> None:
    # Guard the baked _NODE_KINDS against drifting from what discovery would actually assign. Runs only
    # where the classes are importable (the [waivefront] extra); skips cleanly otherwise.
    import streamstudio
    from streamstudio.export import _node_kind

    mappings = streamstudio.NODE_CLASS_MAPPINGS
    checked = 0
    for node_type, baked_kind in _NODE_KINDS.items():
        real_kind = _node_kind(node_type, mappings)
        if real_kind is not None:
            assert real_kind == baked_kind, f"baked kind for {node_type} ({baked_kind}) != real ({real_kind})"
            checked += 1
    if checked == 0:
        pytest.skip("no Taidal types discovered — the [waivefront] extra is not installed")


# --------------------------------------------------------------------------------------------------
# the three reported bugs, pinned on the real graph (both modes)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("flatten", [False, True])
def test_export_keeps_config_and_is_one_connected_component(flatten: bool) -> None:
    graph = extract_ops_workflow(_load(), _mappings(), flatten=flatten)
    types = _types(graph)

    # Bug 1 — the config/value producers are KEPT (was: "missing all ImpairmentsTx/RxConfig"). These
    # are top-level nodes, so the assertion holds in BOTH modes.
    assert types.count("ImpairmentsTxConfig") == 2, f"flatten={flatten}: ImpairmentsTxConfig dropped"
    assert types.count("ImpairmentsRxConfig") == 2, f"flatten={flatten}: ImpairmentsRxConfig dropped"

    # the core (top-level) signal-processing chain leading to the target survives.
    for op in ("SqueezeOp", "CropSignalLengthOp", "ImpairmentsChain", "NoiseFloorOp"):
        assert op in types, f"flatten={flatten}: {op} missing from the exported pipeline"

    # CONNECTED — exactly one weakly-connected component (the strong check: catches a mid-chain split,
    # not just orphans), no dangling link refs, and JSON-safe (the route json.dumps-es it to the browser).
    isolated, dangling, components = _connectivity(graph)
    assert isolated == [], f"flatten={flatten}: isolated nodes {isolated}"
    assert dangling == 0, f"flatten={flatten}: {dangling} dangling input link refs"
    assert components == 1, f"flatten={flatten}: graph split into {components} disconnected pieces"
    json.dumps(graph)


def test_flattened_surfaces_the_target_op_and_drops_the_viewer_taps() -> None:
    # flatten=True surfaces the inner ops: the MARKED target (RegionPowerAnalysisOp) rides in the cone,
    # while its sibling dead-end viewer taps (RenderSignalPlotOp) — outside the cone — are dropped.
    types = _types(extract_ops_workflow(_load(), _mappings(), flatten=True))
    assert "RegionPowerAnalysisOp" in types, "the marked target op must survive"
    assert "RenderSignalPlotOp" not in types, "an off-cone dead-end viewer tap leaked into the export"


def test_subgraphs_intact_keeps_in_cone_instances_and_drops_the_dead_end_taps() -> None:
    # flatten=False keeps subgraph instances as black boxes WITH their definitions. The target's
    # containing instance (#378) is kept; the two dead-end "Spectral Analysis" viewer taps the user
    # reported as "unconnected" (#342, #360 — fed by an op but feeding nothing) are off-cone → dropped.
    graph = extract_ops_workflow(_load(), _mappings(), flatten=False)
    ids = {n["id"] for n in graph["nodes"]}
    definitions = (graph.get("definitions") or {}).get("subgraphs") or []
    assert definitions, "in-cone subgraph instances must carry their (transitive) definitions"
    assert {d["id"] for d in definitions} & {str(n["type"]) for n in graph["nodes"]}, "no subgraph instance kept"
    assert 378 in ids, "the target's containing subgraph instance must be kept"
    assert 342 not in ids and 360 not in ids, "the dead-end Spectral-Analysis viewer taps must be dropped"


def test_flattened_inlines_every_subgraph_into_raw_ops() -> None:
    graph = extract_ops_workflow(_load(), _mappings(), flatten=True)
    assert "definitions" not in graph, "flattened export must leave no subgraph definitions"
    assert all(str(n["type"]).startswith("Taidal_") for n in graph["nodes"]), "a non-Taidal node leaked in"


# --------------------------------------------------------------------------------------------------
# the mark — not a hard-coded heuristic — is what scopes the export
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("flatten", [False, True])
def test_target_mark_scopes_the_export_to_its_cone(flatten: bool) -> None:
    # With the mark the export is the target's cone; stripping it falls back to the WHOLE op graph,
    # which is strictly larger — proving the mark (not a heuristic) drives the scoping.
    marked = extract_ops_workflow(_load(), _mappings(), flatten=flatten)
    unmarked_wf = _load()
    _strip_marks(unmarked_wf)
    unmarked = extract_ops_workflow(unmarked_wf, _mappings(), flatten=flatten)
    assert len(marked["nodes"]) < len(unmarked["nodes"])


def test_without_a_mark_the_flattened_export_brings_back_the_viewer_taps() -> None:
    # The dead-end RenderSignalPlot taps are dropped ONLY because they fall outside the target cone —
    # remove the mark and they reappear (the export keeps the whole op graph).
    workflow = _load()
    _strip_marks(workflow)
    graph = extract_ops_workflow(workflow, _mappings(), flatten=True)
    assert "RenderSignalPlotOp" in _types(graph)


# --------------------------------------------------------------------------------------------------
# the YAML ops round-trip — export → import keeps the config nodes CONNECTED
# --------------------------------------------------------------------------------------------------


def test_graph_export_normalizes_collapsed_widget_input_slots() -> None:
    # ComfyUI's in-memory graphToPrompt() (what the graph export reads) can represent a CONVERTED widget
    # as a single input slot and omit the unconverted widgets — so an ImpairmentsChain arrives with only
    # [record, tx_disabled, rx_disabled] (tx_disabled @slot 1), not the full 8 slots. The export must
    # NORMALISE to the full class slot list so the config link stays at its real slot index (slot 3) and
    # renders after re-import. Simulate that collapsed in-memory form and assert the export restores it.
    # Needs the real classes (INPUT_TYPES), so skips without the [waivefront] extra discovered.
    import streamstudio

    mappings = streamstudio.NODE_CLASS_MAPPINGS
    if "Taidal_waivefront_op_signal_ImpairmentsChain" not in mappings:
        pytest.skip("requires the [waivefront] extra discovered (ImpairmentsChain not registered)")

    workflow = _load()
    # collapse every ImpairmentsChain to ONLY its wired inputs (the in-memory shape), keeping links self-
    # consistent by renumbering target slots to the collapsed positions.
    for node in workflow["nodes"]:
        if "ImpairmentsChain" not in str(node["type"]):
            continue
        kept = [(idx, inp) for idx, inp in enumerate(node["inputs"]) if inp.get("link") is not None]
        old_to_new = {old: new for new, (old, _inp) in enumerate(kept)}
        node["inputs"] = [inp for _old, inp in kept]
        for link in workflow["links"]:
            if link[3] == node["id"] and link[4] in old_to_new:
                link[4] = old_to_new[link[4]]
    # sanity: the collapse worked (3-input, tx_disabled now at slot 1)
    collapsed = next(n for n in workflow["nodes"] if "ImpairmentsChain" in str(n["type"]))
    assert len(collapsed["inputs"]) == 3 and collapsed["inputs"][1]["name"] == "tx_disabled"

    graph = extract_ops_workflow(workflow, mappings, flatten=False)
    nodes = {n["id"]: n for n in graph["nodes"]}
    links = {lk[0]: lk for lk in graph["links"]}
    chains = [n for n in graph["nodes"] if "ImpairmentsChain" in str(n["type"])]
    assert chains, "no ImpairmentsChain in the export"
    for chain in chains:
        assert len(chain["inputs"]) == 8, f"#{chain['id']}: export did not restore the full slot list"
        for name in ("tx_disabled", "rx_disabled"):
            slot = next(i for i, inp in enumerate(chain["inputs"]) if inp["name"] == name)
            link_id = chain["inputs"][slot].get("link")
            assert link_id is not None, f"#{chain['id']}: {name} lost its link"
            src = links[link_id]
            assert (
                "Config" in str(nodes[src[1]]["type"]) and src[4] == slot
            ), f"#{chain['id']}: {name} link target_slot {src[4]} != slot {slot} (not normalised)"


def test_ops_yaml_roundtrip_reconnects_the_config_nodes() -> None:
    # The "Export Taidal Ops → Confluid YAML" of this graph hoists the ImpairmentsTx/RxConfig as
    # constants and references them from the ImpairmentsChain via dotted !ref:. Importing that YAML back
    # must RECONNECT the config nodes to ImpairmentsChain.tx_disabled / .rx_disabled — was: imported back
    # disconnected, because the reconstructed node had an incomplete input-slot list so the wired-widget
    # link landed at the wrong slot index (`_complete_widget_input_slots` fix). Materialising the !class:
    # markers needs the real classes, so this skips without the [waivefront] extra discovered.
    import streamstudio
    from streamstudio.graphio import yaml_to_workflow

    mappings = streamstudio.NODE_CLASS_MAPPINGS
    if "Taidal_waivefront_op_signal_ImpairmentsChain" not in mappings:
        pytest.skip("requires the [waivefront] extra discovered (ImpairmentsChain not registered)")

    ops_yaml = _FIXTURE.parent / "impairments_ops.yaml"
    wf = yaml_to_workflow(str(ops_yaml), mappings)  # auto-dispatches to the ops-doc importer
    nodes = {n["id"]: n for n in wf["nodes"]}
    links = {lk[0]: lk for lk in wf["links"]}
    chains = [n for n in wf["nodes"] if "ImpairmentsChain" in str(n["type"])]
    assert chains, "no ImpairmentsChain reconstructed from the ops.yaml"
    for chain in chains:
        for name in ("tx_disabled", "rx_disabled"):
            slot = next((i for i, inp in enumerate(chain["inputs"]) if inp["name"] == name), None)
            assert slot is not None, f"#{chain['id']} {name} input slot missing"
            link_id = chain["inputs"][slot].get("link")
            assert link_id is not None, f"#{chain['id']} {name} is not connected"
            src = links[link_id]
            assert "Config" in str(nodes[src[1]]["type"]), f"#{chain['id']} {name} not fed by a Config node"
            assert src[4] == slot, f"#{chain['id']} {name} link target_slot {src[4]} != input slot {slot}"
