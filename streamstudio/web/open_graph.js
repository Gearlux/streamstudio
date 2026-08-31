import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

console.log("StreamStudio: Loaded Taidal.OpenGraph extension");

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

// The workflow PATH this graph file is opened as, inside ComfyUI's own store — ONE document
// PER GRAPH: ``…/my.workspace.yaml.d/model_echo.json`` → ``taidal/my.workspace/model_echo``.
// It used to collapse to the workspace name alone, so every graph of a workspace shared one
// document — opening echo landed on the efficientvit canvas (user report 2026-08-31).
function workflowPathFor(graphPath) {
    const parts = graphPath.split(/[\\/]/);
    const dir = parts[parts.length - 2] || "";              // "my.workspace.yaml.d"
    const owner = dir.replace(/\.d$/, "").replace(/\.ya?ml$/, "");  // "my.workspace"
    const stem = parts[parts.length - 1].replace(/\.json$/, "");     // "model_echo"
    return `workflows/taidal/${owner || "graphs"}/${stem || "graph"}.json`;
}

// Put the graph in ComfyUI's store and OPEN it from there, so the canvas is a NAMED document.
// Loaded straight onto the canvas it is an "Unsaved Workflow": Ctrl-S then demands a name for a
// graph that already has a home, and cancelling means the save mirror never fires. Persisted
// first, Save is a plain overwrite — and that overwrite is exactly the POST the mirror watches.
async function openAsNamedWorkflow(workflow, graphPath) {
    const store = app.extensionManager && app.extensionManager.workflow;
    if (!store || !store.syncWorkflows || !store.getWorkflowByPath) return false;
    const path = workflowPathFor(graphPath);
    const res = await api.fetchApi(`/userdata/${encodeURIComponent(path)}?overwrite=true`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(workflow),
    });
    if (!res.ok) throw new Error(`could not persist ${path}: ${res.status}`);
    await store.syncWorkflows(); // a just-written file is not in the store's list until it re-reads
    const persisted = store.getWorkflowByPath(path);
    if (!persisted) throw new Error(`${path} was written but is not listed`);
    // loadGraphData with the workflow, NOT store.openWorkflow: opening binds the identity but
    // does not load the content — measured, the canvas stayed empty while the tab showed the
    // right name. Passing the workflow object here does both at once.
    await app.loadGraphData(workflow, true, true, persisted);
    return true;
}

async function applyOpenParams() {
    const url = new URL(window.location.href);
    const graphPath = url.searchParams.get("taidal_graph");
    if (!graphPath) return;
    if (graphPath.endsWith(".json")) {
        // The mirror MAP: workflow document -> the workspace graph file it mirrors to. A tab
        // may hold several taidal graphs at once (echo AND efficientvit as workflow tabs), so
        // one remembered file would mirror whichever canvas saves into the WRONG graph.
        const mirrors = JSON.parse(sessionStorage.getItem("taidal.graphFiles") || "{}");
        mirrors[workflowPathFor(graphPath)] = graphPath;
        sessionStorage.setItem("taidal.graphFiles", JSON.stringify(mirrors));
    }
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
        let opened = false;
        try {
            opened = await openAsNamedWorkflow(workflow, graphPath);
        } catch (err) {
            // Never lose the graph over the naming: fall back to putting it on the canvas as
            // before. The user then gets the Save-As prompt, which still mirrors.
            console.warn("Taidal.OpenGraph: could not open it as a named workflow —", err);
        }
        if (!opened) await loadUntilItSticks(workflow);
        console.log("Taidal.OpenGraph: loaded", graphPath, opened ? "(named workflow)" : "(canvas only)");
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
            const mirrors = JSON.parse(sessionStorage.getItem("taidal.graphFiles") || "{}");
            let target = null;
            if (options && options.method === "POST" && typeof route === "string") {
                const decoded = decodeURIComponent(route);
                // mirror ONLY the save of a workflow document this tab opened FROM a graph
                // file, and only to THAT file — a save of any other workflow (another taidal
                // graph, the user's own documents) must never touch this workspace's graphs
                for (const [workflowPath, graphFile] of Object.entries(mirrors)) {
                    if (decoded.includes("/userdata/" + workflowPath) || decoded.includes("/userdata/" + encodeURIComponent(workflowPath))) {
                        target = graphFile;
                        break;
                    }
                }
            }
            if (target) {
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
