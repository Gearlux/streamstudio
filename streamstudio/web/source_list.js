import { app } from "../../scripts/app.js";

console.log("StreamStudio: Loaded Taidal.SourceListInputs extension");

// Dynamic source-list inputs for engine nodes whose constructor takes a list of sources
// (JointStream.streams — tagged by a seed `source_0` input of type RECORDSTREAM_SOURCE in
// bridge._build_source_node). Wire a Source/engine node's `source` output into `source_0`; a fresh
// `source_1` slot appears, and so on — so you build the source-list visually. The Python side
// (`bridge._collect_sources`) gathers every connected `source_N` (in index order, dropping empties),
// so the slot bookkeeping here is purely UX — the backend is robust to gaps or a missing extension
// (you can still wire the single `source_0`). Source analogue of web/op_list.js.

const SOURCE_TYPE = "RECORDSTREAM_SOURCE";
const SOURCE_RE = /^source_\d+$/;

function sourceSlots(node) {
    return (node.inputs || [])
        .map((inp, index) => ({ inp, index }))
        .filter((x) => x.inp && x.inp.type === SOURCE_TYPE && SOURCE_RE.test(x.inp.name));
}

function ensureTrailingEmptySourceSlot(node) {
    let sources = sourceSlots(node);

    // Index (within the source-slot list) of the last CONNECTED source input.
    let lastConnected = -1;
    sources.forEach((x, k) => {
        if (x.inp.link != null) lastConnected = k;
    });

    // Remove trailing EMPTY source slots, leaving exactly one empty after the last connected one
    // (and never removing `source_0`). Iterate from the end so removeInput's index shifts don't bite.
    for (let k = sources.length - 1; k > Math.max(0, lastConnected + 1); k--) {
        if (sources[k].inp.link == null) {
            node.removeInput(sources[k].index);
        }
    }

    // If the last source slot is now connected (or there are none), append a fresh empty one.
    sources = sourceSlots(node);
    if (sources.length === 0 || sources[sources.length - 1].inp.link != null) {
        node.addInput("source_" + sources.length, SOURCE_TYPE);
    }
    node.setDirtyCanvas(true, true);
}

app.registerExtension({
    name: "Taidal.SourceListInputs",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        const optional = nodeData.input?.optional || {};
        const seed = optional["source_0"];
        if (!seed || seed[0] !== SOURCE_TYPE) return; // not a source-list node — nothing to do

        const onConnectionsChange = nodeType.prototype.onConnectionsChange;
        nodeType.prototype.onConnectionsChange = function (slotType, slotIndex, isConnected, link, ioSlot) {
            if (onConnectionsChange) {
                onConnectionsChange.apply(this, arguments);
            }
            // React only to INPUT-side connection changes.
            if (slotType !== LiteGraph.INPUT) return;
            ensureTrailingEmptySourceSlot(this);
        };
    },
});
