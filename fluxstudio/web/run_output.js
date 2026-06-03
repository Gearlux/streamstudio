import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { ComfyWidgets } from "../../scripts/widgets.js";

console.log("ComfyFlux: Loaded Taidal.RunOutput extension");

// Shows the captured stdout/stderr of a Run node's runner (training progress, metrics, logflow lines)
// in a read-only, COLLAPSIBLE multiline text widget on the node. It updates LIVE during the run: the
// Python RunNode streams the latest `\r`-collapsed snapshot over the `taidal.run_output` websocket
// event (so a tqdm / Lightning progress bar updates IN PLACE), and also returns the final text under
// `ui.text` (restored by onExecuted on re-open). The captured logs are runtime output, not config, so
// they're never serialised into the saved workflow.json.

const RUN_NODE = "Taidal_Run";
const EXPANDED_LABEL = "▾ output";
const COLLAPSED_LABEL = "▸ output";

function ensureOutputWidget(node) {
    let widget = node.widgets?.find((w) => w.name === "run_output");
    if (!widget) {
        widget = ComfyWidgets["STRING"](node, "run_output", ["STRING", { multiline: true }], app).widget;
        widget.inputEl.readOnly = true;
        widget.inputEl.style.opacity = 0.85;
        // Don't persist captured logs into the saved workflow.json — they're runtime output, not config.
        widget.serializeValue = async () => "";
        // Remember the natural computeSize so the collapse toggle can restore it.
        widget._taidalComputeSize = widget.computeSize?.bind(widget);
        applyCollapsed(node, widget);
    }
    return widget;
}

// Fold / unfold the text area. Collapsed: hide the <textarea> and make the widget claim zero height so
// the node shrinks; expanded: restore the element and its natural computeSize. The button label tracks
// state. `node._taidalOutputCollapsed` is the source of truth (default expanded — progress shows
// immediately). Purely cosmetic; the run keeps streaming regardless.
function applyCollapsed(node, widget) {
    const collapsed = !!node._taidalOutputCollapsed;
    if (widget?.inputEl) widget.inputEl.style.display = collapsed ? "none" : "";
    if (widget) {
        widget.computeSize = collapsed
            ? () => [node.size?.[0] ?? 0, 0]
            : widget._taidalComputeSize || widget.computeSize;
    }
    const button = node.widgets?.find((w) => w.name === "__taidal_output_toggle__");
    if (button) button.name = collapsed ? COLLAPSED_LABEL : EXPANDED_LABEL;
    requestAnimationFrame(() => {
        node.setSize(node.computeSize());
        app.graph?.setDirtyCanvas(true, true);
    });
}

function ensureToggle(node) {
    if (node.widgets?.some((w) => w.name === "__taidal_output_toggle__")) return;
    const button = node.addWidget("button", EXPANDED_LABEL, null, () => {
        node._taidalOutputCollapsed = !node._taidalOutputCollapsed;
        applyCollapsed(node, node.widgets?.find((w) => w.name === "run_output"));
    });
    button.name = "__taidal_output_toggle__";
    button.serialize = false;
}

// Write `text` into a node's output widget, keeping the latest line in view; grow only when expanded.
function setOutput(node, text) {
    const widget = ensureOutputWidget(node);
    widget.value = text;
    if (widget.inputEl) widget.inputEl.scrollTop = widget.inputEl.scrollHeight; // follow the live tail
    if (!node._taidalOutputCollapsed) {
        requestAnimationFrame(() => {
            const size = node.computeSize();
            if (size[1] > node.size[1]) node.setSize([node.size[0], size[1]]);
            app.graph?.setDirtyCanvas(true, false);
        });
    }
}

app.registerExtension({
    name: "Taidal.RunOutput",
    async setup() {
        // LIVE stream: the RunNode pushes `{node, text}` every poll while the runner runs.
        api.addEventListener("taidal.run_output", (event) => {
            const detail = event?.detail || {};
            const node = app.graph?.getNodeById?.(Number(detail.node));
            if (node && node.comfyClass === RUN_NODE) setOutput(node, detail.text ?? "");
        });
    },
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== RUN_NODE) return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
            if (this._taidalOutputCollapsed === undefined) this._taidalOutputCollapsed = false;
            ensureToggle(this); // toggle button first so it stays visible when the text area folds away
            return result;
        };

        // Final state (and re-open / re-run): ComfyUI delivers the persisted `ui.text`.
        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            onExecuted?.apply(this, arguments);
            const text = Array.isArray(message?.text) ? message.text.join("") : (message?.text ?? "");
            setOutput(this, text);
        };
    },
});
