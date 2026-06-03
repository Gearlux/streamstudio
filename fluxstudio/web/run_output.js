import { app } from "../../scripts/app.js";
import { ComfyWidgets } from "../../scripts/widgets.js";

console.log("ComfyFlux: Loaded Taidal.RunOutput extension");

// Shows the captured stdout/stderr of a Run node's runner (training progress, metrics, logflow lines)
// in a read-only multiline text widget on the node, populated from the node's
// {ui: {text: [...]}} execution result. This is the standard ComfyUI "ShowText" pattern: the Python
// RunNode returns the captured text under `ui.text`, and onExecuted renders it here.

function ensureOutputWidget(node) {
    let widget = node.widgets?.find((w) => w.name === "run_output");
    if (!widget) {
        widget = ComfyWidgets["STRING"](node, "run_output", ["STRING", { multiline: true }], app).widget;
        widget.inputEl.readOnly = true;
        widget.inputEl.style.opacity = 0.85;
        // Don't persist captured logs into the saved workflow.json — they're runtime output, not config.
        widget.serializeValue = async () => "";
    }
    return widget;
}

app.registerExtension({
    name: "Taidal.RunOutput",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "Taidal_Run") return;

        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            onExecuted?.apply(this, arguments);
            const text = Array.isArray(message?.text) ? message.text.join("") : (message?.text ?? "");
            const widget = ensureOutputWidget(this);
            widget.value = text;
            // Grow the node to fit the new text, then repaint.
            requestAnimationFrame(() => {
                const size = this.computeSize();
                if (size[1] > this.size[1]) this.setSize([this.size[0], size[1]]);
                app.graph?.setDirtyCanvas(true, false);
            });
        };
    },
});
