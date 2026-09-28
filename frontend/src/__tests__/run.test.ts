/**
 * The run panel's pure half: which node "step" pauses before next, which parameter values a rerun sends (the
 * change over what the run's node CURRENTLY holds — the tracer rebuilds from its current values plus the
 * change), what a copy/paste and a rerun form start from, and how a traced value is summarised in one row.
 * The DOM half is measured under playwright in `tests/test_canvas.py`.
 */
import { describe, expect, it } from "vitest";
import type { Run } from "../api";
import type { ModelNode, Param } from "../model";
import { changedParams, nextNode, plainParams, summaryCells } from "../run";
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
