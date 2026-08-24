import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

console.log("ComfyStream: Loaded Taidal.SendToViewer extension");

// Canvas-side companion to the /taidal/send_to_viewer backend route: serialize the WHOLE
// active graph, convert it server-side (same converter as Export Graph → Taidal YAML) and
// POST it to the viewer that launched this editor — no export-file → drop round trip.
// The viewer's address arrives as ?taidal_return=<origin> on launch (open_graph.js stores
// it in sessionStorage); opened by hand, the command asks for the address once per session.

async function sendToViewer() {
    let viewer = sessionStorage.getItem("taidal.viewer");
    if (!viewer) {
        viewer = prompt("Viewer address to send the graph to:", "http://127.0.0.1:8787");
        if (!viewer) return;
        sessionStorage.setItem("taidal.viewer", viewer);
    }
    const { workflow } = await app.graphToPrompt();
    const name = (app.extensionManager?.workflow?.activeWorkflow?.filename || "pipeline").replace(/\.json$/, "");
    try {
        const res = await api.fetchApi("/taidal/send_to_viewer", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ workflow, url: viewer, name }),
        });
        const data = await res.json();
        if (!res.ok) {
            alert("Send to viewer failed:\n\n" + (data.error || res.statusText));
            return;
        }
        const message = (data.sent && data.sent.message) || "sent";
        alert("Sent to " + viewer + ":\n\n" + message);
    } catch (err) {
        console.error("SendToViewer: request failed", err);
        alert("Send to viewer failed: " + err);
    }
}

app.registerExtension({
    name: "Taidal.SendToViewer",
    commands: [{ id: "taidal.sendToViewer", label: "Send Graph → Viewer", function: sendToViewer }],
    // Beside the export commands under the top-level "Taidal" menu.
    menuCommands: [{ path: ["Taidal"], commands: ["taidal.sendToViewer"] }],
});
