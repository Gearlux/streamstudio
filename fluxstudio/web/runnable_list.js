import { app } from "../../scripts/app.js";

console.log("ComfyFlux: Loaded Taidal.RunnableListInputs extension");

// Dynamic runnable-list inputs for a workflow COMBINATOR node whose constructor takes a LIST of
// runnables (sampleflux.workflow.Sequence's `steps` — tagged by a seed `<name>_0` input of type
// SAMPLEFLUX_RUNNABLE in bridge._build_runnable_node, via _RUNNABLE_LIST_PARAMS). Wire a trainer /
// evaluator / nested-combinator node's `runnable` output into `steps_0`; a fresh `steps_1` slot
// appears, and so on — so you build the workflow step list visually. The Python side
// (`bridge._collect_indexed`) gathers every connected `<name>_N` (in index order, dropping empties)
// into the list, so the slot bookkeeping here is purely UX — the backend is robust to gaps or a
// missing extension (you can still wire the single `<name>_0`). Runnable analogue of
// web/object_list.js / web/op_list.js / web/source_list.js, by SAMPLEFLUX_RUNNABLE socket type.

const RUNNABLE_TYPE = "SAMPLEFLUX_RUNNABLE";
const INDEXED_RE = /^(.+)_(\d+)$/;

function baseType(t) {
    return typeof t === "string" && t.includes(":") ? t.split(":")[0] : t;
}

// Every <prefix> for which this node currently has a RUNNABLE `<prefix>_<digit>` input.
function familyPrefixes(node) {
    const prefixes = new Set();
    for (const inp of node.inputs || []) {
        if (!inp || baseType(inp.type) !== RUNNABLE_TYPE) continue;
        const m = INDEXED_RE.exec(inp.name);
        if (m) prefixes.add(m[1]);
    }
    return [...prefixes];
}

function slotsFor(node, prefix) {
    const re = new RegExp(`^${prefix}_\\d+$`);
    return (node.inputs || [])
        .map((inp, index) => ({ inp, index }))
        .filter((x) => x.inp && baseType(x.inp.type) === RUNNABLE_TYPE && re.test(x.inp.name));
}

function ensureTrailingEmpty(node, prefix) {
    let slots = slotsFor(node, prefix);

    // Index (within this prefix's slot list) of the last CONNECTED input.
    let lastConnected = -1;
    slots.forEach((x, k) => {
        if (x.inp.link != null) lastConnected = k;
    });

    // Remove trailing EMPTY slots, leaving exactly one empty after the last connected one (never
    // removing `<prefix>_0`). Iterate from the end so removeInput's index shifts don't bite.
    for (let k = slots.length - 1; k > Math.max(0, lastConnected + 1); k--) {
        if (slots[k].inp.link == null) {
            node.removeInput(slots[k].index);
        }
    }

    // If the last slot is now connected (or there are none), append a fresh empty one.
    slots = slotsFor(node, prefix);
    if (slots.length === 0 || slots[slots.length - 1].inp.link != null) {
        node.addInput(`${prefix}_${slots.length}`, RUNNABLE_TYPE);
    }
    node.setDirtyCanvas(true, true);
}

app.registerExtension({
    name: "Taidal.RunnableListInputs",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        const optional = nodeData.input?.optional || {};
        // Any runnable-list seed (<prefix>_0 of SAMPLEFLUX_RUNNABLE)?
        const hasSeed = Object.keys(optional).some((k) => {
            const m = INDEXED_RE.exec(k);
            return m && m[2] === "0" && baseType(optional[k]?.[0]) === RUNNABLE_TYPE;
        });
        if (!hasSeed) return;

        const onConnectionsChange = nodeType.prototype.onConnectionsChange;
        nodeType.prototype.onConnectionsChange = function (slotType, slotIndex, isConnected, link, ioSlot) {
            if (onConnectionsChange) {
                onConnectionsChange.apply(this, arguments);
            }
            // React only to INPUT-side connection changes.
            if (slotType !== LiteGraph.INPUT) return;
            for (const prefix of familyPrefixes(this)) ensureTrailingEmpty(this, prefix);
        };
    },
});
