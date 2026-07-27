import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

console.log("ComfyStream: Loaded Taidal.DirectoryChooser extension");

app.registerExtension({
    name: "Taidal.DirectoryChooser",
    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        const inputs = nodeData.input?.required || {};
        let dirFields = [];
        
        for (const [name, config] of Object.entries(inputs)) {
            if (config[1] && config[1].taidal_widget === "DIRECTORY") {
                dirFields.push(name);
            }
        }

        if (dirFields.length > 0) {
            const onNodeCreated = nodeType.prototype.onNodeCreated;
            
            nodeType.prototype.onNodeCreated = function() {
                if (onNodeCreated) {
                    onNodeCreated.apply(this, arguments);
                }

                // Add a browse button for each directory field
                for (const name of dirFields) {
                    this.addWidget("button", "📂 Browse " + name, "browse", () => {
                        api.fetchApi('/taidal/choose_directory', { method: 'POST' })
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
                                    alert("Failed to choose directory: " + data.error);
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

