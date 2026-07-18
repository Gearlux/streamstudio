import { app } from "../../scripts/app.js";

console.log("ComfyFlux: Loaded Taidal.RetagOutputType extension");

// Dynamic output socket typing for the Retag node (fluxstudio/nodes.py:RetagObjectNode).
//
// Object-member sockets are ROLE-qualified and TASK-AGNOSTIC (SAMPLEFLUX_OBJECT:<role> — see
// bridge._object_socket_for_class), so a node docks where its ROLE matches (any task). The Retag
// node is the escape hatch: it takes any object (wildcard "*" input) and re-emits it under a chosen
// ROLE. ComfyUI types a socket statically at registration, so this extension retypes the OUTPUT
// socket from the `target_role` combo at edit time:
//   (keep) -> "*"                        (pure wildcard passthrough — docks anywhere)
//   (none) -> "SAMPLEFLUX_OBJECT"          (generic object slot)
//   <role> -> "SAMPLEFLUX_OBJECT:<role>"   (docks only into that role's slots)

const OBJECT_BASE = "SAMPLEFLUX_OBJECT";
const WILDCARD = "*";
const KEEP = "(keep)";
const NONE = "(none)";

function outputTypeFor(role) {
    if (!role || role === KEEP) return WILDCARD;
    if (role === NONE) return OBJECT_BASE;
    return OBJECT_BASE + ":" + role;
}

app.registerExtension({
    name: "Taidal.RetagOutputType",
    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        if (nodeData.name !== "Taidal_Retag") return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            if (onNodeCreated) onNodeCreated.apply(this, arguments);
            const self = this;

            const setType = (disconnectIncompatible) => {
                const w = self.widgets && self.widgets.find((x) => x.name === "target_role");
                const out = self.outputs && self.outputs[0];
                if (!w || !out) return;
                const t = outputTypeFor(w.value);
                if (out.type === t) return;
                out.type = t;
                out.name = t === WILDCARD ? "object" : t.split(":").pop();
                if (disconnectIncompatible && out.links && out.links.length) {
                    // Drop existing output links whose target input no longer matches the new type.
                    for (const linkId of [...out.links]) {
                        const link = app.graph.links[linkId];
                        if (!link) continue;
                        const tgt = app.graph.getNodeById(link.target_id);
                        const inp = tgt && tgt.inputs && tgt.inputs[link.target_slot];
                        if (inp && t !== WILDCARD && inp.type !== t && inp.type !== WILDCARD) {
                            self.disconnectOutput(0, tgt);
                        }
                    }
                }
                app.graph.setDirtyCanvas(true, true);
            };

            // Re-type when the user changes the category combo (and break now-incompatible links).
            const cat = this.widgets && this.widgets.find((x) => x.name === "target_role");
            if (cat) {
                const prev = cat.callback;
                cat.callback = function () {
                    const r = prev ? prev.apply(this, arguments) : undefined;
                    setType(true);
                    return r;
                };
            }

            // Initial pass: set the output type from the default, without disconnecting anything.
            setType(false);
        };

        // After a saved workflow restores this node's widget values, re-apply the output type so the
        // saved link's type is honoured (no disconnect — a saved link already matched at save time).
        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            if (onConfigure) onConfigure.apply(this, arguments);
            const w = this.widgets && this.widgets.find((x) => x.name === "target_role");
            const out = this.outputs && this.outputs[0];
            if (w && out) {
                out.type = outputTypeFor(w.value);
                out.name = out.type === WILDCARD ? "object" : out.type.split(":").pop();
            }
        };
    },
});
