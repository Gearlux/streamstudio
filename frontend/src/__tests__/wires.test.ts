/**
 * A wire drawn on the canvas is SAVED (`wires.ts`): the page sends the two ends to the server (`POST …/wires`) and
 * draws the graph the server answers — or, on a refusal, the server's one plain line as a warning, and NOTHING is
 * drawn: the page never draws an edge of its own (there is no dashed "proposal" any more). Delete on selected wires
 * sends them all in ONE request (`POST …/wires/remove`), so one undo brings them all back. The DOM half — a real drag,
 * a click on a wire, the Delete key — is measured under playwright in `tests/test_canvas.py`.
 */
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api";
import { WIRE_KINDS, modelToFlow, type Graph, type Model, type ModelNode } from "../model";
import { connectionEnds, deleteWires, drawWire, selectedWires, type WireEnds } from "../wires";

const CLASSES: WireEnds = { source: "stream.source", source_socket: "class_names", target: "contract", target_socket: "classes" };

const graph = (name = "source"): Graph => ({
  name,
  path: `/g/${name}.yaml`,
  kind: "contract",
  dirty: true,
  text: "",
  model: { nodes: [], wires: [], problems: [], outputs: [] },
  positions: {},
  groups: [],
  history_depth: 50,
});

describe("connectionEnds — the two ends of a drawn connection, in the routes' words", () => {
  it("reads the output and the input socket from the handles", () => {
    expect(
      connectionEnds({ source: "stream.source", sourceHandle: "out:class_names", target: "contract", targetHandle: "in:classes" }),
    ).toEqual(CLASSES);
  });

  it("a handle on the wrong side, or none, is no wire", () => {
    expect(connectionEnds({ source: "a", sourceHandle: "in:x", target: "b", targetHandle: "in:y" })).toBeUndefined();
    expect(connectionEnds({ source: "a", sourceHandle: "out:x", target: "b", targetHandle: null })).toBeUndefined();
  });
});

describe("drawWire — a connection becomes ONE edit, or one plain warning", () => {
  it("sends the ends once and draws the graph the server answered", async () => {
    const answer = graph();
    const add = vi.fn(async () => answer);
    const accept = vi.fn();
    const refuse = vi.fn();
    expect(await drawWire(CLASSES, { add, accept, refuse })).toBe(true);
    expect(add).toHaveBeenCalledTimes(1);
    expect(add).toHaveBeenCalledWith(CLASSES);
    expect(accept).toHaveBeenCalledWith(answer);
    expect(refuse).not.toHaveBeenCalled();
  });

  it("a refusal is the server's line, verbatim, and nothing is drawn", async () => {
    const line = "This wire would change the order of the steps — the editor can't save that yet.";
    const add = vi.fn(async (): Promise<Graph> => {
      throw new ApiError(400, line);
    });
    const accept = vi.fn();
    const refuse = vi.fn();
    expect(await drawWire(CLASSES, { add, accept, refuse })).toBe(false);
    expect(refuse).toHaveBeenCalledWith(line);
    expect(accept).not.toHaveBeenCalled();
  });

  it("a connection that is no wire asks nothing", async () => {
    const add = vi.fn(async () => graph());
    expect(await drawWire(undefined, { add, accept: vi.fn(), refuse: vi.fn() })).toBe(false);
    expect(add).not.toHaveBeenCalled();
  });
});

describe("selectedWires and deleteWires — Delete on selected wires is ONE request", () => {
  const edges = [
    { selected: true, source: "stream", sourceHandle: "out:stream", target: "contract", targetHandle: "in:stream" },
    { selected: false, source: "source", sourceHandle: "out:stream", target: "stream", targetHandle: "in:source" },
    { selected: true, source: "source", sourceHandle: "out:image", target: "contract", targetHandle: "in:input" },
  ];

  it("lists the selected edges' ends, in the canvas's order", () => {
    expect(selectedWires(edges)).toEqual([
      { source: "stream", source_socket: "stream", target: "contract", target_socket: "stream" },
      { source: "source", source_socket: "image", target: "contract", target_socket: "input" },
    ]);
  });

  it("sends every selected wire at once and draws the answer", async () => {
    const answer = graph();
    const remove = vi.fn(async () => answer);
    const accept = vi.fn();
    expect(await deleteWires(selectedWires(edges), { remove, accept, refuse: vi.fn() })).toBe(true);
    expect(remove).toHaveBeenCalledTimes(1);
    expect(remove).toHaveBeenCalledWith(selectedWires(edges));
    expect(accept).toHaveBeenCalledWith(answer);
  });

  it("nothing selected sends nothing; a refusal is the server's line", async () => {
    const remove = vi.fn(async (): Promise<Graph> => {
      throw new ApiError(400, "The editor can't save wires in this kind of graph yet.");
    });
    const refuse = vi.fn();
    expect(await deleteWires([], { remove, accept: vi.fn(), refuse })).toBe(false);
    expect(remove).not.toHaveBeenCalled();
    expect(await deleteWires(selectedWires(edges), { remove, accept: vi.fn(), refuse })).toBe(false);
    expect(refuse).toHaveBeenCalledWith("The editor can't save wires in this kind of graph yet.");
  });
});

describe("a host wire — where the host puts what it hands in; drawn, never removed", () => {
  const line = "The host fills this when it runs the graph — it isn't a wire you can remove.";
  const edges = [{ selected: true, source: "<host>", sourceHandle: "out:files", target: "file_source", targetHandle: "in:files" }];

  it("Delete on it asks the SERVER, whose one line is shown as it is — the page decides nothing, redraws nothing", async () => {
    const remove = vi.fn(async (): Promise<Graph> => {
      throw new ApiError(400, line);
    });
    const accept = vi.fn();
    const refuse = vi.fn();
    expect(await deleteWires(selectedWires(edges), { remove, accept, refuse })).toBe(false);
    expect(remove).toHaveBeenCalledWith([{ source: "<host>", source_socket: "files", target: "file_source", target_socket: "files" }]);
    expect(refuse).toHaveBeenCalledWith(line);
    expect(accept).not.toHaveBeenCalled();
  });
});

describe("the canvas draws the document's wires only", () => {
  const node = (name: string): ModelNode => ({
    name,
    class_path: `mod.${name}`,
    kind: "value",
    params: {},
    inputs: [],
    outputs: [],
    loc: "",
    source_file: "",
    note: "",
    alias_of: "",
  });
  const model: Model = {
    nodes: [node("source"), node("contract")],
    wires: [
      { source: "source", source_socket: "class_names", target: "contract", target_socket: "classes", kind: "delivered", type: "List[str]" },
      { source: "source", source_socket: "image", target: "contract", target_socket: "input", kind: "delivered", type: "Image" },
    ],
    problems: [],
    outputs: [],
  };

  it("every edge is a document wire, named by its two ends", () => {
    const { edges } = modelToFlow(model, {});
    expect(edges.map((edge) => edge.id)).toEqual(["wire:source.class_names->contract.classes", "wire:source.image->contract.input"]);
    expect(edges.every((edge) => edge.data !== undefined && (WIRE_KINDS as readonly string[]).includes(edge.data.kind))).toBe(true);
  });

  it("a wire the model lists twice still gets an edge id of its own", () => {
    const { edges } = modelToFlow({ ...model, wires: [model.wires[0], model.wires[0]] }, {});
    expect(edges.map((edge) => edge.id)).toEqual([
      "wire:source.class_names->contract.classes",
      "wire:source.class_names->contract.classes#2",
    ]);
  });
});
