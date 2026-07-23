import { app } from "../../scripts/app.js";

console.log("ComfyFlux: Loaded Taidal.NullableNumericSocket extension");

// Connectable nullable-numeric op params.
//
// A nullable numeric op param (Optional[int/float]=None, e.g. NoiseFloorOp.noise_power_db) renders as
// a blank STRING widget so "blank" can mean None (= use the constructor default / stamped-metadata
// fallback — a FLOAT widget can't be empty). But ComfyUI's "Convert to input" types the created
// socket from the widget's DECLARED type (STRING), so a numeric output (a Math result, another op's
// @output like applied_snr_db) is refused by litegraph's string-equality check. The bridge tags these
// widgets `taidal_widget="NULLABLE_NUMERIC"` + `taidal_numeric="FLOAT"|"INT"` (bridge._widget_for);
// this extension RETYPES the converted input socket to that numeric type so a numeric wire connects —
// and the export compilers (export.py / graphio.py) then turn the wire into a ConfigureOp. The inline
// blank-STRING widget itself is unchanged, so the None/fallback semantics are preserved.
//
// Purely a canvas-typing convenience (the sanctioned "glue" class): the authoritative socket type for
// the RUN is irrelevant (a wired param arrives as a real number in execute kwargs), and the export
// keys off the LINK, not the socket type. Defensive + idempotent (mirrors web/retag.js): a no-op when
// the node has no such params or the ComfyUI build differs.

const TAG = "NULLABLE_NUMERIC";

app.registerExtension({
    name: "Taidal.NullableNumericSocket",
    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        // paramName -> numeric socket type ("FLOAT"/"INT"), read from this node's INPUT_TYPES spec.
        const numericByName = {};
        const input = (nodeData && nodeData.input) || {};
        for (const section of [input.required, input.optional]) {
            if (!section) continue;
            for (const [name, spec] of Object.entries(section)) {
                const opts = Array.isArray(spec) && spec.length > 1 ? spec[1] : null;
                if (opts && opts.taidal_widget === TAG && opts.taidal_numeric) {
                    numericByName[name] = opts.taidal_numeric;
                }
            }
        }
        if (!Object.keys(numericByName).length) return;

        // Retype any converted-widget input slot to its numeric type. ComfyUI names a converted input
        // after its widget (== the param name), so match by input name.
        const retypeInputs = (node) => {
            if (!node || !node.inputs) return;
            for (const inp of node.inputs) {
                const t = inp && numericByName[inp.name];
                if (t && inp.type !== t) inp.type = t;
            }
        };

        for (const hook of ["onNodeCreated", "onConfigure", "onConnectionsChange"]) {
            const prev = nodeType.prototype[hook];
            nodeType.prototype[hook] = function () {
                const r = prev ? prev.apply(this, arguments) : undefined;
                retypeInputs(this);
                return r;
            };
        }

        // Just-in-time: when a wire is dragged onto one of these inputs, ensure the slot carries the
        // numeric type BEFORE litegraph's type-equality check, so a numeric output connects. Chains the
        // previous onConnectInput (if any other extension defines one) and respects any block it returns.
        const onConnectInput = nodeType.prototype.onConnectInput;
        nodeType.prototype.onConnectInput = function (inputIndex) {
            const inp = this.inputs && this.inputs[inputIndex];
            const t = inp && numericByName[inp.name];
            if (t && inp.type !== t) inp.type = t;
            return onConnectInput ? onConnectInput.apply(this, arguments) : true;
        };
    },
});
