/**
 * Unsaved work survives a stop, the page's half (DESIGN2 §1, the user's item 1: "streamstudio stop or restart should
 * save the file to a tmp workspace file (hidden), when opening that same workspace streamstudio should ask to start
 * from the tmp file or the original"). `GET api/workspace` says whether the last session left unsaved work; the page
 * asks before anything else:
 *
 *     Unsaved work from the last session (29 Sep 13:25)
 *       demo.yaml         changed
 *       radio_view.yaml   changed — the file on disk changed after the editor stopped
 *     [Restore unsaved work]   [Open the saved files]
 *
 * What is pure lives here: the time in the title, the rows, whether the editor may load a graph yet, and which graph
 * it shows first once the question is answered. The dialog itself is measured under playwright.
 *
 * Every kept graph is a changed FILE: New… writes its file at once, so no graph exists only in the editor.
 */
import { describe, expect, it } from "vitest";
import {
  RECOVERY_PHASES,
  answered,
  firstGraph,
  recoveryError,
  recoveryRows,
  restoredNames,
  recoveryTitle,
  relativePath,
  savedAtLabel,
  discardQuestion,
  type RecoveredGraph,
  type Recovery,
} from "../recovery";
import type { GraphSummary } from "../model";

const AT = "2026-09-29T13:25:07";

/** One kept graph as `GET api/workspace` lists it — every key the server always sends. */
const kept = (name: string, extra: Partial<RecoveredGraph> = {}): RecoveredGraph => ({
  name,
  path: `/w/${name}.yaml`,
  disk: "unchanged",
  reason: null,
  changed_on_disk: false,
  saved_at: AT,
  ...extra,
});

const RECOVERY: Recovery = {
  saved_at: AT,
  graphs: [
    kept("demo"),
    kept("radio_view", { disk: "changed", changed_on_disk: true }),
  ],
  error: null,
  aside: null,
};

/** What the server answers for a recovery file it cannot use: no time, no rows, `error` naming the file. */
const BROKEN: Recovery = {
  saved_at: null,
  graphs: [],
  error: "/w/.streamstudio-workspace.json cannot be read: Expecting value: line 1 column 1 (char 0)",
  aside: "/w/.streamstudio-workspace.broken-2.json",
};

const summary = (name: string, dirty = false): GraphSummary => ({ name, path: `/w/${name}.yaml`, kind: "flow", dirty });

describe("savedAtLabel — when the last session stopped, as the dialog says it", () => {
  it("day, month, hour:minute in the page's own time", () => {
    expect(savedAtLabel("2026-09-29T13:25:07")).toBe("29 Sep 13:25");
    expect(savedAtLabel("2026-01-05T09:03:00")).toBe("5 Jan 09:03");
  });

  it("a time the page cannot read is shown as sent, never as 'Invalid Date'", () => {
    expect(savedAtLabel("yesterday")).toBe("yesterday");
  });
});

describe("recoveryTitle", () => {
  it("names the time the work was kept", () => {
    expect(recoveryTitle(RECOVERY)).toBe("Unsaved work from the last session (29 Sep 13:25)");
  });

  it("a recovery file that could not be read has no time to name", () => {
    expect(recoveryTitle(BROKEN)).toBe("Unsaved work from the last session");
  });
});

describe("recoveryRows — one row per graph with unsaved work", () => {
  it("the file and what is unsaved about it, in the dialog's words", () => {
    expect(recoveryRows(RECOVERY, "/w")).toEqual([
      { name: "demo", file: "demo.yaml", disk: "unchanged", changedOnDisk: false, note: "changed" },
      {
        name: "radio_view",
        file: "radio_view.yaml",
        disk: "changed",
        changedOnDisk: true,
        note: "changed — the file on disk changed after the editor stopped",
      },
    ]);
  });

  it("a file deleted after the stop says so, and that Save writes it again (R10)", () => {
    const rows = recoveryRows({ ...RECOVERY, graphs: [kept("flow", { disk: "gone" })] }, "/w");
    expect(rows[0].note).toBe("changed — the file was deleted or moved away after the editor stopped; Save writes it again");
  });

  it("a file the editor cannot read says why, naming it (R2)", () => {
    const reason = "/elsewhere/away.yaml cannot be read: Permission denied";
    const rows = recoveryRows({ ...RECOVERY, graphs: [kept("away", { path: "/elsewhere/away.yaml", disk: "unreadable", reason })] }, "/w");
    expect(rows[0]).toMatchObject({ file: "../elsewhere/away.yaml", disk: "unreadable", note: `changed — ${reason}` });
  });

  it("a file of another folder is named by its way from the workspace folder (R11)", () => {
    const rows = recoveryRows({ ...RECOVERY, graphs: [kept("flow", { path: "/r/ws/flow.yaml" }), kept("flow~2", { path: "/r/other/flow.yaml" })] }, "/r/ws");
    expect(rows.map((row) => row.file)).toEqual(["flow.yaml", "../other/flow.yaml"]);
  });

  it("two versions of ONE file (kept by two stops) each say when they were kept (R5)", () => {
    const rows = recoveryRows(
      {
        ...RECOVERY,
        graphs: [kept("flow", { saved_at: "2026-09-29T18:09:00" }), kept("flow", { saved_at: "2026-09-29T18:07:00" }), kept("demo")],
      },
      "/w",
    );
    expect(rows.map((row) => row.note)).toEqual(["changed (kept 29 Sep 18:09)", "changed (kept 29 Sep 18:07)", "changed"]);
  });

  it("a broken recovery file has no rows (the dialog shows its error instead)", () => {
    expect(recoveryRows(BROKEN, "/w")).toEqual([]);
  });
});

describe("relativePath — a file named by its way from the workspace folder", () => {
  it("inside it, below it, beside it", () => {
    expect(relativePath("/w/demo.yaml", "/w")).toBe("demo.yaml");
    expect(relativePath("/w/sub/x.yaml", "/w/")).toBe("sub/x.yaml");
    expect(relativePath("/r/other/flow.yaml", "/r/ws")).toBe("../other/flow.yaml");
    expect(relativePath("/elsewhere/a.yaml", "/r/ws")).toBe("../../elsewhere/a.yaml");
  });
});

describe("discardQuestion — what Open the saved files asks before it drops the kept work", () => {
  it("names the files that would be lost, and says it cannot be undone (P10)", () => {
    expect(discardQuestion(RECOVERY, "/w")).toEqual({
      title: "Discard the unsaved work of the last session?",
      message: "The unsaved changes of demo.yaml and radio_view.yaml are lost. This cannot be undone.",
      confirm: "Discard",
      danger: true,
    });
  });

  it("an unreadable recovery file is only moved aside, and the question says where (R9)", () => {
    expect(discardQuestion(BROKEN, "/w")).toEqual({
      title: "Set the unreadable unsaved work aside?",
      message:
        "/w/.streamstudio-workspace.json is moved to /w/.streamstudio-workspace.broken-2.json — nothing is deleted — and the saved files open.",
      confirm: "Set it aside",
    });
  });
});

describe("recoveryError — whether the dialog shows rows or the reason the file cannot be used", () => {
  it("a readable file: the server sends `error: null` — rows, never the error paragraph", () => {
    expect(recoveryError(RECOVERY)).toBeUndefined();
  });

  it("an unreadable file: the server's words, verbatim (they name the file)", () => {
    expect(recoveryError(BROKEN)).toBe("/w/.streamstudio-workspace.json cannot be read: Expecting value: line 1 column 1 (char 0)");
  });
});

describe("answered — the editor loads no graph until the question is answered", () => {
  it("the phases, and which of them let a graph load", () => {
    expect(RECOVERY_PHASES).toEqual(["pending", "asking", "confirming", "restored", "discarded", "none"]);
    expect(RECOVERY_PHASES.filter(answered)).toEqual(["restored", "discarded", "none"]);
  });
});

describe("firstGraph — which graph the editor shows once the question is answered", () => {
  const list = [summary("alpha"), summary("demo", true), summary("fresh", true)];

  it("the one the link names (?graph=), when listed", () => {
    expect(firstGraph(list, "fresh", ["demo"])).toBe("fresh");
  });

  it("else the first RESTORED one — the work the person just brought back", () => {
    expect(firstGraph(list, null, ["demo", "fresh"])).toBe("demo");
    expect(firstGraph(list, "gone", ["fresh"])).toBe("fresh");
  });

  it("else the first listed; nothing when the list is empty", () => {
    expect(firstGraph(list, null, [])).toBe("alpha");
    expect(firstGraph(list, null, ["not-listed"])).toBe("alpha");
    expect(firstGraph([], null, ["demo"])).toBeUndefined();
  });
});

describe("restoredNames — the list names the restored graphs got", () => {
  const restored = (name: string, path: string, flag = true): GraphSummary & { restored: boolean } => ({
    name,
    path,
    kind: "flow",
    dirty: true,
    restored: flag,
  });

  it("the entries the restore flagged, in the recovery file's order — a name taken meanwhile is `demo~2`", () => {
    const list = [restored("demo", "/w/demo.yaml", false), restored("demo~2", "/w/demo.yaml"), restored("radio_view", "/w/radio_view.yaml")];
    expect(restoredNames(list, RECOVERY)).toEqual(["demo~2", "radio_view"]);
  });

  it("two versions of one file: two names, each once", () => {
    const twice = { ...RECOVERY, graphs: [kept("flow"), kept("flow")] };
    const list = [restored("flow", "/w/flow.yaml"), restored("flow~2", "/w/flow.yaml")];
    expect(restoredNames(list, twice)).toEqual(["flow", "flow~2"]);
  });

  it("a graph the list does not hold is left out", () => {
    expect(restoredNames([restored("demo", "/w/demo.yaml")], BROKEN)).toEqual([]);
  });
});
