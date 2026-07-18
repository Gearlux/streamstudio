import { app } from "../../scripts/app.js";

console.log("ComfyFlux: Loaded Taidal.OpListInputs extension");

// Dynamic op-list inputs for higher-order op nodes (Parallel/Tee/Enable/Flux — any op whose
// constructor takes a list of ops; tagged by a seed `<prefix>_0` input of type SAMPLEFLUX_OP in
// bridge._build_op_node / _build_source_node). Wire an op node's `op` output into `<prefix>_0`; a
// fresh `<prefix>_1` slot appears, and so on — so you build the op-list visually. The Python side
// (`bridge._collect_indexed`) gathers every connected `<prefix>_N` (in index order, dropping empties),
// so the slot bookkeeping here is purely UX — the backend is robust to gaps or a missing extension.
//
// GENERALISED to ANY `<prefix>_N` OP family on the node (mirroring web/object_list.js): the sampleflux
// higher-order ops use the `op_N` family (prefix "op"), while a waivefront-helios composite that takes
// SEVERAL op lists (HeliosIfOp → `true_transforms_N` + `false_transforms_N`) gets one family per
// op-list param, each grown independently.

const OP_TYPE = "SAMPLEFLUX_OP";
const INDEXED_RE = /^(.+)_(\d+)$/;

// Every <prefix> for which this node currently has a SAMPLEFLUX_OP `<prefix>_<digit>` input.
function familyPrefixes(node) {
    const prefixes = new Set();
    for (const inp of node.inputs || []) {
        if (!inp || inp.type !== OP_TYPE) continue;
        const m = INDEXED_RE.exec(inp.name);
        if (m) prefixes.add(m[1]);
    }
    return [...prefixes];
}

function slotsFor(node, prefix) {
    const re = new RegExp(`^${prefix}_\\d+$`);
    return (node.inputs || [])
        .map((inp, index) => ({ inp, index }))
        .filter((x) => x.inp && x.inp.type === OP_TYPE && re.test(x.inp.name));
}

function ensureTrailingEmpty(node, prefix) {
    let slots = slotsFor(node, prefix);

    // Index (within this prefix's slot list) of the last CONNECTED op input.
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
        node.addInput(`${prefix}_${slots.length}`, OP_TYPE);
    }
    node.setDirtyCanvas(true, true);
}

app.registerExtension({
    name: "Taidal.OpListInputs",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        const optional = nodeData.input?.optional || {};
        // Any op-list seed (<prefix>_0 of SAMPLEFLUX_OP)? (object_0/source_0 are OBJECT/SOURCE-typed,
        // handled by their own extensions, so the type filter excludes them.)
        const hasSeed = Object.keys(optional).some((k) => {
            const m = INDEXED_RE.exec(k);
            return m && m[2] === "0" && optional[k]?.[0] === OP_TYPE;
        });
        if (!hasSeed) return; // not a higher-order op node — nothing to do

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
