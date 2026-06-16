import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

console.log("ComfyFlux: Loaded Taidal.ImportYaml extension");

// Canvas-side companion to the /taidal/import_yaml backend route + the `fluxstudio import` CLI.
// Picks a Confluid YAML config from disk, converts it to a ComfyUI workflow server-side via
// fluxstudio.graphio.yaml_to_workflow, and brings it onto the canvas. TWO menu commands, differing
// only in WHERE the config lands (the merge vs. load distinction is the only one users care about):
//   * Current Graph — MERGE the config's nodes + wiring into the active graph (added to what you are
//                     building) via the litegraph clipboard/paste path (ComfyUI's node-template
//                     insertion mechanism) — pasted at the mouse, selected, in one undo step.
//   * New Graph     — load the config as its OWN workflow / a fresh tab via app.loadGraphData.
// loadGraphData is also the automatic fallback for Current Graph on a build without pasteFromClipboard.
// Both use as_="graph" (flat). The backend yaml_to_workflow + `fluxstudio import --as subgraph|group`
// CLI still package a config as a subgraph blueprint / group box, but those modes are no longer their
// own menu commands — after a merge import you can use ComfyUI's native group / "Convert to Subgraph".

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

// Reshape a ComfyUI workflow.json dict (as produced by fluxstudio.graphio.yaml_to_workflow) into the
// litegraph CLIPBOARD shape that LGraphCanvas.pasteFromClipboard() consumes. Two shape differences vs.
// workflow.json — both verified against the comfyui_frontend deserializer (LGraphCanvas._serializeItems
// / _deserializeItems):
//   * links — workflow.json stores 6-arrays [id, origin_id, origin_slot, target_id, target_slot, type];
//     the clipboard paste loop reads link.origin_id / .target_id as OBJECT properties (it does NOT route
//     top-level links through LLink.configure, so a 6-array would silently fail to connect). Each link is
//     reshaped to the LLink.asSerialisable() object form. Subgraph-DEFINITION inner links are left as
//     6-arrays — those flow through createSubgraph().configure(), whose LLink.configure accepts both forms.
//   * subgraphs — workflow.json nests definitions under definitions.subgraphs; the clipboard format carries
//     them at the top level as `subgraphs` (paste remaps each id to a fresh UUID + the instance node type).
function workflowToClipboard(wf) {
    const toLink = (l) =>
        Array.isArray(l)
            ? { id: l[0], origin_id: l[1], origin_slot: l[2], target_id: l[3], target_slot: l[4], type: l[5] }
            : l;
    return {
        nodes: wf.nodes || [],
        groups: wf.groups || [],
        reroutes: (wf.extra && wf.extra.reroutes) || wf.reroutes || [],
        links: (wf.links || []).map(toLink),
        subgraphs: (wf.definitions && wf.definitions.subgraphs) || [],
    };
}

// Insert a workflow fragment into the ACTIVE graph via the clipboard/paste path, so the imported nodes
// merge into what the user is building (pasted at the canvas mouse position, selected, in one undo step)
// instead of replacing the graph / opening a separate tab. The user's own clipboard is preserved. Returns
// false if this ComfyUI build exposes no pasteFromClipboard (caller then falls back to a full load).
function pasteIntoCurrentGraph(wf) {
    const canvas = app.canvas;
    if (!canvas || typeof canvas.pasteFromClipboard !== "function") return false;
    const KEY = "litegrapheditor_clipboard";
    const prev = localStorage.getItem(KEY);
    try {
        localStorage.setItem(KEY, JSON.stringify(workflowToClipboard(wf)));
        canvas.pasteFromClipboard();
    } finally {
        if (prev === null) localStorage.removeItem(KEY);
        else localStorage.setItem(KEY, prev);
    }
    return true;
}

async function importYaml(as_, { merge = true } = {}) {
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
        // merge=true (Graph/Subgraph/Group): paste the imported nodes INTO the active graph — added to
        // what the user is building. merge=false ("New Graph"): load the config as its own workflow.
        // app.loadGraphData re-flows positions + reconstructs dynamic op_N/source_N inputs but replaces
        // the graph / opens a fresh tab — it is also the automatic fallback when pasteFromClipboard is
        // absent, so a merge import never silently no-ops.
        if (!merge || !pasteIntoCurrentGraph(data.workflow)) {
            await app.loadGraphData(data.workflow);
        }
    } catch (err) {
        console.error("ImportYaml: request failed", err);
        alert("Import Taidal YAML failed: " + err);
    }
}

app.registerExtension({
    name: "Taidal.ImportYaml",
    commands: [
        {
            id: "taidal.importYamlCurrentGraph",
            label: "Import Taidal YAML → Current Graph",
            function: () => importYaml("graph"),
        },
        {
            id: "taidal.importYamlNewGraph",
            label: "Import Taidal YAML → New Graph",
            function: () => importYaml("graph", { merge: false }),
        },
    ],
    // Surfaced under the top-level "Taidal" menu (same place as Export Taidal Ops).
    menuCommands: [
        {
            path: ["Taidal"],
            commands: ["taidal.importYamlCurrentGraph", "taidal.importYamlNewGraph"],
        },
    ],
});
