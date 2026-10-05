/**
 * Set source… (`slots.ts`): which slots it fills, which classes it offers, the form it shows before anything is set,
 * and the values it sends — the PRO and the CON case of each.
 */
import { describe, expect, it } from "vitest";
import type { NodeSpecWithSettings } from "../api";
import type { Model, ModelNode, Supertypes } from "../model";
import {
  draftParams,
  draftValues,
  isSourceSlot,
  missingRequired,
  schemaWord,
  slotCandidates,
  slotEffect,
  slotParams,
  slotTitle,
  sourceSlots,
} from "../slots";

// with its settings — what a picked class answers (`GET api/palette/<class>`); a list entry is the same less those
const spec = (
  name: string,
  kind: string,
  outputs: { name: string; type: string }[],
  params: NodeSpecWithSettings["params_schema"] = {},
): NodeSpecWithSettings => ({
  class_path: `pkg.${name}`,
  name,
  category: kind,
  group: "",
  kind,
  params_schema: params,
  params_defs: {},
  inputs: [],
  outputs: outputs.map((socket) => ({ ...socket, required: false, doc: "" })),
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

const TYPES: Supertypes = { Stream: ["Source"], integer: ["number"] };

const STREAM: ModelNode = {
  name: "stream",
  class_path: "recordstream.core.stream.Stream",
  kind: "engine",
  params: {},
  inputs: [
    { name: "source", type: "Source", required: true, doc: "", entry: "" },
    { name: "ops[0]", type: "Op", required: false, doc: "", entry: "" },
  ],
  outputs: [{ name: "stream", type: "Stream", required: false, doc: "", entry: "" }],
  loc: "",
  source_file: "",
  note: "",
  alias_of: "",
};

const FILES: ModelNode = { ...STREAM, name: "stream.source", class_path: "recordstream.sources.files.FilesSource", kind: "source", inputs: [] };

const EMPTY: Model = { nodes: [STREAM], wires: [], problems: [], outputs: [] };
const FILLED: Model = {
  nodes: [FILES, STREAM],
  wires: [{ source: "stream.source", source_socket: "stream", target: "stream", target_socket: "source", kind: "ref", type: "Source" }],
  problems: [],
  outputs: [],
};

describe("sourceSlots — the slots one source fills, and what fills them now", () => {
  it("a Stream's source slot: empty, or filled by the marker drawn as stream.source", () => {
    expect(sourceSlots(EMPTY, STREAM)).toEqual([{ node: "stream", slot: "source", type: "Source", filled: undefined }]);
    expect(sourceSlots(FILLED, STREAM)).toEqual([{ node: "stream", slot: "source", type: "Source", filled: "stream.source" }]);
  });

  it("CON: an op list item and a list of sources are not slots one marker fills", () => {
    expect(isSourceSlot({ name: "ops[0]", type: "Op" })).toBe(false);
    expect(isSourceSlot({ name: "sources[0]", type: "Source" })).toBe(false);
    expect(isSourceSlot({ name: "source", type: "Source" })).toBe(true);
    expect(sourceSlots(FILLED, FILES)).toEqual([]);
  });
});

describe("slotCandidates — the classes a Source slot takes", () => {
  const SPECS = [
    spec("Stream", "engine", [{ name: "stream", type: "Stream" }]),
    spec("HuggingFaceSource", "source", [
      { name: "stream", type: "Source" },
      { name: "class_names", type: "List[str]" },
    ]),
    spec("FilesSource", "source", [{ name: "stream", type: "Source" }]),
    spec("Threshold", "op", [
      { name: "record", type: "Record" },
      { name: "op", type: "Op" },
    ]),
    { ...spec("Subgraph", "op", [{ name: "op", type: "Op" }]), class_path: "recordstream.flow.subgraph.Subgraph" },
  ];

  it("sources first, then engines (a Stream IS a source), by name; an op never", () => {
    expect(slotCandidates(SPECS, "Source", TYPES).map((candidate) => candidate.name)).toEqual(["FilesSource", "HuggingFaceSource", "Stream"]);
  });

  it("the search narrows them as the palette's does", () => {
    expect(slotCandidates(SPECS, "Source", TYPES, "hugging").map((candidate) => candidate.name)).toEqual(["HuggingFaceSource"]);
    expect(slotCandidates(SPECS, "Source", TYPES, "nothing like it")).toEqual([]);
  });
});

describe("schemaWord — the model's one word for a parameter (document._param_type)", () => {
  it("the first accepted type, null skipped", () => {
    expect(schemaWord({ type: "string" })).toBe("string");
    expect(schemaWord({ anyOf: [{ type: "integer" }, { type: "null" }] })).toBe("integer");
    expect(schemaWord({ anyOf: [{ type: "array", items: { type: "string" } }, { type: "string" }, { type: "null" }] })).toBe("List[str]");
    expect(schemaWord({ type: "array", prefixItems: [{ type: "number" }, { type: "number" }] })).toBe("object");
    expect(schemaWord({ $ref: "#/$defs/X" })).toBe("object");
    expect(schemaWord({})).toBe("*");
    expect(schemaWord({ type: "null" })).toBe("*");
  });
});

describe("the form before anything is set, and what Set sends", () => {
  const HF = spec("HuggingFaceSource", "source", [{ name: "stream", type: "Source" }], {
    path: { type: "string", default: "", description: "HF dataset identifier" },
    split: { type: "string", default: "train" },
    count: { anyOf: [{ type: "integer" }, { type: "null" }], default: null },
    root: { type: "string" },
  });
  const params = draftParams(HF);

  it("every parameter at its default, typed as the model would; one without a default is required", () => {
    expect(params.map((param) => [param.name, param.type, param.value, param.required])).toEqual([
      ["path", "string", "", false],
      ["split", "string", "train", false],
      ["count", "integer", null, false],
      ["root", "string", null, true],
    ]);
    expect(params[0].doc).toBe("HF dataset identifier");
    expect(params.every((param) => !param.spelled)).toBe(true);
  });

  it("the values shown follow what was typed", () => {
    expect(draftValues(params, { path: "ylecun/mnist" })).toEqual({ path: "ylecun/mnist", split: "train", count: null, root: null });
  });

  it("Set sends only what differs from the defaults — a value typed back to its default is not sent", () => {
    expect(slotParams(params, { path: "ylecun/mnist", split: "train", count: 10 })).toEqual({ path: "ylecun/mnist", count: 10 });
    expect(slotParams(params, {})).toEqual({});
  });

  it("Set waits for every required parameter", () => {
    expect(missingRequired(params, {})).toEqual(["root"]);
    expect(missingRequired(params, { root: "" })).toEqual(["root"]);
    expect(missingRequired(params, { root: "/data" })).toEqual([]);
  });
});

describe("the dialog's words", () => {
  it("the title names the slot and the node; the head says whether it fills or replaces", () => {
    expect(slotTitle({ node: "stream", slot: "source" })).toBe("Set the source of stream");
    expect(slotEffect({ node: "stream", slot: "source", filled: undefined }, EMPTY, "HuggingFaceSource")).toBe(
      "HuggingFaceSource goes into the empty source slot.",
    );
    expect(slotEffect({ node: "stream", slot: "source", filled: "stream.source" }, FILLED, "HuggingFaceSource")).toBe(
      "HuggingFaceSource replaces stream.source (FilesSource).",
    );
  });
  it("a slot holding a REFERENCE to a node of its own: that node stays, unwired (measured before: 'replaces source')", () => {
    const named: ModelNode = { ...FILES, name: "source" };
    const referenced: Model = {
      nodes: [named, STREAM],
      wires: [{ source: "source", source_socket: "stream", target: "stream", target_socket: "source", kind: "ref", type: "Source" }],
      problems: [],
      outputs: [],
    };
    expect(slotEffect({ node: "stream", slot: "source", filled: "source" }, referenced, "FilesSource")).toBe(
      "FilesSource goes into stream's source slot in place of its reference to source; source (FilesSource) stays in the document, unwired.",
    );
    expect(slotEffect({ node: "stream", slot: "source", filled: "shared" }, FILLED, "HuggingFaceSource")).toBe(
      "HuggingFaceSource goes into stream's source slot in place of its reference to shared; shared stays in the document, unwired.",
    );
  });
});
