/**
 * Op lists, the pure half (DESIGN2 §3, the user's "yes" to "adding and removing work on `ops` list members"): which
 * op lists a level holds, where a node added from the Nodes sidebar, the palette or Add Node ▸ goes, what the new
 * member will be called, and what `POST …/steps` is sent. The DOM half (a click in the sidebar, a pick in the
 * palette) is measured under playwright in `tests/test_canvas.py`.
 *
 * The models are the ones `streamstudio/document.py` derives (probed 2026-09-29 with `Document.open(...).model`):
 *
 *     stream: !class:recordstream.core.stream.Stream          # demo.yaml
 *       source: !class:recordstream.sources.files.FilesSource {files: [demo.png]}
 *       ops:
 *         - !class:recordstream.ops.image.ReadImage {}          # stream.ops[0]
 *         - !class:recordstream.ops.image.ConvertMode {mode: L} # stream.ops[1]
 *         - !class:recordstream.ops.numpy.Threshold {…}         # stream.ops[2]
 *
 * — the holder `stream` carries one `Op` input per member (`ops[0]`…), or ONE free `ops[0]` when the list is empty;
 * every member is wired into it by a `list` wire.
 */
import { describe, expect, it } from "vitest";
import {
  OP_TYPE,
  addedMember,
  beside,
  inputTarget,
  insertLabel,
  insertTarget,
  levelHasSteps,
  memberList,
  opLists,
  shiftsMembers,
  stepFields,
  wireTarget,
} from "../oplists";
import type { Model, ModelNode, Socket, Wire } from "../model";

const socket = (name: string, type: string): Socket => ({ name, type, required: false, doc: "", entry: "" });

const node = (name: string, kind: ModelNode["kind"], inputs: [string, string][] = [["record", "Record"]]): ModelNode => ({
  name,
  class_path: `mod.${name}`,
  kind,
  params: {},
  inputs: inputs.map(([socketName, type]) => socket(socketName, type)),
  outputs: [socket("record", "Record")],
  loc: "",
  source_file: "",
  note: "",
  alias_of: "",
});

const wire = (source: string, target: string, target_socket: string, kind: Wire["kind"], type: string): Wire => ({
  source,
  source_socket: kind === "record" ? "record" : "op",
  target,
  target_socket,
  kind,
  type,
});

/** `stream` with a source and `members` ops. */
function pipeline(members: number, holder = "stream"): Model {
  const slots: [string, string][] = Array.from({ length: Math.max(members, 1) }, (_, i) => [`ops[${i}]`, OP_TYPE]);
  const nodes = [node(holder, "engine", [["source", "Source"], ...slots]), node(`${holder}.source`, "source", [])];
  const wires = [wire(`${holder}.source`, holder, "source", "ref", "Source")];
  for (let i = 0; i < members; i++) {
    nodes.push(node(`${holder}.ops[${i}]`, "op"));
    wires.push(wire(`${holder}.ops[${i}]`, holder, `ops[${i}]`, "list", OP_TYPE));
    if (i > 0) wires.push(wire(`${holder}.ops[${i - 1}]`, `${holder}.ops[${i}]`, "record", "record", "Record"));
  }
  return { nodes, wires, problems: [], outputs: [] };
}

/** A flow whose step `prep` holds a `Pipeline` with one transform, then `mask`. */
const FLOW_WITH_PIPELINE: Model = {
  nodes: [
    node("prep", "op", [["record", "Record"], ["transforms[0]", OP_TYPE]]),
    node("prep.transforms[0]", "op"),
    node("mask", "op"),
  ],
  wires: [
    wire("prep.transforms[0]", "prep", "transforms[0]", "list", OP_TYPE),
    wire("prep", "mask", "record", "record", "Record"),
  ],
  problems: [],
  outputs: ["mask"],
};

/** A JointStream holding a Stream: a `streams` list (type Stream — NOT an op list) and the inner Stream's `ops`. */
const JOINT: Model = {
  nodes: [
    node("j", "engine", [["streams[0]", "Stream"]]),
    node("j.streams[0]", "engine", [["source", "Source"], ["ops[0]", OP_TYPE]]),
    node("j.streams[0].ops[0]", "op"),
  ],
  wires: [
    wire("j.streams[0].ops[0]", "j.streams[0]", "ops[0]", "list", OP_TYPE),
    wire("j.streams[0]", "j", "streams[0]", "list", "Stream"),
  ],
  problems: [],
  outputs: [],
};

describe("opLists — the op lists a level holds", () => {
  it("finds a Stream's ops, its members in list order and how many items it has", () => {
    expect(opLists(pipeline(3))).toEqual([
      { name: "stream.ops", holder: "stream", slot: "ops", members: ["stream.ops[0]", "stream.ops[1]", "stream.ops[2]"], items: 3 },
    ]);
  });

  it("an EMPTY list is still a list — the holder's free `ops[0]` socket says so", () => {
    expect(opLists(pipeline(0))).toEqual([{ name: "stream.ops", holder: "stream", slot: "ops", members: [], items: 0 }]);
  });

  it("a Pipeline's `transforms` held by a flow step is an op list too", () => {
    expect(opLists(FLOW_WITH_PIPELINE)).toEqual([
      { name: "prep.transforms", holder: "prep", slot: "transforms", members: ["prep.transforms[0]"], items: 1 },
    ]);
  });

  it("a list of STREAMS is not an op list; the Stream inside it holds one", () => {
    expect(opLists(JOINT).map((list) => list.name)).toEqual(["j.streams[0].ops"]);
  });

  it("an item spelled `!ref:foo` counts as an item but is no member (it is another node, not a place in the list)", () => {
    const model = pipeline(1);
    model.nodes.push(node("foo", "op"));
    model.wires.push(wire("foo", "stream", "ops[1]", "list", OP_TYPE));
    model.nodes[0].inputs.push(socket("ops[1]", OP_TYPE));
    expect(opLists(model)).toEqual([{ name: "stream.ops", holder: "stream", slot: "ops", members: ["stream.ops[0]"], items: 2 }]);
  });

  it("a flow with no Pipeline holds none", () => {
    expect(opLists({ nodes: [node("grey", "op"), node("mask", "op")], wires: [], problems: [], outputs: ["mask"] })).toEqual([]);
  });

  it("memberList names the list a node is a member of, and nothing for a non-member", () => {
    expect(memberList(pipeline(3), "stream.ops[1]")?.name).toBe("stream.ops");
    expect(memberList(pipeline(3), "stream.source")).toBeUndefined();
    expect(memberList(pipeline(3), "stream")).toBeUndefined();
  });
});

describe("levelHasSteps — whether a level is a flow's steps (else: its op lists take the adds)", () => {
  it("the top of a `flow:` or a source graph holds steps; the top of a plain graph (a Stream) does not", () => {
    expect(levelHasSteps("", "flow")).toBe(true);
    expect(levelHasSteps("", "contract")).toBe(true);
    expect(levelHasSteps("", "graph")).toBe(false);
  });

  it("the inside of a subgraph is a steps mapping, whatever the graph", () => {
    expect(levelHasSteps("stream.ops[0]", "graph")).toBe(true);
  });
});

describe("insertTarget — where an ADDED node goes", () => {
  it("a Stream, a member selected: after that member (was: 'stream.ops[1]' is not a flow step)", () => {
    expect(insertTarget(pipeline(3), "", "graph", "stream.ops[1]")).toEqual({ kind: "after", node: "stream.ops[1]" });
  });

  it("a Stream, nothing selected: at the END of its list (was: after the last spelled node, refused)", () => {
    expect(insertTarget(pipeline(3), "", "graph", undefined)).toEqual({ kind: "end", list: "stream.ops" });
  });

  it("an EMPTY list, nothing selected: at the end of it — its first member", () => {
    expect(insertTarget(pipeline(0), "", "graph", undefined)).toEqual({ kind: "end", list: "stream.ops" });
  });

  it("a Stream, the source or the Stream itself selected: still the end of the list", () => {
    expect(insertTarget(pipeline(3), "", "graph", "stream.source")).toEqual({ kind: "end", list: "stream.ops" });
    expect(insertTarget(pipeline(3), "", "graph", "stream")).toEqual({ kind: "end", list: "stream.ops" });
  });

  it("two lists and nothing that says which: refused in words naming both — the holder or a member says which", () => {
    const two: Model = { ...pipeline(1), nodes: [...pipeline(1).nodes, ...pipeline(0, "other").nodes], wires: pipeline(1).wires };
    const refused = insertTarget(two, "", "graph", undefined);
    expect(refused).toEqual({
      kind: "none",
      why: "this graph holds 2 op lists (stream.ops, other.ops) — select a member of one, or the node holding it, to add to it",
    });
    expect(insertTarget(two, "", "graph", "other")).toEqual({ kind: "end", list: "other.ops" });
    expect(insertTarget(two, "", "graph", "stream.ops[0]")).toEqual({ kind: "after", node: "stream.ops[0]" });
  });

  it("CON — a flow keeps the flow rule: the selected step, else the flow's output; a list's holder is a STEP there", () => {
    expect(insertTarget(FLOW_WITH_PIPELINE, "", "flow", "mask")).toEqual({ kind: "after", node: "mask" });
    expect(insertTarget(FLOW_WITH_PIPELINE, "", "flow", undefined)).toEqual({ kind: "after", node: "mask" });
    expect(insertTarget(FLOW_WITH_PIPELINE, "", "flow", "prep")).toEqual({ kind: "after", node: "prep" });
  });

  it("a flow, a MEMBER of a step's Pipeline selected: after that member, in the Pipeline's list", () => {
    expect(insertTarget(FLOW_WITH_PIPELINE, "", "flow", "prep.transforms[0]")).toEqual({ kind: "after", node: "prep.transforms[0]" });
  });

  it("a plain graph with no op list keeps today's rule (the server answers it in its own words)", () => {
    const plain: Model = { nodes: [node("a", "source", []), node("b", "sink", [])], wires: [], problems: [], outputs: [] };
    expect(insertTarget(plain, "", "graph", undefined)).toEqual({ kind: "after", node: "b" });
    expect(insertTarget({ nodes: [], wires: [], problems: [], outputs: [] }, "", "graph", undefined)).toEqual({ kind: "none" });
  });

  it("inside a subgraph: the selected inner step, else the last inner step — never <out>", () => {
    const inside: Model = {
      nodes: [node("prep/<in>", "boundary"), node("prep/grey", "op"), node("prep/scaled", "op"), node("prep/<out>", "boundary")],
      wires: [],
      problems: [],
      outputs: [],
    };
    expect(insertTarget(inside, "prep", "graph", undefined)).toEqual({ kind: "after", node: "prep/scaled" });
    expect(insertTarget(inside, "prep", "graph", "prep/grey")).toEqual({ kind: "after", node: "prep/grey" });
  });
});

describe("wireTarget — a wire dropped on empty canvas names the node the pick goes after", () => {
  it("dragged out of a member (or into the next one, whose record it reads): after that member", () => {
    expect(wireTarget(pipeline(3), "", "graph", "stream.ops[1]")).toEqual({ kind: "after", node: "stream.ops[1]" });
  });

  it("dragged out of a Stream's source: the end of the list (the source is no place in it)", () => {
    expect(wireTarget(pipeline(3), "", "graph", "stream.source")).toEqual({ kind: "end", list: "stream.ops" });
  });

  it("CON — on a flow level the named node, exactly as before (a subgraph's <in> included)", () => {
    expect(wireTarget(FLOW_WITH_PIPELINE, "", "flow", "prep")).toEqual({ kind: "after", node: "prep" });
    expect(wireTarget(FLOW_WITH_PIPELINE, "prep", "flow", "prep/<in>")).toEqual({ kind: "after", node: "prep/<in>" });
  });
});

describe("what the page says and sends", () => {
  it("insertLabel: the palette's 'added …' and the sidebar's 'Click adds …'", () => {
    expect(insertLabel({ kind: "after", node: "gain" })).toBe("after gain");
    expect(insertLabel({ kind: "end", list: "stream.ops" })).toBe("at the end of stream.ops");
    expect(insertLabel({ kind: "none" })).toBeUndefined();
  });

  it("stepFields: `after` for a step or a member, `list` (no `after`) to append", () => {
    expect(stepFields({ kind: "after", node: "stream.ops[1]" })).toEqual({ after: "stream.ops[1]" });
    expect(stepFields({ kind: "end", list: "stream.ops" })).toEqual({ list: "stream.ops" });
    expect(stepFields({ kind: "none" })).toBeUndefined();
  });
});

describe("addedMember — the name the new member will have (members are named by POSITION)", () => {
  it("after `stream.ops[1]` → `stream.ops[2]` (the old ops[2] becomes ops[3] — a diff of names would pick ops[3])", () => {
    expect(addedMember(pipeline(3), { kind: "after", node: "stream.ops[1]" })).toBe("stream.ops[2]");
  });

  it("at the end of a 3-item list → `stream.ops[3]`; of an empty one → `stream.ops[0]`", () => {
    expect(addedMember(pipeline(3), { kind: "end", list: "stream.ops" })).toBe("stream.ops[3]");
    expect(addedMember(pipeline(0), { kind: "end", list: "stream.ops" })).toBe("stream.ops[0]");
  });

  it("CON — a flow step is named by its key: nothing to predict (the page reads the new name off the answer)", () => {
    expect(addedMember(FLOW_WITH_PIPELINE, { kind: "after", node: "mask" })).toBeUndefined();
    expect(addedMember(pipeline(3), { kind: "none" })).toBeUndefined();
  });
});

describe("beside — the drawn node a clicked add is placed next to", () => {
  it("the node it goes after; for an append the last member, else (an empty list) the holder", () => {
    expect(beside(pipeline(3), { kind: "after", node: "stream.ops[1]" })).toBe("stream.ops[1]");
    expect(beside(pipeline(3), { kind: "end", list: "stream.ops" })).toBe("stream.ops[2]");
    expect(beside(pipeline(0), { kind: "end", list: "stream.ops" })).toBe("stream");
    expect(beside(pipeline(0), { kind: "none" })).toBeUndefined();
  });
});

describe("shiftsMembers — whether a remove renames other nodes (the selection must not follow the positions)", () => {
  it("removing a member shifts the later ones", () => {
    expect(shiftsMembers(pipeline(3), ["stream.ops[1]"])).toBe(true);
  });

  it("CON — removing flow steps renames nothing", () => {
    expect(shiftsMembers(FLOW_WITH_PIPELINE, ["mask"])).toBe(false);
  });
});

/**
 * `piped.yaml` (P3): a Stream whose ops are [ReadImage, Pipeline {transforms: [ConvertMode, Threshold]}, PrintRecordOp]
 * — the Pipeline is BOTH a member of `stream.ops` and the holder of `stream.ops[1].transforms`.
 */
function piped(transforms: number): Model {
  const base = pipeline(3);
  const holder = "stream.ops[1]";
  const slots: [string, string][] = Array.from({ length: Math.max(transforms, 1) }, (_, i) => [`transforms[${i}]`, OP_TYPE]);
  const nodes = base.nodes.map((each) => (each.name === holder ? node(holder, "op", [["record", "Record"], ...slots]) : each));
  const wires = [...base.wires];
  for (let i = 0; i < transforms; i++) {
    nodes.push(node(`${holder}.transforms[${i}]`, "op"));
    wires.push(wire(`${holder}.transforms[${i}]`, holder, `transforms[${i}]`, "list", OP_TYPE));
  }
  return { ...base, nodes, wires };
}

describe("a Pipeline in a Stream's ops — adding INTO its transforms (P3)", () => {
  it("the Pipeline selected: at the end of ITS transforms, as the sidebar footer promises (was: after it, in stream.ops)", () => {
    expect(insertTarget(piped(2), "", "graph", "stream.ops[1]")).toEqual({ kind: "end", list: "stream.ops[1].transforms" });
    expect(insertTarget(piped(0), "", "graph", "stream.ops[1]")).toEqual({ kind: "end", list: "stream.ops[1].transforms" });
  });

  it("CON — a member that holds no list is still added after; a transform selected goes after that transform", () => {
    expect(insertTarget(piped(2), "", "graph", "stream.ops[0]")).toEqual({ kind: "after", node: "stream.ops[0]" });
    expect(insertTarget(piped(2), "", "graph", "stream.ops[1].transforms[0]")).toEqual({
      kind: "after",
      node: "stream.ops[1].transforms[0]",
    });
  });

  it("the new member's name and the card it is placed beside", () => {
    const target = insertTarget(piped(2), "", "graph", "stream.ops[1]");
    expect(addedMember(piped(2), target)).toBe("stream.ops[1].transforms[2]");
    expect(beside(piped(2), target)).toBe("stream.ops[1].transforms[1]");
  });
});

describe("inputTarget — a wire dragged OUT of an op-list input socket and dropped on empty canvas (P3)", () => {
  it("the free socket of an empty list: the end of that list — its first member", () => {
    expect(inputTarget(piped(0), "stream.ops[1]", "transforms[0]")).toEqual({ kind: "end", list: "stream.ops[1].transforms" });
  });

  it("the socket of item k: the new member becomes item k — after item k-1", () => {
    expect(inputTarget(piped(2), "stream.ops[1]", "transforms[1]")).toEqual({ kind: "after", node: "stream.ops[1].transforms[0]" });
  });

  it("the socket of the first item: no place before it — refused in words", () => {
    expect(inputTarget(piped(2), "stream.ops[1]", "transforms[0]")).toEqual({
      kind: "none",
      why: "a class is added after a member of stream.ops[1].transforms or at its end — select stream.ops[1] to add at the end",
    });
  });

  it("CON — a socket that is no op-list item (a record input) names no list: undefined, the caller's rule applies", () => {
    expect(inputTarget(piped(2), "stream.ops[1]", "record")).toBeUndefined();
    expect(inputTarget(piped(2), "stream.ops[2]", "record")).toBeUndefined();
  });
});
