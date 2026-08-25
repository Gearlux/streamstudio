import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

console.log("ComfyStream: Loaded Taidal.OpenGraph extension");

// StreamStudio: a workspace's SOURCE GRAPH, edited here, owned as a file.
//
// A launching viewer appends ?taidal_graph=<server-side path> naming the graph it works
// from. A .json path is the workspace's source-graph FILE (its native workflow form):
//   * on open, the file is put on the canvas (via /taidal/read_graph) — when it does not
//     exist yet, the editor keeps its own last session and the first save creates it;
//   * every native workflow SAVE (Ctrl-S / Save / Save As — the /userdata/workflows/…
//     POST) is MIRRORED into that file via /taidal/write_graph. The file, not a network
//     push, is how the canvas reaches the viewer: the viewer stats it on focus.
// A .yaml path is a Confluid config, converted by /taidal/import_yaml as before.
// Params are stripped once applied, so a reload (F5) keeps what the user builds; the
// mirror target survives in sessionStorage for the tab's lifetime.

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

// Load and make sure it STUCK (a late session restore can still replace the canvas once);
// retries fire only while the canvas does not hold our content.
async function loadUntilItSticks(workflow) {
    const want = (workflow.nodes || []).map((n) => n.type).sort().join(",");
    for (let attempt = 0; attempt < 3; attempt++) {
        await app.loadGraphData(workflow);
        await sleep(800);
        if (fingerprint() === want) return true;
    }
    return false;
}

async function fetchWorkflow(graphPath) {
    if (graphPath.endsWith(".json")) {
        const res = await api.fetchApi("/taidal/read_graph?path=" + encodeURIComponent(graphPath));
        if (res.status === 404) return null; // not created yet: the first save will create it
        const data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || res.statusText);
        return data.workflow;
    }
    const res = await api.fetchApi("/taidal/import_yaml", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: graphPath, as: "graph" }),
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || res.statusText);
    return data.workflow;
}

async function applyOpenParams() {
    const url = new URL(window.location.href);
    const graphPath = url.searchParams.get("taidal_graph");
    if (!graphPath) return;
    if (graphPath.endsWith(".json")) sessionStorage.setItem("taidal.sourceFile", graphPath); // the mirror target
    url.searchParams.delete("taidal_graph");
    window.history.replaceState({}, "", url.pathname + url.search + url.hash);
    await appSettled();
    try {
        const workflow = await fetchWorkflow(graphPath);
        if (workflow === null) {
            console.log("Taidal.OpenGraph:", graphPath, "does not exist yet — the first save creates it");
            return;
        }
        const want = (workflow.nodes || []).map((n) => n.type).sort().join(",");
        if (fingerprint() === want) {
            console.log("Taidal.OpenGraph: the canvas already shows", graphPath);
            return; // never stack a duplicate workflow tab for the graph already on screen
        }
        await loadUntilItSticks(workflow);
        console.log("Taidal.OpenGraph: loaded", graphPath);
    } catch (err) {
        console.error("Taidal.OpenGraph: could not open", graphPath, err);
        alert("Open graph failed for " + graphPath + ":\n\n" + err + "\n\nThe canvas is left as it was.");
    }
}

// The SAVE MIRROR: a synchronous pass-through wrap of api.fetchApi (an async wrap defers
// by a microtask and flips frontend races — measured on loadGraphData). A successful
// workflow save is re-posted verbatim to the workspace's source-graph file.
if (!api.__taidalSaveMirror) {
    const original = api.fetchApi.bind(api);
    api.fetchApi = function (route, options) {
        const result = original(route, options);
        try {
            const target = sessionStorage.getItem("taidal.sourceFile");
            const isSave =
                target &&
                options &&
                options.method === "POST" &&
                typeof route === "string" &&
                decodeURIComponent(route).includes("/userdata/workflows/");
            if (isSave) {
                const body = JSON.parse(options.body);
                Promise.resolve(result)
                    .then((res) => {
                        if (!res.ok) return;
                        return original("/taidal/write_graph", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ path: target, workflow: body }),
                        }).then(() => console.log("Taidal.OpenGraph: mirrored the save to", target));
                    })
                    .catch((err) => console.warn("Taidal.OpenGraph: save mirror failed —", err));
            }
        } catch (e) {
            /* the mirror must never break a save */
        }
        return result;
    };
    api.__taidalSaveMirror = true;
}

app.registerExtension({
    name: "Taidal.OpenGraph",
    setup() {
        // DETACHED on purpose: the app awaits every extension's setup() before its own
        // initial load — awaiting the boot here would deadlock the splash (measured).
        applyOpenParams().catch((err) => console.error("Taidal.OpenGraph:", err));
    },
});
