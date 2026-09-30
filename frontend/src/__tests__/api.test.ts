/**
 * The requests the round-2 routes are sent (DESIGN2 §1 and §3), pinned against a stubbed `fetch`: the method, the
 * RELATIVE path (the page works under a mount prefix) and the body — what the server's contract names, nothing else.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  addStep,
  discardRecovery,
  getWorkspace,
  joinSession,
  newGraph,
  onRestarted,
  removeSteps,
  restoreRecovery,
  setParam,
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

describe("POST …/steps/remove — members are removed like steps: ONE request for the whole selection", () => {
  it("sends every name at once", async () => {
    const sent = stub({});
    await removeSteps("demo", ["stream.ops[1]", "stream.ops[3]"]);
    expect(sent).toEqual([{ path: "api/graphs/demo/steps/remove", method: "POST", body: { nodes: ["stream.ops[1]", "stream.ops[3]"] } }]);
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
