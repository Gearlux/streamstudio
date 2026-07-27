import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

console.log("ComfyStream: Loaded Taidal.HFSplitChooser extension");

// Optional split picker for a HuggingFace source's `split` input (tagged taidal_widget="HF_SPLIT"
// by streamstudio/bridge.py). The underlying STRING widget stays freely editable — HF supports slice
// syntax like "train[:90%]" / "train+test". This adds a dropdown of the SELECTED dataset's actual
// splits, fetched from /taidal/hf_dataset_splits for the node's current dataset-id field
// (HF_DATASET_ID) — the cached-dataset picker one level down, like the feature/column picker.

const PLACEHOLDER = "— pick a split —";
const _splitsCache = new Map(); // "path|name" -> array of split names

function cacheKey(path, name) {
    return [path || "", name || ""].join("|");
}

async function fetchSplits(path, name) {
    const key = cacheKey(path, name);
    if (_splitsCache.has(key)) return _splitsCache.get(key);
    let splits = [];
    try {
        const params = new URLSearchParams({ path: path || "" });
        if (name) params.set("name", name);
        const res = await api.fetchApi("/taidal/hf_dataset_splits?" + params.toString());
        const data = await res.json();
        splits = Array.isArray(data.splits) ? data.splits : [];
    } catch (err) {
        console.warn("HFSplitChooser: failed to list dataset splits", err);
        splits = [];
    }
    _splitsCache.set(key, splits);
    return splits;
}

app.registerExtension({
    name: "Taidal.HFSplitChooser",
    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        const inputs = nodeData.input?.required || {};
        const splitFields = [];
        let idField = null;

        for (const [name, config] of Object.entries(inputs)) {
            const tag = config[1] && config[1].taidal_widget;
            if (tag === "HF_SPLIT") splitFields.push(name);
            else if (tag === "HF_DATASET_ID" && idField === null) idField = name;
        }

        if (splitFields.length === 0) return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;

        nodeType.prototype.onNodeCreated = function () {
            if (onNodeCreated) {
                onNodeCreated.apply(this, arguments);
            }
            const node = this;

            // Add a "splits" combo next to each split field. Selecting an entry copies it into the
            // text widget; the text widget stays editable for slice syntax.
            const pickers = {};
            for (const name of splitFields) {
                const picker = node.addWidget(
                    "combo",
                    "🪓 " + name + " (splits)",
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
            // -> just the placeholder. Tracks the dataset selection so the splits always match.
            const refreshSplits = () => {
                const path = idField ? widgetValue(idField) : undefined;
                const subset = widgetValue("name");
                if (!path) {
                    for (const f of splitFields) pickers[f].options.values = [PLACEHOLDER];
                    app.graph.setDirtyCanvas(true);
                    return;
                }
                fetchSplits(path, subset).then((splits) => {
                    for (const f of splitFields) {
                        pickers[f].options.values = [PLACEHOLDER, ...splits];
                    }
                    app.graph.setDirtyCanvas(true);
                });
            };

            // Refresh whenever the dataset id / subset changes. The cached-dataset picker
            // (web/hf_dataset_chooser.js) also fires the id widget's callback after filling it, so
            // selecting a cached dataset reloads the split list too.
            for (const dep of [idField, "name"]) {
                if (!dep) continue;
                const w = node.widgets.find((x) => x.name === dep);
                if (!w) continue;
                const prev = w.callback;
                w.callback = function () {
                    const r = prev ? prev.apply(this, arguments) : undefined;
                    refreshSplits();
                    return r;
                };
            }

            refreshSplits(); // warm the list for the initial dataset (if any)

            // Force a resize so the new combos fit.
            if (node.onResize) {
                node.onResize(node.size);
            }
        };
    },
});
