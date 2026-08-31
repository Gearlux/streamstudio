import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

console.log("StreamStudio: Loaded Taidal.DirectoryChooser extension");

// One browse affordance per picker KIND: a DIRECTORY-tagged param opens the folder dialog, a
// FILE-tagged one (checkpoint_path and friends) opens the file dialog — a folder dialog cannot
// select a file (user report 2026-08-31).
const PICKERS = {
    DIRECTORY: { icon: "\u{1F4C2}", route: "/taidal/choose_directory", label: "directory" },
    FILE: { icon: "\u{1F4C4}", route: "/taidal/choose_file", label: "file" },
};

app.registerExtension({
    name: "Taidal.DirectoryChooser",
    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        const inputs = nodeData.input?.required || {};
        let pickerFields = [];

        for (const [name, config] of Object.entries(inputs)) {
            const kind = config[1] && PICKERS[config[1].taidal_widget];
            if (kind) {
                pickerFields.push([name, kind]);
            }
        }

        if (pickerFields.length > 0) {
            const onNodeCreated = nodeType.prototype.onNodeCreated;

            nodeType.prototype.onNodeCreated = function() {
                if (onNodeCreated) {
                    onNodeCreated.apply(this, arguments);
                }

                // Add a browse button for each picker field
                for (const [name, kind] of pickerFields) {
                    this.addWidget("button", kind.icon + " Browse " + name, "browse", () => {
                        api.fetchApi(kind.route, { method: 'POST' })
                            .then(res => res.json())
                            .then(data => {
                                if (data.path) {
                                    // Find the widget at click-time
                                    const widget = this.widgets.find(w => w.name === name);
                                    if (widget) {
                                        widget.value = data.path;
                                        app.graph.setDirtyCanvas(true);
                                    } else {
                                        alert("Could not find the text widget to update.");
                                    }
                                } else if (data.error) {
                                    alert("Failed to choose " + kind.label + ": " + data.error);
                                }
                            })
                            .catch(err => alert("API Error: " + err));
                    });
                }

                // Force a resize so the new buttons fit
                if (this.onResize) {
                    this.onResize(this.size);
                }
            };
        }
    }
});
