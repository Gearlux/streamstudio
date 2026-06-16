import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

console.log("ComfyFlux: Loaded Taidal.PaletteFilter extension");

// "Use only Taidal nodes" (the CLI's --only-taidal-nodes flag → FLUXSTUDIO_ONLY_TAIDAL_NODES=1).
// Removes every NON-Taidal node type from the Add-Node menu / search — ComfyUI built-ins AND other
// third-party custom packs — keeping only Taidal_* nodes plus a configurable keep-list (PreviewImage /
// PreviewAny by default; extend with the CLI's --keep-node) and a few litegraph structural types so
// the canvas plumbing survives.
//
// Done on the FRONTEND, in `setup()` (after ALL node types are registered): the backend import-time
// prune (fluxstudio.__init__._prune_default_comfy_nodes) can't see custom packs that load AFTER
// fluxstudio in ComfyUI's alphabetical custom-node order, but the browser registry is complete by the
// time this runs. Config comes from the /taidal/palette_config route.

// litegraph graph-plumbing node types — generic, not "ComfyUI default nodes" the user wants gone.
const STRUCTURAL = new Set([
    "PrimitiveNode",
    "Reroute",
    "Note",
    "MarkdownNote",
    "PrimitiveInt",
    "PrimitiveFloat",
    "PrimitiveString",
    "PrimitiveStringMultiline",
    "PrimitiveBoolean",
]);

function shouldKeep(key, keep) {
    return key.startsWith("Taidal") || keep.has(key) || STRUCTURAL.has(key);
}

app.registerExtension({
    name: "Taidal.PaletteFilter",
    async setup() {
        let cfg;
        try {
            cfg = await (await api.fetchApi("/taidal/palette_config")).json();
        } catch (e) {
            console.warn("Taidal.PaletteFilter: could not fetch /taidal/palette_config — leaving palette as-is.", e);
            return;
        }
        if (!cfg?.only_taidal) return; // default mode (or --no-default-nodes, handled backend) — nothing to do
        if (typeof LiteGraph === "undefined" || !LiteGraph.registered_node_types) return;

        const keep = new Set(cfg.keep || []);
        const types = LiteGraph.registered_node_types;
        let removed = 0;
        for (const key of Object.keys(types)) {
            if (!shouldKeep(key, keep)) {
                delete types[key];
                removed++;
            }
        }
        console.log(
            `Taidal.PaletteFilter: only-Taidal mode — removed ${removed} non-Taidal node types ` +
                `(kept Taidal/* + ${[...keep].join(", ") || "(none)"} + structural).`,
        );
    },
});
