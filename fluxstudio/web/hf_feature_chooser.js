import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

console.log("ComfyFlux: Loaded Taidal.HFFeatureChooser extension");

// Optional column picker for HuggingFace feature-selector inputs (input_feature / target_feature,
// tagged taidal_widget="HF_FEATURE" by fluxstudio/bridge.py). The underlying STRING widget stays
// freely editable — you can type any column name. This adds a dropdown of the SELECTED dataset's
// actual feature columns (image, label, …), fetched from /taidal/hf_dataset_features for the node's
// current dataset-id field (HF_DATASET_ID). It is the cached-dataset picker one level down: there
// you pick which dataset, here you pick which of its columns maps onto Sample.input / .target.

const PLACEHOLDER = "— pick a column —";
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
        console.warn("HFFeatureChooser: failed to list dataset features", err);
        features = [];
    }
    _featuresCache.set(key, features);
    return features;
}

app.registerExtension({
    name: "Taidal.HFFeatureChooser",
    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        const inputs = nodeData.input?.required || {};
        const featureFields = [];
        let idField = null;

        for (const [name, config] of Object.entries(inputs)) {
            const tag = config[1] && config[1].taidal_widget;
            if (tag === "HF_FEATURE") featureFields.push(name);
            else if (tag === "HF_DATASET_ID" && idField === null) idField = name;
        }

        if (featureFields.length === 0) return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;

        nodeType.prototype.onNodeCreated = function () {
            if (onNodeCreated) {
                onNodeCreated.apply(this, arguments);
            }
            const node = this;

            // Add a "columns" combo next to each feature field. Selecting an entry copies it into
            // the text widget; the text widget stays editable for any column name.
            const pickers = {};
            for (const name of featureFields) {
                const picker = node.addWidget(
                    "combo",
                    "🏷️ " + name + " (columns)",
                    PLACEHOLDER,
                    (value) => {
                        if (!value || value === PLACEHOLDER) return;
                        const target = node.widgets.find((w) => w.name === name);
                        if (target) {
                            target.value = value;
                            app.graph.setDirtyCanvas(true);
                        }
                    },
                    { values: [PLACEHOLDER] }
                );
                // The picker is a helper — don't persist it into the saved workflow.
                picker.serialize = false;
                pickers[name] = picker;
            }

            const widgetValue = (n) => {
                const w = n ? node.widgets.find((x) => x.name === n) : null;
                return w ? w.value : undefined;
            };

            // (Re)populate every picker from the dataset currently named in the id field. Empty id
            // -> just the placeholder. Tracks the dataset selection so the columns always match.
            const refreshFeatures = () => {
                const path = idField ? widgetValue(idField) : undefined;
                const subset = widgetValue("name");
                const split = widgetValue("split");
                if (!path) {
                    for (const f of featureFields) pickers[f].options.values = [PLACEHOLDER];
                    app.graph.setDirtyCanvas(true);
                    return;
                }
                fetchFeatures(path, subset, split).then((features) => {
                    for (const f of featureFields) {
                        pickers[f].options.values = [PLACEHOLDER, ...features];
                    }
                    app.graph.setDirtyCanvas(true);
                });
            };

            // Refresh whenever the dataset id / subset / split changes. The cached-dataset picker
            // (web/hf_dataset_chooser.js) also fires the id widget's callback after filling it, so
            // selecting a cached dataset refreshes the column list too.
            for (const dep of [idField, "name", "split"]) {
                if (!dep) continue;
                const w = node.widgets.find((x) => x.name === dep);
                if (!w) continue;
                const prev = w.callback;
                w.callback = function () {
                    const r = prev ? prev.apply(this, arguments) : undefined;
                    refreshFeatures();
                    return r;
                };
            }

            refreshFeatures(); // warm the list for the initial dataset (if any)

            // Force a resize so the new combos fit.
            if (node.onResize) {
                node.onResize(node.size);
            }
        };
    },
});
