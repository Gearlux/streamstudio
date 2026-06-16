import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

console.log("ComfyFlux: Loaded Taidal.ExportOps extension");

// Canvas-side companion to the /taidal/export_ops backend route + the `fluxstudio export` CLI.
// Compiles the current graph's OPS (dropping the source + Walk/Extract adapter nodes) into
// Confluid YAML op-lists and downloads them, so they can be reattached to any source via
// Flux.from_ops_yaml / confluid.materialize(confluid.load(path)["ops"]). A graph holding
// several disconnected op chains downloads ONE file per chain (ops_1.yaml … ops_N.yaml).

function downloadYaml(text, filename) {
    const blob = new Blob([text], { type: "text/yaml" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

async function exportOps() {
    // graphToPrompt() returns { workflow, output }: `output` is the API-format prompt the backend
    // compiler consumes (same shape as ComfyUI "Save (API Format)"); `workflow` is the full UI
    // graph — node properties + subgraph definitions included. The export-skip / export-target
    // marks live in node.properties (which the prompt strips), so we send the WHOLE workflow and
    // the backend reads the marks in Python (export.collect_export_marks) — subgraph-aware and the
    // same single source of truth the whole-graph export uses. This is why a mark placed INSIDE a
    // subgraph is now honoured (the old JS collector only saw top-level nodes).
    const { output, workflow } = await app.graphToPrompt();
    try {
        const res = await api.fetchApi("/taidal/export_ops", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ prompt: output, workflow }),
        });
        const data = await res.json();
        if (!res.ok) {
            // 400 → UnsupportedTopologyError (actionable: fan-in, cycle, link-driven widget…).
            alert("Export ops failed:\n\n" + (data.error || res.statusText));
            return;
        }
        // One doc per independent op chain (`yamls`; `yaml` is the older single-doc field).
        const docs = data.yamls || [data.yaml];
        if (docs.length === 1) {
            downloadYaml(docs[0], "ops.yaml");
        } else {
            // The browser may ask once to allow multiple downloads — expected.
            docs.forEach((doc, i) => downloadYaml(doc, `ops_${i + 1}.yaml`));
        }
    } catch (err) {
        console.error("ExportOps: request failed", err);
        alert("Export ops failed: " + err);
    }
}

app.registerExtension({
    name: "Taidal.ExportOps",
    commands: [
        {
            id: "taidal.exportOps",
            label: "Export Taidal Ops → Confluid YAML",
            function: exportOps,
        },
    ],
    // Surfaced under a top-level "Taidal" menu (newer ComfyUI menu API).
    menuCommands: [{ path: ["Taidal"], commands: ["taidal.exportOps"] }],
});
