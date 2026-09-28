import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

console.log("StreamStudio: Loaded Taidal.ExportWorkflow extension");

// Canvas-side companion to the /taidal/export_workflow backend route + the `streamstudio
// export-workflow` CLI. Serializes the WHOLE active graph (sources, ops, object members, the
// runnable, and their wiring — not just the op chain that "Export Taidal Ops" handles) and converts
// it to a hoisted-!ref: Confluid YAML server-side via streamstudio.graphio.workflow_to_yaml, then
// downloads it. The output matches the navigaitor / hand-written reference config shape, so it runs
// directly with `recordstream run config.yaml`.

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

async function exportWorkflow() {
    // graphToPrompt() returns { workflow, output }; `workflow` is the UI/litegraph form (nodes,
    // links, groups, definitions) — the whole-graph artifact the converter consumes.
    const { workflow } = await app.graphToPrompt();
    try {
        const res = await api.fetchApi("/taidal/export_workflow", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ workflow }),
        });
        const data = await res.json();
        if (!res.ok) {
            // 400 → an unconvertible node (e.g. a native ComfyUI node wired into a config kwarg).
            alert("Export Graph → Taidal YAML failed:\n\n" + (data.error || res.statusText));
            return;
        }
        downloadYaml(data.yaml, "config.yaml");
    } catch (err) {
        console.error("ExportWorkflow: request failed", err);
        alert("Export Graph → Taidal YAML failed: " + err);
    }
}

app.registerExtension({
    name: "Taidal.ExportWorkflow",
    commands: [
        { id: "taidal.exportWorkflow", label: "Export Graph → Taidal YAML", function: exportWorkflow },
    ],
    // Surfaced under the top-level "Taidal" menu, next to Export Taidal Ops.
    menuCommands: [{ path: ["Taidal"], commands: ["taidal.exportWorkflow"] }],
});
