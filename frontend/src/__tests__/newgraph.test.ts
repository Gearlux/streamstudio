/**
 * File ▸ New… writes the file at once (DESIGN3 — the user, 2026-09-29: "New ... by mistake should not happen. It
 * should ask to save"). The dialog asks the NAME and the FOLDER and shows where the file lands before Create:
 *
 *     New flow graph
 *       Name     [ fresh                      ]
 *       Folder   [ /Users/gert/streamstudio-try ] [Browse…]
 *       → /Users/gert/streamstudio-try/fresh.yaml
 *     [Cancel]  [Create]
 *
 * What is pure lives here: the title per kind, the path shown, and what Create sends (`POST api/graphs {name, kind,
 * dir?}` — `dir` only for a folder other than the editor's). The dialog itself is measured under playwright.
 */
import { describe, expect, it } from "vitest";
import { NEW_GRAPH_KINDS } from "../model";
import { newGraphTitle, newTarget, sameFolder } from "../newgraph";

describe("newGraphTitle — the dialog names the kind the New ▸ entry picked", () => {
  it("one title per kind", () => {
    expect(NEW_GRAPH_KINDS.map(newGraphTitle)).toEqual(["New flow graph", "New pipeline", "New source graph"]);
  });
});

describe("newTarget — what Create sends, and the path the dialog shows", () => {
  it("in the editor's folder: no `dir` (the server's graph folder), the path shown in full", () => {
    expect(newTarget("fresh", "/Users/gert/streamstudio-try", "/Users/gert/streamstudio-try")).toEqual({
      name: "fresh",
      path: "/Users/gert/streamstudio-try/fresh.yaml",
    });
  });

  it("in another folder: `dir` is that folder", () => {
    expect(newTarget("fresh", "/data/graphs", "/Users/gert/streamstudio-try")).toEqual({
      name: "fresh",
      dir: "/data/graphs",
      path: "/data/graphs/fresh.yaml",
    });
  });

  it("the editor's folder typed with a trailing slash is still the editor's folder", () => {
    expect(newTarget("fresh", "/w/", "/w")).toEqual({ name: "fresh", path: "/w/fresh.yaml" });
  });

  it("a typed `.yaml` / `.yml` is not doubled, and spaces around the name or folder go", () => {
    expect(newTarget("  fresh.yaml ", " /w ", "/w")).toEqual({ name: "fresh", path: "/w/fresh.yaml" });
    expect(newTarget("fresh.yml", "/w", "/w")).toEqual({ name: "fresh", path: "/w/fresh.yaml" });
  });

  it("the name is sent as typed otherwise — the server's name rule is the one that refuses (in its own words)", () => {
    expect(newTarget("my graph", "/w", "/w")).toEqual({ name: "my graph", path: "/w/my graph.yaml" });
    expect(newTarget("x.steps", "/w", "/w")).toEqual({ name: "x.steps", path: "/w/x.steps.yaml" });
  });

  it("nothing to create without a name or a folder: Create stays greyed", () => {
    expect(newTarget("", "/w", "/w")).toBeNull();
    expect(newTarget("   ", "/w", "/w")).toBeNull();
    expect(newTarget(".yaml", "/w", "/w")).toBeNull();
    expect(newTarget("fresh", "", "/w")).toBeNull();
    expect(newTarget("fresh", "  ", "")).toBeNull();
  });

  it("the editor's folder unknown (GET api/workspace failed): a typed folder is sent as `dir`", () => {
    expect(newTarget("fresh", "/w", "")).toEqual({ name: "fresh", dir: "/w", path: "/w/fresh.yaml" });
  });
});

describe("sameFolder", () => {
  it("ignores trailing slashes, keeps the root", () => {
    expect(sameFolder("/w/", "/w")).toBe(true);
    expect(sameFolder("/w", "/w2")).toBe(false);
    expect(sameFolder("/", "/")).toBe(true);
    expect(sameFolder("", "/w")).toBe(false);
  });
});
