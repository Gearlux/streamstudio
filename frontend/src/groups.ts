/**
 * Groups: a titled, coloured box the canvas draws around nodes — the sidecar's `groups:` block, never the
 * document's (a group is how a person arranged the drawing, not something the pipeline does; the YAML has no
 * spelling for one, so boxing two nodes must not be a document edit). The server stores a group as the page
 * spells it and reads inside it only to re-key a renamed member (`streamstudio/layout.py`).
 *
 * React Flow carries children with a parent node: a member gets `parentId` and a position RELATIVE to it, so
 * dragging the box moves every member by the same delta for free. The sidecar keeps ABSOLUTE positions (a
 * node's place must not depend on whether it is grouped — ungrouping returns it exactly where it is drawn), so
 * the box is DERIVED on every draw from its members' absolute positions and measured sizes (`withGroups`), and a
 * drag is turned back into absolute positions before it is saved (`absolutePositions`).
 */
import type { CanvasNode, GenericNode, GroupNode } from "./model";

/** One group as the sidecar holds it. Any other key is the page's own and survives a round trip. */
export interface Group {
  id: string;
  title: string;
  color: string;
  nodes?: string[];
  [extra: string]: unknown;
}

/** Room around the members, in canvas pixels. */
export const GROUP_PAD = 24;
/** Room above the members for the title bar. */
export const GROUP_HEAD = 32;
/** The canvas's snap grid: a box's corner sits on it so that a drag by whole grid steps moves members exactly. */
const GRID = 10;
/** A node card's size until React Flow has measured it (the card's min-width, a typical height). */
const ASSUMED = { width: 240, height: 120 };

/** The colours a new group cycles through, and the recolor button steps through. */
export const GROUP_COLORS = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899", "#14b8a6", "#64748b"];

export type Size = { width: number; height: number };
export type XY = { x: number; y: number };

/** The React Flow id of a group's box — prefixed so it can never collide with a document node name. */
export const groupNodeId = (id: string): string => `group:${id}`;

const floorToGrid = (value: number): number => Math.floor(value / GRID) * GRID;

/**
 * The flow's nodes with every group drawn: a box per group (placed BEFORE its members, as React Flow requires),
 * each member re-positioned relative to it. A node is in the FIRST group naming it; a group none of whose
 * members the model has is not drawn (it stays in the sidecar).
 */
export function withGroups(nodes: GenericNode[], groups: Group[], sizes: Record<string, Size>): CanvasNode[] {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const claimed = new Map<string, { parentId: string; origin: XY }>();
  const boxes: GroupNode[] = [];
  for (const group of groups) {
    const members = (group.nodes ?? []).filter((name) => byId.has(name) && !claimed.has(name));
    if (members.length === 0) continue;
    const rects = members.map((name) => ({ ...byId.get(name)!.position, ...(sizes[name] ?? ASSUMED) }));
    const origin = {
      x: floorToGrid(Math.min(...rects.map((r) => r.x)) - GROUP_PAD),
      y: floorToGrid(Math.min(...rects.map((r) => r.y)) - GROUP_PAD - GROUP_HEAD),
    };
    const id = groupNodeId(group.id);
    const width = Math.max(...rects.map((r) => r.x + r.width)) + GROUP_PAD - origin.x;
    const height = Math.max(...rects.map((r) => r.y + r.height)) + GROUP_PAD - origin.y;
    boxes.push({
      id,
      type: "group",
      position: origin,
      // `width`/`height` as well as the style: React Flow clamps an `extent: "parent"` member to the box's
      // DIMENSIONS (`getNodeDimensions` reads measured ?? width ?? 0, never the style), so a box known only by its
      // style is 0×0 until measured and every member is pushed into its corner on the first draw (measured).
      width,
      height,
      style: { width, height },
      dragHandle: ".group-title",
      data: { group, members },
    });
    for (const name of members) claimed.set(name, { parentId: id, origin });
  }
  const drawn = nodes.map((node): CanvasNode => {
    const parent = claimed.get(node.id);
    if (!parent) return node;
    // No `extent: "parent"`: the box FOLLOWS its members (it is derived from their positions on every draw), so a
    // member is never clamped into it. Clamping measured wrong — on a fresh load the box is sized from ASSUMED card
    // heights until React Flow measures them, and the clamp drew members up to 37 px off their saved place for
    // 0.1–0.8 s. A member dragged past the edge simply grows the box on the next answer.
    return {
      ...node,
      parentId: parent.parentId,
      position: { x: node.position.x - parent.origin.x, y: node.position.y - parent.origin.y },
    };
  });
  return [...boxes, ...drawn];
}

/**
 * Every document node's ABSOLUTE position (a group box is not a document node and is left out) — what the
 * sidecar stores. `overrides` holds positions React Flow reported before the array caught up (a drag's last
 * frame), for a member or for the box it sits in.
 */
export function absolutePositions(nodes: CanvasNode[], overrides: Record<string, XY> = {}): Record<string, XY> {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const at = (node: CanvasNode): XY => overrides[node.id] ?? node.position;
  const placed: Record<string, XY> = {};
  for (const node of nodes) {
    if (node.type === "group") continue;
    const own = at(node);
    const parent = node.parentId ? byId.get(node.parentId) : undefined;
    const origin = parent ? at(parent) : { x: 0, y: 0 };
    placed[node.id] = { x: origin.x + own.x, y: origin.y + own.y };
  }
  return placed;
}

/**
 * The boxes re-fitted to their members' CURRENT sizes (React Flow measures a node after it is drawn, and a node
 * grows when a widget does): a box's width and height follow its members, its corner never moves — so nothing a
 * member's position depends on changes. Answers the same array when nothing needed a new size.
 */
export function refitGroups(nodes: CanvasNode[]): CanvasNode[] {
  const extent = new Map<string, Size>();
  for (const node of nodes) {
    if (!node.parentId) continue;
    const size = { width: node.measured?.width ?? ASSUMED.width, height: node.measured?.height ?? ASSUMED.height };
    const far = extent.get(node.parentId) ?? { width: 0, height: 0 };
    extent.set(node.parentId, {
      width: Math.max(far.width, node.position.x + size.width + GROUP_PAD),
      height: Math.max(far.height, node.position.y + size.height + GROUP_PAD),
    });
  }
  let changed = false;
  const fitted = nodes.map((node) => {
    const want = extent.get(node.id);
    if (node.type !== "group" || !want) return node;
    if (node.style?.width === want.width && node.style?.height === want.height) return node;
    changed = true;
    return { ...node, ...want, style: { ...node.style, ...want } };
  });
  return changed ? fitted : nodes;
}

/** The id a new group takes: one past the highest `g<n>` in use. */
export function nextGroupId(groups: Group[]): string {
  const used = groups.map((group) => /^g(\d+)$/.exec(group.id)).map((match) => (match ? Number(match[1]) : 0));
  return `g${Math.max(0, ...used) + 1}`;
}

/** The colour after `color` in the cycle (the first for a colour the cycle does not hold). */
export function nextColor(color: string): string {
  const index = GROUP_COLORS.indexOf(color);
  return GROUP_COLORS[(index + 1) % GROUP_COLORS.length];
}

/**
 * The groups after boxing `members` together: a new group (`group <n>`, the n-th colour) naming them in the
 * order given, each member taken out of any group it was in — a node is drawn in one box — and a group left
 * with no member dropped.
 */
export function groupTogether(groups: Group[], members: string[]): Group[] {
  const taken = new Set(members);
  const kept = groups
    .map((group) => (group.nodes ? { ...group, nodes: group.nodes.filter((name) => !taken.has(name)) } : group))
    .filter((group) => !group.nodes || group.nodes.length > 0);
  const id = nextGroupId(groups);
  const n = Number(id.slice(1));
  return [...kept, { id, title: `group ${n}`, color: GROUP_COLORS[(n - 1) % GROUP_COLORS.length], nodes: [...members] }];
}
