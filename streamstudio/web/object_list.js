import { app } from "../../scripts/app.js";

console.log("ComfyStream: Loaded Taidal.ObjectListInputs extension");

// Dynamic object-list inputs for object nodes whose constructor takes a LIST of objects (a
// pytorch_lightning.Trainer's `logger` / `callbacks` / `plugins` — tagged by a seed `<name>_0` input
// of type RECORDSTREAM_OBJECT in bridge._build_object_node, via _admits_object_list). Wire a producer
// node's `instance` output into `logger_0`; a fresh `logger_1` slot appears, and so on — so you build
// the list visually. The Python side (`bridge._collect_indexed`) gathers every connected `<name>_N`
// (in index order, dropping empties) into a list, so the slot bookkeeping here is purely UX — the
// backend is robust to gaps or a missing extension (you can still wire the single `<name>_0`). Object
// analogue of web/op_list.js / web/source_list.js, generalised to ANY `<name>_N` OBJECT family on the
// node (one object node can have several: logger_N AND callbacks_N).

const OBJECT_TYPE = "RECORDSTREAM_OBJECT";
const INDEXED_RE = /^(.+)_(\d+)$/;

// Object sockets may be role-qualified ("RECORDSTREAM_OBJECT:model"); list seeds are role-less but compare
// base-aware to be safe.
function baseType(t) {
    return typeof t === "string" && t.includes(":") ? t.split(":")[0] : t;
}

// Every <prefix> for which this node currently has an OBJECT `<prefix>_<digit>` input.
function familyPrefixes(node) {
    const prefixes = new Set();
    for (const inp of node.inputs || []) {
        if (!inp || baseType(inp.type) !== OBJECT_TYPE) continue;
        const m = INDEXED_RE.exec(inp.name);
        if (m) prefixes.add(m[1]);
    }
    return [...prefixes];
}

function slotsFor(node, prefix) {
    const re = new RegExp(`^${prefix}_\\d+$`);
    return (node.inputs || [])
        .map((inp, index) => ({ inp, index }))
        .filter((x) => x.inp && baseType(x.inp.type) === OBJECT_TYPE && re.test(x.inp.name));
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
        node.addInput(`${prefix}_${slots.length}`, OBJECT_TYPE);
    }
    node.setDirtyCanvas(true, true);
}

app.registerExtension({
    name: "Taidal.ObjectListInputs",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        const optional = nodeData.input?.optional || {};
        // Any object-list seed (<prefix>_0 of RECORDSTREAM_OBJECT)? (op_0/source_0 are OP/SOURCE-typed,
        // handled by their own extensions, so the type filter excludes them.)
        const hasSeed = Object.keys(optional).some((k) => {
            const m = INDEXED_RE.exec(k);
            return m && m[2] === "0" && baseType(optional[k]?.[0]) === OBJECT_TYPE;
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
