import { app } from "../../scripts/app.js";

console.log("ComfyStream: Loaded Taidal.ArrayHistogram extension");

// Interactive viewer for the values of an array / tensor — the StreamStudio analogue of
// ComfyUI-Olm-Histogram. The python node (`streamstudio.nodes.ArrayHistogramViewerNode`, class key
// `Taidal_ArrayHistogram`) is an OUTPUT_NODE that returns `{"ui": {"taidal_array_histogram":[payload]}}`;
// ComfyUI delivers that payload to `onExecuted(message)`. The payload carries: a base64 PNG of the
// colormapped image, the histogram counts/bin_edges + stats, and a small nearest-downsampled value
// grid (+ original dims) so we can map a hovered display pixel back to its source value/coordinate.
// ALL computation lives in recordstream — this file only DRAWS + hovers.
//
// RENDERING — we DRAW ON THE LITEGRAPH NODE CANVAS (onDrawForeground), NOT a DOM widget (the
// ComfyUI-Olm-Histogram pattern). A DOM widget under ComfyUI's shrink-to-fit parent broke width
// sizing, resize, and pointer routing; drawing on the node's own 2D context removes all of that.
//
// LAYOUT — the IMAGE is FIT-IN-BOX: scaled to fill the node's spare area (width AND height),
// aspect-preserved and centered, so growing the node in either axis enlarges the image as much as
// possible. The histogram band + stats text sit below it at the bottom; the stats line WORD-WRAPS to
// multiple lines when the node is too narrow. `computeSize` returns a SMALL minimum so the node
// resizes FREELY (no snap-back); the image then fills whatever space is available.

const NODE_KEY = "Taidal_ArrayHistogram";
const MIN_W = 260; // minimum node width
const MARGIN = 10; // content inset from the node's left/right edges
const HIST_H = 90; // histogram band height
const READOUT_H = 16; // hover-readout line
const STATS_LINE_H = 12; // per-line height of the (wrapping) stats text
const GAP = 6; // vertical gap between sections
const MIN_IMG_H = 60; // smallest image area — the node can't shrink below this
const DEFAULT_IMG_AREA = 300; // initial image-area height on the first run (one-time)
const WIDGET_H = 20; // litegraph widget row height fallback (LiteGraph.NODE_WIDGET_HEIGHT)

function widgetH() {
    return (typeof LiteGraph !== "undefined" && LiteGraph.NODE_WIDGET_HEIGHT) || WIDGET_H;
}

// Y (node-body coords) where our drawing starts: below the last litegraph widget (set by litegraph
// each draw via last_y).
function contentStartY(node) {
    let y = widgetH();
    for (const w of node.widgets || []) {
        if (typeof w.last_y === "number") y = Math.max(y, w.last_y + widgetH());
    }
    return y + GAP;
}

// Natural image dims from the decoded image, falling back to the payload's reported dims.
function imgDims(node) {
    const img = node._taidalImg;
    if (img && img.complete && img.naturalWidth > 0) return { w: img.naturalWidth, h: img.naturalHeight };
    const p = node._taidalPayload;
    if (p && p.image_width > 0 && p.image_height > 0) return { w: p.image_width, h: p.image_height };
    return null;
}

// Offscreen 2D context for pixel-accurate text measurement (used by computeSize AND draw so the
// reserved height matches the wrapped lines). NOT a DOM widget — just a measuring tool.
let _measureCtx = null;
function measureText(text) {
    if (!_measureCtx) _measureCtx = document.createElement("canvas").getContext("2d");
    _measureCtx.font = "10px monospace";
    return _measureCtx.measureText(text).width;
}

// Greedy word-wrap to fit maxW px (monospace). A run with no spaces stays on one (clipped) line.
function wrapText(text, maxW) {
    if (!text) return [""];
    if (measureText(text) <= maxW) return [text];
    const words = text.split(" ");
    const lines = [];
    let cur = "";
    for (const w of words) {
        const trial = cur ? `${cur} ${w}` : w;
        if (cur && measureText(trial) > maxW) {
            lines.push(cur);
            cur = w;
        } else {
            cur = trial;
        }
    }
    if (cur) lines.push(cur);
    return lines;
}

function fmt(v) {
    if (v === null || v === undefined) return "—";
    if (Math.abs(v) >= 1e4 || (v !== 0 && Math.abs(v) < 1e-3)) return v.toExponential(3);
    return (Math.round(v * 1e4) / 1e4).toString();
}

function statsString(node) {
    const p = node._taidalPayload;
    if (!p) return "Queue Prompt to inspect an array / tensor";
    if (p.message && p.stats.count === 0) return `${p.message} — ${p.shape || "?"} ${p.dtype || ""}`;
    const s = p.stats;
    const ch = p.channels > 1 ? `ch ${p.channel < 0 ? "all" : p.channel}/${p.channels}` : "1ch";
    return `min ${fmt(s.min)}  max ${fmt(s.max)}  mean ${fmt(s.mean)}  std ${fmt(s.std)}  n=${s.count}  ${ch}  ${p.shape || ""} ${p.dtype || ""}`;
}

// Vertical space below the image area (gap + readout + gap + histogram + gap + N stats lines).
function bottomHeight(statsLineCount) {
    return GAP + READOUT_H + GAP + HIST_H + GAP + Math.max(1, statsLineCount) * STATS_LINE_H;
}

// --------------------------------------------------------------------------- //
// Layout — rects (node-body coords) for the image, histogram, log button, stats. Stashed on the node
// each draw so the mouse handlers hit-test the same geometry.
// --------------------------------------------------------------------------- //

function computeLayout(node) {
    const top = contentStartY(node);
    const nodeW = node.size?.[0] ?? 300;
    const nodeH = node.size?.[1] ?? 200;
    const contentW = Math.max(16, nodeW - MARGIN * 2);

    const statsLines = wrapText(statsString(node), contentW);
    const bottom = bottomHeight(statsLines.length);
    const imageAreaH = Math.max(MIN_IMG_H, nodeH - top - bottom);

    // FIT-IN-BOX: largest aspect-preserved size inside (contentW × imageAreaH), centered.
    const dims = imgDims(node);
    let imgRect = null;
    if (dims && node._taidalPayload?.image) {
        const scale = Math.min(contentW / dims.w, imageAreaH / dims.h);
        const dW = Math.max(1, Math.round(dims.w * scale));
        const dH = Math.max(1, Math.round(dims.h * scale));
        imgRect = {
            x: Math.round((nodeW - dW) / 2),
            y: Math.round(top + (imageAreaH - dH) / 2),
            w: dW,
            h: dH,
        };
    }

    let y = top + imageAreaH + GAP;
    const readoutY = y;
    y += READOUT_H + GAP;
    const histRect = { x: MARGIN, y, w: contentW, h: HIST_H };
    y += HIST_H + GAP;
    const statsY = y;
    const logBtn = { x: histRect.x + histRect.w - 34, y: histRect.y + 2, w: 32, h: 13 };
    return { top, nodeW, contentW, imageAreaTop: top, imageAreaH, imgRect, readoutY, histRect, statsY, statsLines, logBtn };
}

// --------------------------------------------------------------------------- //
// Drawing
// --------------------------------------------------------------------------- //

function clippedText(ctx, text, x, y, maxW, color) {
    ctx.save();
    ctx.beginPath();
    ctx.rect(x, y - 11, maxW, 15);
    ctx.clip();
    ctx.fillStyle = color;
    ctx.font = "10px monospace";
    ctx.textBaseline = "alphabetic";
    ctx.fillText(text, x, y);
    ctx.restore();
}

function drawNode(node, ctx) {
    if (node.flags?.collapsed) return;
    const L = computeLayout(node);
    node._taidalLayout = L;
    const p = node._taidalPayload;

    // Image (fit-in-box, centered).
    if (L.imgRect && node._taidalImg) {
        const r = L.imgRect;
        ctx.save();
        ctx.fillStyle = "#1a1a1a";
        ctx.fillRect(r.x, r.y, r.w, r.h);
        try {
            ctx.drawImage(node._taidalImg, r.x, r.y, r.w, r.h);
        } catch (e) {
            /* image not ready */
        }
        ctx.strokeStyle = "#444";
        ctx.lineWidth = 1;
        ctx.strokeRect(r.x + 0.5, r.y + 0.5, r.w - 1, r.h - 1);
        const hv = node._taidalHover;
        if (hv) {
            ctx.strokeStyle = "rgba(120,220,255,0.9)";
            const cx = Math.round(r.x + hv.fx * r.w) + 0.5;
            const cy = Math.round(r.y + hv.fy * r.h) + 0.5;
            ctx.beginPath();
            ctx.moveTo(cx, r.y);
            ctx.lineTo(cx, r.y + r.h);
            ctx.moveTo(r.x, cy);
            ctx.lineTo(r.x + r.w, cy);
            ctx.stroke();
        }
        ctx.restore();
    }

    // Hover readout (single line) or hint.
    const readout = node._taidalReadout || (L.imgRect ? "hover the image to read a value" : "");
    clippedText(ctx, readout, MARGIN, L.readoutY + 11, L.contentW, "#cfe");

    // Histogram band.
    const hr = L.histRect;
    ctx.save();
    ctx.fillStyle = "#141414";
    ctx.fillRect(hr.x, hr.y, hr.w, hr.h);
    const counts = p?.histogram?.counts;
    if (counts && counts.length) {
        const logScale = !!node._taidalLog;
        const tx = counts.map((c) => (logScale ? Math.log10(1 + Math.max(0, c)) : c));
        const peak = tx.reduce((m, c) => Math.max(m, c), 0) || 1;
        const barW = hr.w / tx.length;
        ctx.fillStyle = "#4ea1ff";
        for (let i = 0; i < tx.length; i++) {
            const bh = (tx[i] / peak) * (hr.h - 2);
            ctx.fillRect(hr.x + i * barW, hr.y + hr.h - bh, Math.max(1, barW - 0.4), bh);
        }
    } else {
        clippedText(ctx, "(no data — Queue Prompt)", hr.x + 6, hr.y + 16, hr.w - 12, "#666");
    }
    ctx.restore();

    // log/lin toggle button.
    const b = L.logBtn;
    ctx.save();
    ctx.fillStyle = "rgba(0,0,0,0.45)";
    ctx.fillRect(b.x, b.y, b.w, b.h);
    ctx.fillStyle = node._taidalLog ? "#4ea1ff" : "#aaa";
    ctx.font = "9px monospace";
    ctx.textBaseline = "middle";
    ctx.fillText(node._taidalLog ? "log" : "lin", b.x + 8, b.y + b.h / 2 + 0.5);
    ctx.restore();

    // Stats — word-wrapped to as many lines as needed.
    let sy = L.statsY + 10;
    for (const line of L.statsLines) {
        clippedText(ctx, line, MARGIN, sy, L.contentW, "#aaa");
        sy += STATS_LINE_H;
    }
}

// --------------------------------------------------------------------------- //
// Mouse — node-local coordinates from litegraph (NO DOM events).
// --------------------------------------------------------------------------- //

function inRect(pos, r) {
    return r && pos[0] >= r.x && pos[0] <= r.x + r.w && pos[1] >= r.y && pos[1] <= r.y + r.h;
}

function onMove(node, pos) {
    const p = node._taidalPayload;
    const L = node._taidalLayout;
    if (!p || !L || !L.imgRect || !inRect(pos, L.imgRect)) {
        if (node._taidalHover) {
            node._taidalHover = null;
            node._taidalReadout = null;
            node.setDirtyCanvas(true, true);
        }
        return;
    }
    const r = L.imgRect;
    const fx = Math.min(0.999999, Math.max(0, (pos[0] - r.x) / r.w));
    const fy = Math.min(0.999999, Math.max(0, (pos[1] - r.y) / r.h));
    node._taidalHover = { fx, fy };
    if (p.values && p.grid_width > 0 && p.grid_height > 0) {
        const gx = Math.min(p.grid_width - 1, Math.floor(fx * p.grid_width));
        const gy = Math.min(p.grid_height - 1, Math.floor(fy * p.grid_height));
        const value = p.values[gy * p.grid_width + gx];
        const ox = Math.floor(fx * (p.orig_width || p.grid_width));
        const oy = Math.floor(fy * (p.orig_height || p.grid_height));
        node._taidalReadout = `(${ox}, ${oy}) = ${value === null || value === undefined ? "NaN" : fmt(value)}`;
    }
    node.setDirtyCanvas(true, true);
}

// --------------------------------------------------------------------------- //
// Registration
// --------------------------------------------------------------------------- //

app.registerExtension({
    name: "Taidal.ArrayHistogram",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData?.name !== NODE_KEY) return;

        // Node MINIMUM = widget area + a small image floor + the (wrapped) bottom band. Returning a
        // small minimum (not the image's natural size) lets the user resize FREELY in both axes with
        // no snap-back; the image then fills whatever space is available (fit-in-box, in computeLayout).
        nodeType.prototype.computeSize = function (out) {
            const size = LiteGraph.LGraphNode.prototype.computeSize.call(this, out);
            const w = Math.max(MIN_W, size[0]);
            const statsLines = wrapText(statsString(this), w - MARGIN * 2);
            size[0] = w;
            size[1] = size[1] + MIN_IMG_H + bottomHeight(statsLines.length);
            return size;
        };

        const onDrawForeground = nodeType.prototype.onDrawForeground;
        nodeType.prototype.onDrawForeground = function (ctx) {
            onDrawForeground?.apply(this, arguments);
            drawNode(this, ctx);
        };

        // Native litegraph mouse routing — `pos` is node-local (same coords as onDrawForeground).
        const onMouseMove = nodeType.prototype.onMouseMove;
        nodeType.prototype.onMouseMove = function (e, pos, canvas) {
            onMouseMove?.apply(this, arguments);
            onMove(this, pos);
        };
        const onMouseDown = nodeType.prototype.onMouseDown;
        nodeType.prototype.onMouseDown = function (e, pos, canvas) {
            const handled = onMouseDown?.apply(this, arguments);
            const L = this._taidalLayout;
            if (L && inRect(pos, L.logBtn)) {
                this._taidalLog = !this._taidalLog; // toggle log/linear histogram (display-only)
                this.setDirtyCanvas(true, true);
                return true; // consume so the node isn't dragged
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

        // The payload arrives on every run (and is re-delivered by ComfyUI on workflow re-open).
        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            onExecuted?.apply(this, arguments);
            const payload = Array.isArray(message?.taidal_array_histogram)
                ? message.taidal_array_histogram[0]
                : undefined;
            if (!payload) return;
            this._taidalPayload = payload;
            this._taidalHover = null;
            this._taidalReadout = null;
            const node = this;
            if (payload.image) {
                const img = new Image();
                img.onload = () => {
                    // One-time: give a fresh node a sensible image area sized to this image's aspect.
                    // Never resize a node the user has already sized (so re-runs respect their layout).
                    if (!node._taidalSized) {
                        node._taidalSized = true;
                        const w = node.size?.[0] ?? 300;
                        const base = LiteGraph.LGraphNode.prototype.computeSize.call(node, [0, 0])[1];
                        const aspect = img.naturalWidth / img.naturalHeight || 1.5;
                        const areaH = Math.min(Math.round((w - MARGIN * 2) / aspect), DEFAULT_IMG_AREA);
                        const lines = wrapText(statsString(node), w - MARGIN * 2);
                        node.setSize([w, base + Math.max(MIN_IMG_H, areaH) + bottomHeight(lines.length)]);
                    }
                    node.setDirtyCanvas(true, true);
                };
                img.src = payload.image;
                this._taidalImg = img;
            } else {
                this._taidalImg = null;
            }
            this.setDirtyCanvas(true, true);
        };
    },
});
