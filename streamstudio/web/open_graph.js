import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

console.log("ComfyStream: Loaded Taidal.OpenGraph extension");

// StreamStudio: a DETERMINISTIC canvas when the editor is opened by another tool.
//
// A launching viewer appends ONE of two URL params so what appears on the canvas is what the
// user asked for — never whatever the previous editor session left behind:
//   * ?taidal_graph=<server-side config path> — load that Confluid YAML as the canvas, converted
//     by the /taidal/import_yaml backend route ({path: …} body; same route the import menu uses).
//   * ?taidal_new=1                           — start from an empty canvas.
// The params are stripped from the URL once applied, so a manual reload (F5) keeps whatever the
// user has built since — the directive fires once per open, not once per reload.
//
// TIMING (every clause below was measured, not guessed): extension setup() runs while the app
// is still booting — loading then aborts with "getCanvas: canvas is null" (the canvas store is
// fed when the GraphCanvas component mounts) and the session restore finishing later replaces
// what we loaded. setup() must not await the boot either: the app awaits every setup(), so that
// deadlocks the splash. And the restore does not reliably pass through app.loadGraphData, so
// wrapping it is no signal (and an async wrapper alone flips the restore's own mount race).
// What is left is the observable state: wait until the app is interactive and the graph has
// stopped changing (the restore landed, or was never coming), then load — and verify the load
// stuck, retrying while it does not.

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
        return JSON.parse(JSON.stringify(src));
    } catch (e) {
        return { ...BLANK_GRAPH };
    }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function fingerprint() {
    try {
        return app.graph.nodes.map((n) => n.type).sort().join(",");
    } catch (e) {
        return "<no graph>";
    }
}

// Interactive (app + graph + mounted canvas element), then quiet: the graph fingerprint
// unchanged for 1.5 s. Capped at ~45 s so a broken boot cannot park us forever.
async function appSettled() {
    const deadline = Date.now() + 45000;
    while (Date.now() < deadline) {
        const interactive = window.app && window.app.graph && document.querySelector("canvas#graph-canvas");
        if (interactive) break;
        await sleep(200);
    }
    let last = fingerprint();
    let since = Date.now();
    while (Date.now() - since < 1500 && Date.now() < deadline) {
        await sleep(250);
        const now = fingerprint();
        if (now !== last) {
            last = now;
            since = Date.now();
        }
    }
}

// Load and make sure it STUCK (a late restore can still replace the canvas once); retries
// fire only while the canvas does not hold our content, so a settled canvas is never touched.
async function loadUntilItSticks(workflow) {
    const want = (workflow.nodes || []).map((n) => n.type).sort().join(",");
    for (let attempt = 0; attempt < 3; attempt++) {
        await app.loadGraphData(workflow);
        await sleep(800);
        if (fingerprint() === want) return true;
    }
    return fingerprint() === want;
}

async function applyOpenParams() {
    const url = new URL(window.location.href);
    const graphPath = url.searchParams.get("taidal_graph");
    const wantNew = url.searchParams.get("taidal_new");
    const returnTo = url.searchParams.get("taidal_return");
    if (returnTo) sessionStorage.setItem("taidal.viewer", returnTo); // where "send to viewer" posts
    if (!graphPath && !wantNew && !returnTo) return;
    url.searchParams.delete("taidal_graph");
    url.searchParams.delete("taidal_new");
    url.searchParams.delete("taidal_return");
    window.history.replaceState({}, "", url.pathname + url.search + url.hash);
    if (!graphPath && !wantNew) return;
    await appSettled();
    if (graphPath) {
        try {
            const res = await api.fetchApi("/taidal/import_yaml", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ path: graphPath, as: "graph" }),
            });
            const data = await res.json();
            if (!res.ok || data.error) throw new Error(data.error || res.statusText);
            await loadUntilItSticks(data.workflow);
            console.log("Taidal.OpenGraph: loaded", graphPath);
            return;
        } catch (err) {
            console.error("Taidal.OpenGraph: could not open", graphPath, err);
            alert("Open graph failed for " + graphPath + ":\n\n" + err + "\n\nStarting from an empty canvas.");
        }
    }
    await loadUntilItSticks(freshBlankGraph());
    console.log("Taidal.OpenGraph: opened an empty canvas");
}

app.registerExtension({
    name: "Taidal.OpenGraph",
    setup() {
        // DETACHED on purpose — see TIMING above: the app awaits every setup().
        applyOpenParams().catch((err) => console.error("Taidal.OpenGraph:", err));
    },
});
