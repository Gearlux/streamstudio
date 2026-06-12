import { app } from "../../scripts/app.js";

console.log("ComfyFlux: Loaded Taidal.ExportSkip extension");

// The CANVAS-ONLY mark: a node whose properties.taidal_export === "skip" keeps running on
// the canvas (unlike ComfyUI's mute/bypass) but is EXCLUDED from the Taidal exports — it
// and everything data-downstream of it. Toggled from the node's right-click menu; the
// mark lives in node.properties so it persists in workflow.json. Two visual cues so the
// state is unmissable: the node body/title dim to slate, and a red "NOT EXPORTED" badge
// is drawn on the title bar every frame (drawn from the property itself, so it survives
// reloads, collapses, and color edits). web/export_ops.js collects the marked ids and
// posts them as `skip_ids`; the offline CLI twin is `fluxstudio export --skip <id>`.

const SKIP_PROPERTY = "taidal_export"; // mirrors export.py _EXPORT_SKIP_PROPERTY
const SKIP_VALUE = "skip";
const PREV_BGCOLOR_PROPERTY = "taidal_export_prev_bgcolor";
const PREV_COLOR_PROPERTY = "taidal_export_prev_color";
const SKIP_BGCOLOR = "#3b3b4f"; // dimmed slate body — clearly "inactive for export", not muted
const SKIP_TITLECOLOR = "#2e2e3e"; // dimmed title bar
const BADGE_BG = "#b54747"; // the unmissable cue
const BADGE_FG = "#ffffff";
const BADGE_TEXT = "NOT EXPORTED";

function isMarked(node) {
    return node?.properties?.[SKIP_PROPERTY] === SKIP_VALUE;
}

function applyVisual(node) {
    if (!node.properties) return;
    if (isMarked(node)) {
        if (node.properties[PREV_BGCOLOR_PROPERTY] === undefined) {
            node.properties[PREV_BGCOLOR_PROPERTY] = node.bgcolor ?? null;
            node.properties[PREV_COLOR_PROPERTY] = node.color ?? null;
        }
        node.bgcolor = SKIP_BGCOLOR;
        node.color = SKIP_TITLECOLOR;
    } else if (node.properties[PREV_BGCOLOR_PROPERTY] !== undefined) {
        node.bgcolor = node.properties[PREV_BGCOLOR_PROPERTY] ?? undefined;
        node.color = node.properties[PREV_COLOR_PROPERTY] ?? undefined;
        delete node.properties[PREV_BGCOLOR_PROPERTY];
        delete node.properties[PREV_COLOR_PROPERTY];
    }
}

function toggleMark(node) {
    node.properties = node.properties || {};
    if (isMarked(node)) {
        delete node.properties[SKIP_PROPERTY];
    } else {
        node.properties[SKIP_PROPERTY] = SKIP_VALUE;
    }
    applyVisual(node);
    node.setDirtyCanvas?.(true, true);
}

// Drawn in NODE coordinates (origin = body top-left; the title bar spans negative y).
function drawBadge(ctx, node) {
    const titleHeight = globalThis.LiteGraph?.NODE_TITLE_HEIGHT ?? 30;
    ctx.save();
    ctx.font = "bold 10px sans-serif";
    const paddingX = 6;
    const width = ctx.measureText(BADGE_TEXT).width + paddingX * 2;
    const height = 14;
    const x = node.size[0] - width - 4;
    const y = -titleHeight + (titleHeight - height) / 2;
    ctx.fillStyle = BADGE_BG;
    if (ctx.roundRect) {
        ctx.beginPath();
        ctx.roundRect(x, y, width, height, 4);
        ctx.fill();
    } else {
        ctx.fillRect(x, y, width, height);
    }
    ctx.fillStyle = BADGE_FG;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(BADGE_TEXT, x + paddingX, y + height / 2 + 0.5);
    ctx.restore();
}

// The marked node ids of the ACTIVE graph, as the backend expects them (strings). A marked
// subgraph-INSTANCE id excludes the whole inlined instance (the compiler prefix-matches
// "<id>:" inner ids). Nodes marked INSIDE a subgraph definition are not collected here
// (the whole-graph export reads their properties from the workflow itself).
export function collectExportSkipIds(graph) {
    const nodes = graph?._nodes ?? graph?.nodes ?? [];
    return nodes.filter((node) => isMarked(node)).map((node) => String(node.id));
}

app.registerExtension({
    name: "Taidal.ExportSkip",
    beforeRegisterNodeDef(nodeType) {
        const originalMenu = nodeType.prototype.getExtraMenuOptions;
        nodeType.prototype.getExtraMenuOptions = function (canvas, options) {
            originalMenu?.apply(this, arguments);
            options.push({
                content: isMarked(this) ? "Taidal: include in export" : "Taidal: exclude from export",
                callback: () => toggleMark(this),
            });
        };
        const originalDraw = nodeType.prototype.onDrawForeground;
        nodeType.prototype.onDrawForeground = function (ctx) {
            originalDraw?.apply(this, arguments);
            if (isMarked(this)) {
                drawBadge(ctx, this);
            }
        };
    },
    // Restore the dim when a saved workflow (with marks in properties) loads.
    loadedGraphNode(node) {
        applyVisual(node);
    },
});
