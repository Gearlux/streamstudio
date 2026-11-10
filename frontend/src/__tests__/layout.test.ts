/**
 * The client-side arrange: dagre over the MEASURED node boxes along the wires — left to right, diagonal or top to
 * bottom — snapped to the canvas's 10-px grid. The button and the saved sidecar are measured under playwright in `tests/test_canvas.py`.
 */
import { describe, expect, it } from "vitest";
import { DIAGONAL_GAP, GRID, arrange, runsDown, type Direction } from "../layout";

const box = (id: string, width = 240, height = 120) => ({ id, width, height });

describe("arrange", () => {
  const chain = [box("a"), box("b"), box("c"), box("d")];
  const wires = [
    { source: "a", target: "b" },
    { source: "b", target: "c" },
    { source: "c", target: "d" },
  ];

  it("puts a chain in one row, left to right in wire order", () => {
    const placed = arrange(chain, wires);
    const xs = ["a", "b", "c", "d"].map((id) => placed[id].x);
    expect(xs).toEqual([...xs].sort((p, q) => p - q));
    expect(new Set(xs).size).toBe(4);
    expect(new Set(Object.values(placed).map((p) => p.y)).size).toBe(1);
  });

  it("snaps every corner to the grid", () => {
    const placed = arrange([box("a", 233, 97), box("b", 251, 143)], [{ source: "a", target: "b" }]);
    for (const p of Object.values(placed)) {
      expect(p.x % GRID).toBe(0);
      expect(p.y % GRID).toBe(0);
    }
  });

  it("leaves no two boxes overlapping, a fork included", () => {
    const boxes = [box("a"), box("b", 240, 300), box("c"), box("d")];
    const placed = arrange(boxes, [
      { source: "a", target: "b" },
      { source: "a", target: "c" },
      { source: "b", target: "d" },
      { source: "c", target: "d" },
    ]);
    const rects = boxes.map((b) => ({ ...placed[b.id], w: b.width, h: b.height }));
    for (const [i, r] of rects.entries())
      for (const s of rects.slice(i + 1))
        expect(r.x + r.w <= s.x || s.x + s.w <= r.x || r.y + r.h <= s.y || s.y + s.h <= r.y).toBe(true);
  });

  it("ignores a wire to a box it was not given, and places a box nothing wires", () => {
    const placed = arrange([box("a"), box("lonely")], [{ source: "a", target: "ghost" }]);
    expect(Object.keys(placed).sort()).toEqual(["a", "lonely"]);
  });

  it("is deterministic", () => expect(arrange(chain, wires)).toEqual(arrange(chain, wires)));

  it("puts a chain in one column, top to bottom in wire order", () => {
    const placed = arrange(chain, wires, "TB");
    const ys = ["a", "b", "c", "d"].map((id) => placed[id].y);
    expect(ys).toEqual([...ys].sort((p, q) => p - q));
    expect(new Set(ys).size).toBe(4);
    expect(new Set(Object.values(placed).map((p) => p.x)).size).toBe(1);
  });

  it("steps a chain from the top left to the bottom right, each card below the one before", () => {
    const boxes = [box("a", 240, 120), box("b", 240, 200), box("c", 240, 90), box("d")];
    const placed = arrange(boxes, wires, "diagonal");
    const across = arrange(boxes, wires, "LR");
    for (const [i, id] of ["b", "c", "d"].entries()) {
      const before = boxes[i];
      expect(placed[id].x).toBeGreaterThan(placed[before.id].x + before.width - 1);
      expect(placed[id].y).toBe(placed[before.id].y + before.height + DIAGONAL_GAP);
      expect(placed[id].x).toBe(across[id].x); // the columns are left to right's
    }
  });

  it("keeps a fork's column stacked as left to right stacks it, the next column below its tallest card", () => {
    const boxes = [box("a"), box("b", 240, 300), box("c"), box("d")];
    const fork = [
      { source: "a", target: "b" },
      { source: "a", target: "c" },
      { source: "b", target: "d" },
      { source: "c", target: "d" },
    ];
    const placed = arrange(boxes, fork, "diagonal");
    const across = arrange(boxes, fork, "LR");
    expect(placed.c.y - placed.b.y).toBe(across.c.y - across.b.y);
    expect(Math.min(placed.b.y, placed.c.y)).toBe(placed.a.y + 120 + DIAGONAL_GAP);
    expect(placed.d.y).toBe(Math.min(placed.b.y, placed.c.y) + 300 + DIAGONAL_GAP);
  });

  for (const direction of ["LR", "diagonal", "TB"] as Direction[])
    it(`leaves no two boxes overlapping and snaps to the grid (${direction})`, () => {
      const boxes = [box("a"), box("b", 260, 300), box("c", 200, 80), box("d"), box("e", 233, 97)];
      const placed = arrange(
        boxes,
        [
          { source: "a", target: "b" },
          { source: "a", target: "c" },
          { source: "b", target: "d" },
          { source: "c", target: "d" },
          { source: "c", target: "e" },
        ],
        direction,
      );
      const rects = boxes.map((b) => ({ ...placed[b.id], w: b.width, h: b.height }));
      for (const [i, r] of rects.entries()) {
        expect(r.x % GRID).toBe(0);
        expect(r.y % GRID).toBe(0);
        for (const s of rects.slice(i + 1))
          expect(r.x + r.w <= s.x || s.x + s.w <= r.x || r.y + r.h <= s.y || s.y + s.h <= r.y).toBe(true);
      }
    });
});

describe("runsDown — whether a level's sockets sit on the cards' top and bottom edges", () => {
  const sized = (ids: string[], heights: Record<string, number> = {}) => ids.map((id) => box(id, 240, heights[id] ?? 120));
  const placedBy = (boxes: ReturnType<typeof sized>, wires: { source: string; target: string }[], direction: Direction) => {
    const placed = arrange(boxes, wires, direction);
    return Object.fromEntries(boxes.map((b) => [b.id, { ...placed[b.id], width: b.width, height: b.height }]));
  };
  const shapes: Record<string, [ReturnType<typeof sized>, { source: string; target: string }[]]> = {
    chain: [sized(["a", "b", "c", "d"], { b: 400 }), [
      { source: "a", target: "b" },
      { source: "b", target: "c" },
      { source: "c", target: "d" },
    ]],
    fork: [sized(["a", "b", "c", "d"], { b: 300 }), [
      { source: "a", target: "b" },
      { source: "a", target: "c" },
      { source: "b", target: "d" },
      { source: "c", target: "d" },
    ]],
    "three-way fan": [sized(["a", "b", "c", "d", "e"]), [
      { source: "a", target: "b" },
      { source: "a", target: "c" },
      { source: "a", target: "d" },
      { source: "d", target: "e" },
    ]],
  };

  for (const [name, [boxes, wires]] of Object.entries(shapes)) {
    it(`is true after Arrange top to bottom, false after left to right and diagonally (${name})`, () => {
      expect(runsDown(placedBy(boxes, wires, "TB"), wires)).toBe(true);
      expect(runsDown(placedBy(boxes, wires, "LR"), wires)).toBe(false);
      expect(runsDown(placedBy(boxes, wires, "diagonal"), wires)).toBe(false);
    });
  }

  it("counts only wires between cards it was given, and calls a level of no wires across", () => {
    const boxes = { a: { x: 0, y: 0, width: 240, height: 120 }, b: { x: 0, y: 200, width: 240, height: 120 } };
    expect(runsDown(boxes, [{ source: "a", target: "b" }, { source: "a", target: "ghost" }, { source: "a", target: "a" }])).toBe(true);
    expect(runsDown(boxes, [])).toBe(false);
  });

  it("calls a tie across", () => {
    const boxes = {
      a: { x: 0, y: 0, width: 240, height: 120 },
      b: { x: 0, y: 200, width: 240, height: 120 },
      c: { x: 400, y: 0, width: 240, height: 120 },
    };
    expect(runsDown(boxes, [{ source: "a", target: "b" }, { source: "a", target: "c" }])).toBe(false);
  });
});

