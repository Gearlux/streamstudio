import { app } from "../../scripts/app.js";

console.log("ComfyFlux: Loaded Taidal.OpListInputs extension");

// Dynamic op-list inputs for higher-order op nodes (Parallel/Tee/… — any op whose constructor
// takes a list of ops; tagged by a seed `op_0` input of type DATAFLUX_OP in bridge._build_op_node).
// Wire an op node's `op` output into `op_0`; a fresh `op_1` slot appears, and so on — so you build
// the op-list visually. The Python side (`bridge._collect_ops`) gathers every connected `op_N` (in
// index order, dropping empties), so the slot bookkeeping here is purely UX — the backend is robust
// to gaps or a missing extension (you can still wire the single `op_0`).

const OP_TYPE = "DATAFLUX_OP";
const OP_RE = /^op_\d+$/;

function opSlots(node) {
    return (node.inputs || [])
        .map((inp, index) => ({ inp, index }))
        .filter((x) => x.inp && x.inp.type === OP_TYPE && OP_RE.test(x.inp.name));
}

function ensureTrailingEmptyOpSlot(node) {
    let ops = opSlots(node);

    // Index (within the op-slot list) of the last CONNECTED op input.
    let lastConnected = -1;
    ops.forEach((x, k) => {
        if (x.inp.link != null) lastConnected = k;
    });

    // Remove trailing EMPTY op slots, leaving exactly one empty after the last connected one
    // (and never removing `op_0`). Iterate from the end so removeInput's index shifts don't bite.
    for (let k = ops.length - 1; k > Math.max(0, lastConnected + 1); k--) {
        if (ops[k].inp.link == null) {
            node.removeInput(ops[k].index);
        }
    }

    // If the last op slot is now connected (or there are none), append a fresh empty one.
    ops = opSlots(node);
    if (ops.length === 0 || ops[ops.length - 1].inp.link != null) {
        node.addInput("op_" + ops.length, OP_TYPE);
    }
    node.setDirtyCanvas(true, true);
}

app.registerExtension({
    name: "Taidal.OpListInputs",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        const optional = nodeData.input?.optional || {};
        const seed = optional["op_0"];
        if (!seed || seed[0] !== OP_TYPE) return; // not a higher-order op node — nothing to do

        const onConnectionsChange = nodeType.prototype.onConnectionsChange;
        nodeType.prototype.onConnectionsChange = function (slotType, slotIndex, isConnected, link, ioSlot) {
            if (onConnectionsChange) {
                onConnectionsChange.apply(this, arguments);
            }
            // React only to INPUT-side connection changes.
            if (slotType !== LiteGraph.INPUT) return;
            ensureTrailingEmptyOpSlot(this);
        };
    },
});
