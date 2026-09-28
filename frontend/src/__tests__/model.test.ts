/**
 * The pure half of the page: the meet rule with the server's table, the handle-id convention and the
 * model → React Flow derivation. The DOM half is measured under playwright in `tests/test_canvas.py`.
 */
import { describe, expect, it } from "vitest";
import { compatible, handleId, modelToFlow, parseHandle, socketType, type Model, type ModelNode } from "../model";

const node = (name: string, extra: Partial<ModelNode> = {}): ModelNode => ({
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
  ...extra,
});

describe("compatible — document.py's _meets with the server's table", () => {
  const types = { Stream: ["Source"], integer: ["number"], Photo: ["Image"], Image: [] };
  it("identical types meet", () => expect(compatible("Image", "Image", types)).toBe(true));
  it("the wildcard meets everything, either side", () => {
    expect(compatible("*", "Label", types)).toBe(true);
    expect(compatible("Label", "*", types)).toBe(true);
  });
  it("a type meets its listed supertypes, not the other way round", () => {
    expect(compatible("Photo", "Image", types)).toBe(true);
    expect(compatible("Image", "Photo", types)).toBe(false);
    expect(compatible("Stream", "Source", types)).toBe(true);
    expect(compatible("Source", "Stream", types)).toBe(false);
  });
  it("two unrelated types do not meet", () => expect(compatible("Image", "Label", types)).toBe(false));
  it("a type the table does not know meets only itself and the wildcard", () => {
    expect(compatible("Spectrogram", "Image", {})).toBe(false);
    expect(compatible("Spectrogram", "Spectrogram", {})).toBe(true);
  });
});

describe("handles", () => {
  it("are `in:<socket>` / `out:<socket>` and parse back", () => {
    expect(handleId("in", "record")).toBe("in:record");
    expect(parseHandle("out:image")).toEqual({ kind: "out", socket: "image" });
    expect(parseHandle("in:ops[1]")).toEqual({ kind: "in", socket: "ops[1]" });
    expect(parseHandle("bogus")).toBeUndefined();
    expect(parseHandle(null)).toBeUndefined();
  });
  it("type an input, an output, or — for `in:` — a parameter (a bind: target)", () => {
    const n = node("gain", {
      inputs: [{ name: "image", type: "Image", required: true, doc: "", entry: "image" }],
      outputs: [{ name: "record", type: "Record", required: false, doc: "", entry: "" }],
      params: { gain: { name: "gain", type: "number", value: 2, default: 1, doc: "", schema: {}, spelled: true, required: false } },
    });
    expect(socketType(n, "in:image")).toBe("Image");
    expect(socketType(n, "out:record")).toBe("Record");
    expect(socketType(n, "in:gain")).toBe("number");
    expect(socketType(n, "out:gain")).toBeUndefined();
    expect(socketType(n, "in:nosuch")).toBeUndefined();
  });
});

describe("modelToFlow", () => {
  const model: Model = {
    nodes: [node("a"), node("stream.ops[1]")],
    wires: [{ source: "a", source_socket: "record", target: "stream.ops[1]", target_socket: "record", kind: "record", type: "Record" }],
    problems: [],
    outputs: [],
  };
  it("names the React Flow node by the document's node name and places it at the sidecar's position", () => {
    const { nodes } = modelToFlow(model, { a: [40, 40] });
    expect(nodes.map((n) => n.id)).toEqual(["a", "stream.ops[1]"]);
    expect(nodes[0].position).toEqual({ x: 40, y: 40 });
    expect(nodes[1].position).toEqual({ x: 0, y: 0 }); // no sidecar entry: the origin, never a crash
    expect(nodes[0].data.node).toBe(model.nodes[0]);
  });
  it("turns a wire into an edge between the two handles, classed by its kind", () => {
    const { edges } = modelToFlow(model, {});
    expect(edges).toHaveLength(1);
    expect(edges[0]).toMatchObject({
      source: "a",
      sourceHandle: "out:record",
      target: "stream.ops[1]",
      targetHandle: "in:record",
      className: "wire wire-record",
      data: { kind: "record", type: "Record" },
    });
  });
});
