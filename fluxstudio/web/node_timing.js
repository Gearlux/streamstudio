import { app } from "../../scripts/app.js";

// Draws per-node wall-clock timing (returned as the last entry in ui.text by bridge._node_class)
// directly on the node canvas via onDrawForeground.  This is purely visual — nothing is added to
// the widget list and nothing is serialised into the saved workflow.
//
// For RUNNABLE nodes the timing is appended to their training-log text and shown in the existing
// run_output.js text area; skip them here to avoid double-handling.

console.log("ComfyFlux: Loaded Taidal.NodeTiming extension");

function isTaidalNode(nodeData) {
    return !!(nodeData?.category?.startsWith?.("Taidal") || nodeData?.category?.startsWith?.("SampleFlux"));
}

function isRunnableNode(nodeData) {
    // Only runnable nodes declare the hidden `unique_id` input.
    return nodeData?.input?.hidden?.unique_id !== undefined;
}

// Extract "⏱ X ms" from the last text entry returned by bridge._node_class.
function extractTiming(message) {
    const texts = Array.isArray(message?.text) ? message.text : [];
    if (!texts.length) return null;
    const last = String(texts[texts.length - 1]);
    // Grab the last line, which is where bridge appends the timing label.
    const lines = last.split("\n");
    return lines[lines.length - 1] || null;
}

app.registerExtension({
    name: "Taidal.NodeTiming",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (!isTaidalNode(nodeData)) return;
        if (isRunnableNode(nodeData)) return; // run_output.js owns runnables

        // --- onExecuted: capture timing into a transient property ---
        const prevOnExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            prevOnExecuted?.apply(this, arguments);
            const timing = extractTiming(message);
            if (timing) {
                this._taidalTiming = timing;
                this.setDirtyCanvas?.(true, false);
            }
        };

        // --- onDrawForeground: paint timing in the bottom-right corner ---
        const prevOnDrawForeground = nodeType.prototype.onDrawForeground;
        nodeType.prototype.onDrawForeground = function (ctx) {
            prevOnDrawForeground?.apply(this, arguments);
            const timing = this._taidalTiming;
            if (!timing || this.collapsed) return;

            ctx.save();
            ctx.font = "10px monospace";
            ctx.fillStyle = "rgba(255,255,255,0.35)";
            ctx.textAlign = "right";
            ctx.textBaseline = "bottom";
            // 6px margin from the right edge, 4px from the bottom.
            ctx.fillText(timing, this.size[0] - 6, this.size[1] - 4);
            ctx.restore();
        };
    },
});
