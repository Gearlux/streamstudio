import { app } from "../../scripts/app.js";
import { ComfyWidgets } from "../../scripts/widgets.js";

console.log("StreamStudio: Loaded Taidal.DisplayAny extension");

// Shows the text output of a Display Any node (`streamstudio.nodes.DisplayAnyNode`, class key
// `Taidal_DisplayAny`) in a read-only multiline widget ON the node. The node is an OUTPUT_NODE that
// returns `{"ui": {"text": [text]}}`; ComfyUI delivers it to `onExecuted(message)` and re-delivers it
// on workflow re-open. Plain text → a STRING widget is the right tool here (the canvas-draw rule is
// only for image/interactive viewers). Mirrors web/run_output.js and the original WWAA Display Any.

const NODE_KEY = "Taidal_DisplayAny";

function setOutput(node, text) {
    let widget = node.widgets?.find((w) => w.name === "displaytext");
    if (!widget) {
        widget = ComfyWidgets["STRING"](node, "displaytext", ["STRING", { multiline: true }], app).widget;
        widget.inputEl.readOnly = true;
        widget.inputEl.style.opacity = 0.85;
        // Runtime output, not config — never persist it into the saved workflow.json.
        widget.serializeValue = async () => "";
    }
    widget.value = text;
    requestAnimationFrame(() => {
        const size = node.computeSize();
        if (size[1] > node.size[1]) node.setSize([node.size[0], size[1]]);
        app.graph?.setDirtyCanvas(true, false);
    });
}

app.registerExtension({
    name: "Taidal.DisplayAny",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData?.name !== NODE_KEY) return;

        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            onExecuted?.apply(this, arguments);
            const text = Array.isArray(message?.text) ? message.text.join("") : (message?.text ?? "");
            setOutput(this, text);
        };
    },
});
