import { app } from "../../scripts/app.js";

console.log("StreamStudio: Loaded Taidal.ExportSkip extension");

// Export marks, both stored in node.properties.taidal_export (persisted in workflow.json,
// mutually exclusive per node, toggled from the right-click menu):
//
// * "skip"   — CANVAS-ONLY: the node keeps running on the canvas (unlike mute/bypass) but
//   is EXCLUDED from the Taidal exports, together with everything data-downstream of it.
//   Forward-only: the nodes BEFORE the mark still export — to drop a whole side branch,
//   mark its head. Visual: dimmed body/title + a red "NOT EXPORTED" badge.
// * "target" — EXPORT TARGET: inverts the selection — the export contains exactly the
//   ops that COMPUTE the marked node(s), one document per target (side branches and
//   viewer helpers are simply not scheduled; no per-helper skip marks needed). Visual:
//   a teal "EXPORT TARGET" badge.
//
// Badges are drawn from the property itself every frame (onDrawForeground), so they
// survive reloads, collapses, and color edits. This extension is UI-only: the marks live in
// node.properties.taidal_export, web/export_flow.js posts the WHOLE workflow, and the backend
// reads them in Python (export.collect_export_marks) — subgraph-aware, so a mark placed inside a
// subgraph is honoured (the old per-top-level-node JS collector could not see it). The offline
// CLI twins are `streamstudio export --skip <id>` / `--target <id>` (subgraph-inner ids resolve by
// segment match against the expanded prompt id, e.g. `--target 371` hits `378:368:371`).

const PROPERTY = "taidal_export"; // mirrors export.py _EXPORT_SKIP_PROPERTY
const SKIP_VALUE = "skip";
const TARGET_VALUE = "target";
const PREV_BGCOLOR_PROPERTY = "taidal_export_prev_bgcolor";
const PREV_COLOR_PROPERTY = "taidal_export_prev_color";
const SKIP_BGCOLOR = "#3b3b4f"; // dimmed slate body — clearly "inactive for export", not muted
const SKIP_TITLECOLOR = "#2e2e3e"; // dimmed title bar
const SKIP_BADGE_BG = "#b54747";
const TARGET_BADGE_BG = "#2e7d6b";
const BADGE_FG = "#ffffff";

function markOf(node) {
    return node?.properties?.[PROPERTY];
}

function applyVisual(node) {
    if (!node.properties) return;
    if (markOf(node) === SKIP_VALUE) {
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

function setMark(node, value) {
    node.properties = node.properties || {};
    if (value === undefined || markOf(node) === value) {
        delete node.properties[PROPERTY]; // toggle off (or explicit clear)
    } else {
        node.properties[PROPERTY] = value; // also replaces the other mark — mutually exclusive
    }
    applyVisual(node);
    node.setDirtyCanvas?.(true, true);
}

// Drawn in NODE coordinates (origin = body top-left; the title bar spans negative y).
function drawBadge(ctx, node, text, background) {
    const titleHeight = globalThis.LiteGraph?.NODE_TITLE_HEIGHT ?? 30;
    ctx.save();
    ctx.font = "bold 10px sans-serif";
    const paddingX = 6;
    const width = ctx.measureText(text).width + paddingX * 2;
    const height = 14;
    const x = node.size[0] - width - 4;
    const y = -titleHeight + (titleHeight - height) / 2;
    ctx.fillStyle = background;
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
    ctx.fillText(text, x + paddingX, y + height / 2 + 0.5);
    ctx.restore();
}

// NOTE: mark COLLECTION is no longer done here. The backend reads node.properties.taidal_export
// straight from the posted workflow (export.collect_export_marks), which also sees nodes INSIDE
// subgraph definitions — the case this JS collector missed. This extension only owns the UI:
// the right-click menu, the per-frame badge, and the dim applied to a "skip"-marked node.

app.registerExtension({
    name: "Taidal.ExportSkip",
    beforeRegisterNodeDef(nodeType) {
        const originalMenu = nodeType.prototype.getExtraMenuOptions;
        nodeType.prototype.getExtraMenuOptions = function (canvas, options) {
            originalMenu?.apply(this, arguments);
            const mark = markOf(this);
            options.push({
                content: mark === SKIP_VALUE ? "Taidal: include in export" : "Taidal: exclude from export",
                callback: () => setMark(this, SKIP_VALUE),
            });
            options.push({
                content: mark === TARGET_VALUE ? "Taidal: clear export target" : "Taidal: set as export target",
                callback: () => setMark(this, TARGET_VALUE),
            });
        };
        const originalDraw = nodeType.prototype.onDrawForeground;
        nodeType.prototype.onDrawForeground = function (ctx) {
            originalDraw?.apply(this, arguments);
            const mark = markOf(this);
            if (mark === SKIP_VALUE) {
                drawBadge(ctx, this, "NOT EXPORTED", SKIP_BADGE_BG);
            } else if (mark === TARGET_VALUE) {
                drawBadge(ctx, this, "EXPORT TARGET", TARGET_BADGE_BG);
            }
        };
    },
    // Restore the dim when a saved workflow (with marks in properties) loads.
    loadedGraphNode(node) {
        applyVisual(node);
    },
});
