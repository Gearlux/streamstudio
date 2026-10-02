/**
 * The page's half of being mounted in a host (`host.ts`): the host `GET api/workspace` names, what Run asks to trace,
 * the words for what ran, and the panel's messages — the PRO and the CON case of each.
 */
import { describe, expect, it } from "vitest";
import {
  MESSAGE_SOURCE,
  hasOwnSource,
  hostOf,
  parseHostCommand,
  filledLines,
  ranOn,
  runRecord,
  savedMessage,
  stateMessage,
} from "../host";
import type { Model, ModelNode, Wire } from "../model";

const node = (name: string, kind: ModelNode["kind"], extra: Partial<ModelNode> = {}): ModelNode => ({
  name,
  class_path: `m.${name}`,
  kind,
  params: {},
  inputs: [],
  outputs: [],
  loc: "",
  source_file: "",
  note: "",
  alias_of: "",
  ...extra,
});

const wire = (source: string, target: string, target_socket: string, kind: Wire["kind"] = "ref"): Wire => ({
  source,
  source_socket: "stream",
  target,
  target_socket,
  kind,
  type: "Source",
});

/** A Stream whose source slot is wired: record #N of its own source runs. */
const PIPELINE: Model = {
  nodes: [node("stream.source", "source"), node("stream", "engine"), node("stream.ops[0]", "op")],
  wires: [wire("stream.source", "stream", "source"), wire("stream.ops[0]", "stream", "ops[0]", "list")],
  problems: [],
  outputs: [],
};

/** A pane graph: a flow with a contract, no source of its own. */
const PANE: Model = {
  nodes: [node("window", "op"), node("classify", "op"), node("contract", "contract")],
  wires: [{ ...wire("window", "classify", "record", "record"), source_socket: "record", type: "Record" }],
  problems: [],
  outputs: ["classify"],
};

/** A Stream whose source slot is EMPTY: no source of its own either. */
const OPEN_STREAM: Model = { nodes: [node("stream", "engine")], wires: [], problems: [], outputs: [] };

describe("hostOf — the host GET api/workspace names", () => {
  it("{name} is that host; a name-less one is 'the host'", () => {
    expect(hostOf({ folder: "/w", session: "s", recovery: null, host: { name: "annotaide" } })).toEqual({ name: "annotaide" });
    expect(hostOf({ host: { name: "  " } })).toEqual({ name: "the host" });
    expect(hostOf({ host: true })).toEqual({ name: "the host" });
    expect(hostOf({ host: " annotaide " })).toEqual({ name: "annotaide" });
  });

  it("a standalone editor: host null, false or absent", () => {
    expect(hostOf({ folder: "/w", session: "s", recovery: null, host: null })).toBeNull();
    expect(hostOf({ folder: "/w", session: "s", recovery: null })).toBeNull();
    expect(hostOf({ host: false })).toBeNull();
    expect(hostOf(null)).toBeNull();
    expect(hostOf("annotaide")).toBeNull(); // the workspace answer itself must be an object
  });
});

describe("hasOwnSource — a Stream whose source slot is wired (the server's trace draws from it)", () => {
  it("a wired source slot on an engine node", () => {
    expect(hasOwnSource(PIPELINE)).toBe(true);
  });

  it("a pane graph, and a Stream with an empty source slot, have none", () => {
    expect(hasOwnSource(PANE)).toBe(false);
    expect(hasOwnSource(OPEN_STREAM)).toBe(false);
  });

  it("a wire into a 'source' socket of a node that is not an engine does not count", () => {
    const odd: Model = { ...PANE, wires: [wire("window", "classify", "source")] };
    expect(hasOwnSource(odd)).toBe(false);
  });
});

describe("runRecord — the record number Run sends", () => {
  const annotaide = { name: "annotaide" };

  it("mounted, a graph without its own source: none — the host's record", () => {
    expect(runRecord(annotaide, PANE, 3)).toBeNull();
    expect(runRecord(annotaide, OPEN_STREAM, 0)).toBeNull();
  });

  it("mounted, a graph WITH its own source: record #N of that source", () => {
    expect(runRecord(annotaide, PIPELINE, 3)).toBe(3);
  });

  it("standalone: always record #N — the server refuses a graph with no source in its own words", () => {
    expect(runRecord(null, PANE, 0)).toBe(0);
    expect(runRecord(null, PIPELINE, 2)).toBe(2);
  });
});

describe("ranOn — what the run panel says ran", () => {
  it("the host's own words when it gives a label", () => {
    expect(ranOn({ record: null, host: { label: "rec_7 · 0.25–0.75 s · 1–2 MHz", filled: { window: { start_s: 0.25 } } } })).toBe(
      "rec_7 · 0.25–0.75 s · 1–2 MHz",
    );
  });

  it("else what it filled in, per node", () => {
    const filled = { window: { start_s: 0.25, stop_s: 0.75 }, band: { f0_hz: 1e6, f1_hz: 2e6 } };
    expect(ranOn({ record: null, host: { filled } })).toBe(
      "the host's record · window: start_s = 0.25, stop_s = 0.75 · band: f0_hz = 1000000, f1_hz = 2000000",
    );
    expect(ranOn({ record: null, host: {} })).toBe("the host's record");
    expect(filledLines({ window: { start_s: 0.25 }, odd: 3 })).toEqual(["window: start_s = 0.25"]);
    expect(filledLines(null)).toEqual([]);
  });

  it("a record of the graph's own source: record #N", () => {
    expect(ranOn({ record: 1, host: null })).toBe("record #1");
    expect(ranOn({ record: 0 })).toBe("record #0");
  });
});

describe("the panel's messages — posted to the host", () => {
  it("state: the graph, its dirt, every unsaved graph, the tab", () => {
    expect(stateMessage("view_bte", true, ["view_bte", "source"], "settings")).toEqual({
      source: MESSAGE_SOURCE,
      type: "state",
      graph: "view_bte",
      dirty: true,
      unsaved: ["view_bte", "source"],
      tab: "settings",
    });
  });

  it("saved: the graph and the file written", () => {
    expect(savedMessage("view_bte", "/w/state/view_bte.yaml")).toEqual({
      source: "streamstudio",
      type: "saved",
      graph: "view_bte",
      path: "/w/state/view_bte.yaml",
    });
  });
});

describe("parseHostCommand — the one command a host sends the page", () => {
  it("open a graph, with a tab and a level when given", () => {
    expect(parseHostCommand({ target: "streamstudio", type: "open", graph: "view_bte" })).toEqual({ type: "open", graph: "view_bte" });
    expect(parseHostCommand({ target: "streamstudio", type: "open", graph: " view_bte ", tab: "settings", level: "prep" })).toEqual({
      type: "open",
      graph: "view_bte",
      tab: "settings",
      level: "prep",
    });
  });

  it("anything else is no command: another sender, another type, no graph, a tab the panel does not have", () => {
    expect(parseHostCommand({ target: "other", type: "open", graph: "view_bte" })).toBeUndefined();
    expect(parseHostCommand({ source: "streamstudio", type: "state", graph: "view_bte" })).toBeUndefined();
    expect(parseHostCommand({ target: "streamstudio", type: "close" })).toBeUndefined();
    expect(parseHostCommand({ target: "streamstudio", type: "open", graph: "" })).toBeUndefined();
    expect(parseHostCommand({ target: "streamstudio", type: "open", graph: 3 })).toBeUndefined();
    expect(parseHostCommand({ target: "streamstudio", type: "open", graph: "a", tab: "app" })).toBeUndefined();
    expect(parseHostCommand({ target: "streamstudio", type: "open", graph: "a", level: 1 })).toBeUndefined();
    expect(parseHostCommand("open view_bte")).toBeUndefined();
    expect(parseHostCommand(null)).toBeUndefined();
  });
});
