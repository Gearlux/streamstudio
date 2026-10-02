/**
 * What a link asks of the page (`link.ts`): the tab, the panel mode, the URL kept in step with what is shown, and — the
 * behaviour this round changes — a `?graph=` the editor does not list is answered with a message, never with another
 * graph.
 */
import { describe, expect, it } from "vitest";
import { embedFromSearch, graphFromSearch, missingGraphMessage, pageHref, pickGraph, tabFromSearch } from "../link";
import type { GraphSummary } from "../model";

const summary = (name: string, dirty = false): GraphSummary => ({ name, path: `/w/${name}.yaml`, kind: "flow", dirty });

describe("tabFromSearch — ?tab= opens a side panel tab", () => {
  it("settings and run by name; none, or one the panel does not have, is the text tab", () => {
    expect(tabFromSearch("?graph=view_bte&tab=settings")).toBe("settings");
    expect(tabFromSearch("?tab=run")).toBe("run");
    expect(tabFromSearch("?graph=view_bte")).toBe("text");
    expect(tabFromSearch("?tab=app")).toBe("text");
  });
});

describe("embedFromSearch — ?embed=1 puts the page in a host's panel", () => {
  it("1, true and the bare parameter embed; 0, false, no and none do not", () => {
    expect(embedFromSearch("?graph=view_bte&embed=1")).toBe(true);
    expect(embedFromSearch("?embed=true")).toBe(true);
    expect(embedFromSearch("?embed")).toBe(true);
    expect(embedFromSearch("?embed=0")).toBe(false);
    expect(embedFromSearch("?embed=false")).toBe(false);
    expect(embedFromSearch("?embed=no")).toBe(false);
    expect(embedFromSearch("?graph=view_bte")).toBe(false);
  });
});

describe("graphFromSearch", () => {
  it("the name a link gives, else null", () => {
    expect(graphFromSearch("?graph=view_bte&tab=settings")).toBe("view_bte");
    expect(graphFromSearch("?tab=settings")).toBeNull();
  });
});

describe("pageHref — the link follows the graph, the level and the tab; the host's parameters stay", () => {
  it("under a mount prefix, with embed kept: the path and every other parameter are untouched", () => {
    const href = pageHref("http://h:8789/studio/?graph=view_fft&embed=1", "view_bte", "", "settings");
    const url = new URL(href);
    expect(url.pathname).toBe("/studio/");
    expect(url.searchParams.get("graph")).toBe("view_bte");
    expect(url.searchParams.get("tab")).toBe("settings");
    expect(url.searchParams.get("embed")).toBe("1");
    expect(url.searchParams.has("level")).toBe(false);
  });

  it("the text tab drops the parameter, so a page that never left it keeps the link it was opened with", () => {
    const opened = "http://h/?graph=demo";
    expect(pageHref(opened, "demo", "", "text")).toBe(opened);
    expect(pageHref("http://h/?graph=demo&tab=run", "demo", "", "text")).toBe(opened);
  });

  it("a level is kept beside the tab", () => {
    const url = new URL(pageHref("http://h/?graph=demo", "demo", "prep", "run"));
    expect(url.searchParams.get("level")).toBe("prep");
    expect(url.searchParams.get("tab")).toBe("run");
  });
});

describe("pickGraph — which graph the page shows once the list is known", () => {
  const list = [summary("alpha"), summary("demo", true), summary("fresh", true)];

  it("the one the link names, when listed", () => {
    expect(pickGraph(list, "fresh", ["demo"])).toEqual({ kind: "open", name: "fresh" });
  });

  it("a link naming a graph the list does not hold opens NOTHING — not the first restored one, not the first listed", () => {
    // measured before: `?graph=nosuch` opened the first graph of the folder, silently
    expect(pickGraph(list, "nosuch", ["fresh"])).toEqual({ kind: "missing", wanted: "nosuch" });
    expect(pickGraph(list, "nosuch", [])).toEqual({ kind: "missing", wanted: "nosuch" });
    expect(pickGraph([], "view_bte", [])).toEqual({ kind: "missing", wanted: "view_bte" });
  });

  it("without a link: the first RESTORED one — the work just brought back — else the first listed", () => {
    expect(pickGraph(list, null, ["demo", "fresh"])).toEqual({ kind: "open", name: "demo" });
    expect(pickGraph(list, null, ["not-listed"])).toEqual({ kind: "open", name: "alpha" });
    expect(pickGraph(list, "", [])).toEqual({ kind: "open", name: "alpha" });
  });

  it("nothing at all for an empty list and no link", () => {
    expect(pickGraph([], null, ["demo"])).toEqual({ kind: "none" });
  });
});

describe("missingGraphMessage — the graph named, and the graphs there are", () => {
  it("names the folder and every listed graph", () => {
    expect(missingGraphMessage("nosuch", [summary("source"), summary("view_bte"), summary("view_fft")], "/w/state")).toBe(
      "There is no graph 'nosuch' in /w/state — the graphs there are: source, view_bte and view_fft.",
    );
    expect(missingGraphMessage("nosuch", [summary("source"), summary("view_bte")], "/w")).toBe(
      "There is no graph 'nosuch' in /w — the graphs there are: source and view_bte.",
    );
    expect(missingGraphMessage("nosuch", [summary("source")], "/w")).toBe("There is no graph 'nosuch' in /w — the graphs there are: source.");
  });

  it("an empty folder says it holds none; an unknown folder is 'here'", () => {
    expect(missingGraphMessage("view_bte", [], "/w/state")).toBe("There is no graph 'view_bte' in /w/state — it holds no graph yet.");
    expect(missingGraphMessage("x", [summary("a")], "")).toBe("There is no graph 'x' here — the graphs there are: a.");
  });
});
