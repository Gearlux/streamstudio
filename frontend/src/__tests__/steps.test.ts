/**
 * The palette's pure half: which classes a dropped wire narrows the list to, what the new step is named, and
 * which node it is inserted after. The DOM half is measured under playwright in `tests/test_canvas.py`.
 */
import { describe, expect, it } from "vitest";
import type { NodeSpec } from "../api";
import type { Graph, ModelNode } from "../model";
import { PALETTE_ROWS, paletteMatches, recordPredecessor, stepAnchor, stepName } from "../steps";

const spec = (classPath: string, inputs: [string, string][] = [], outputs: [string, string][] = []): NodeSpec => ({
  class_path: classPath,
  name: classPath.split(".").pop() ?? classPath,
  category: "op",
  group: "",
  kind: "op",
  params_schema: {},
  params_defs: {},
  inputs: inputs.map(([name, type]) => ({ name, type, required: false, doc: "" })),
  outputs: outputs.map(([name, type]) => ({ name, type, required: false, doc: "" })),
  consumes: {},
  produces: {},
  consumes_types: [],
  produces_types: [],
  flags: [],
  reports: [],
  doc: "",
  tuple_form: false,
  problem: "",
});

const node = (name: string, kind: ModelNode["kind"] = "op"): ModelNode => ({
  name,
  class_path: kind === "host" ? "" : `mod.${name}`,
  kind,
  params: {},
  inputs: [],
  outputs: [],
  loc: "",
  source_file: "",
  note: "",
  alias_of: "",
});

const graph = (nodes: ModelNode[], outputs: string[] = [], wires: Graph["model"]["wires"] = []): Graph => ({
  name: "g",
  path: "/tmp/g.yaml",
  kind: "flow",
  text: "",
  model: { nodes, wires, problems: [], outputs },
  positions: {},
  groups: [],
  history_depth: 50,
});

describe("paletteMatches", () => {
  const types = { Photo: ["Image"] };
  const gain = spec("mod.Gain", [["record", "Record"], ["image", "Image"]], [["record", "Record"], ["image", "Image"]]);
  const threshold = spec("mod.Threshold", [["record", "Record"]], [["record", "Record"], ["op", "Op"]]);
  it("lists everything when nothing was dropped", () => {
    expect(paletteMatches(threshold, undefined, types)).toBe(true);
  });
  it("from an OUTPUT of type T, keeps the classes with an input T can be wired into", () => {
    expect(paletteMatches(gain, { type: "Image", side: "inputs" }, types)).toBe(true);
    expect(paletteMatches(threshold, { type: "Image", side: "inputs" }, types)).toBe(false);
    expect(paletteMatches(gain, { type: "Photo", side: "inputs" }, types)).toBe(true); // a Photo IS an Image
    expect(paletteMatches(threshold, { type: "Record", side: "inputs" }, types)).toBe(true);
  });
  it("from an INPUT of type T, keeps the classes with an output that meets T", () => {
    expect(paletteMatches(gain, { type: "Image", side: "outputs" }, types)).toBe(true);
    expect(paletteMatches(threshold, { type: "Image", side: "outputs" }, types)).toBe(false);
    expect(paletteMatches(threshold, { type: "Op", side: "outputs" }, types)).toBe(true);
  });
  it("caps the rendered rows at 200", () => expect(PALETTE_ROWS).toBe(200));
});

describe("stepName", () => {
  it("is the class short name in lower case, numbered when taken", () => {
    expect(stepName("recordstream.ops.numpy.Threshold", [])).toBe("threshold");
    expect(stepName("recordstream.ops.numpy.Threshold", ["threshold"])).toBe("threshold2");
    expect(stepName("recordstream.ops.numpy.Threshold", ["threshold", "threshold2"])).toBe("threshold3");
    expect(stepName("mod.TinyGain", ["gain"])).toBe("tinygain");
  });
});

describe("stepAnchor — the node a picked class is inserted after", () => {
  const flow = graph([node("decode"), node("gain"), node("count"), node("<host>", "host")], ["count"]);
  it("is the node last selected", () => expect(stepAnchor(flow, "gain")).toBe("gain"));
  it("is the flow's output when nothing was selected", () => expect(stepAnchor(flow, undefined)).toBe("count"));
  it("is the last real node of a document without an output — a Stream's last op", () => {
    const stream = graph([node("stream", "engine"), node("stream.source", "source"), node("stream.ops[0]"), node("stream.ops[1]")]);
    expect(stepAnchor(stream, undefined)).toBe("stream.ops[1]");
  });
  it("ignores a selection the model no longer has, and is undefined on an empty document", () => {
    expect(stepAnchor(flow, "gone")).toBe("count");
    expect(stepAnchor(graph([node("<host>", "host")]), undefined)).toBeUndefined();
  });
});

describe("recordPredecessor — where a step dropped from an INPUT goes", () => {
  const wires: Graph["model"]["wires"] = [
    { source: "decode", source_socket: "record", target: "gain", target_socket: "record", kind: "record", type: "Record" },
    { source: "decode", source_socket: "image", target: "gain", target_socket: "image", kind: "entry", type: "Image" },
  ];
  const flow = graph([node("decode"), node("gain")], ["gain"], wires);
  it("is the node whose record the target reads", () => expect(recordPredecessor(flow, "gain")).toBe("decode"));
  it("is undefined for the first step (nothing feeds it)", () => expect(recordPredecessor(flow, "decode")).toBeUndefined());
});
