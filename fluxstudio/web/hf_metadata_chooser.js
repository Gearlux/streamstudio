import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

console.log("ComfyFlux: Loaded Taidal.HFMetadataChooser extension");

// Optional MULTI-select column picker for a HuggingFace source's `metadata_features` input (tagged
// taidal_widget="HF_METADATA" by fluxstudio/bridge.py). The underlying STRING widget holds a
// comma-separated list and stays freely editable. This adds an "add column" combo that APPENDS the
// picked column (multi-select by picking repeatedly), and offers a special "*" entry meaning
// "every column except input/target" (the METADATA_ALL_FEATURES sentinel resolved by
// sampleflux.sources). Columns come from /taidal/hf_dataset_features (the same route the feature
// picker uses) for the node's current dataset-id field, minus whatever is currently mapped as
// input_feature / target_feature (those are already consumed, and "*" covers "the rest" anyway).

const PLACEHOLDER = "— add a column —";
const STAR = "*"; // METADATA_ALL_FEATURES: all columns except input/target
const _featuresCache = new Map(); // "path|name|split" -> array of feature names

function cacheKey(path, name, split) {
    return [path || "", name || "", split || ""].join("|");
}

async function fetchFeatures(path, name, split) {
    const key = cacheKey(path, name, split);
    if (_featuresCache.has(key)) return _featuresCache.get(key);
    let features = [];
    try {
        const params = new URLSearchParams({ path: path || "" });
        if (name) params.set("name", name);
        if (split) params.set("split", split);
        const res = await api.fetchApi("/taidal/hf_dataset_features?" + params.toString());
        const data = await res.json();
        features = Array.isArray(data.features) ? data.features : [];
    } catch (err) {
        console.warn("HFMetadataChooser: failed to list dataset features", err);
        features = [];
    }
    _featuresCache.set(key, features);
    return features;
}

app.registerExtension({
    name: "Taidal.HFMetadataChooser",
    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        const inputs = nodeData.input?.required || {};
        const metaFields = [];
        let idField = null;

        for (const [name, config] of Object.entries(inputs)) {
            const tag = config[1] && config[1].taidal_widget;
            if (tag === "HF_METADATA") metaFields.push(name);
            else if (tag === "HF_DATASET_ID" && idField === null) idField = name;
        }

        if (metaFields.length === 0) return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;

        nodeType.prototype.onNodeCreated = function () {
            if (onNodeCreated) {
                onNodeCreated.apply(this, arguments);
            }
            const node = this;

            const widgetValue = (n) => {
                const w = n ? node.widgets.find((x) => x.name === n) : null;
                return w ? w.value : undefined;
            };

            // Append `value` to the comma-separated metadata field. "*" is exclusive (it already
            // means "the rest"); a specific column replaces a lone "*" or empty field, else is
            // appended (deduped). The field stays freely editable.
            const appendToField = (name, value) => {
                const target = node.widgets.find((w) => w.name === name);
                if (!target) return;
                if (value === STAR) {
                    target.value = STAR;
                } else {
                    const cur = String(target.value || "").trim();
                    if (cur === "" || cur === STAR) {
                        target.value = value;
                    } else {
                        const parts = cur
                            .split(",")
                            .map((s) => s.trim())
                            .filter(Boolean);
                        if (!parts.includes(value)) parts.push(value);
                        target.value = parts.join(", ");
                    }
                }
                if (typeof target.callback === "function") target.callback(target.value);
                app.graph.setDirtyCanvas(true);
            };

            const pickers = {};
            for (const name of metaFields) {
                const picker = node.addWidget(
                    "combo",
                    "🗂️ " + name + " (add column)",
                    PLACEHOLDER,
                    (value) => {
                        if (!value || value === PLACEHOLDER) return;
                        appendToField(name, value);
                        // Reset to the placeholder so the next pick adds another column.
                        picker.value = PLACEHOLDER;
                    },
                    { values: [PLACEHOLDER, STAR] }
                );
                // The picker is a helper — don't persist it into the saved workflow.
                picker.serialize = false;
                pickers[name] = picker;
            }

            // Options = placeholder + "*" + the dataset's columns MINUS the ones already used as
            // input_feature / target_feature (those are consumed; "*" covers the remainder).
            const refreshColumns = () => {
                const path = idField ? widgetValue(idField) : undefined;
                const subset = widgetValue("name");
                const split = widgetValue("split");
                if (!path) {
                    for (const f of metaFields) pickers[f].options.values = [PLACEHOLDER, STAR];
                    app.graph.setDirtyCanvas(true);
                    return;
                }
                fetchFeatures(path, subset, split).then((features) => {
                    const used = new Set([widgetValue("input_feature"), widgetValue("target_feature")]);
                    const cols = features.filter((c) => !used.has(c));
                    for (const f of metaFields) {
                        pickers[f].options.values = [PLACEHOLDER, STAR, ...cols];
                    }
                    app.graph.setDirtyCanvas(true);
                });
            };

            // Refresh when the dataset id / subset / split changes (column set), or when the
            // input/target feature changes (the exclusion set). The cached-dataset picker also
            // fires the id widget's callback after filling it, so a cached pick refreshes too.
            for (const dep of [idField, "name", "split", "input_feature", "target_feature"]) {
                if (!dep) continue;
                const w = node.widgets.find((x) => x.name === dep);
                if (!w) continue;
                const prev = w.callback;
                w.callback = function () {
                    const r = prev ? prev.apply(this, arguments) : undefined;
                    refreshColumns();
                    return r;
                };
            }

            refreshColumns(); // warm the list for the initial dataset (if any)

            if (node.onResize) {
                node.onResize(node.size);
            }
        };
    },
});
