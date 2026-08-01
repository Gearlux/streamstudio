import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

console.log("ComfyStream: Loaded Taidal.ExportFlow extension");

// Canvas-side companion to the /taidal/export_flow + /taidal/export_ops_graph backend routes and
// the `streamstudio export` CLI. Both commands export the current graph's OPS (the op-kind nodes +
// the Mix fan-in, dropping the source + Walk/Extract/viewer adapters), differing in OUTPUT format:
//   * → Flow YAML — COMPILE the op DAG to Confluid `flow:` document(s): named steps whose
//     `from:` / `merge_from:` / `bind:` keys ARE the wires you drew, plus constant hoisting. ONE
//     file per disconnected component (flow_1.yaml … flow_N.yaml). Consume with
//     FlowGraph.from_yaml(path). This is the ONLY interchange format — the flat serial `ops:`
//     export was removed 2026-08-01 (it could not express a fork, and the context ops that used to
//     manufacture one were deleted from recordstream with the lowering pass).
//   * → Graph     — extract the RAW op DAG *as drawn* into ONE ComfyUI workflow.json (no
//     compilation — positions/widgets/wiring preserved), for saving or sharing the op subgraph.
//     Downloaded as ops_graph.json.

function downloadBlob(text, filename, mime) {
    const blob = new Blob([text], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

async function exportFlow() {
    // graphToPrompt() returns { workflow, output }: `output` is the API-format prompt the backend
    // compiler consumes (same shape as ComfyUI "Save (API Format)"); `workflow` is the full UI
    // graph — node properties + subgraph definitions included. The export-skip / export-target
    // marks live in node.properties (which the prompt strips), so we send the WHOLE workflow and
    // the backend reads the marks in Python (export.collect_export_marks) — subgraph-aware and the
    // same single source of truth the whole-graph export uses. This is why a mark placed INSIDE a
    // subgraph is now honoured (the old JS collector only saw top-level nodes).
    const { output, workflow } = await app.graphToPrompt();
    try {
        const res = await api.fetchApi("/taidal/export_flow", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ prompt: output, workflow }),
        });
        const data = await res.json();
        if (!res.ok) {
            // 400 → UnsupportedTopologyError (actionable: fan-in, cycle, link-driven widget…).
            alert("Export flow failed:\n\n" + (data.error || res.statusText));
            return;
        }
        // One doc per independent op component (`yamls`; `yaml` is the older single-doc field).
        const docs = data.yamls || [data.yaml];
        const stem = "flow";
        if (docs.length === 1) {
            downloadBlob(docs[0], `${stem}.yaml`, "text/yaml");
        } else {
            // The browser may ask once to allow multiple downloads — expected.
            docs.forEach((doc, i) => downloadBlob(doc, `${stem}_${i + 1}.yaml`, "text/yaml"));
        }
    } catch (err) {
        console.error("ExportOps: request failed", err);
        alert("Export ops failed: " + err);
    }
}

// Export the op DAG as a raw ComfyUI workflow.json (NOT the compiled ops-yaml). Posts the full UI
// `workflow` (positions + widgets + wiring); the backend extracts the op-DAG nodes. Because a real
// pipeline routes the record THROUGH subgraph instances (not op nodes), there are two modes:
//   * flatten=false — keep the subgraph instances intact (as black boxes, with their definitions),
//     so the pipeline stays connected; downloads `ops_graph.json`.
//   * flatten=true  — inline every subgraph and keep ONLY op nodes, reconnecting the pipeline through
//     the dropped pass-throughs; downloads `ops_graph_flat.json`.
async function exportOpsGraph(flatten) {
    const { workflow } = await app.graphToPrompt();
    try {
        const res = await api.fetchApi("/taidal/export_ops_graph", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ workflow, flatten: !!flatten }),
        });
        const data = await res.json();
        if (!res.ok) {
            alert("Export ops graph failed:\n\n" + (data.error || res.statusText));
            return;
        }
        downloadBlob(
            JSON.stringify(data.workflow, null, 2),
            flatten ? "ops_graph_flat.json" : "ops_graph.json",
            "application/json",
        );
    } catch (err) {
        console.error("ExportOpsGraph: request failed", err);
        alert("Export ops graph failed: " + err);
    }
}

app.registerExtension({
    name: "Taidal.ExportFlow",
    commands: [
        {
            id: "taidal.exportFlow",
            label: "Export Taidal Ops → Flow YAML",
            function: () => exportFlow(),
        },
        {
            id: "taidal.exportOpsGraphSubgraphs",
            label: "Export Taidal Ops → Graph (subgraphs intact)",
            function: () => exportOpsGraph(false),
        },
        {
            id: "taidal.exportOpsGraphFlat",
            label: "Export Taidal Ops → Graph (flattened)",
            function: () => exportOpsGraph(true),
        },
    ],
    // Surfaced under a top-level "Taidal" menu (newer ComfyUI menu API).
    menuCommands: [
        {
            path: ["Taidal"],
            commands: [
                "taidal.exportFlow",
                "taidal.exportOpsGraphSubgraphs",
                "taidal.exportOpsGraphFlat",
            ],
        },
    ],
});
