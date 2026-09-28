/**
 * The client-side arrange: dagre over the MEASURED node boxes, left to right along the wires, snapped to the
 * canvas's 10-px grid. The button and the saved sidecar are measured under playwright in `tests/test_canvas.py`.
 */
import { describe, expect, it } from "vitest";
import { GRID, arrange } from "../layout";

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
});
