import { app } from "../../scripts/app.js";

console.log("StreamStudio: Loaded Taidal.WalkDatasetStep extension");

// Makes the Walk Dataset node's `step` widget control how much `index` advances after each
// Queue Prompt, instead of ComfyUI's hard-wired +1.
//
// ComfyUI's `applyWidgetControl` reads `widget.options.step2` from the INT widget to determine
// the increment/decrement size (default 1). This extension syncs the node's `step` widget value
// into `indexWidget.options.step2` so the native `control_after_generate` mechanism picks it up —
// no monkey-patching of ComfyUI internals required.
//
// Result: with step=10 and control="increment", each Queue Prompt advances the index by 10
// (e.g. 5 → 15 → 25 → 35 …). The displayed index is exactly the record being viewed.
// Python's walk() receives the already-advanced index and uses it directly.

const NODE_KEY = "Taidal_WalkDataset";

app.registerExtension({
    name: "Taidal.WalkDatasetStep",
    nodeCreated(node) {
        if (node.comfyClass !== NODE_KEY) return;

        const indexWidget = node.widgets?.find((w) => w.name === "index");
        const stepWidget = node.widgets?.find((w) => w.name === "step");
        if (!indexWidget || !stepWidget) return;

        // Push the current step value into index.options.step2 so ComfyUI's
        // control_after_generate uses it as the increment size.
        const syncStep = () => {
            const s = Math.max(1, parseInt(stepWidget.value) || 1);
            if (!indexWidget.options) indexWidget.options = {};
            indexWidget.options.step2 = s;
        };

        // Apply immediately (handles graph load / node creation with a non-default step).
        syncStep();

        // Re-apply whenever the user edits the step widget so the next queue uses the new step.
        const origCallback = stepWidget.callback;
        stepWidget.callback = function (...args) {
            origCallback?.apply(this, args);
            syncStep();
        };
    },
});
