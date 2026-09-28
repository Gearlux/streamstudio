import { app } from "../../scripts/app.js";

console.log("StreamStudio: Loaded Taidal.StreamOp extension");

// Compile a subgraph you drew into a Stream op — ON REQUEST, never behind your back.
//
// A `Stream`'s ops are recordstream callables over a RECORD, and a subgraph instance can carry one
// on a promoted `RECORDSTREAM_OP` output — verified in the live editor: the boundary preserves the
// custom socket type, `LiteGraph.isValidConnection` accepts the drag, and the Stream grows its next
// `op_N` on connect. What was missing was the CEREMONY: knowing `Taidal_SubgraphOp` ("Extract as
// Op") exists, placing it on the end of the record chain, and promoting its output by hand.
//
// **"Taidal: compile to Op"** does all of it, and only when you ask. An earlier version ran on every
// conversion automatically; that is withdrawn on user instruction (2026-08-26) — a canvas edit that
// happens without being asked for is one you have to undo, and it interacted badly with ComfyUI's
// own Convert to Subgraph. Compiling is now a decision, it reports what it built, and it is
// re-runnable: the nodes it owns are marked and rebuilt, so changing the boundary and compiling
// again just works.
//
// WHAT IT BUILDS, and why those nodes and not others:
//
//   * every op in the rectangle joined into ONE chain (`chainAllOps`), in canvas reading order, so
//     the pipeline is the whole graph rather than whichever branch happened to reach the end.
//     Anything you wired by hand keeps the order you gave it; only the gaps are filled.
//   * a *Build Record* gathering the nodes that feed the promoted OUTPUTS, one socket per label.
//     The labels you promote ARE the record keys, so the wiring states the contract:
//     `Extract → Build Record → Extract as Op → op`, every arrow present.
//   * an *Extract as Op* terminator on the end, its `op` promoted to the boundary.
//
// WHAT IT DOES NOT DO: turn any of that into runnable ops. `Extract from Record` and `Build Record`
// are canvas glue with no recordstream counterpart, so neither can appear inside a compiled
// `Pipeline`. The COMPILER translates the shape — `export.expand_record_builders` reads each
// builder entry as `RenameField(field -> label)` and closes with `SelectFields` — which is what
// lets the canvas keep the graph you drew. An earlier version placed those rename/select nodes on
// the canvas instead; they ran correctly and read as nonsense, because nothing connected them to
// the outputs they described (user instruction 2026-08-26: *"this makes the whole process visible,
// now it seems a lot of hocus pocus without connections"*).

const RECORD_TYPE = "RECORDSTREAM_RECORD";
const OP_TYPE = "RECORDSTREAM_OP";
const TERMINATOR_TYPE = "Taidal_SubgraphOp"; // streamstudio.nodes.SubgraphOpExtractNode
const RECORD_SLOT = "record";
const OP_SLOT = "op";
const TERMINATOR_OFFSET = [320, 90]; // placed down-right of the tail so it never lands on top of it
const EXTRACT_TYPE = "Taidal_ExtractRecord"; // streamstudio.nodes.RecordExtractorNode
const BUILDER_TYPE = "Taidal_RecordBuilder"; // streamstudio.nodes.RecordBuilderNode
const CONTRACT_MARK = "taidal_contract"; // node.properties flag: built by us, rebuilt on every pass

const slotsOf = (node, key) => (node && node[key]) || [];
const hasType = (node, key, type) => slotsOf(node, key).some((s) => s.type === type);

/** A record TRANSFORMATION: it hands a record onward. The terminator itself is not one. */
function isRecordOp(node) {
    return node?.type !== TERMINATOR_TYPE && hasType(node, "outputs", RECORD_TYPE);
}

/** Index of a node's first slot of `type`, or -1. */
function slotIndex(node, key, type) {
    return slotsOf(node, key).findIndex((s) => s.type === type);
}

/** Record-wire adjacency INSIDE a subgraph: {from: Set(to)} over node ids. */
function recordEdges(sg) {
    const edges = new Map();
    for (const node of sg._nodes || []) {
        for (const out of slotsOf(node, "outputs")) {
            if (out.type !== RECORD_TYPE) continue;
            for (const linkId of out.links || []) {
                const link = sg._links?.get?.(linkId) ?? sg._links?.[linkId];
                if (!link) continue;
                if (!edges.has(node.id)) edges.set(node.id, new Set());
                edges.get(node.id).add(link.target_id);
            }
        }
    }
    return edges;
}

/** True when this node's record output feeds another node inside the subgraph. */
function feedsARecord(sg, node, edges) {
    return (edges.get(node.id)?.size ?? 0) > 0;
}

/** True when a record wire arrives at this node from another INNER node. */
function fedByARecord(sg, node) {
    const idx = slotIndex(node, "inputs", RECORD_TYPE);
    if (idx < 0) return false;
    const linkId = slotsOf(node, "inputs")[idx]?.link;
    if (linkId == null) return false;
    const link = sg._links?.get?.(linkId) ?? sg._links?.[linkId];
    return !!link && String(link.origin_id) !== String(sg.inputNode?.id);
}

/** Reading order: left to right, then top to bottom — how the canvas is laid out and read. */
function byPosition(a, b) {
    const ax = a.pos?.[0] ?? 0, bx = b.pos?.[0] ?? 0;
    if (ax !== bx) return ax - bx;
    const ay = a.pos?.[1] ?? 0, by = b.pos?.[1] ?? 0;
    if (ay !== by) return ay - by;
    return String(a.id) < String(b.id) ? -1 : 1;
}

/**
 * The record-wire COMPONENTS among a subgraph's ops, each as `{head, tail}`, in reading order.
 *
 * A component is a run of ops already wired record → record. An op the user dropped in without
 * wiring it is a component of one. Both are the same thing to the caller, which is the point: the
 * pipeline is EVERY op in the rectangle, however much of the wiring the user did by hand.
 */
function opComponents(sg) {
    const ops = (sg._nodes || []).filter(isRecordOp);
    if (!ops.length) return [];
    const edges = recordEdges(sg);
    const byId = new Map(ops.map((n) => [String(n.id), n]));
    const seen = new Set();
    const components = [];
    for (const start of [...ops].sort(byPosition)) {
        if (seen.has(String(start.id))) continue;
        // Undirected walk: collect everything reachable through record wires either way.
        const members = [];
        const queue = [start];
        while (queue.length) {
            const node = queue.pop();
            const key = String(node.id);
            if (seen.has(key)) continue;
            seen.add(key);
            members.push(node);
            for (const target of edges.get(node.id) ?? []) {
                const other = byId.get(String(target));
                if (other && !seen.has(String(other.id))) queue.push(other);
            }
            for (const [from, targets] of edges) {
                if (!targets.has(node.id)) continue;
                const other = byId.get(String(from));
                if (other && !seen.has(String(other.id))) queue.push(other);
            }
        }
        const inner = new Set(members.map((n) => String(n.id)));
        const head = members.find((n) => !fedByARecord(sg, n)) ?? members.sort(byPosition)[0];
        const tail =
            members.find((n) => ![...(edges.get(n.id) ?? [])].some((t) => inner.has(String(t)))) ??
            members[members.length - 1];
        components.push({ head, tail, members });
    }
    return components;
}

/**
 * Wire every component into ONE chain, in reading order, and return its final tail.
 *
 * This is what makes the rectangle's `op` mean the WHOLE graph. Measured before it existed: three
 * ops dropped into a subgraph without wiring compiled to `Pipeline([ToTensor])` — the terminator
 * takes its record-ANCESTOR cone, and two of the three were nobody's ancestor, so they vanished
 * silently. The order is the canvas order because that is the one the user chose and can see;
 * anything the user already wired is left exactly as wired, and only the gaps BETWEEN components
 * are joined.
 */
function chainAllOps(sg) {
    const components = opComponents(sg);
    if (!components.length) return null;
    for (let i = 1; i < components.length; i += 1) {
        const from = components[i - 1].tail;
        const to = components[i].head;
        const fromSlot = slotIndex(from, "outputs", RECORD_TYPE);
        const toSlot = slotIndex(to, "inputs", RECORD_TYPE);
        if (fromSlot < 0 || toSlot < 0) continue;
        try {
            from.connect(fromSlot, to, toSlot);
        } catch (err) {
            console.debug("Taidal.StreamOp: could not join two op components", err);
        }
    }
    return components[components.length - 1].tail;
}

/** Give the boundary its `record` input and wire it to the head op. No-op if already there. */
function ensureRecordInput(sg) {
    let slot = (sg.inputs || []).findIndex((i) => i.type === RECORD_TYPE);
    if (slot < 0) {
        sg.addInput(RECORD_SLOT, RECORD_TYPE);
        slot = (sg.inputs || []).length - 1;
    }
    const head = opComponents(sg)[0]?.head ?? null;
    if (!head) return; // no op chain to feed — the boundary slot is what mattered
    const headSlot = slotIndex(head, "inputs", RECORD_TYPE);
    if (headSlot < 0) return;
    if (slotsOf(head, "inputs")[headSlot]?.link != null) return; // already fed — leave it alone
    try {
        sg.inputNode?.connect?.(slot, head, headSlot);
    } catch (err) {
        console.debug("Taidal.StreamOp: could not wire the record boundary", err);
    }
}

/** What a subgraph OUTPUT slot promises, read off the node that feeds it. */
function outputPromises(sg) {
    const promises = [];
    const outputs = sg.outputs || [];
    for (let slot = 0; slot < outputs.length; slot += 1) {
        const label = outputs[slot]?.name;
        if (!label || label === OP_SLOT) continue; // our own op socket is not part of the contract
        const link = [...(sg._links?.values?.() ?? Object.values(sg._links ?? {}))].find(
            (l) => String(l?.target_id) === String(sg.outputNode?.id) && l?.target_slot === slot,
        );
        const producer = link && (sg._nodes || []).find((n) => String(n.id) === String(link.origin_id));
        if (!producer) continue;
        if (producer.outputs?.[link.origin_slot]?.type === RECORD_TYPE) {
            promises.push({ label, kind: "record", producer, slot: link.origin_slot });
        } else {
            // A typed value promoted under `label` IS a record entry; the compiler derives the
            // rename from the producing node (an extract carries the source `field`).
            promises.push({
                label,
                kind: "value",
                producer,
                slot: link.origin_slot,
                source: producer.type === EXTRACT_TYPE ? String(producer.widgets?.[0]?.value ?? "") : "",
            });
        }
    }
    return promises;
}

/** Remove what a previous compile owned, so the boundary can be re-read from scratch. */
function clearContractOps(sg) {
    for (const node of [...(sg._nodes || [])]) {
        if (node?.properties?.[CONTRACT_MARK]) sg.remove(node);
    }
}

/**
 * Wire the nodes that feed the promoted OUTPUTS into a *Build Record*, and return it.
 *
 * This is the shape the user reads: ``Extract → Build Record → Extract as Op → op``, every arrow
 * present. An earlier version instead dropped loose ``RenameField`` / ``SelectFields`` nodes beside
 * the extracts — equivalent to run, and unreadable, because nothing on the canvas connected them to
 * the outputs they described. The translation into record ops now happens in the COMPILER
 * (``export.expand_record_builders``), where it belongs: the canvas keeps the shape you drew.
 *
 * Each builder socket is NAMED after the output label it packs, so the node states the record it
 * makes. Returns ``null`` when no promoted output is fed by anything.
 */
function buildRecordFromOutputs(sg) {
    const promises = outputPromises(sg);
    const values = promises.filter((p) => p.kind === "value" && p.producer);
    if (!values.length) return null;
    const builder = window.LiteGraph.createNode(BUILDER_TYPE);
    if (!builder) return null;
    builder.properties = { ...(builder.properties || {}), [CONTRACT_MARK]: true };
    const anchor = values[0].producer;
    builder.pos = [(anchor.pos?.[0] ?? 0) + TERMINATOR_OFFSET[0], anchor.pos?.[1] ?? 0];
    sg.add(builder);
    // Start from nothing: the node declares no socket, and the compile names one per label.
    builder.inputs = [];
    values.forEach((promise, index) => {
        builder.addInput(promise.label, "*");
        try {
            promise.producer.connect(promise.slot, builder, index);
        } catch (err) {
            console.debug("Taidal.StreamOp: could not wire an output into the record builder", err);
        }
    });
    return builder;
}

/** Place the terminator on the spine tail and promote its `op`. No-op if the subgraph has one. */
function ensureOpOutput(sg, tail) {
    if ((sg._nodes || []).some((n) => n.type === TERMINATOR_TYPE)) return true; // user placed their own
    const terminator = window.LiteGraph.createNode(TERMINATOR_TYPE);
    if (!terminator) return false; // streamstudio not registered (should not happen inside the studio)
    terminator.properties = { ...(terminator.properties || {}), [CONTRACT_MARK]: true };
    terminator.pos = [(tail.pos?.[0] ?? 0) + TERMINATOR_OFFSET[0], (tail.pos?.[1] ?? 0) + TERMINATOR_OFFSET[1]];
    sg.add(terminator);
    const tailSlot = slotIndex(tail, "outputs", RECORD_TYPE);
    if (tailSlot >= 0) tail.connect(tailSlot, terminator, 0);
    if (!(sg.outputs || []).some((o) => o.type === OP_TYPE)) sg.addOutput(OP_SLOT, OP_TYPE);
    const boundary = (sg.outputs || []).find((o) => o.type === OP_TYPE);
    if (!boundary) return false;
    // A boundary slot is connected through the SubgraphOutput's OWN `connect(outputSlot, node)`.
    // `node.connect(slot, sg.outputNode, index)` silently returns null against it — measured — which
    // left the terminator sitting there with its `op` going nowhere: the socket existed on the
    // instance and the wire the user was looking for did not.
    try {
        boundary.connect(terminator.outputs[0], terminator);
    } catch (err) {
        console.debug("Taidal.StreamOp: could not promote the op output", err);
        return false;
    }
    return (boundary.linkIds || []).length > 0;
}

/** Tell the user what happened — a compile that reports nothing is one they cannot trust. */
function report(severity, summary, detail) {
    try {
        app.extensionManager.toast.add({ severity, summary, detail, life: 6000 });
    } catch (err) {
        console[severity === "error" ? "error" : "log"](`Taidal.StreamOp: ${summary} — ${detail}`);
    }
}

/** Compile ONE subgraph. Returns a short description of what was built, or a reason it was not. */
function compileToOp(sg) {
    if (!sg || !Array.isArray(sg._nodes)) return { ok: false, reason: "that is not a subgraph" };
    const promises = outputPromises(sg);
    const hasOps = sg._nodes.some(isRecordOp);
    const statesAContract = promises.some((p) => p.kind === "value" && p.producer);
    if (!hasOps && !statesAContract) {
        return {
            ok: false,
            reason:
                "it holds nothing a Stream can run: no record-to-record op, and no promoted output " +
                "naming a record entry. Promote an output (its label becomes the record key), or " +
                "drop an op inside.",
        };
    }
    if (promises.filter((p) => p.kind === "record").length > 1) {
        return {
            ok: false,
            reason:
                "two promoted outputs each carry a whole record. Combining them is a fan-in, and a " +
                "Pipeline is a straight op list — use the flow export for a branchy graph.",
        };
    }
    // Rebuildable: drop what a previous compile owned, then lay it out again from the boundary.
    clearContractOps(sg);
    ensureRecordInput(sg);
    const chainTail = chainAllOps(sg);
    const tail = buildRecordFromOutputs(sg) ?? chainTail;
    if (!tail) return { ok: false, reason: "nothing to put the terminator on" };
    const built = ensureOpOutput(sg, tail);
    if (!built) return { ok: false, reason: "the Extract as Op terminator could not be placed" };
    const contract = promises.filter((p) => p.kind === "value").map((p) => p.label);
    return {
        ok: true,
        summary: `${sg.name || "subgraph"} → op`,
        detail: contract.length
            ? `record contract: {${contract.join(", ")}}. Drag the rectangle's 'op' output into a Stream.`
            : "Drag the rectangle's 'op' output into a Stream.",
    };
}

/** The subgraph the user means: the selected instance, or the one currently open. */
function targetSubgraph() {
    const selected = [...(app.canvas?.selectedItems ?? [])].find((n) => n?.subgraph);
    if (selected) return selected.subgraph;
    const node = app.graph?._nodes?.find((n) => n.subgraph && app.canvas?.selected_nodes?.[n.id]);
    if (node) return node.subgraph;
    const open = app.canvas?.graph;
    return open && open !== app.graph && Array.isArray(open._nodes) ? open : null;
}

function commandCompileToOp() {
    const sg = targetSubgraph();
    if (!sg) {
        report("warn", "Nothing to compile", "Select a subgraph node, or open the subgraph you want to compile.");
        return;
    }
    const result = compileToOp(sg);
    if (result.ok) report("success", result.summary, result.detail);
    else report("warn", `Not compiled: ${sg.name || "subgraph"}`, result.reason);
}

/** Convert the current selection into a subgraph AND compile it, in one action. */
function commandFromSelection() {
    const before = new Set(allSubgraphs().map((sg) => sg.id));
    try {
        app.extensionManager?.command?.execute?.("Comfy.Graph.ConvertToSubgraph");
    } catch (err) {
        report("error", "Convert to Subgraph failed", String(err));
        return;
    }
    queueMicrotask(() => {
        const fresh = allSubgraphs().filter((sg) => !before.has(sg.id));
        if (!fresh.length) {
            report("warn", "Nothing converted", "Select the nodes you want in the rectangle first.");
            return;
        }
        for (const sg of fresh) {
            const result = compileToOp(sg);
            if (result.ok) report("success", result.summary, result.detail);
            else report("warn", `Converted, but not compiled: ${sg.name || "subgraph"}`, result.reason);
        }
    });
}

app.registerExtension({
    name: "Taidal.StreamOp",
    // NOTHING runs on nodeCreated / afterConfigureGraph any more: compiling is a decision the user
    // makes (2026-08-26 instruction), so the canvas is never edited without being asked.
    setup() {
        // A subgraph INSTANCE is not a registered node type — it is typed by the subgraph's uuid —
        // so `beforeRegisterNodeDef` never sees one and the menu item never appeared (measured).
        // The canvas-level hook is the one that fires for every node, instances included.
        const canvasProto = window.LGraphCanvas?.prototype;
        if (!canvasProto || canvasProto.__taidalStreamOpMenu) return;
        canvasProto.__taidalStreamOpMenu = true;
        const original = canvasProto.getNodeMenuOptions;
        canvasProto.getNodeMenuOptions = function (node) {
            const options = original?.apply(this, arguments) ?? [];
            if (node?.subgraph) {
                options.push(null, {
                    content: "Taidal: compile to Op",
                    callback: () => {
                        const result = compileToOp(node.subgraph);
                        if (result.ok) report("success", result.summary, result.detail);
                        else report("warn", `Not compiled: ${node.subgraph.name || "subgraph"}`, result.reason);
                        app.graph.setDirtyCanvas(true, true);
                    },
                });
            }
            return options;
        };
    },
    commands: [
        {
            id: "taidal.compileToOp",
            label: "Taidal: compile subgraph to Op",
            function: () => commandCompileToOp(),
        },
        {
            id: "taidal.streamOpFromSelection",
            label: "Taidal: make a Stream op from selection",
            function: () => commandFromSelection(),
        },
    ],
    menuCommands: [{ path: ["Taidal"], commands: ["taidal.compileToOp", "taidal.streamOpFromSelection"] }],
});
