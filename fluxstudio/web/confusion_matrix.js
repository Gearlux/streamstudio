import { app } from "../../scripts/app.js";

console.log("ComfyFlux: Loaded Taidal.ConfusionMatrix extension");

// Interactive confusion-matrix viewer. The python node (`fluxstudio.nodes.ConfusionMatrixViewerNode`,
// class key `Taidal_ConfusionMatrix`) is an OUTPUT_NODE that returns
// `{"ui": {"taidal_confusion_matrix":[payload, ...]}}` — a LIST of payloads, ONE per confusion matrix
// extracted from the evaluator's `all_metrics` output (there can be several). ComfyUI delivers the list
// to `onExecuted(message)`. Each payload carries: `counts` (N×N ints), `normalized.{true,pred,all}`
// (precomputed in dataflux so the normalize toggle never re-runs), `class_names`, `n_classes`, `total`,
// `name`. ALL computation lives in dataflux (`confusion_matrices_payload`) — this file only DRAWS + hovers.
//
// RENDERS ON THE LITEGRAPH NODE CANVAS (onDrawForeground), NOT a DOM widget (the ArrayHistogram pattern):
// rows = true class, columns = predicted class, cells coloured by value (the diagonal = correct), class
// names down the left + across the top. A `norm:` button cycles the normalize mode (none→true→pred→all);
// a `cm:` button cycles between matrices when there are several. Hover a cell to read true/pred + value.

const NODE_KEY = "Taidal_ConfusionMatrix";
const MIN_W = 280; // minimum node width
const MARGIN = 10; // content inset from the node's left/right edges
const READOUT_H = 16; // hover-readout line
const TITLE_H = 14; // title / summary line
const GAP = 6;
const LEFT_LABEL_W = 64; // width reserved for row (true-class) labels
const TOP_LABEL_H = 18; // height reserved for column (pred-class) labels + the toggle buttons
const MIN_GRID = 120; // smallest grid square
const DEFAULT_GRID = 320; // initial grid size on first run
const WIDGET_H = 20;
// View modes: absolute counts, or the row/col/all normalization shown as PERCENTAGES (the "%" option).
const MODES = [
    { key: "none", label: "count" },
    { key: "true", label: "row %" },
    { key: "pred", label: "col %" },
    { key: "all", label: "all %" },
];
const AXIS_FONT = "8px monospace"; // class (axis) labels — kept small so more of them fit
const VAL_FONT = "9px monospace"; // cell values

function widgetH() {
    return (typeof LiteGraph !== "undefined" && LiteGraph.NODE_WIDGET_HEIGHT) || WIDGET_H;
}

function slotH() {
    return (typeof LiteGraph !== "undefined" && LiteGraph.NODE_SLOT_HEIGHT) || 20;
}

// Y (node-body coords) where our drawing starts: BELOW the input/output socket rows AND below any
// widgets. The bug this fixes: with only INPUT SOCKETS (no widgets) the old widget-based start sat at
// the top and the grid overlapped the sockets — so reserve `max(#inputs, #outputs) * slotHeight` too.
function contentStartY(node) {
    const nIn = (node.inputs || []).length;
    const nOut = (node.outputs || []).length;
    let y = Math.max(nIn, nOut) * slotH();
    for (const w of node.widgets || []) {
        if (typeof w.last_y === "number") y = Math.max(y, w.last_y + widgetH());
    }
    return y + GAP;
}

let _measureCtx = null;
function measureText(text, font) {
    if (!_measureCtx) _measureCtx = document.createElement("canvas").getContext("2d");
    _measureCtx.font = font || "10px monospace";
    return _measureCtx.measureText(text).width;
}

// Truncate a label with an ellipsis so it fits within maxW px (monospace).
function ellipsize(text, maxW, font) {
    if (!text) return "";
    if (measureText(text, font) <= maxW) return text;
    let s = text;
    while (s.length > 1 && measureText(s + "…", font) > maxW) s = s.slice(0, -1);
    return s + "…";
}

// The currently selected payload (one confusion matrix) out of the list.
function currentPayload(node) {
    const ps = node._taidalPayloads;
    if (!ps || !ps.length) return null;
    const i = Math.min(ps.length - 1, Math.max(0, node._taidalIndex || 0));
    return ps[i];
}

// The N×N grid the current view mode displays, its max (for colour scaling), and a percent flag.
function displayGrid(node) {
    const p = currentPayload(node);
    if (!p || !p.n_classes) return null;
    const mode = MODES[node._taidalMode || 0];
    if (mode.key === "none") {
        let max = 1;
        for (const row of p.counts) for (const c of row) max = Math.max(max, c);
        return { grid: p.counts, max, isPercent: false };
    }
    const grid = p.normalized?.[mode.key] || p.counts;
    return { grid, max: 1, isPercent: true };
}

// A normalized fraction (0..1) as a percentage string: one decimal below 10 %, whole above.
function pctText(v) {
    if (v === null || v === undefined) return "—";
    const pct = v * 100;
    return (pct > 0 && pct < 10 ? pct.toFixed(1) : Math.round(pct).toString()) + "%";
}

function cellColor(t) {
    // White (low) → blue (high). t in [0, 1]; null/NaN handled by caller.
    const r = Math.round(255 - t * (255 - 33));
    const g = Math.round(255 - t * (255 - 102));
    const b = Math.round(255 - t * (255 - 172));
    return `rgb(${r},${g},${b})`;
}

function summaryString(node) {
    const ps = node._taidalPayloads;
    if (!ps) return "Queue Prompt — wire the evaluator's all_metrics output";
    if (!ps.length) return "no confusion-matrix metric found in the wired metrics";
    const p = currentPayload(node);
    const which = ps.length > 1 ? ` · ${(node._taidalIndex || 0) + 1}/${ps.length}` : "";
    const name = p.name ? `${p.name} · ` : "";
    return `${name}${p.n_classes} classes · n=${p.total} · ${MODES[node._taidalMode || 0].label}${which}`;
}

function bottomHeight() {
    return GAP + READOUT_H + GAP + TITLE_H;
}

// --------------------------------------------------------------------------- //
// Layout — rects (node-body coords), stashed on the node each draw so the mouse handlers hit-test it.
// --------------------------------------------------------------------------- //

function computeLayout(node) {
    const top = contentStartY(node);
    const nodeW = node.size?.[0] ?? 320;
    const nodeH = node.size?.[1] ?? 320;
    const contentW = Math.max(16, nodeW - MARGIN * 2);

    const bottom = bottomHeight();
    const availH = Math.max(MIN_GRID, nodeH - top - bottom - TOP_LABEL_H);
    const availW = Math.max(MIN_GRID, contentW - LEFT_LABEL_W);
    const side = Math.max(MIN_GRID, Math.min(availW, availH)); // a SQUARE grid

    const gridX = MARGIN + LEFT_LABEL_W;
    const gridY = top + TOP_LABEL_H;
    const grid = { x: gridX, y: gridY, w: side, h: side };
    const readoutY = gridY + side + GAP;
    const titleY = readoutY + READOUT_H + GAP;
    const normBtn = { x: gridX + side - 74, y: top, w: 72, h: 13 };
    const cmBtn = { x: gridX + side - 140, y: top, w: 60, h: 13 };
    return { top, nodeW, contentW, grid, readoutY, titleY, normBtn, cmBtn };
}

// --------------------------------------------------------------------------- //
// Drawing
// --------------------------------------------------------------------------- //

function clippedText(ctx, text, x, y, maxW, color, font, align) {
    ctx.save();
    ctx.beginPath();
    // The clip rect MUST follow the text alignment: a center-aligned label is drawn AROUND x, so the
    // clip has to start at x - maxW/2 — otherwise the left half of every cell number is cut off (the
    // "numbers don't display correctly" bug). Left = [x, x+maxW]; right = [x-maxW, x].
    const clipX = align === "center" ? x - maxW / 2 : align === "right" ? x - maxW : x;
    ctx.rect(clipX, y - 11, maxW, 15);
    ctx.clip();
    ctx.fillStyle = color;
    ctx.font = font || "10px monospace";
    ctx.textBaseline = "alphabetic";
    ctx.textAlign = align || "left";
    ctx.fillText(text, x, y);
    ctx.restore();
}

function drawButton(ctx, b, label, active) {
    ctx.save();
    ctx.fillStyle = "rgba(0,0,0,0.5)";
    ctx.fillRect(b.x, b.y, b.w, b.h);
    ctx.fillStyle = active ? "#4ea1ff" : "#aaa";
    ctx.font = "9px monospace";
    ctx.textBaseline = "middle";
    ctx.textAlign = "center";
    ctx.fillText(label, b.x + b.w / 2, b.y + b.h / 2 + 0.5);
    ctx.restore();
}

function drawNode(node, ctx) {
    if (node.flags?.collapsed) return;
    const L = computeLayout(node);
    node._taidalLayout = L;
    const p = currentPayload(node);
    const g = L.grid;

    // Grid background.
    ctx.save();
    ctx.fillStyle = "#141414";
    ctx.fillRect(g.x, g.y, g.w, g.h);
    ctx.restore();

    const disp = displayGrid(node);
    if (!disp) {
        clippedText(ctx, summaryString(node), MARGIN, g.y + g.h / 2, L.contentW, "#888");
        return;
    }
    const n = p.n_classes;
    const cw = g.w / n;
    const ch = g.h / n;
    const showLabels = cw >= 12; // hide per-cell/axis labels when cells get too small

    // Cells.
    for (let i = 0; i < n; i++) {
        for (let j = 0; j < n; j++) {
            const v = disp.grid[i]?.[j];
            const x = g.x + j * cw;
            const y = g.y + i * ch;
            const t = v === null || v === undefined ? 0 : Math.max(0, Math.min(1, v / disp.max));
            ctx.fillStyle = v === null || v === undefined ? "#222" : cellColor(t);
            ctx.fillRect(x, y, Math.ceil(cw), Math.ceil(ch));
            if (showLabels) {
                const label = disp.isPercent ? pctText(v) : v === null || v === undefined ? "—" : String(v);
                clippedText(ctx, label, x + cw / 2, y + ch / 2 + 3, cw, t > 0.55 ? "#eee" : "#222", VAL_FONT, "center");
            }
        }
    }
    // Diagonal outline (correct predictions).
    ctx.save();
    ctx.strokeStyle = "rgba(90,200,120,0.8)";
    ctx.lineWidth = 1;
    for (let i = 0; i < n; i++) ctx.strokeRect(g.x + i * cw + 0.5, g.y + i * ch + 0.5, cw - 1, ch - 1);
    ctx.restore();

    // Grid border + hovered cell highlight.
    ctx.save();
    ctx.strokeStyle = "#444";
    ctx.strokeRect(g.x + 0.5, g.y + 0.5, g.w - 1, g.h - 1);
    const hv = node._taidalHover;
    if (hv) {
        ctx.strokeStyle = "rgba(120,220,255,0.95)";
        ctx.lineWidth = 2;
        ctx.strokeRect(g.x + hv.j * cw + 1, g.y + hv.i * ch + 1, cw - 2, ch - 2);
    }
    ctx.restore();

    // Axis labels: row (true) on the left, column (pred) on top. Hidden when cells are tiny.
    if (showLabels) {
        for (let k = 0; k < n; k++) {
            const name = p.class_names[k] ?? String(k);
            clippedText(ctx, ellipsize(name, LEFT_LABEL_W - 4, AXIS_FONT), MARGIN, g.y + k * ch + ch / 2 + 3,
                LEFT_LABEL_W - 4, "#bbb", AXIS_FONT, "left");
            clippedText(ctx, ellipsize(name, cw, AXIS_FONT), g.x + k * cw + cw / 2, g.y - 4, cw, "#bbb", AXIS_FONT, "center");
        }
    }
    clippedText(ctx, "pred →", MARGIN, g.y - 4, LEFT_LABEL_W - 4, "#777", AXIS_FONT, "left");

    // Buttons: view mode (count / row % / col % / all %) + matrix selector (only when several).
    drawButton(ctx, L.normBtn, `view: ${MODES[node._taidalMode || 0].label}`, !!node._taidalMode);
    if ((node._taidalPayloads?.length || 0) > 1) {
        drawButton(ctx, L.cmBtn, `cm: ${(node._taidalIndex || 0) + 1}/${node._taidalPayloads.length}`, true);
    }

    // Hover readout + summary.
    const readout = node._taidalReadout || "hover a cell to read true / pred / value";
    clippedText(ctx, readout, MARGIN, L.readoutY + 11, L.contentW, "#cfe");
    clippedText(ctx, summaryString(node), MARGIN, L.titleY + 10, L.contentW, "#aaa");
}

// --------------------------------------------------------------------------- //
// Mouse — node-local coordinates from litegraph (NO DOM events).
// --------------------------------------------------------------------------- //

function inRect(pos, r) {
    return r && pos[0] >= r.x && pos[0] <= r.x + r.w && pos[1] >= r.y && pos[1] <= r.y + r.h;
}

function onMove(node, pos) {
    const p = currentPayload(node);
    const L = node._taidalLayout;
    if (!p || !p.n_classes || !L || !inRect(pos, L.grid)) {
        if (node._taidalHover) {
            node._taidalHover = null;
            node._taidalReadout = null;
            node.setDirtyCanvas(true, true);
        }
        return;
    }
    const g = L.grid;
    const n = p.n_classes;
    const j = Math.min(n - 1, Math.max(0, Math.floor(((pos[0] - g.x) / g.w) * n))); // pred (column)
    const i = Math.min(n - 1, Math.max(0, Math.floor(((pos[1] - g.y) / g.h) * n))); // true (row)
    node._taidalHover = { i, j };
    const count = p.counts[i]?.[j];
    const disp = displayGrid(node);
    const tname = p.class_names[i] ?? String(i);
    const pname = p.class_names[j] ?? String(j);
    // Always show the count; add the percentage when a % view is active.
    const valStr = disp?.isPercent ? `${pctText(disp.grid[i]?.[j])} (n=${count})` : `n=${count}`;
    node._taidalReadout = `true=${tname}  pred=${pname}  ${valStr}`;
    node.setDirtyCanvas(true, true);
}

// --------------------------------------------------------------------------- //
// Registration
// --------------------------------------------------------------------------- //

app.registerExtension({
    name: "Taidal.ConfusionMatrix",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData?.name !== NODE_KEY) return;

        // Small minimum so the node resizes freely; the grid fills the available square (computeLayout).
        nodeType.prototype.computeSize = function (out) {
            const size = LiteGraph.LGraphNode.prototype.computeSize.call(this, out);
            size[0] = Math.max(MIN_W, size[0]);
            size[1] = contentStartY(this) + TOP_LABEL_H + MIN_GRID + bottomHeight();
            return size;
        };

        const onDrawForeground = nodeType.prototype.onDrawForeground;
        nodeType.prototype.onDrawForeground = function (ctx) {
            onDrawForeground?.apply(this, arguments);
            drawNode(this, ctx);
        };

        const onMouseMove = nodeType.prototype.onMouseMove;
        nodeType.prototype.onMouseMove = function (e, pos, canvas) {
            onMouseMove?.apply(this, arguments);
            onMove(this, pos);
        };
        const onMouseDown = nodeType.prototype.onMouseDown;
        nodeType.prototype.onMouseDown = function (e, pos, canvas) {
            const handled = onMouseDown?.apply(this, arguments);
            const L = this._taidalLayout;
            if (L && inRect(pos, L.normBtn)) {
                this._taidalMode = ((this._taidalMode || 0) + 1) % MODES.length; // cycle normalize (display-only)
                this.setDirtyCanvas(true, true);
                return true;
            }
            if (L && (this._taidalPayloads?.length || 0) > 1 && inRect(pos, L.cmBtn)) {
                this._taidalIndex = ((this._taidalIndex || 0) + 1) % this._taidalPayloads.length; // next matrix
                this._taidalHover = null;
                this._taidalReadout = null;
                this.setDirtyCanvas(true, true);
                return true;
            }
            return handled;
        };
        const onMouseLeave = nodeType.prototype.onMouseLeave;
        nodeType.prototype.onMouseLeave = function (e, pos, canvas) {
            onMouseLeave?.apply(this, arguments);
            if (this._taidalHover) {
                this._taidalHover = null;
                this._taidalReadout = null;
                this.setDirtyCanvas(true, true);
            }
        };

        // The payload LIST arrives on every run (re-delivered by ComfyUI on workflow re-open).
        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            onExecuted?.apply(this, arguments);
            const payloads = Array.isArray(message?.taidal_confusion_matrix) ? message.taidal_confusion_matrix : undefined;
            if (!payloads) return;
            this._taidalPayloads = payloads;
            this._taidalIndex = 0;
            this._taidalHover = null;
            this._taidalReadout = null;
            if (this._taidalMode === undefined) this._taidalMode = 0;
            // One-time: give a fresh node a sensible square grid. Never resize a node the user has sized.
            if (!this._taidalSized) {
                this._taidalSized = true;
                const w = Math.max(MIN_W, this.size?.[0] ?? 320);
                this.setSize([w, contentStartY(this) + TOP_LABEL_H + DEFAULT_GRID + bottomHeight()]);
            }
            this.setDirtyCanvas(true, true);
        };
    },
});
