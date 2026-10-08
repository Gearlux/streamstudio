/**
 * The requests the round-2 routes are sent (DESIGN2 §1 and §3), pinned against a stubbed `fetch`: the method, the
 * RELATIVE path (the page works under a mount prefix) and the body — what the server's contract names, nothing else.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  addNode,
  addStep,
  addWire,
  discardRecovery,
  getWorkspace,
  hfDatasets,
  hfFeatures,
  hfSplits,
  pluginLookup,
  joinSession,
  getApp,
  listFiles,
  listGraphs,
  saveGraph,
  newGraph,
  onRestarted,
  removeSteps,
  removeWires,
  restoreRecovery,
  setParam,
  setSlot,
  trace,
  undo,
} from "../api";

interface Sent {
  path: string;
  method: string;
  body: unknown;
}

/** Every header a request carried, lower-cased. */
const headersOf: Record<string, string>[] = [];

/** Stub `fetch` to answer `answer` and record every request. */
function stub(answer: unknown, status = 200): Sent[] {
  const sent: Sent[] = [];
  headersOf.length = 0;
  vi.stubGlobal("fetch", async (path: string, init: RequestInit) => {
    sent.push({ path, method: String(init.method), body: init.body === undefined ? undefined : JSON.parse(String(init.body)) });
    headersOf.push(Object.fromEntries(Object.entries((init.headers ?? {}) as Record<string, string>).map(([k, v]) => [k.toLowerCase(), v])));
    return new Response(JSON.stringify(answer), { status, statusText: status === 200 ? "OK" : "BAD REQUEST" });
  });
  return sent;
}

afterEach(() => {
  vi.unstubAllGlobals();
  joinSession(null);
  onRestarted(null);
});

describe("POST …/steps — a flow step, a list member after another, a member at the end of a list", () => {
  it("after a step or a member: `after`, and no `list`", async () => {
    const sent = stub({});
    await addStep("demo", { name: "threshold", after: "stream.ops[1]", class_path: "m.Threshold", params: {} });
    expect(sent).toEqual([
      {
        path: "api/graphs/demo/steps",
        method: "POST",
        body: { name: "threshold", after: "stream.ops[1]", class_path: "m.Threshold", params: {} },
      },
    ]);
  });

  it("at the end of a list: `list` names the list node, and no `after`", async () => {
    const sent = stub({});
    await addStep("demo", { name: "threshold", list: "stream.ops", class_path: "m.Threshold", params: {} });
    expect(sent[0].body).toEqual({ name: "threshold", list: "stream.ops", class_path: "m.Threshold", params: {} });
  });
});

describe("a class that is not an op — a CARD of its own (2026-10-05)", () => {
  const SCAN = "recordstream.ops.contract.ClassNamesScan";

  it("POST …/steps carries a card's dropped wire, and no place when the page has none", async () => {
    const sent = stub({ added: "class_names_output" });
    const wire = { target: "contract", target_socket: "classes" };
    expect(await addStep("files", { name: "classnamesoutput", class_path: "m.ClassNamesOutput", params: {}, wire })).toEqual({
      added: "class_names_output",
    });
    expect(sent).toEqual([
      {
        path: "api/graphs/files/steps",
        method: "POST",
        body: { name: "classnamesoutput", class_path: "m.ClassNamesOutput", params: {}, wire },
      },
    ]);
  });

  it("POST …/nodes: the class and its values, and the dropped wire only when there is one", async () => {
    const sent = stub({ added: "class_names_scan" });
    expect(await addNode("files", SCAN, {})).toEqual({ added: "class_names_scan" });
    await addNode("files", SCAN, { key: "label" }, { source: "file_source", source_socket: "stream" });
    expect(sent).toEqual([
      { path: "api/graphs/files/nodes", method: "POST", body: { class_path: SCAN, params: {} } },
      {
        path: "api/graphs/files/nodes",
        method: "POST",
        body: { class_path: SCAN, params: { key: "label" }, wire: { source: "file_source", source_socket: "stream" } },
      },
    ]);
  });
});

describe("POST …/steps/remove — members are removed like steps: ONE request for the whole selection", () => {
  it("sends every name at once", async () => {
    const sent = stub({});
    await removeSteps("demo", ["stream.ops[1]", "stream.ops[3]"]);
    expect(sent).toEqual([{ path: "api/graphs/demo/steps/remove", method: "POST", body: { nodes: ["stream.ops[1]", "stream.ops[3]"] } }]);
  });
});

describe("POST …/wires — a drawn wire is saved; …/wires/remove — Delete on wires is ONE request", () => {
  const ends = { source: "stream.source", source_socket: "class_names", target: "contract", target_socket: "classes" };

  it("a wire: its two ends, as the canvas drew them; the answer names the card it now comes from", async () => {
    const sent = stub({ name: "source", wired: "source" });
    expect(await addWire("source", ends)).toEqual({ name: "source", wired: "source" });
    expect(sent).toEqual([{ path: "api/graphs/source/wires", method: "POST", body: ends }]);
  });

  it("removing: every wire at once", async () => {
    const sent = stub({});
    await removeWires("source", [ends, { ...ends, source: "source", source_socket: "image", target_socket: "input" }]);
    expect(sent).toEqual([
      {
        path: "api/graphs/source/wires/remove",
        method: "POST",
        body: { wires: [ends, { ...ends, source: "source", source_socket: "image", target_socket: "input" }] },
      },
    ]);
  });

  it("a refusal is the server's one line", async () => {
    const said = "This wire would change the order of the steps — the editor can't save that yet.";
    stub({ error: said }, 400);
    await expect(addWire("pets", ends)).rejects.toMatchObject({ status: 400, error: said });
  });
});

describe("POST api/graphs — New… writes the file at once (DESIGN3)", () => {
  it("in the editor's folder: `{name, kind}`, no `dir`; the answer is the graph, not dirty", async () => {
    const answer = { name: "fresh", path: "/w/fresh.yaml", kind: "flow", dirty: false };
    const sent = stub(answer);
    expect(await newGraph("fresh", "flow")).toEqual(answer);
    expect(sent).toEqual([{ path: "api/graphs", method: "POST", body: { name: "fresh", kind: "flow" } }]);
  });

  it("in another folder: `dir` names it", async () => {
    const sent = stub({});
    await newGraph("fresh", "pipeline", "/data/graphs");
    expect(sent[0].body).toEqual({ name: "fresh", kind: "pipeline", dir: "/data/graphs" });
  });

  it("a refusal (nothing written) carries the server's words, for the dialog to show", async () => {
    const said = "/w/fresh.yaml already exists — pick another name, or open it with File ▸ Open";
    stub({ error: said }, 400);
    await expect(newGraph("fresh", "flow")).rejects.toMatchObject({ status: 400, error: said });
  });
});

describe("the workspace and its recovery file", () => {
  it("GET api/workspace", async () => {
    const answer = { folder: "/w", recovery: null };
    const sent = stub(answer);
    expect(await getWorkspace()).toEqual(answer);
    expect(sent).toEqual([{ path: "api/workspace", method: "GET", body: undefined }]);
  });

  it("POST …/recovery/restore and …/discard answer the graph list", async () => {
    const list = [{ name: "demo", path: "/w/demo.yaml", kind: "graph", dirty: true }];
    const sent = stub(list);
    expect(await restoreRecovery()).toEqual(list);
    expect(await discardRecovery()).toEqual(list);
    expect(sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      "POST api/workspace/recovery/restore",
      "POST api/workspace/recovery/discard",
    ]);
  });

  it("a refusal carries the server's words", async () => {
    stub({ error: "/w/.streamstudio-workspace.json: not JSON" }, 400);
    await expect(restoreRecovery()).rejects.toMatchObject({ error: "/w/.streamstudio-workspace.json: not JSON" });
  });
});

describe("the session a page joined — a tab left open across a restart is told, never edits blind (R1, P1)", () => {
  it("no session joined: no header (a script, the first requests of a page)", async () => {
    stub({});
    await setParam("demo", "mask", "low_level", 0.8);
    expect(headersOf[0]["x-streamstudio-session"]).toBeUndefined();
  });

  it("once joined, every request carries it", async () => {
    stub({});
    joinSession("abc123");
    await setParam("demo", "mask", "low_level", 0.8);
    await getWorkspace();
    expect(headersOf.map((headers) => headers["x-streamstudio-session"])).toEqual(["abc123", "abc123"]);
  });

  it("a 409 is the editor restarted: the listener hears it once per answer, and the call still refuses", async () => {
    stub({ error: "the editor was restarted after this page was loaded — nothing was changed" }, 409);
    joinSession("old");
    const heard: string[] = [];
    onRestarted((message) => heard.push(message));
    await expect(setParam("demo", "mask", "low_level", 0.8)).rejects.toMatchObject({ status: 409 });
    expect(heard).toEqual(["the editor was restarted after this page was loaded — nothing was changed"]);
  });

  it("an undo answers how member names moved (P7)", async () => {
    stub({ changed: true, shifts: [{ list: "stream.ops", first: 0, by: 1 }] });
    expect((await undo("demo")).shifts).toEqual([{ list: "stream.ops", first: 0, by: 1 }]);
  });
});

describe("POST …/trace — record #N of the graph's own source, or the host's record (DESIGN5 item 1)", () => {
  it("a record number, as before; `until` beside it", async () => {
    const sent = stub({});
    await trace("demo");
    await trace("demo", 2, "stream.ops[1]");
    expect(sent.map((request) => request.body)).toEqual([{ record: 0 }, { record: 2, until: "stream.ops[1]" }]);
  });

  it("the host's record: no record number at all — `{}`, the server asks its host", async () => {
    const sent = stub({});
    await trace("view_bte", null);
    await trace("view_bte", null, "classify");
    expect(sent).toEqual([
      { path: "api/graphs/view_bte/trace", method: "POST", body: {} },
      { path: "api/graphs/view_bte/trace", method: "POST", body: { until: "classify" } },
    ]);
  });
});

describe("POST …/slot — Set source… writes ONE marker into a slot (DESIGN5 item 5)", () => {
  it("the node, the slot, the class and only the values set", async () => {
    const answer = { name: "source", dirty: true, set: "stream.source" };
    const sent = stub(answer);
    expect(await setSlot("source", "stream", "source", "recordstream.sources.huggingface.HuggingFaceSource", { path: "ylecun/mnist" })).toEqual(
      answer,
    );
    expect(sent).toEqual([
      {
        path: "api/graphs/source/slot",
        method: "POST",
        body: { node: "stream", slot: "source", class_path: "recordstream.sources.huggingface.HuggingFaceSource", params: { path: "ylecun/mnist" } },
      },
    ]);
  });

  it("a refusal carries the server's located words, for the dialog to show", async () => {
    const said = "/w/source.yaml:7:5: stream has no slot 'sink'";
    stub({ error: said }, 400);
    await expect(setSlot("source", "stream", "sink", "m.X", {})).rejects.toMatchObject({ status: 400, error: said });
  });
});

describe("the HuggingFace lookups — asked of the editor's machine (DESIGN5 item 6)", () => {
  it("GET api/hf/datasets answers the cached datasets, bare or under its key", async () => {
    const sent = stub({ datasets: ["ylecun/mnist", "kitofrank/RFUAV"], reason: "" });
    expect(await hfDatasets()).toEqual({ names: ["ylecun/mnist", "kitofrank/RFUAV"], reason: "" });
    stub(["ylecun/mnist"]);
    expect(await hfDatasets()).toEqual({ names: ["ylecun/mnist"], reason: "" });
    expect(sent).toEqual([{ path: "api/hf/datasets", method: "GET", body: undefined }]);
  });

  it("GET api/hf/features and api/hf/splits name the dataset in the query; an empty part is left out", async () => {
    const sent = stub({ features: ["image", "label"], all_columns: "*" });
    expect(await hfFeatures("ylecun/mnist", "", "train")).toEqual({ names: ["image", "label"], reason: "" });
    stub({ splits: ["train", "test"] });
    expect(await hfSplits("ylecun/mnist")).toEqual({ names: ["train", "test"], reason: "" });
    expect(sent[0].path).toBe("api/hf/features?path=ylecun%2Fmnist&split=train");
  });

  it("an empty list keeps the server's reason; an answer without the list is no list", async () => {
    stub({ datasets: [], reason: "no datasets cached at /home/me/.cache/huggingface/hub" });
    expect(await hfDatasets()).toEqual({ names: [], reason: "no datasets cached at /home/me/.cache/huggingface/hub" });
    stub({ reason: "huggingface_hub is not installed" });
    expect(await hfDatasets()).toEqual({ names: [], reason: "huggingface_hub is not installed" });
    stub({ features: [1, "label", null] });
    expect(await hfFeatures("x")).toEqual({ names: ["label"], reason: "" });
  });
});

describe("a plugin's list — asked of the editor's machine with the node's values", () => {
  it("GET api/lookups/<tag> names the values in the query, an empty one left out, and keeps the reason", async () => {
    const sent = stub({ names: ["1.0", "2.0"], reason: "" });
    expect(await pluginLookup("SHOP_VERSION", { dataset: "fruit", project: "" })).toEqual({
      names: ["1.0", "2.0"],
      reason: "",
    });
    expect(sent[0].path).toBe("api/lookups/SHOP_VERSION?dataset=fruit");
    stub({ names: [], reason: "TimeoutError: the shop did not answer" });
    expect(await pluginLookup("SHOP_DATASET", {})).toEqual({ names: [], reason: "TimeoutError: the shop did not answer" });
  });
});

describe("under a mount prefix — every request is relative to the page (DESIGN5 item 7)", () => {
  it("a page at /studio/?graph=view_bte&embed=1 asks /studio/api/…, never the host's own /api/…", async () => {
    const sent = stub([]);
    await listGraphs();
    await getWorkspace();
    await trace("view_bte", null);
    const page = "http://127.0.0.1:8789/studio/?graph=view_bte&embed=1";
    expect(sent.map((request) => new URL(request.path, page).pathname)).toEqual([
      "/studio/api/graphs",
      "/studio/api/workspace",
      "/studio/api/graphs/view_bte/trace",
    ]);
  });
});

describe("GET …/app — the graph's settings (DESIGN5 item 2)", () => {
  it("asks the graph's app route and answers what the server resolved", async () => {
    const answer = { declared: true, fields: [{ node: "classify", param: "line_db", loc: "/w/bte.yaml:412:7" }], form: {}, problems: [] };
    const sent = stub(answer);
    expect(await getApp("view_bte")).toEqual(answer);
    expect(sent).toEqual([{ path: "api/graphs/view_bte/app", method: "GET", body: undefined }]);
  });
});

describe("GET api/files — the graph browser's listing, or every file for a file picker", () => {
  it("no query for the graph folder; dir and show=all only when asked", async () => {
    const sent = stub({ dir: "/w", parent: "/", entries: [] });
    await listFiles();
    await listFiles("/data/x y");
    await listFiles("$DATA_ROOT/models", "all");
    await listFiles(undefined, "all");
    expect(sent.map((request) => request.path)).toEqual([
      "api/files",
      "api/files?dir=%2Fdata%2Fx+y",
      "api/files?dir=%24DATA_ROOT%2Fmodels&show=all",
      "api/files?show=all",
    ]);
  });
});

describe("a Save mounted in a host carries the host's failure, the file written all the same", () => {
  it("host_error rides on the saved graph", async () => {
    stub({ name: "pane", dirty: false, host_error: "RuntimeError: the workspace is closing" });
    expect((await saveGraph("pane")).host_error).toBe("RuntimeError: the workspace is closing");
  });
});
