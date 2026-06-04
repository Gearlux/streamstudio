import { app } from "../../scripts/app.js";

// FluxStudio: start with an EMPTY canvas instead of ComfyUI's Stable-Diffusion sample workflow.
//
// ComfyUI ships a built-in `defaultGraph` (CheckpointLoader -> CLIP Text Encode -> KSampler -> VAE
// Decode -> Save Image) that it loads as the initial canvas AND re-loads whenever the LAST workflow
// tab is closed (frontend workflowService.closeWorkflow -> loadDefaultWorkflow ->
// app.loadGraphData(defaultGraph)). Those nodes are pure Stable-Diffusion image-gen — nothing to do
// with FluxStudio's Taidal pipeline nodes — so we substitute ComfyUI's own `blankGraph` (an empty
// canvas) wherever the default graph would be loaded.
//
// The frontend exposes both on `window.comfyAPI.defaultGraph` (a documented-as-internal shim, so we
// stay defensive). Two complementary hooks, because internals are not a stable public API:
//   (1) Wrap `app.loadGraphData` to swap the default-graph object for a blank one at call time —
//       catches BOTH first-launch and the all-tabs-closed reload, with no timing race.
//   (2) Blank the shared `defaultGraph` object IN PLACE — `loadDefaultWorkflow` captured that exact
//       object reference at module-eval time, so emptying its contents neutralizes the reload even if
//       (1)'s `app` reference isn't the one the workflow service calls.

// Fallback empty graph, matching the frontend's own `blankGraph` shape — used only if
// window.comfyAPI.defaultGraph.blankGraph is somehow unavailable.
const BLANK_GRAPH = {
    last_node_id: 0,
    last_link_id: 0,
    nodes: [],
    links: [],
    groups: [],
    config: {},
    extra: {},
    version: 0.4,
};

function freshBlankGraph() {
    const dg = window.comfyAPI && window.comfyAPI.defaultGraph;
    const src = (dg && dg.blankGraph) || BLANK_GRAPH;
    try {
        return JSON.parse(JSON.stringify(src)); // a fresh copy each load — never share a mutable object
    } catch (e) {
        return { ...BLANK_GRAPH };
    }
}

// (2) Empty the shared default-graph object in place. Returns true once it has been handled.
function neutralizeDefaultGraphObject() {
    const dg = window.comfyAPI && window.comfyAPI.defaultGraph;
    if (!dg || !dg.defaultGraph) return false;
    const target = dg.defaultGraph;
    if (Array.isArray(target.nodes) && target.nodes.length === 0) return true; // already blank
    for (const key of Object.keys(target)) delete target[key];
    Object.assign(target, freshBlankGraph());
    try {
        dg.defaultGraphJSON = JSON.stringify(target);
    } catch (e) {
        /* non-fatal — the object form is what loadDefaultWorkflow uses */
    }
    return true;
}

// (1) Wrap loadGraphData: when asked to load the default-graph object, load a blank one instead.
if (app && typeof app.loadGraphData === "function" && !app.__taidalEmptyDefaultGraph) {
    const original = app.loadGraphData.bind(app);
    app.loadGraphData = function (graphData, ...rest) {
        const dg = window.comfyAPI && window.comfyAPI.defaultGraph;
        if (dg && graphData && graphData === dg.defaultGraph) {
            graphData = freshBlankGraph();
        }
        return original(graphData, ...rest);
    };
    app.__taidalEmptyDefaultGraph = true;
}

// The default-graph module can be a lazily-loaded chunk, so it may not be present yet at extension
// load. Poll briefly until it is, then blank it once. The all-tabs-closed path lives in that same
// chunk, so this always wins before the user can trigger a reload.
if (!neutralizeDefaultGraphObject()) {
    let tries = 0;
    const timer = setInterval(() => {
        if (neutralizeDefaultGraphObject() || ++tries > 100) clearInterval(timer); // ~5s cap (100 * 50ms)
    }, 50);
}

app.registerExtension({
    name: "Taidal.EmptyDefaultGraph",
    setup() {
        neutralizeDefaultGraphObject();
    },
});

console.log("ComfyFlux: Loaded Taidal.EmptyDefaultGraph extension (empty canvas instead of the SD sample workflow)");
