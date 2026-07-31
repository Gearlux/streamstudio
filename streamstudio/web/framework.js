import { app } from "../../scripts/app.js";

console.log("ComfyStream: Loaded Taidal.FrameworkSockets extension");

// Refuse a cross-ENGINE object wire as it is dragged.
//
// An object socket may CLAIM the engine whose API it belongs to, as a third segment inside a comma
// union minted by bridge._object_socket:
//
//     RECORDSTREAM_OBJECT:loss:torch,RECORDSTREAM_OBJECT:loss
//
// The union is deliberate: both litegraph and ComfyUI's backend treat a comma type as "compatible if
// the sets INTERSECT", so the bare `…:loss` member keeps a TAGGED producer wireable into an UNTAGGED
// slot (and vice versa) — which is the rule confluid's framework axis defines: exclusion requires TWO
// claims. The flip side is that litegraph's own check then says yes to a torch loss in a Keras slot,
// because both strings contain `…:loss`. This extension applies the missing half on the canvas, so the
// wire is refused while you drag it instead of at Queue time.
//
// bridge._validate_node_inputs is the authoritative backend twin (it also catches a graph loaded from
// disk, or built by anything that is not this canvas). Keep the two rules identical.
//
// Purely a connection GUARD: no socket is retyped, nothing is persisted, and the Retag node stays the
// deliberate escape hatch (its wildcard `*` output claims no engine, so it is never refused).

const OBJECT_TYPE = "RECORDSTREAM_OBJECT";

// The engines a socket type claims — the third segment of each union member. An empty set means
// "makes no claim", which is what keeps an untagged class connecting everywhere.
function frameworksOf(type) {
    const claims = new Set();
    if (typeof type !== "string") return claims;
    for (const member of type.split(",")) {
        const parts = member.trim().split(":");
        if (parts.length >= 3 && parts[0] === OBJECT_TYPE && parts[2]) claims.add(parts[2]);
    }
    return claims;
}

// Exclusion requires two claims — the same predicate as bridge._frameworks_compatible.
function compatible(received, expected) {
    const produced = frameworksOf(received);
    const wanted = frameworksOf(expected);
    if (produced.size === 0 || wanted.size === 0) return true;
    for (const f of produced) if (wanted.has(f)) return true;
    return false;
}

app.registerExtension({
    name: "Taidal.FrameworkSockets",
    async beforeRegisterNodeDef(nodeType) {
        // Chain any previously-installed handler and respect a block it returns (the pattern
        // web/nullable_numeric_socket.js established — several extensions hook this one method).
        const onConnectInput = nodeType.prototype.onConnectInput;
        nodeType.prototype.onConnectInput = function (inputIndex, outputType, outputSlot, sourceNode, sourceSlot) {
            const input = this.inputs && this.inputs[inputIndex];
            // `outputType` is litegraph's own second argument; fall back to reading the source slot
            // for builds that pass it differently.
            const received =
                outputType || (outputSlot && outputSlot.type) || (sourceNode && sourceNode.outputs?.[sourceSlot]?.type);
            if (input && received && !compatible(received, input.type)) {
                const produced = [...frameworksOf(received)].join(", ");
                const wanted = [...frameworksOf(input.type)].join(", ");
                console.warn(
                    `Taidal: refused a ${produced} object on the ${wanted} slot '${input.name}' — ` +
                        `different engines. Use a ${wanted} object, or the Retag node to override.`,
                );
                return false;
            }
            return onConnectInput ? onConnectInput.apply(this, arguments) : true;
        };
    },
});
