/**
 * Groups: the pure half — how a `groups:` entry of the sidecar becomes a React Flow parent node around its
 * members (absolute positions in, relative positions out), and back (a dragged parent's members in absolute
 * positions, for `PUT …/positions`). The gestures are measured under playwright in `tests/test_canvas.py`.
 */
import { describe, expect, it } from "vitest";
import {
  DEFAULT_BOX,
  GROUP_COLORS,
  GROUP_HEAD,
  GROUP_PAD,
  absolutePositions,
  addGroupAt,
  groupAt,
  groupTogether,
  moveGroup,
  nextColor,
  placeBox,
  replaceConverted,
  nextGroupId,
  refitGroups,
  regroupAfterDrop,
  resizeGroup,
  withGroups,
  type Group,
  type Rect,
} from "../groups";
import type { GenericNode, ModelNode } from "../model";

const modelNode = (name: string): ModelNode => ({
  name,
  class_path: `mod.${name}`,
  kind: "op",
  params: {},
  inputs: [],
  outputs: [],
  loc: "",
  source_file: "",
  note: "",
  alias_of: "",
});

const generic = (name: string, x: number, y: number): GenericNode => ({
  id: name,
  type: "generic",
  position: { x, y },
  data: { node: modelNode(name) },
});

const group = (id: string, nodes: string[]): Group => ({ id, title: `title ${id}`, color: "#3b82f6", nodes });

describe("withGroups", () => {
  const nodes = [generic("a", 40, 40), generic("b", 380, 40), generic("c", 720, 40)];
  const sizes = { a: { width: 240, height: 100 }, b: { width: 240, height: 140 } };

  it("wraps the members in a parent node placed BEFORE them, on the 10-px grid, with room for a title", () => {
    const flow = withGroups(nodes, [group("g1", ["a", "b"])], sizes);
    expect(flow.map((node) => node.id)).toEqual(["group:g1", "a", "b", "c"]);
    const parent = flow[0];
    expect(parent.type).toBe("group");
    // origin: min x 40 - PAD 24 = 16 → 10 on the grid; min y 40 - PAD - HEAD 32 = -16 → -20 on the grid
    expect(parent.position).toEqual({ x: 10, y: -20 });
    // extent: right edge 380 + 240 + PAD = 644 → width 634; bottom 40 + 140 + PAD = 204 → height 224
    expect(parent.style).toEqual({ width: 634, height: 224 });
    // the dimensions React Flow clamps members to — it never reads the style for that
    expect([parent.width, parent.height]).toEqual([634, 224]);
    expect(GROUP_PAD).toBe(24);
    expect(GROUP_HEAD).toBe(32);
  });

  it("gives a member its position RELATIVE to the parent and never clamps it (the box follows the members)", () => {
    const flow = withGroups(nodes, [group("g1", ["a", "b"])], sizes);
    expect(flow[1]).toMatchObject({ id: "a", parentId: "group:g1", position: { x: 30, y: 60 } });
    expect(flow[2]).toMatchObject({ id: "b", parentId: "group:g1", position: { x: 370, y: 60 } });
    expect(flow[1].extent).toBeUndefined();
    expect(flow[3].parentId).toBeUndefined();
    expect(flow[3].position).toEqual({ x: 720, y: 40 });
  });

  it("carries the group and its DRAWN members in the parent's data", () => {
    const flow = withGroups(nodes, [group("g1", ["a", "nosuch", "b"])], sizes);
    expect(flow[0].data).toEqual({ group: group("g1", ["a", "nosuch", "b"]), members: ["a", "b"] });
  });

  it("draws no parent for a group whose members the model no longer has", () => {
    const flow = withGroups(nodes, [group("g1", ["gone"])], sizes);
    expect(flow.map((node) => node.id)).toEqual(["a", "b", "c"]);
  });

  it("puts a node in the FIRST group that names it", () => {
    const flow = withGroups(nodes, [group("g1", ["a"]), group("g2", ["a", "b"])], sizes);
    expect(flow.find((node) => node.id === "a")?.parentId).toBe("group:g1");
    expect(flow.find((node) => node.id === "b")?.parentId).toBe("group:g2");
  });

  it("assumes a node card's size until React Flow has measured it", () => {
    const flow = withGroups([generic("a", 40, 40)], [group("g1", ["a"])], {});
    expect(flow[0].style).toEqual({ width: 240 + 2 * GROUP_PAD + 6, height: 120 + 2 * GROUP_PAD + GROUP_HEAD + 4 });
  });
});

describe("absolutePositions", () => {
  it("adds a member's relative position to its parent's, and leaves a free node alone", () => {
    const flow = withGroups([generic("a", 40, 40), generic("c", 720, 40)], [group("g1", ["a"])], {
      a: { width: 240, height: 100 },
    });
    expect(absolutePositions(flow)).toEqual({ a: { x: 40, y: 40 }, c: { x: 720, y: 40 } });
  });

  it("takes an override for a node whose position moved (the dragged parent) before the array caught up", () => {
    const flow = withGroups([generic("a", 40, 40)], [group("g1", ["a"])], { a: { width: 240, height: 100 } });
    const moved = absolutePositions(flow, { "group:g1": { x: 130, y: 60 } });
    expect(moved).toEqual({ a: { x: 130 + 30, y: 60 + 60 } });
  });
});

describe("nextGroupId", () => {
  it("counts past the highest id in use", () => {
    expect(nextGroupId([])).toBe("g1");
    expect(nextGroupId([group("g1", []), group("g3", [])])).toBe("g4");
    expect(nextGroupId([group("odd", [])])).toBe("g1");
  });
});

describe("refitGroups", () => {
  it("follows the members' measured sizes and never moves the box's corner", () => {
    const flow = withGroups([generic("a", 40, 40)], [group("g1", ["a"])], {});
    const measured = flow.map((node) => (node.id === "a" ? { ...node, measured: { width: 300, height: 200 } } : node));
    const fitted = refitGroups(measured);
    expect(fitted[0].position).toEqual(flow[0].position);
    // member relative (30, 60) + size + PAD
    expect(fitted[0].style).toEqual({ width: 30 + 300 + GROUP_PAD, height: 60 + 200 + GROUP_PAD });
    expect([fitted[0].width, fitted[0].height]).toEqual([30 + 300 + GROUP_PAD, 60 + 200 + GROUP_PAD]);
    expect(fitted[1]).toBe(measured[1]);
  });

  it("answers the same array when every box already fits", () => {
    const flow = withGroups([generic("a", 40, 40)], [group("g1", ["a"])], { a: { width: 240, height: 100 } });
    const measured = flow.map((node) => (node.id === "a" ? { ...node, measured: { width: 240, height: 100 } } : node));
    expect(refitGroups(measured)).toBe(measured);
  });
});

describe("groupTogether", () => {
  it("adds 'group <n>' in the n-th colour, taking the members out of any group they were in", () => {
    const before = [group("g1", ["a", "b"]), group("g2", ["c"])];
    const after = groupTogether(before, ["b", "c"]);
    expect(after).toEqual([
      { ...group("g1", ["a"]) },
      { id: "g3", title: "group 3", color: GROUP_COLORS[2], nodes: ["b", "c"] },
    ]);
    expect(before[0].nodes).toEqual(["a", "b"]); // not edited in place
  });

  it("keeps a group that names no members at all (the page's own)", () => {
    const bare: Group = { id: "g7", title: "t", color: "#000" };
    expect(groupTogether([bare], ["a"])[0]).toEqual(bare);
  });
});

describe("nextColor", () => {
  it("steps through the cycle and wraps; an unknown colour starts it", () => {
    expect(nextColor(GROUP_COLORS[0])).toBe(GROUP_COLORS[1]);
    expect(nextColor(GROUP_COLORS[GROUP_COLORS.length - 1])).toBe(GROUP_COLORS[0]);
    expect(nextColor("red")).toBe(GROUP_COLORS[0]);
  });
});

// ---------------------------------------------------------------------------------------------------------------
// the box a person places and sizes: `bounds`, the level it lives on: `parent`
// ---------------------------------------------------------------------------------------------------------------

const boxed = (id: string, nodes: string[], bounds: [number, number, number, number], parent?: string): Group => ({
  ...group(id, nodes),
  bounds,
  ...(parent === undefined ? {} : { parent }),
});

describe("withGroups — a group with bounds", () => {
  it("draws an EMPTY group at its bounds", () => {
    const flow = withGroups([generic("a", 40, 40)], [boxed("g1", [], [300, 200, 400, 240])], {});
    expect(flow[0]).toMatchObject({ id: "group:g1", type: "group", position: { x: 300, y: 200 }, width: 400, height: 240 });
    expect(flow[0].data).toEqual({ group: boxed("g1", [], [300, 200, 400, 240]), members: [] });
    expect(flow[1].parentId).toBeUndefined();
  });

  it("draws the box at its bounds, not around the members, and places the members relative to it", () => {
    const flow = withGroups([generic("a", 340, 260)], [boxed("g1", ["a"], [300, 200, 600, 400])], {});
    expect(flow[0]).toMatchObject({ position: { x: 300, y: 200 }, style: { width: 600, height: 400 } });
    expect(flow[1]).toMatchObject({ id: "a", parentId: "group:g1", position: { x: 40, y: 60 } });
  });

  it("draws only the groups of the level on the canvas; `parent` absent is the top", () => {
    const groups = [boxed("g1", [], [0, 0, 100, 100]), boxed("g2", [], [0, 0, 100, 100], "prep")];
    expect(withGroups([], groups, {}).map((node) => node.id)).toEqual(["group:g1"]);
    expect(withGroups([], groups, {}, "prep").map((node) => node.id)).toEqual(["group:g2"]);
  });
});

describe("refitGroups — a sized box keeps its size", () => {
  it("leaves a group with bounds alone when its members grow", () => {
    const flow = withGroups([generic("a", 340, 260)], [boxed("g1", ["a"], [300, 200, 600, 400])], {});
    const measured = flow.map((node) => (node.id === "a" ? { ...node, measured: { width: 900, height: 900 } } : node));
    expect(refitGroups(measured)).toBe(measured);
  });
});

const rect = (x: number, y: number, width = 100, height = 60): Rect => ({ x, y, width, height });

describe("groupAt", () => {
  const boxes = [
    { id: "outer", rect: rect(0, 0, 1000, 1000) },
    { id: "inner", rect: rect(100, 100, 200, 200) },
  ];
  it("answers the smallest box holding the point", () => {
    expect(groupAt(boxes, { x: 150, y: 150 })).toBe("inner");
    expect(groupAt(boxes, { x: 500, y: 500 })).toBe("outer");
    expect(groupAt(boxes, { x: 1500, y: 0 })).toBeUndefined();
  });
});

describe("regroupAfterDrop — dragging a node into a box adds it, out of a box removes it", () => {
  const boxes = [
    { id: "g1", rect: rect(0, 0, 400, 300) },
    { id: "g2", rect: rect(600, 0, 400, 300) },
  ];

  it("adds a free node whose centre lands in a box", () => {
    const groups = [group("g1", ["a"]), group("g2", ["b"])];
    const after = regroupAfterDrop(groups, boxes, [{ name: "c", rect: rect(100, 100) }]);
    expect(after.map((g) => g.nodes)).toEqual([["a", "c"], ["b"]]);
    expect(groups[0].nodes).toEqual(["a"]); // not edited in place
  });

  it("removes a member whose centre lands outside its box", () => {
    const groups = [group("g1", ["a", "c"])];
    const after = regroupAfterDrop(groups, boxes, [{ name: "c", rect: rect(450, 400) }]);
    expect(after).toEqual([group("g1", ["a"])]);
  });

  it("keeps a box its last member left, at the place it was drawn", () => {
    const after = regroupAfterDrop([group("g1", ["c"])], boxes, [{ name: "c", rect: rect(450, 400) }]);
    expect(after).toEqual([{ ...group("g1", []), bounds: [0, 0, 400, 300] }]);
  });

  it("moves a node from one box to another", () => {
    const after = regroupAfterDrop([group("g1", ["a", "c"]), group("g2", ["b"])], boxes, [
      { name: "c", rect: rect(700, 100) },
    ]);
    expect(after.map((g) => g.nodes)).toEqual([["a"], ["b", "c"]]);
  });

  it("answers the SAME array when every node stayed in its own box, or stayed out of all of them", () => {
    const groups = [group("g1", ["a"]), group("g2", ["b"])];
    expect(regroupAfterDrop(groups, boxes, [{ name: "a", rect: rect(20, 20) }])).toBe(groups);
    expect(regroupAfterDrop(groups, boxes, [{ name: "z", rect: rect(2000, 2000) }])).toBe(groups);
  });

  it("never touches a group of another level", () => {
    const groups = [group("g1", ["a"]), { ...group("g9", ["c"]), parent: "prep" }];
    const after = regroupAfterDrop(groups, boxes, [{ name: "c", rect: rect(100, 100) }]);
    expect(after.map((g) => g.nodes)).toEqual([["a", "c"], ["c"]]);
  });
});

describe("resizeGroup — a resized box holds what lies inside it", () => {
  const nodes = [
    { name: "a", rect: rect(20, 20) },
    { name: "b", rect: rect(500, 20) },
    { name: "c", rect: rect(20, 500) },
  ];

  it("sets the bounds and takes exactly the nodes whose centre is inside, out of any other box", () => {
    const groups = [group("g1", ["a"]), group("g2", ["b", "c"])];
    const after = resizeGroup(groups, "g1", rect(0, 0, 700, 200), nodes);
    expect(after).toEqual([{ ...group("g1", ["a", "b"]), bounds: [0, 0, 700, 200] }, group("g2", ["c"])]);
  });

  it("drops a box drawn around its members once the resized one took them all (as grouping does)", () => {
    const after = resizeGroup([group("g1", ["a"]), group("g2", ["b"])], "g1", rect(0, 0, 700, 200), nodes);
    expect(after).toEqual([{ ...group("g1", ["a", "b"]), bounds: [0, 0, 700, 200] }]);
  });

  it("keeps a SIZED box that lost its members — a person placed it", () => {
    const after = resizeGroup([group("g1", ["a"]), boxed("g2", ["b"], [480, 0, 200, 100])], "g1", rect(0, 0, 700, 200), nodes);
    expect(after[1]).toEqual(boxed("g2", [], [480, 0, 200, 100]));
  });

  it("lets go of a member the smaller box no longer holds", () => {
    const after = resizeGroup([group("g1", ["a", "c"])], "g1", rect(0, 0, 300, 300), nodes);
    expect(after).toEqual([{ ...group("g1", ["a"]), bounds: [0, 0, 300, 300] }]);
  });
});

describe("moveGroup / addGroupAt", () => {
  it("moves a sized box's corner; a derived box has nothing to move", () => {
    expect(moveGroup([boxed("g1", [], [0, 0, 100, 100])], "g1", { x: 50, y: 70 })).toEqual([boxed("g1", [], [50, 70, 100, 100])]);
    const derived = [group("g1", ["a"])];
    expect(moveGroup(derived, "g1", { x: 50, y: 70 })).toBe(derived);
  });

  it("adds an empty box at the point, on the grid, on the level, in the next colour", () => {
    const after = addGroupAt([group("g1", ["a"])], { x: 123, y: 58 });
    expect(after[1]).toEqual({
      id: "g2",
      title: "group 2",
      color: GROUP_COLORS[1],
      nodes: [],
      bounds: [120, 50, DEFAULT_BOX.width, DEFAULT_BOX.height],
    });
    expect(addGroupAt([], { x: 0, y: 0 }, "prep")[0].parent).toBe("prep");
  });
});

describe("replaceConverted — a group whose members became a subgraph names the subgraph", () => {
  const gone = (names: string[]) => (name: string) => names.includes(name);

  it("replaces the converted members by the new node, once, where the first of them stood", () => {
    const groups = [group("g1", ["decode", "grey", "scaled"]), group("g2", ["mask"])];
    const after = replaceConverted(groups, ["grey", "scaled"], "subgraph", gone(["grey", "scaled"]));
    expect(after).toEqual([group("g1", ["decode", "subgraph"]), group("g2", ["mask"])]);
  });

  it("never writes a group naming a node that vanished: a box of exactly the converted steps frames the subgraph", () => {
    const after = replaceConverted([group("g1", ["grey", "scaled"])], ["grey", "scaled"], "subgraph", gone(["grey", "scaled"]));
    expect(after).toEqual([group("g1", ["subgraph"])]);
  });

  it("leaves a converted name that is still a node (a list member's position, now the subgraph or a later member)", () => {
    const groups = [group("g1", ["stream.ops[1]", "stream.ops[2]"])];
    // ops[1] IS the new subgraph; ops[2] is now the member after it — the server re-keys those, not the page
    expect(replaceConverted(groups, ["stream.ops[1]", "stream.ops[2]"], "stream.ops[1]", () => false)).toBe(groups);
  });

  it("answers the same array when no group held a converted member (nothing to write)", () => {
    const groups = [group("g1", ["mask"])];
    expect(replaceConverted(groups, ["grey"], "subgraph", gone(["grey"]))).toBe(groups);
  });

  it("touches only the groups of the converted steps' level", () => {
    const inner = { ...group("g2", ["prep/grey"]), parent: "prep" };
    const after = replaceConverted([group("g1", ["grey"]), inner], ["prep/grey"], "prep/subgraph", gone(["prep/grey"]), "prep");
    expect(after).toEqual([group("g1", ["grey"]), { ...inner, nodes: ["prep/subgraph"] }]);
  });
});

describe("placeBox — where Graph ▸ Add group drops its empty box", () => {
  const view = rect(0, 0, 1000, 800);

  it("takes the wanted corner when the box covers no card there", () => {
    expect(placeBox({ x: 300, y: 300 }, DEFAULT_BOX, [rect(0, 0, 240, 120)], view)).toEqual({ x: 300, y: 300 });
  });

  it("moves to the nearest free place in view when a card is in the way — never over a card it does not hold", () => {
    const cards = [rect(280, 40, 240, 120), rect(560, 40, 240, 120)];
    const at = placeBox({ x: 290, y: 20 }, DEFAULT_BOX, cards, view);
    const box = { ...at, ...DEFAULT_BOX };
    const overlaps = (a: Rect, b: Rect) => a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
    expect(cards.some((card) => overlaps(card, box))).toBe(false);
    expect(at.x % 10).toBe(0);
    expect(at.y % 10).toBe(0);
    expect(box.x >= view.x && box.y >= view.y && box.x + box.width <= view.x + view.width && box.y + box.height <= view.y + view.height).toBe(true);
  });

  it("goes below every card when the view holds no free place", () => {
    const cards = [rect(0, 0, 1000, 800)];
    expect(placeBox({ x: 100, y: 100 }, DEFAULT_BOX, cards, view)).toEqual({ x: 0, y: 840 });
  });
});
