import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

console.log("ComfyStream: Loaded Taidal.MelodyDatasetChooser extension");

// Optional picker for Melody/MAF dataset-id inputs (tagged taidal_widget="MELODY_DATASET_ID"
// by streamstudio/bridge.py — the HeliosMelodyDataset value node's `name` field). The underlying
// STRING widget stays freely editable — you can type any platform dataset name. This adds a
// convenience dropdown of datasets available on the platform, served by the
// /taidal/melody_datasets backend route (needs maf_client + credentials; empty otherwise).

const PLACEHOLDER = "— pick a Melody dataset —";
const _byServer = {}; // server -> Promise<string[]> (cache per MAF instance)

async function fetchMelodyDatasets(server) {
    const key = server || "PROD";
    if (_byServer[key]) return _byServer[key];
    _byServer[key] = (async () => {
        try {
            const res = await api.fetchApi("/taidal/melody_datasets?server=" + encodeURIComponent(key));
            const data = await res.json();
            return Array.isArray(data.datasets) ? data.datasets : [];
        } catch (err) {
            console.warn("MelodyDatasetChooser: failed to list datasets", err);
            return [];
        }
    })();
    return _byServer[key];
}

app.registerExtension({
    name: "Taidal.MelodyDatasetChooser",
    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        const inputs = nodeData.input?.required || {};
        const fields = [];

        for (const [name, config] of Object.entries(inputs)) {
            if (config[1] && config[1].taidal_widget === "MELODY_DATASET_ID") {
                fields.push(name);
            }
        }

        if (fields.length === 0) return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;

        nodeType.prototype.onNodeCreated = function () {
            if (onNodeCreated) {
                onNodeCreated.apply(this, arguments);
            }

            // Add a "datasets" combo next to each Melody dataset-id field. Selecting an entry
            // copies it into the text widget; the text widget remains editable.
            for (const name of fields) {
                const node = this;
                const reload = (picker) => {
                    const serverWidget = node.widgets?.find((w) => w.name === "server");
                    fetchMelodyDatasets(serverWidget ? serverWidget.value : "PROD").then((datasets) => {
                        picker.options.values = [PLACEHOLDER, ...datasets];
                        app.graph.setDirtyCanvas(true);
                    });
                };

                const picker = this.addWidget(
                    "combo",
                    "🎵 " + name + " (platform)",
                    PLACEHOLDER,
                    (value) => {
                        if (!value || value === PLACEHOLDER) return;
                        const target = node.widgets.find((w) => w.name === name);
                        if (target) {
                            target.value = value;
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

                // Refresh the list when the MAF server selection changes (DEV/STAGING/PROD).
                const serverWidget = this.widgets?.find((w) => w.name === "server");
                if (serverWidget) {
                    const prev = serverWidget.callback;
                    serverWidget.callback = function () {
                        const r = prev ? prev.apply(this, arguments) : undefined;
                        reload(picker);
                        return r;
                    };
                }

                reload(picker);
            }

            if (this.onResize) {
                this.onResize(this.size);
            }
        };
    },
});
