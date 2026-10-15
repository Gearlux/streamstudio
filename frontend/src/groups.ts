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
 *
 * A box a person PLACED or SIZED carries `bounds: [x, y, w, h]` (Add Group drops an empty one where the canvas was
 * right-clicked; the resize handles set it), and is then drawn at its bounds rather than around its members. Its
 * membership follows the geometry of a gesture, ComfyUI style: a node dropped with its centre inside a box joins it,
 * one dropped outside leaves it (`regroupAfterDrop`), and a resized box holds exactly the nodes whose centre it covers
 * (`resizeGroup`). `parent` names the subgraph level a group lives on (`""` or absent = the top): a canvas draws only
 * the groups of the level it shows.
 */
import type { CanvasNode, GenericNode, GroupNode } from "./model";

/** One group as the sidecar holds it. Any other key is the page's own and survives a round trip. */
export interface Group {
  id: string;
  title: string;
  color: string;
  nodes?: string[];
  /** `[x, y, width, height]` of a box a person placed or sized; absent for a box drawn around its members. */
  bounds?: [number, number, number, number];
  /** The subgraph level the group lives on (`prep`, `prep/inner`); absent or `""` is the top. */
  parent?: string;
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

/** What the Colour menu calls each of them. */
export const COLOR_NAMES: Record<string, string> = {
  "#3b82f6": "Blue",
  "#10b981": "Green",
  "#f59e0b": "Amber",
  "#ef4444": "Red",
  "#8b5cf6": "Violet",
  "#ec4899": "Pink",
  "#14b8a6": "Teal",
  "#64748b": "Slate",
};

export type Size = { width: number; height: number };
export type XY = { x: number; y: number };
export type Rect = XY & Size;

/** The size of a box Add Group drops. */
export const DEFAULT_BOX: Size = { width: 420, height: 260 };

/** The level a group lives on: `""` is the top. */
export const levelOf = (group: Group): string => group.parent ?? "";

/** The React Flow id of a group's box — prefixed so it can never collide with a document node name. */
export const groupNodeId = (id: string): string => `group:${id}`;

const floorToGrid = (value: number): number => Math.floor(value / GRID) * GRID;

/**
 * The flow's nodes with every group drawn: a box per group (placed BEFORE its members, as React Flow requires),
 * each member re-positioned relative to it. A node is in the FIRST group naming it; a group none of whose
 * members the model has is not drawn (it stays in the sidecar).
 */
export function withGroups(
  nodes: GenericNode[],
  groups: Group[],
  sizes: Record<string, Size>,
  level = "",
): CanvasNode[] {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const claimed = new Map<string, { parentId: string; origin: XY }>();
  const boxes: GroupNode[] = [];
  for (const group of groups) {
    if (levelOf(group) !== level) continue;
    const members = (group.nodes ?? []).filter((name) => byId.has(name) && !claimed.has(name));
    if (members.length === 0 && !group.bounds) continue;
    const id = groupNodeId(group.id);
    let origin: XY;
    let width: number;
    let height: number;
    if (group.bounds) {
      [origin, width, height] = [{ x: group.bounds[0], y: group.bounds[1] }, group.bounds[2], group.bounds[3]];
    } else {
      const rects = members.map((name) => ({ ...byId.get(name)!.position, ...(sizes[name] ?? ASSUMED) }));
      origin = {
        x: floorToGrid(Math.min(...rects.map((r) => r.x)) - GROUP_PAD),
        y: floorToGrid(Math.min(...rects.map((r) => r.y)) - GROUP_PAD - GROUP_HEAD),
      };
      width = Math.max(...rects.map((r) => r.x + r.width)) + GROUP_PAD - origin.x;
      height = Math.max(...rects.map((r) => r.y + r.height)) + GROUP_PAD - origin.y;
    }
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
    // a box a person sized keeps the size they gave it; a member may overhang it
    if (node.type !== "group" || !want || node.data.group.bounds) return node;
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
 * order given, each member taken out of any group of the same level it was in — a node is drawn in one box — and a
 * box drawn around its members that is left with none dropped (a SIZED box stays: a person placed it).
 */
export function groupTogether(groups: Group[], members: string[], level = ""): Group[] {
  const taken = new Set(members);
  const kept = groups
    .map((group) =>
      group.nodes && levelOf(group) === level ? { ...group, nodes: group.nodes.filter((name) => !taken.has(name)) } : group,
    )
    .filter((group) => !group.nodes || group.nodes.length > 0 || group.bounds);
  const id = nextGroupId(groups);
  const n = Number(id.slice(1));
  const group: Group = { id, title: `group ${n}`, color: GROUP_COLORS[(n - 1) % GROUP_COLORS.length], nodes: [...members] };
  return [...kept, level === "" ? group : { ...group, parent: level }];
}

// ---------------------------------------------------------------------------------------------------------------
// geometry: a gesture decides membership
// ---------------------------------------------------------------------------------------------------------------

const centreOf = (rect: Rect): XY => ({ x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 });

const holds = (rect: Rect, point: XY): boolean =>
  point.x >= rect.x && point.x <= rect.x + rect.width && point.y >= rect.y && point.y <= rect.y + rect.height;

/** A group's box as drawn: its id and its rectangle in flow coordinates. */
export interface Box {
  id: string;
  rect: Rect;
}

/** The group whose drawn box holds `point` — the smallest one, so a box drawn inside another is the one hit. */
export function groupAt(boxes: Box[], point: XY): string | undefined {
  const hits = boxes.filter((box) => holds(box.rect, point));
  hits.sort((a, b) => a.rect.width * a.rect.height - b.rect.width * b.rect.height);
  return hits[0]?.id;
}

const withoutMember = (group: Group, name: string): Group => ({ ...group, nodes: (group.nodes ?? []).filter((n) => n !== name) });
const withMember = (group: Group, name: string): Group => ({ ...group, nodes: [...(group.nodes ?? []), name] });

/**
 * The groups after nodes were dropped: each dropped node belongs to the box its centre landed in (the boxes as drawn
 * before the drop), or to none. A box that loses its last member stays where it was drawn — it gets `bounds` — so a
 * drag out of a group never deletes the group. Only the groups of `level` are touched; the SAME array comes back when
 * no membership changed, so the caller writes nothing.
 */
export function regroupAfterDrop(groups: Group[], boxes: Box[], dropped: { name: string; rect: Rect }[], level = ""): Group[] {
  const drawn = new Map(boxes.map((box) => [box.id, box.rect]));
  let next = groups;
  for (const { name, rect } of dropped) {
    const target = groupAt(boxes, centreOf(rect));
    const current = next.find((group) => levelOf(group) === level && (group.nodes ?? []).includes(name))?.id;
    if (target === current) continue;
    next = next.map((group) => {
      if (levelOf(group) !== level) return group;
      if (group.id === current) {
        const left = withoutMember(group, name);
        const box = drawn.get(group.id);
        return (left.nodes ?? []).length === 0 && !left.bounds && box
          ? { ...left, bounds: [box.x, box.y, box.width, box.height] }
          : left;
      }
      return group.id === target ? withMember(group, name) : group;
    });
  }
  return next;
}

/**
 * The groups after box `id` was resized to `rect`: it gets those bounds and holds exactly the nodes of its level whose
 * centre `rect` covers, each taken out of any other box. A box drawn around its members that is left with none is
 * dropped (as grouping drops one); a sized box is kept, empty.
 */
export function resizeGroup(groups: Group[], id: string, rect: Rect, nodes: { name: string; rect: Rect }[]): Group[] {
  const resized = groups.find((group) => group.id === id);
  if (!resized) return groups;
  const level = levelOf(resized);
  const inside = nodes.filter((node) => holds(rect, centreOf(node.rect))).map((node) => node.name);
  const taken = new Set(inside);
  return groups
    .map((group): Group => {
      if (group.id === id) return { ...group, nodes: inside, bounds: [rect.x, rect.y, rect.width, rect.height] };
      if (levelOf(group) !== level || !group.nodes) return group;
      return { ...group, nodes: group.nodes.filter((name) => !taken.has(name)) };
    })
    .filter((group) => group.id === id || group.bounds || !group.nodes || group.nodes.length > 0);
}

/** A sized box moved by its title bar: its corner follows. A box drawn around its members has nothing to store. */
export function moveGroup(groups: Group[], id: string, to: XY): Group[] {
  const moved = groups.find((group) => group.id === id);
  if (!moved?.bounds) return groups;
  return groups.map((group) =>
    group.id === id ? { ...group, bounds: [to.x, to.y, moved.bounds![2], moved.bounds![3]] as Group["bounds"] } : group,
  );
}

/** Add Group: an EMPTY box at `point` (its corner on the grid), `group <n>` in the n-th colour, on `level`. */
export function addGroupAt(groups: Group[], point: XY, level = "", size: Size = DEFAULT_BOX): Group[] {
  const id = nextGroupId(groups);
  const n = Number(id.slice(1));
  const group: Group = {
    id,
    title: `group ${n}`,
    color: GROUP_COLORS[(n - 1) % GROUP_COLORS.length],
    nodes: [],
    bounds: [floorToGrid(point.x), floorToGrid(point.y), size.width, size.height],
  };
  return [...groups, level === "" ? group : { ...group, parent: level }];
}

/**
 * The groups after steps became ONE subgraph node `name` (Convert selection to subgraph, Convert group to subgraph): in
 * every group of `level`, a member among `converted` that is no longer a node (`gone`) is replaced by `name` — once,
 * where the first of them stood — so a box around the converted steps frames the new node and no group is written
 * naming nodes that vanished. A converted name that is still a node is left alone: a list member is named by its
 * POSITION, so `stream.ops[1]` is now the subgraph itself and `stream.ops[2]` the member after it — re-keying those is
 * the server's (it holds the old → new names). The SAME array comes back when no group held a converted member.
 */
export function replaceConverted(
  groups: Group[],
  converted: string[],
  name: string,
  gone: (node: string) => boolean,
  level = "",
): Group[] {
  const moved = new Set(converted.filter(gone));
  if (moved.size === 0) return groups;
  let changed = false;
  const next = groups.map((group) => {
    if (levelOf(group) !== level || !(group.nodes ?? []).some((member) => moved.has(member))) return group;
    changed = true;
    const nodes: string[] = [];
    for (const member of group.nodes ?? []) {
      const kept = moved.has(member) ? name : member;
      if (!nodes.includes(kept)) nodes.push(kept);
    }
    return { ...group, nodes };
  });
  return changed ? next : groups;
}

/** The room Add group keeps between its box and a card or another box. */
const BOX_GAP = 20;
/** How far apart the places Add group tries are (two grid cells). */
const BOX_STEP = 2 * GRID;

/**
 * Where Graph ▸ Add group drops its EMPTY box (`size`, flow coordinates): `want` (the middle of the view) when the box
 * covers no card or box there, else the nearest place inside `view` where it covers none, else below everything —
 * never over a card it does not hold. The corner is on the grid.
 */
export function placeBox(want: XY, size: Size, obstacles: Rect[], view: Rect): XY {
  const free = (x: number, y: number): boolean =>
    !obstacles.some(
      (card) =>
        x < card.x + card.width + BOX_GAP &&
        card.x < x + size.width + BOX_GAP &&
        y < card.y + card.height + BOX_GAP &&
        card.y < y + size.height + BOX_GAP,
    );
  const start = { x: floorToGrid(want.x), y: floorToGrid(want.y) };
  if (free(start.x, start.y)) return start;
  let best: XY | undefined;
  let bestDistance = Infinity;
  const x0 = Math.ceil(view.x / GRID) * GRID;
  const y0 = Math.ceil(view.y / GRID) * GRID;
  for (let y = y0; y + size.height <= view.y + view.height; y += BOX_STEP)
    for (let x = x0; x + size.width <= view.x + view.width; x += BOX_STEP) {
      const distance = (x - want.x) ** 2 + (y - want.y) ** 2;
      if (distance < bestDistance && free(x, y)) {
        best = { x, y };
        bestDistance = distance;
      }
    }
  if (best) return best;
  const bottom = Math.max(view.y, ...obstacles.map((card) => card.y + card.height));
  return { x: floorToGrid(view.x), y: Math.ceil((bottom + 2 * BOX_GAP) / GRID) * GRID };
}
