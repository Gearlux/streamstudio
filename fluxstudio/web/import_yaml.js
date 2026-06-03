import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

console.log("ComfyFlux: Loaded Taidal.ImportYaml extension");

// Canvas-side companion to the /taidal/import_yaml backend route + the `fluxstudio import` CLI.
// Picks a Confluid YAML config from disk, converts it to a ComfyUI workflow (nodes + links +
// groups + subgraphs) server-side via fluxstudio.graphio.yaml_to_workflow, and loads it onto the
// active canvas. Three menu commands cover the import modes:
//   * Graph    — lay the config's nodes out on the active canvas.
//   * Subgraph — package it as a reusable definitions.subgraphs[] blueprint + an instance node.
//   * Group    — lay it out wrapped in one visual group box.

function pickYamlFile() {
    return new Promise((resolve) => {
        const input = document.createElement("input");
        input.type = "file";
        input.accept = ".yaml,.yml";
        input.onchange = () => {
            const file = input.files && input.files[0];
            if (!file) {
                resolve(null);
                return;
            }
            const reader = new FileReader();
            reader.onload = () => resolve({ text: reader.result, name: file.name.replace(/\.[^.]+$/, "") });
            reader.onerror = () => resolve(null);
            reader.readAsText(file);
        };
        input.click();
    });
}

async function importYaml(as_) {
    const picked = await pickYamlFile();
    if (!picked) return;
    try {
        const res = await api.fetchApi("/taidal/import_yaml", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ yaml: picked.text, as: as_, name: picked.name }),
        });
        const data = await res.json();
        if (!res.ok) {
            alert("Import Taidal YAML failed:\n\n" + (data.error || res.statusText));
            return;
        }
        // loadGraphData replaces the active graph with the imported workflow (nodes/links/groups/
        // definitions). ComfyUI re-flows positions and reconstructs dynamic op_N/source_N inputs.
        await app.loadGraphData(data.workflow);
    } catch (err) {
        console.error("ImportYaml: request failed", err);
        alert("Import Taidal YAML failed: " + err);
    }
}

app.registerExtension({
    name: "Taidal.ImportYaml",
    commands: [
        { id: "taidal.importYamlGraph", label: "Import Taidal YAML → Graph", function: () => importYaml("graph") },
        {
            id: "taidal.importYamlSubgraph",
            label: "Import Taidal YAML → Subgraph",
            function: () => importYaml("subgraph"),
        },
        { id: "taidal.importYamlGroup", label: "Import Taidal YAML → Group", function: () => importYaml("group") },
    ],
    // Surfaced under the top-level "Taidal" menu (same place as Export Taidal Ops).
    menuCommands: [
        {
            path: ["Taidal"],
            commands: ["taidal.importYamlGraph", "taidal.importYamlSubgraph", "taidal.importYamlGroup"],
        },
    ],
});
