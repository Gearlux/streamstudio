import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// Live, advisory connection filtering for SAMPLEFLUX_SAMPLE wires.
//
// Every Sample wire carries the ComfyUI socket type "SAMPLEFLUX_SAMPLE", so litegraph's built-in
// string-equality check lets ANY op connect to ANY other. This extension layers the richer SampleFlux
// type contract on top: it fetches each node's declared ACCEPTS/PRODUCES (from /taidal/node_specs) and,
// when the user drags a Sample wire, rejects it if the producer's PRODUCES is not compatible with the
// consumer's ACCEPTS. It hooks `onConnectInput`, which litegraph calls BEFORE the socket-type check and
// which cancels the connection when it returns false.
//
// This is ADVISORY (drag-drop only; graph-load / programmatic wiring bypass onConnectInput). The
// authoritative check is the runtime accept-check in fluxstudio/bridge.py. The matcher here is the
// PERMISSIVE port of sampleflux.typespec.compatible(): anything unknown / Any passes, so un-annotated
// ops are never blocked.

console.log("ComfyFlux: Loaded Taidal.ConnectionValidator extension");

const SAMPLE_TYPE = "SAMPLEFLUX_SAMPLE";

// node-name -> { accepts: SampleType|null, produces: SampleType|null } (SampleType is .to_dict() form)
let nodeSpecs = {};

// dtype family table — mirrors sampleflux.typespec._DTYPE_FAMILIES so dtype matching agrees with Python.
const FLOATING = ["float16", "bfloat16", "float32", "float64"];
const INTEGER = ["int8", "int16", "int32", "int64"];
const UNSIGNED = ["uint8", "uint16", "uint32", "uint64"];
const COMPLEX = ["complex64", "complex128"];
const DTYPE_FAMILIES = {
    floating: FLOATING,
    integer: INTEGER,
    unsigned: UNSIGNED,
    bool: ["bool"],
    complex: COMPLEX,
    numeric: [...FLOATING, ...INTEGER, ...UNSIGNED],
};

function dtypeAccepts(consumer, producer) {
    if (consumer === producer) return true;
    const cf = DTYPE_FAMILIES[consumer];
    if (cf) {
        if (cf.includes(producer)) return true;
        const pf = DTYPE_FAMILIES[producer];
        return !!pf && pf.every((d) => cf.includes(d));
    }
    return false;
}

function dimCompatible(c, p) {
    // Permissive overlap (mirrors Dim.compatible): the two intervals could overlap.
    const cLo = c.min == null ? 0 : c.min;
    const pLo = p.min == null ? 0 : p.min;
    if (c.max != null && pLo > c.max) return false;
    if (p.max != null && p.max < cLo) return false;
    return true;
}

function arrayCompatible(c, p) {
    if (c.ndim != null && p.ndim != null && c.ndim !== p.ndim) return false;
    if (c.shape && p.shape) {
        if (c.shape.length !== p.shape.length) return false;
        for (let i = 0; i < c.shape.length; i++) {
            if (!dimCompatible(c.shape[i], p.shape[i])) return false;
        }
    }
    if (c.dtype != null && p.dtype != null && !dtypeAccepts(c.dtype, p.dtype)) return false;
    if (c.frameworks && p.frameworks) {
        if (!c.frameworks.some((f) => p.frameworks.includes(f))) return false;
    }
    return true;
}

// Permissive single-slot compatibility — the JS port of sampleflux.typespec.compatible().
function slotCompatible(c, p) {
    if (!c || !p) return true; // unknown -> permissive
    if (c.kind === "any" || p.kind === "any") return true;
    if (p.kind === "union") return (p.members || []).every((m) => slotCompatible(c, m));
    if (c.kind === "union") return (c.members || []).some((m) => slotCompatible(m, p));
    if (c.kind !== p.kind) return false;
    if (c.kind === "array") return arrayCompatible(c, p);
    if (c.kind === "python") return c.qualname === p.qualname;
    if (c.kind === "list") return slotCompatible(c.item, p.item);
    if (c.kind === "mapping") {
        const pf = p.fields || {};
        return Object.entries(c.fields || {}).every(([k, cv]) => k in pf && slotCompatible(cv, pf[k]));
    }
    return false;
}

function sampleCompatible(consumer, producer) {
    // consumer = ACCEPTS (SampleType dict), producer = PRODUCES (SampleType dict). Null on either
    // side (un-annotated) -> permit. Both slots (input + target) must be compatible.
    if (!consumer || !producer) return true;
    return slotCompatible(consumer.input, producer.input) && slotCompatible(consumer.target, producer.target);
}

app.registerExtension({
    name: "Taidal.ConnectionValidator",
    async setup() {
        try {
            nodeSpecs = await api.fetchApi("/taidal/node_specs").then((r) => r.json());
        } catch (err) {
            console.warn("ComfyFlux: could not load /taidal/node_specs; connection validation disabled", err);
            nodeSpecs = {};
        }
    },
    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        const originalOnConnectInput = nodeType.prototype.onConnectInput;
        nodeType.prototype.onConnectInput = function (inputIndex, outputType, outputSlot, outputNode, outputIndex) {
            // Chain the original hook first — respect any existing block.
            if (originalOnConnectInput) {
                const original = originalOnConnectInput.apply(this, arguments);
                if (original === false) return false;
            }
            // Only gate our own Sample wire; leave SOURCE / scalar / IMAGE connections to ComfyUI.
            const inputSlot = this.inputs && this.inputs[inputIndex];
            if (!inputSlot || inputSlot.type !== SAMPLE_TYPE) return true;

            const consumer = nodeSpecs[this.comfyClass] && nodeSpecs[this.comfyClass].accepts;
            const producer = outputNode && nodeSpecs[outputNode.comfyClass] && nodeSpecs[outputNode.comfyClass].produces;
            const ok = sampleCompatible(consumer, producer);
            if (!ok) {
                console.warn(
                    `ComfyFlux: blocked incompatible connection ${outputNode && outputNode.comfyClass} -> ${this.comfyClass}`
                );
            }
            return ok;
        };
    },
});
