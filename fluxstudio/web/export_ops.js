import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

console.log("ComfyFlux: Loaded Taidal.ExportOps extension");

// Canvas-side companion to the /taidal/export_ops backend route + the `fluxstudio export` CLI.
// Compiles the current graph's OPS (dropping the source + Walk/Extract adapter nodes) into a
// Confluid YAML op-list and downloads it, so it can be reattached to any source via
// Flux.from_ops_yaml / confluid.materialize(confluid.load(path)["ops"]).

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
    // graphToPrompt() returns { workflow, output }; `output` is the API-format prompt the
    // backend compiler consumes (same shape as ComfyUI "Save (API Format)").
    const { output } = await app.graphToPrompt();
    try {
        const res = await api.fetchApi("/taidal/export_ops", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ prompt: output }),
        });
        const data = await res.json();
        if (!res.ok) {
            // 400 → UnsupportedTopologyError (actionable: fan-in, multi-head, link-driven widget…).
            alert("Export ops failed:\n\n" + (data.error || res.statusText));
            return;
        }
        downloadYaml(data.yaml, "ops.yaml");
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
