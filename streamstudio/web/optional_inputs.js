import { app } from "../../scripts/app.js";

// Visually distinguish OPTIONAL input sockets on Taidal nodes.
//
// ComfyUI's INPUT_TYPES splits inputs into `required` and `optional`. streamstudio's bridge puts a
// runnable's non-mandatory member sockets (val_set / optimizer / loss_fn / metrics / lightning …) and
// a source/op node's optional `source` / `record` under `optional`, while the Mandatory[...] / no-default
// inputs (model / train_set) stay `required` — see bridge._build_runnable_node + the confluid I/O
// contract (input_specs.required). litegraph renders both the same by default, so this extension marks
// the optional SOCKET slots: a " (optional)" label suffix, a dimmed unconnected-dot colour, and a hollow
// slot shape when the litegraph build exposes one. Purely cosmetic — the required/optional EXECUTION
// semantics (an unconnected required socket blocks the run; an unconnected optional one passes None) are
// owned by ComfyUI + bridge.py; this only makes the distinction legible on the canvas.

console.log("StreamStudio: Loaded Taidal.OptionalInputs extension");

const OPTIONAL_DOT_COLOR = "#7d8590"; // muted slate for the unconnected optional dot
const OPTIONAL_SUFFIX = " (optional)";
// litegraph slot shape for a hollow dot, when this build exposes it (older builds don't) — guarded.
const HOLLOW_SHAPE = typeof LiteGraph !== "undefined" && LiteGraph.HollowCircle ? LiteGraph.HollowCircle : null;
// The dynamic list slots (op_0, source_1, logger_0, callbacks_0, … — any `<name>_<digit>`) have their
// OWN add-more UX (op_list.js / source_list.js / object_list.js); don't clutter them with the marker.
const DYNAMIC_LIST_RE = /_\d+$/;

function isTaidalNode(nodeData) {
    const category = nodeData && nodeData.category;
    return typeof category === "string" && category.startsWith("Taidal");
}

// The set of optional input names worth marking — every key under `input.optional` EXCEPT the
// dynamic-list slots. Returns null when the node has none, so we skip hooking it entirely.
function optionalInputNames(nodeData) {
    const optional = (nodeData && nodeData.input && nodeData.input.optional) || {};
    const names = Object.keys(optional).filter((name) => !DYNAMIC_LIST_RE.test(name));
    return names.length ? new Set(names) : null;
}

function markOptionalSlots(node, optionalNames) {
    if (!node.inputs) return;
    for (const input of node.inputs) {
        if (!input || !optionalNames.has(input.name)) continue;
        // Suffix the DISPLAY label (never the functional `name`, which serialisation/linking use).
        // Guard on the suffix itself — idempotent even if litegraph serialised the suffixed label
        // into a saved workflow (so a reload never double-appends).
        const base = input.label || input.name;
        if (!base.endsWith(OPTIONAL_SUFFIX)) input.label = base + OPTIONAL_SUFFIX;
        // Re-applied each pass (idempotent) so styling survives slot rebuilds on graph load / reconnect.
        input.color_off = OPTIONAL_DOT_COLOR;
        if (HOLLOW_SHAPE != null) input.shape = HOLLOW_SHAPE;
    }
    node.setDirtyCanvas(true, true);
}

app.registerExtension({
    name: "Taidal.OptionalInputs",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (!isTaidalNode(nodeData)) return;
        const optionalNames = optionalInputNames(nodeData);
        if (!optionalNames) return;

        // Mark on first creation, after dynamic slots change, and after a saved workflow restores slots.
        for (const hook of ["onNodeCreated", "onConnectionsChange", "onConfigure"]) {
            const original = nodeType.prototype[hook];
            nodeType.prototype[hook] = function () {
                const result = original ? original.apply(this, arguments) : undefined;
                markOptionalSlots(this, optionalNames);
                return result;
            };
        }
    },
});
