/**
 * The widths of the two sidebars — the pure half: what a drag of a sidebar's edge makes of its width, the limits it
 * stays within, and what this browser remembers of it. A width is the PERSON's, never the graph's: it is kept in
 * `localStorage` (best effort, like the Nodes sidebar's open state) and nothing of it reaches a graph's files.
 * `panels/SizeEdge.tsx` is the edge that is dragged; `App.tsx` hands the widths to the layout as CSS variables.
 */

/** Which sidebar: the Nodes sidebar on the left, the settings / run / text panel on the right. */
export type Sidebar = "nodes" | "side";

/** No sidebar is dragged narrower than this. */
export const MIN_WIDTH = 200;

/** The canvas keeps at least this much, whatever the sidebars ask for. */
export const MIN_CANVAS = 240;

const KEYS: Record<Sidebar, string> = { nodes: "streamstudio.nodes.width", side: "streamstudio.side.width" };

/** The width a sidebar has before anybody dragged it; the right panel is narrower inside a host's panel. */
export function initialWidth(sidebar: Sidebar, embed: boolean): number {
  if (sidebar === "nodes") return 300;
  return embed ? 320 : 380;
}

/**
 * `wanted` within the limits: at least `MIN_WIDTH`, and no wider than what leaves the canvas `MIN_CANVAS` beside
 * the `other` sidebar (0 when that one is closed). A page too narrow for both limits keeps the sidebar's minimum.
 */
export function clampWidth(wanted: number, page: number, other: number): number {
  const widest = Math.max(MIN_WIDTH, page - other - MIN_CANVAS);
  return Math.round(Math.min(Math.max(wanted, MIN_WIDTH), widest));
}

/** The width after the pointer moved from `fromX` to `x`: the left sidebar grows to the right, the right one to the left. */
export function draggedWidth(sidebar: Sidebar, start: number, fromX: number, x: number): number {
  return sidebar === "nodes" ? start + (x - fromX) : start - (x - fromX);
}

/** The width this browser remembers for a sidebar — `null` when none, or when what is stored is no width. */
export function readWidth(sidebar: Sidebar, storage: Pick<Storage, "getItem"> | null = safeStorage()): number | null {
  try {
    const stored = storage?.getItem(KEYS[sidebar]) ?? null;
    const width = stored === null || stored.trim() === "" ? NaN : Number(stored);
    return Number.isFinite(width) && width > 0 ? width : null;
  } catch {
    return null;
  }
}

/** Remember a sidebar's width, or forget it (`null`: the sidebar is back at its initial width). */
export function writeWidth(
  sidebar: Sidebar,
  width: number | null,
  storage: Pick<Storage, "setItem" | "removeItem"> | null = safeStorage(),
): void {
  try {
    if (width === null) storage?.removeItem(KEYS[sidebar]);
    else storage?.setItem(KEYS[sidebar], String(Math.round(width)));
  } catch {
    // storage may be blocked; the width still holds for this page
  }
}

function safeStorage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}
