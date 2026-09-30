/**
 * The subgraph LEVEL the canvas draws, the pure half: which model a level shows (`model.inside[…]`, recursive),
 * the breadcrumb above the canvas, the qualified names every edit sends (`prep/grey`), where a step added at a level
 * goes, and how a subgraph rename re-keys what the page holds. The DOM half — double-click enters, a crumb or Escape
 * goes up — is measured under playwright in `tests/test_canvas.py`.
 *
 * The document the fixtures mirror (DESIGN §0.2):
 *
 *     flow:
 *       prep:
 *         op: !class:recordstream.flow.subgraph.Subgraph
 *           steps:
 *             grey:   {op: …ConvertMode}
 *             inner:  {op: …Subgraph {steps: {x: …}}}
 *             scaled: {op: …Scale}
 *           result: scaled
 *       mask:
 *         op: …Threshold
 *     outputs: mask
 */
import { describe, expect, it } from "vitest";
import {
  EMPTY_MODEL,
  allNodeNames,
  breadcrumb,
  existingLevel,
  findNode,
  innerSteps,
  levelAnchor,
  levelChain,
  levelFromSearch,
  levelHref,
  levelModel,
  levelOfNode,
  levelPositions,
  levelPredecessor,
  parentLevel,
  qualify,
  rekeyName,
  followShifts,
  renamedTo,
  shiftKeys,
  shiftName,
  shortName,
  stepNames,
} from "../levels";
import type { Model, ModelNode, Wire } from "../model";

const node = (name: string, kind: ModelNode["kind"] = "op"): ModelNode => ({
  name,
  class_path: kind === "boundary" ? "" : kind === "subgraph" ? "recordstream.flow.subgraph.Subgraph" : `mod.${shortName(name)}`,
  kind,
  params: {},
  inputs: [],
  outputs: [],
  loc: "",
  source_file: "",
  note: "",
  alias_of: "",
});

const wire = (source: string, target: string): Wire => ({
  source,
  source_socket: "record",
  target,
  target_socket: "record",
  kind: "record",
  type: "Record",
});

const innerOfInner: Model = {
  nodes: [node("prep/inner/<in>", "boundary"), node("prep/inner/x"), node("prep/inner/<out>", "boundary")],
  wires: [wire("prep/inner/<in>", "prep/inner/x"), wire("prep/inner/x", "prep/inner/<out>")],
  problems: [],
  outputs: ["prep/inner/x"],
};

const prep: Model = {
  nodes: [
    node("prep/<in>", "boundary"),
    node("prep/grey"),
    node("prep/inner", "subgraph"),
    node("prep/scaled"),
    node("prep/<out>", "boundary"),
  ],
  wires: [
    wire("prep/<in>", "prep/grey"),
    wire("prep/grey", "prep/inner"),
    wire("prep/inner", "prep/scaled"),
    wire("prep/scaled", "prep/<out>"),
  ],
  problems: [{ message: "prep/scaled: a problem inside", loc: "demo.yaml:9:9" }],
  outputs: ["prep/scaled"],
  inside: { "prep/inner": innerOfInner },
};

const demo: Model = {
  nodes: [node("prep", "subgraph"), node("mask")],
  wires: [wire("prep", "mask")],
  problems: [],
  outputs: ["mask"],
  inside: { prep },
};

describe("names: shortName / levelOfNode / qualify / levelChain / parentLevel", () => {
  it("a qualified name is its level, a slash, and the step's own name", () => {
    expect(shortName("prep/grey")).toBe("grey");
    expect(levelOfNode("prep/grey")).toBe("prep");
    expect(shortName("prep/inner/x")).toBe("x");
    expect(levelOfNode("prep/inner/x")).toBe("prep/inner");
  });

  it("a top-level name, and a list member, have no level", () => {
    expect(shortName("mask")).toBe("mask");
    expect(levelOfNode("mask")).toBe("");
    expect(shortName("stream.ops[1]")).toBe("stream.ops[1]");
    expect(levelOfNode("stream.ops[1]")).toBe("");
    expect(levelOfNode("stream.ops[1]/grey")).toBe("stream.ops[1]");
  });

  it("qualify puts a step on a level; the top level leaves it bare", () => {
    expect(qualify("", "threshold")).toBe("threshold");
    expect(qualify("prep", "threshold")).toBe("prep/threshold");
    expect(qualify("prep/inner", "y")).toBe("prep/inner/y");
  });

  it("levelChain lists the subgraph nodes from the top down; parentLevel goes up one", () => {
    expect(levelChain("")).toEqual([]);
    expect(levelChain("prep")).toEqual(["prep"]);
    expect(levelChain("prep/inner")).toEqual(["prep", "prep/inner"]);
    expect(levelChain("stream.ops[1]/inner")).toEqual(["stream.ops[1]", "stream.ops[1]/inner"]);
    expect(parentLevel("prep/inner")).toBe("prep");
    expect(parentLevel("prep")).toBe("");
    expect(parentLevel("")).toBe("");
  });
});

describe("levelModel — what the canvas draws at a level", () => {
  it("is the document's own model at the top", () => expect(levelModel(demo, "")).toBe(demo));

  it("is the inside of the subgraph node the level names", () => {
    expect(levelModel(demo, "prep")).toBe(prep);
    expect(levelModel(demo, "prep")!.nodes.map((n) => shortName(n.name))).toEqual(["<in>", "grey", "inner", "scaled", "<out>"]);
  });

  it("walks down nested insides by their qualified keys", () => expect(levelModel(demo, "prep/inner")).toBe(innerOfInner));

  it("also finds a nested inside keyed by the step's own name", () => {
    const shortKeyed: Model = { ...demo, inside: { prep: { ...prep, inside: { inner: innerOfInner } } } };
    expect(levelModel(shortKeyed, "prep/inner")).toBe(innerOfInner);
  });

  it("is undefined for a level the document does not hold (any more), and on a server without insides", () => {
    expect(levelModel(demo, "gone")).toBeUndefined();
    expect(levelModel(demo, "prep/gone")).toBeUndefined();
    expect(levelModel({ ...demo, inside: undefined }, "prep")).toBeUndefined();
  });

  it("a list member's subgraph is a level too", () => {
    const stream: Model = {
      nodes: [node("stream", "engine"), node("stream.ops[0]"), node("stream.ops[1]", "subgraph")],
      wires: [],
      problems: [],
      outputs: [],
      inside: { "stream.ops[1]": { nodes: [node("stream.ops[1]/grey")], wires: [], problems: [], outputs: [] } },
    };
    expect(levelModel(stream, "stream.ops[1]")!.nodes.map((n) => n.name)).toEqual(["stream.ops[1]/grey"]);
  });
});

describe("existingLevel — where the canvas falls back when its level went away", () => {
  it("keeps a level the document holds", () => {
    expect(existingLevel(demo, "prep/inner")).toBe("prep/inner");
    expect(existingLevel(demo, "")).toBe("");
  });
  it("climbs to the deepest level that is still there (an undo removed the subgraph, a rename moved it)", () => {
    expect(existingLevel(demo, "prep/gone")).toBe("prep");
    expect(existingLevel(demo, "gone/deeper")).toBe("");
  });
});

describe("breadcrumb — `demo › prep › inner` above the canvas", () => {
  it("starts with the graph, then one crumb per subgraph entered, each naming the step's own name", () => {
    expect(breadcrumb("demo", "prep/inner")).toEqual([
      { label: "demo", level: "" },
      { label: "prep", level: "prep" },
      { label: "inner", level: "prep/inner" },
    ]);
  });
  it("is the graph alone at the top", () => expect(breadcrumb("demo", "")).toEqual([{ label: "demo", level: "" }]));
});

describe("renamedTo — what a rename typed on a level sends", () => {
  it("keeps the node on its level: `grey` renamed `mono` inside prep is `prep/mono`", () => {
    expect(renamedTo("prep/grey", "mono")).toBe("prep/mono");
    expect(renamedTo("prep/inner/x", "y")).toBe("prep/inner/y");
  });
  it("a top-level node takes the typed name as it is", () => expect(renamedTo("mask", "binary")).toBe("binary"));
  it("a typed name holding a slash is sent as typed, and the server says what it thinks of it", () => {
    expect(renamedTo("prep/grey", "other/mono")).toBe("other/mono");
  });
});

describe("rekeyName — a subgraph rename moves every name inside it", () => {
  it("renames the node itself and every name below it", () => {
    expect(rekeyName("prep", "prep", "pre")).toBe("pre");
    expect(rekeyName("prep/grey", "prep", "pre")).toBe("pre/grey");
    expect(rekeyName("prep/inner/x", "prep", "pre")).toBe("pre/inner/x");
    expect(rekeyName("prep/inner/x", "prep/inner", "prep/core")).toBe("prep/core/x");
  });
  it("leaves a name that only starts with the same letters, and every other name", () => {
    expect(rekeyName("prep2/grey", "prep", "pre")).toBe("prep2/grey");
    expect(rekeyName("mask", "prep", "pre")).toBe("mask");
    expect(rekeyName("", "prep", "pre")).toBe("");
  });
});

describe("allNodeNames / findNode — every level at once", () => {
  it("lists the nodes of every level, top first, each inside after its level's own", () => {
    expect(allNodeNames(demo)).toEqual([
      "prep",
      "mask",
      "prep/<in>",
      "prep/grey",
      "prep/inner",
      "prep/scaled",
      "prep/<out>",
      "prep/inner/<in>",
      "prep/inner/x",
      "prep/inner/<out>",
    ]);
  });
  it("finds a node on any level by its qualified name", () => {
    expect(findNode(demo, "mask")?.kind).toBe("op");
    expect(findNode(demo, "prep/inner/x")?.name).toBe("prep/inner/x");
    expect(findNode(demo, "prep/nothing")).toBeUndefined();
  });
});

describe("stepNames — the nodes a gesture acts on", () => {
  it("are the level's nodes in the model's order, the <in>/<out> boundaries left out", () => {
    expect(stepNames(prep)).toEqual(["prep/grey", "prep/inner", "prep/scaled"]);
  });
  it("keeps only the names given, in the model's order", () => {
    expect(stepNames(prep, ["prep/scaled", "prep/<in>", "prep/grey", "mask"])).toEqual(["prep/grey", "prep/scaled"]);
  });
});

describe("levelAnchor — the node a class, a paste, a template or a steps file goes after", () => {
  it("at the top it is the page's usual rule: the selection, else the flow's output, else the last step", () => {
    expect(levelAnchor(demo, "", "prep")).toBe("prep");
    expect(levelAnchor(demo, "", undefined)).toBe("mask");
  });
  it("inside a subgraph it is the selected inner node", () => expect(levelAnchor(prep, "prep", "prep/grey")).toBe("prep/grey"));
  it("else the last inner step — never the <out> boundary, never a node of another level", () => {
    expect(levelAnchor(prep, "prep", undefined)).toBe("prep/scaled");
    expect(levelAnchor(prep, "prep", "prep/<out>")).toBe("prep/scaled");
    expect(levelAnchor(prep, "prep", "mask")).toBe("prep/scaled");
  });
  it("is undefined inside an empty subgraph", () => {
    const empty: Model = { nodes: [node("e/<in>", "boundary"), node("e/<out>", "boundary")], wires: [], problems: [], outputs: [] };
    expect(levelAnchor(empty, "e", undefined)).toBeUndefined();
    expect(levelAnchor(EMPTY_MODEL, "e", undefined)).toBeUndefined();
  });
});

describe("levelPredecessor — where a step dropped from an input goes, on a level", () => {
  it("is the node whose record the target reads", () => expect(levelPredecessor(prep, "prep/scaled")).toBe("prep/inner"));
  it("is undefined for the head step inside a subgraph: <in> is not a step to insert after", () => {
    expect(levelPredecessor(prep, "prep/grey")).toBeUndefined();
  });
  it("at the top is the usual rule", () => expect(levelPredecessor(demo, "mask")).toBe("prep"));
});

describe("innerSteps — what a subgraph node's card lists", () => {
  it("names the steps inside, by their own names, the boundaries left out", () => {
    expect(innerSteps(demo, "prep")).toEqual(["grey", "inner", "scaled"]);
    expect(innerSteps(demo, "prep/inner")).toEqual(["x"]);
  });
  it("is empty for a node without an inside", () => expect(innerSteps(demo, "mask")).toEqual([]));
});

describe("levelPositions — every node of a level gets a place", () => {
  it("answers the SAME map when every node has one (no redraw)", () => {
    const positions = { "prep/<in>": [0, 40], "prep/grey": [280, 40], "prep/inner": [560, 40], "prep/scaled": [840, 40], "prep/<out>": [1120, 40] } as const;
    const given = Object.fromEntries(Object.entries(positions).map(([k, v]) => [k, [...v] as [number, number]]));
    expect(levelPositions(prep, given)).toBe(given);
  });
  it("puts a node the sidecar does not place to the right of the placed ones, in the model's order, on the grid", () => {
    const given = { "prep/<in>": [0, 43] as [number, number], "prep/grey": [283, 60] as [number, number] };
    const placed = levelPositions(prep, given);
    expect(placed["prep/<in>"]).toEqual([0, 43]);
    expect(placed["prep/inner"]).toEqual([570, 40]);
    expect(placed["prep/scaled"]).toEqual([850, 40]);
    expect(placed["prep/<out>"]).toEqual([1130, 40]);
  });
  it("starts at the origin row when nothing is placed", () => {
    const placed = levelPositions(innerOfInner, {});
    expect(placed["prep/inner/<in>"]).toEqual([0, 40]);
    expect(placed["prep/inner/x"]).toEqual([280, 40]);
  });
});

describe("levelFromSearch / levelHref — ?level= kept in step with the canvas, like ?graph=", () => {
  it("reads the level a link asks for; none is the top", () => {
    expect(levelFromSearch("?graph=blobs&level=prep")).toBe("prep");
    expect(levelFromSearch("?graph=blobs&level=prep%2Finner")).toBe("prep/inner");
    expect(levelFromSearch("?graph=blobs")).toBe("");
  });

  it("writes the graph and the level shown; the top drops the level", () => {
    expect(levelHref("http://h/?graph=a", "blobs", "prep")).toBe("http://h/?graph=blobs&level=prep");
    expect(levelHref("http://h/?graph=blobs&level=prep", "blobs", "")).toBe("http://h/?graph=blobs");
    expect(levelFromSearch(new URL(levelHref("http://h/", "blobs", "stream.ops[1]/inner")).search)).toBe("stream.ops[1]/inner");
  });
});

describe("shiftName / followShifts — an op list member is named by its POSITION (P7)", () => {
  const back = { list: "stream.ops", first: 0, by: 1 }; // an undo put a member back at stream.ops[0]

  it("a later member, and every name under it, moves with the shift", () => {
    expect(shiftName("stream.ops[0]", back)).toBe("stream.ops[1]");
    expect(shiftName("stream.ops[0]/grey", back)).toBe("stream.ops[1]/grey");
    expect(shiftName("stream.ops[3].source", back)).toBe("stream.ops[4].source");
  });

  it("CON — an earlier member, another list, and a longer index of the same digits stay as they are", () => {
    expect(shiftName("stream.ops[0]", { list: "stream.ops", first: 1, by: 1 })).toBe("stream.ops[0]");
    expect(shiftName("other.ops[0]", back)).toBe("other.ops[0]");
    expect(shiftName("stream.ops[10]", { list: "stream.ops", first: 11, by: 1 })).toBe("stream.ops[10]");
    expect(shiftName("stream", back)).toBe("stream");
  });

  it("a member a removal took away is gone: undefined", () => {
    expect(shiftName("stream.ops[1]", { list: "stream.ops", first: 2, by: -1 })).toBeUndefined();
    expect(shiftName("stream.ops[1]/grey", { list: "stream.ops", first: 2, by: -1 })).toBeUndefined();
  });

  it("the level drawn follows its subgraph member — never another subgraph under the same breadcrumb", () => {
    expect(followShifts("stream.ops[0]", [back])).toBe("stream.ops[1]");
    expect(followShifts("prep/pipe.transforms[0]/x", [{ list: "prep/pipe.transforms", first: 0, by: 1 }])).toBe(
      "prep/pipe.transforms[1]/x",
    );
    expect(followShifts("stream.ops[0]", [])).toBe("stream.ops[0]");
  });

  it("a level whose member is gone climbs to the level that holds the list", () => {
    expect(followShifts("stream.ops[1]", [{ list: "stream.ops", first: 2, by: -1 }])).toBe("");
    expect(followShifts("prep/pipe.transforms[0]", [{ list: "prep/pipe.transforms", first: 1, by: -1 }])).toBe("prep");
  });

  it("shiftKeys re-keys what the page holds by name (a card's measured size)", () => {
    expect(shiftKeys({ "stream.ops[0]": 1, "stream.ops[1]": 2, stream: 3 }, { list: "stream.ops", first: 1, by: 1 })).toEqual({
      "stream.ops[0]": 1,
      "stream.ops[2]": 2,
      stream: 3,
    });
  });
});
