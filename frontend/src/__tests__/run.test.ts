/**
 * The run panel's pure half: which node "step" pauses before next, which parameter values a rerun sends (the
 * change over what the run's node CURRENTLY holds — the tracer rebuilds from its current values plus the
 * change), what a copy/paste and a rerun form start from, and how a traced value is summarised in one row.
 * The DOM half is measured under playwright in `tests/test_canvas.py`.
 */
import { describe, expect, it } from "vitest";
import type { Run, TraceNode } from "../api";
import type { ModelNode, Param } from "../model";
import { changedParams, nextNode, plainParams, runRows, summaryCells } from "../run";
import { searchMatches } from "../steps";

const run = (paused: string | null, names = ["s.ops[0]", "s.ops[1]", "s.ops[2]"]): Run => ({
  run: "run_1",
  graph: "g",
  record: 0,
  trace: { nodes: names.map((node) => ({ node, op: "Op", status: "ok", ms: 0.1, params: {} })), check: [], paused_at: paused },
  statuses: {},
  generations: {},
  paused_at: paused,
});

const param = (name: string, value: Param["value"], extra: Partial<Param> = {}): Param => ({
  name,
  type: typeof value === "number" ? "number" : typeof value === "boolean" ? "boolean" : "string",
  value,
  default: value,
  doc: "",
  schema: {},
  spelled: true,
  required: false,
  ...extra,
});

const node = (params: Param[]): ModelNode => ({
  name: "n",
  class_path: "mod.N",
  kind: "op",
  params: Object.fromEntries(params.map((p) => [p.name, p])),
  inputs: [],
  outputs: [],
  loc: "",
  source_file: "",
  note: "",
  alias_of: "",
});

describe("nextNode — where 'step' pauses next", () => {
  it("is the node after the one the run paused before", () => expect(nextNode(run("s.ops[1]"))).toBe("s.ops[2]"));
  it("is null when the paused node is the last (step runs to the end)", () => expect(nextNode(run("s.ops[2]"))).toBeNull());
  it("is undefined when the run is not paused (nothing to step)", () => expect(nextNode(run(null))).toBeUndefined());
});

/** A traced run of the §0.2 document: `prep` (a subgraph holding grey, inner/x, scaled), then `mask`. */
const nested = (paused: string | null, order?: string[]): Run => {
  const rows: Record<string, TraceNode> = {
    prep: { node: "prep", op: "Subgraph", status: "ok", ms: 0.3, params: {}, inner: ["prep/grey", "prep/inner", "prep/scaled"] },
    "prep/grey": { node: "prep/grey", op: "ConvertMode", status: "ok", ms: 0.1, params: {}, parent: "prep" },
    "prep/inner": { node: "prep/inner", op: "Subgraph", status: "ok", ms: 0.1, params: {}, parent: "prep", inner: ["prep/inner/x"] },
    "prep/inner/x": { node: "prep/inner/x", op: "X", status: "ok", ms: 0.1, params: {}, parent: "prep/inner" },
    "prep/scaled": { node: "prep/scaled", op: "Scale", status: "ok", ms: 0.1, params: {}, parent: "prep" },
    mask: { node: "mask", op: "Threshold", status: "ok", ms: 0.1, params: {} },
  };
  const names = order ?? ["prep", "prep/grey", "prep/inner", "prep/inner/x", "prep/scaled", "mask"];
  return { ...run(paused, []), trace: { nodes: names.map((name) => rows[name]), check: [], paused_at: paused } };
};

describe("runRows — the run panel lists inner rows indented under their subgraph", () => {
  it("gives every row its depth and the subgraph it sits in (`parent`)", () => {
    const rows = runRows(nested(null).trace.nodes);
    expect(rows.map((row) => [row.node.node, row.depth, row.parent])).toEqual([
      ["prep", 0, undefined],
      ["prep/grey", 1, "prep"],
      ["prep/inner", 1, "prep"],
      ["prep/inner/x", 2, "prep/inner"],
      ["prep/scaled", 1, "prep"],
      ["mask", 0, undefined],
    ]);
  });

  it("puts an inner row under its subgraph even when the answer lists it later", () => {
    const rows = runRows(nested(null, ["prep", "mask", "prep/grey", "prep/inner", "prep/scaled", "prep/inner/x"]).trace.nodes);
    expect(rows.map((row) => row.node.node)).toEqual(["prep", "prep/grey", "prep/inner", "prep/inner/x", "prep/scaled", "mask"]);
  });

  it("keeps a row whose subgraph the answer does not list, at the top", () => {
    const rows = runRows([{ node: "prep/grey", op: "X", status: "ok", ms: 0, params: {}, parent: "prep" }]);
    expect(rows.map((row) => [row.node.node, row.depth])).toEqual([["prep/grey", 0]]);
  });

  it("a run with no subgraph is its rows as listed, all at depth 0", () => {
    expect(runRows(run(null).trace.nodes).map((row) => row.depth)).toEqual([0, 0, 0]);
  });
});

describe("nextNode — stepping through a subgraph", () => {
  it("steps INTO a subgraph: paused before prep, the next pause is before its first inner node", () => {
    expect(nextNode(nested("prep"))).toBe("prep/grey");
  });
  it("carries on through the inside, then the outer steps", () => {
    expect(nextNode(nested("prep/inner/x"))).toBe("prep/scaled");
    expect(nextNode(nested("prep/scaled"))).toBe("mask");
  });
  it("follows the panel's order when the answer lists the inside later", () => {
    expect(nextNode(nested("prep/scaled", ["prep", "mask", "prep/grey", "prep/inner", "prep/scaled", "prep/inner/x"]))).toBe("mask");
  });
});

describe("plainParams — what a paste copies and a rerun form starts from", () => {
  it("keeps the spelled values JSON can carry and leaves the rest to the constructor", () => {
    const n = node([
      param("gain", 2.0),
      param("mode", "fast"),
      param("on", true),
      param("names", ["a", "b"], { type: "List[str]" }),
      param("unset", 1.0, { spelled: false }),
      param("nested", { a: 1 }, { type: "object" }),
    ]);
    expect(plainParams(n)).toEqual({ gain: 2.0, mode: "fast", on: true, names: ["a", "b"] });
  });
});

describe("changedParams — what a rerun sends", () => {
  it("is every value that differs from what the run's node holds now", () => {
    expect(changedParams({ low_level: 0.9, low_op: ">" }, { low_level: 0.5, low_op: ">" })).toEqual({ low_level: 0.9 });
  });
  it("is empty when nothing changed (a plain rerun of the node)", () => {
    expect(changedParams({ a: 1 }, { a: 1 })).toEqual({});
  });
  it("compares lists by value and sends a key the run never recorded", () => {
    expect(changedParams({ names: ["a"], extra: 3 }, { names: ["a"] })).toEqual({ extra: 3 });
  });
});

describe("summaryCells — one traced value in a table row", () => {
  it("an array: type, shape, dtype and range", () => {
    expect(summaryCells({ type: "Image", shape: [4, 4], dtype: "float32", min: 0.25, max: 0.5 })).toEqual({
      type: "Image",
      shape: "4×4",
      dtype: "float32",
      range: "0.25 … 0.5",
      value: "",
    });
  });
  it("rounds a range to 4 significant digits, and names a non-finite bound as the tracer does", () => {
    expect(summaryCells({ type: "Image", min: 0, max: 0.9882352948188782 }).range).toBe("0 … 0.9882");
    expect(summaryCells({ type: "Image", min: "-inf", max: 12345.678 }).range).toBe("-inf … 12350");
  });
  it("a scalar or a label: its value; a mask: its true fraction", () => {
    expect(summaryCells({ type: "Label", value: "dog" }).value).toBe("dog");
    expect(summaryCells({ type: "Mask", shape: [2], dtype: "bool", true_fraction: 0.5 }).range).toBe("50% true");
    expect(summaryCells({ type: "Thing", repr: "Thing()" }).value).toBe("Thing()");
  });
});

describe("searchMatches — the palette's text filter", () => {
  const spec = { class_path: "recordstream.ops.numpy.Threshold", name: "Threshold" };
  it("is a case-insensitive substring of the class path or the name", () => {
    expect(searchMatches(spec, "")).toBe(true);
    expect(searchMatches(spec, "numpy.thresh")).toBe(true);
    expect(searchMatches(spec, "THRESHOLD")).toBe(true);
    expect(searchMatches(spec, "gain")).toBe(false);
  });
});
