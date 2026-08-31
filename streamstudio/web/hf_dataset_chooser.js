import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

console.log("StreamStudio: Loaded Taidal.HFDatasetChooser extension");

// Optional picker for HuggingFace dataset-id inputs (tagged taidal_widget="HF_DATASET_ID"
// by streamstudio/bridge.py). The underlying STRING widget stays freely editable — you
// can type any hub repo id (e.g. "kitofrank/RFUAV") or a local imagefolder path. This
// only adds a convenience dropdown of datasets ALREADY downloaded to the local cache,
// served by the /taidal/hf_datasets backend route.

const PLACEHOLDER = "— pick a cached dataset —";
let _cachedDatasets = null;

// An empty picker that explains nothing is indistinguishable from a broken one, so the
// backend sends WHY it is empty (a changed HF_HOME the running editor never saw is the
// usual answer) and it becomes the placeholder instead of a silent blank list.
async function fetchCachedDatasets() {
    if (_cachedDatasets) return _cachedDatasets;
    try {
        const res = await api.fetchApi("/taidal/hf_datasets");
        const data = await res.json();
        const datasets = Array.isArray(data.datasets) ? data.datasets : [];
        _cachedDatasets = datasets.length || !data.reason ? datasets : [`— ${data.reason} —`];
        if (data.reason) console.warn("HFDatasetChooser: no cached datasets —", data.reason);
    } catch (err) {
        console.warn("HFDatasetChooser: failed to list cached datasets", err);
        _cachedDatasets = [];
    }
    return _cachedDatasets;
}

app.registerExtension({
    name: "Taidal.HFDatasetChooser",
    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        const inputs = nodeData.input?.required || {};
        const hfFields = [];

        for (const [name, config] of Object.entries(inputs)) {
            if (config[1] && config[1].taidal_widget === "HF_DATASET_ID") {
                hfFields.push(name);
            }
        }

        if (hfFields.length === 0) return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;

        nodeType.prototype.onNodeCreated = function () {
            if (onNodeCreated) {
                onNodeCreated.apply(this, arguments);
            }

            // Add a "cached datasets" combo next to each HF dataset-id field. Selecting an
            // entry copies it into the text widget; the text widget remains editable.
            for (const name of hfFields) {
                const picker = this.addWidget(
                    "combo",
                    "📚 " + name + " (cached)",
                    PLACEHOLDER,
                    (value) => {
                        if (!value || value === PLACEHOLDER) return;
                        const target = this.widgets.find((w) => w.name === name);
                        if (target) {
                            target.value = value;
                            // Fire the field's own callback so dependent UI reacts to the
                            // programmatic change — notably the feature picker
                            // (web/hf_feature_chooser.js) reloads the dataset's columns.
                            if (typeof target.callback === "function") target.callback(value);
                            app.graph.setDirtyCanvas(true);
                        } else {
                            alert("Could not find the dataset text field to update.");
                        }
                    },
                    { values: [PLACEHOLDER] }
                );
                // The picker is just a helper — don't persist it into the saved workflow.
                picker.serialize = false;

                fetchCachedDatasets().then((datasets) => {
                    picker.options.values = [PLACEHOLDER, ...datasets];
                    app.graph.setDirtyCanvas(true);
                });
            }

            // Force a resize so the new combo fits.
            if (this.onResize) {
                this.onResize(this.size);
            }
        };
    },
});
