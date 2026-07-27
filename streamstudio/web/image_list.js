import { app } from "../../scripts/app.js";

console.log("ComfyStream: Loaded Taidal.ImageListInputs extension");

// Dynamic image-list inputs for the Image Grid node — tagged by a seed `image_0` optional input
// of type IMAGE. Wire any IMAGE output into `image_0`; a fresh `image_1` slot appears, and so on.
// The Python side (_collect_images in nodes.py) gathers every connected `image_N` (in index order,
// dropping None / unconnected slots), so slot bookkeeping here is purely UX — the backend is robust
// to gaps or a missing extension. Source analogue of web/op_list.js / web/source_list.js.

const IMAGE_TYPE = "IMAGE";
const IMAGE_RE = /^image_\d+$/;

function imageSlots(node) {
    return (node.inputs || [])
        .map((inp, index) => ({ inp, index }))
        .filter((x) => x.inp && x.inp.type === IMAGE_TYPE && IMAGE_RE.test(x.inp.name));
}

function ensureTrailingEmptyImageSlot(node) {
    let slots = imageSlots(node);

    // Index (within the image-slot list) of the last CONNECTED image input.
    let lastConnected = -1;
    slots.forEach((x, k) => {
        if (x.inp.link != null) lastConnected = k;
    });

    // Remove trailing EMPTY image slots, leaving exactly one empty after the last connected one
    // (and never removing `image_0`). Iterate from the end so removeInput's index shifts don't bite.
    for (let k = slots.length - 1; k > Math.max(0, lastConnected + 1); k--) {
        if (slots[k].inp.link == null) {
            node.removeInput(slots[k].index);
        }
    }

    // If the last image slot is now connected (or there are none), append a fresh empty one.
    slots = imageSlots(node);
    if (slots.length === 0 || slots[slots.length - 1].inp.link != null) {
        node.addInput("image_" + slots.length, IMAGE_TYPE);
    }
    node.setDirtyCanvas(true, true);
}

app.registerExtension({
    name: "Taidal.ImageListInputs",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        const optional = nodeData.input?.optional || {};
        const seed = optional["image_0"];
        if (!seed || seed[0] !== IMAGE_TYPE) return; // not an image-list node — nothing to do

        const onConnectionsChange = nodeType.prototype.onConnectionsChange;
        nodeType.prototype.onConnectionsChange = function (slotType, slotIndex, isConnected, link, ioSlot) {
            if (onConnectionsChange) {
                onConnectionsChange.apply(this, arguments);
            }
            // React only to INPUT-side connection changes.
            if (slotType !== LiteGraph.INPUT) return;
            ensureTrailingEmptyImageSlot(this);
        };
    },
});
