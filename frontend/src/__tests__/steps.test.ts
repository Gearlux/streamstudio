/**
 * The palette's pure half: which classes a dropped wire narrows the list to, what the new step is named, and
 * which node it is inserted after. The DOM half is measured under playwright in `tests/test_canvas.py`.
 */
import { describe, expect, it } from "vitest";
import type { NodeSpec } from "../api";
import type { Graph, ModelNode } from "../model";
import {
  PALETTE_ROWS,
  freeSpot,
  isAnchor,
  paletteMatches,
  recordPredecessor,
  selectedAnchor,
  spotBeside,
  stepAnchor,
  stepName,
  uniqueName,
} from "../steps";

const spec = (classPath: string, inputs: [string, string][] = [], outputs: [string, string][] = []): NodeSpec => ({
  class_path: classPath,
  name: classPath.split(".").pop() ?? classPath,
  category: "op",
  group: "",
  kind: "op",
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
  dirty: false,
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

describe("uniqueName — a subgraph's or a template's suggested name", () => {
  it("is the base when free, numbered from 2 when taken", () => {
    expect(uniqueName("subgraph", [])).toBe("subgraph");
    expect(uniqueName("subgraph", ["subgraph"])).toBe("subgraph2");
    expect(uniqueName("subgraph", ["subgraph", "subgraph2"])).toBe("subgraph3");
  });
});

describe("freeSpot — where a node added by a click lands", () => {
  const size = { width: 240, height: 120 };
  it("is the start when nothing is there", () => {
    expect(freeSpot({ x: 100, y: 100 }, size, [])).toEqual({ x: 100, y: 100 });
  });
  it("steps down past every card it would cover, staying on the 10-px grid", () => {
    const cards = [
      { x: 90, y: 90, width: 240, height: 120 },
      { x: 90, y: 230, width: 240, height: 120 },
    ];
    const spot = freeSpot({ x: 100, y: 100 }, size, cards);
    expect(spot.x).toBe(100);
    expect(spot.y).toBeGreaterThanOrEqual(350);
    expect(spot.y % 10).toBe(0);
  });
  it("snaps the start to the grid", () => {
    expect(freeSpot({ x: 103, y: 97 }, size, [])).toEqual({ x: 100, y: 100 });
  });
});

describe("spotBeside — a node added by a click lands below the one it follows, clear of every card (P8)", () => {
  const size = { width: 240, height: 200 };

  it("measured against the cards where they are AFTER the add — the server moved the later member down", () => {
    // piped.yaml fresh from disk: transforms[0] at (40, 440); the member added after it pushed the old transforms[1]
    // (now transforms[2]) from (40, 640) down to (40, 840) — measured before, the new card was placed against the OLD
    // place and landed on top of it
    const anchor = { x: 40, y: 440, ...size };
    const after = [anchor, { x: 40, y: 840, ...size }];
    const spot = spotBeside(anchor, after, size);
    const overlaps = after.some(
      (card) => spot.x < card.x + card.width && card.x < spot.x + size.width && spot.y < card.y + card.height && card.y < spot.y + size.height,
    );
    expect(overlaps).toBe(false);
    expect(spot.x).toBe(100);
  });

  it("with room below the anchor, right there", () => {
    const anchor = { x: 40, y: 40, ...size };
    expect(spotBeside(anchor, [anchor], size)).toEqual({ x: 100, y: 280 });
  });
});

describe("the host card and the root are never an anchor (the user's rule, 2026-10-05)", () => {
  // the files graph a host workspace seeds: a source, a Stream reading each file, the root, the host card
  const files = graph([
    node("file_source", "source"),
    node("stream", "engine"),
    node("stream.ops[0]"),
    node("contract", "contract"),
    node("<host>", "host"),
  ]);

  it("isAnchor: neither the host card nor the root; any other card", () => {
    expect(isAnchor(node("<host>", "host"))).toBe(false);
    expect(isAnchor(node("contract", "contract"))).toBe(false);
    expect(isAnchor(node("stream.ops[0]"))).toBe(true);
    expect(isAnchor(node("file_source", "source"))).toBe(true);
  });

  it("stepAnchor: the host card or the root selected counts as nothing selected", () => {
    // measured before: Add Node with the host card selected sent {after: "<host>"} — `a new flow step needs a name`
    expect(stepAnchor(files, "<host>")).toBe("stream.ops[0]");
    expect(stepAnchor(files, "contract")).toBe("stream.ops[0]");
    expect(stepAnchor(files, undefined)).toBe("stream.ops[0]");
  });

  it("selectedAnchor: the LAST selected node that can be one — a mixed selection acts on the document card", () => {
    expect(selectedAnchor(files.model, ["stream.ops[0]", "<host>"])).toBe("stream.ops[0]");
    expect(selectedAnchor(files.model, ["<host>", "file_source"])).toBe("file_source");
  });

  it("selectedAnchor: a selection holding only them, or nothing the level draws, is none", () => {
    expect(selectedAnchor(files.model, ["<host>"])).toBeUndefined();
    expect(selectedAnchor(files.model, ["contract", "<host>"])).toBeUndefined();
    expect(selectedAnchor(files.model, ["gone"])).toBeUndefined();
    expect(selectedAnchor(files.model, [])).toBeUndefined();
  });
});
