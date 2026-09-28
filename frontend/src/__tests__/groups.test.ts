/**
 * Groups: the pure half — how a `groups:` entry of the sidecar becomes a React Flow parent node around its
 * members (absolute positions in, relative positions out), and back (a dragged parent's members in absolute
 * positions, for `PUT …/positions`). The gestures are measured under playwright in `tests/test_canvas.py`.
 */
import { describe, expect, it } from "vitest";
import {
  GROUP_COLORS,
  GROUP_HEAD,
  GROUP_PAD,
  absolutePositions,
  groupTogether,
  nextColor,
  nextGroupId,
  refitGroups,
  withGroups,
  type Group,
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
