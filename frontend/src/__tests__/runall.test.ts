/**
 * The full run's pure half: what the page says of a job and which job belongs to a graph. The buttons, the plan and
 * the strip are measured under playwright in `tests/test_canvas.py`.
 */
import { describe, expect, it } from "vitest";
import type { Job, RunPlan } from "../api";
import { countLine, deleteLines, elapsedLine, endingLine, firstRunning, fraction, jobTitle, latestFor, runningFor, tickedScopes } from "../runall";

const job = (over: Partial<Job> = {}): Job => ({
  id: "j1",
  key: "run:g",
  title: "g",
  unit: "records",
  done: 0,
  total: null,
  message: "",
  detail: { graph: "g", scopes: [] },
  status: "running",
  running: true,
  stop: false,
  error: "",
  elapsed_s: 0,
  ...over,
});

describe("a job's count", () => {
  it("says done of total", () => {
    expect(countLine(job({ done: 312, total: 807 }))).toBe("312 of 807 records");
    expect(fraction(job({ done: 312, total: 807 }))).toBeCloseTo(0.3866, 3);
  });

  it("says only the count while the total is unknown, and fills nothing", () => {
    expect(countLine(job({ done: 3 }))).toBe("3 records");
    expect(fraction(job({ done: 3 }))).toBeNull();
    expect(fraction(job({ done: 3, total: 0 }))).toBeNull();
  });

  it("never fills past the end", () => {
    expect(fraction(job({ done: 9, total: 5 }))).toBe(1);
  });
});

describe("a duration", () => {
  it.each([
    [0.4, "0 s"],
    [42, "42 s"],
    [252, "4 min 12 s"],
    [3780, "1 h 3 min"],
  ])("%d seconds reads %s", (seconds, line) => {
    expect(elapsedLine(seconds)).toBe(line);
  });
});

describe("a job's ending", () => {
  it("is the server's message and how long it took", () => {
    const done = job({ status: "done", running: false, message: "g: 807 records run", elapsed_s: 252 });
    expect(endingLine(done)).toBe("g: 807 records run (4 min 12 s)");
  });

  it("says a failure in the error's words", () => {
    const failed = job({ status: "failed", running: false, error: "OSError: the disk is full", elapsed_s: 3 });
    expect(endingLine(failed)).toBe("g failed — OSError: the disk is full (3 s)");
  });

  it("falls back to the status for a job that says nothing", () => {
    expect(endingLine(job({ status: "stopped", running: false, elapsed_s: 1 }))).toBe("g: stopped (1 s)");
  });
});

describe("which job is a graph's", () => {
  const jobs = [
    job({ id: "j3", detail: { graph: "b" } }),
    job({ id: "j2", detail: { graph: "a" }, running: false, status: "done" }),
    job({ id: "j1", detail: { graph: "a" }, running: false, status: "failed" }),
  ];

  it("the running one, if any", () => {
    expect(runningFor(jobs, "b")?.id).toBe("j3");
    expect(runningFor(jobs, "a")).toBeUndefined();
  });

  it("the newest one for the run tab", () => {
    expect(latestFor(jobs, "a")?.id).toBe("j2");
    expect(latestFor(jobs, "c")).toBeUndefined();
  });

  it("the first running one of any graph for the strip", () => {
    expect(firstRunning(jobs)?.id).toBe("j3");
    expect(firstRunning(jobs.slice(1))).toBeUndefined();
  });
});

describe("the scopes", () => {
  it("are sent in the order the graph declares them", () => {
    expect(tickedScopes(["smoke", "val"], new Set(["val", "smoke"]))).toEqual(["smoke", "val"]);
    expect(tickedScopes(["smoke", "val"], new Set())).toEqual([]);
  });

  it("are named beside the graph in a job's title", () => {
    expect(jobTitle(job())).toBe("g");
    expect(jobTitle(job({ detail: { graph: "g", scopes: ["smoke", "val"] } }))).toBe("g, smoke + val");
    expect(jobTitle(job({ detail: { graph: "g" } }))).toBe("g");
  });
});

describe("what a run deletes", () => {
  const plan = (actions: string[], steps: RunPlan[] = []): RunPlan => ({
    title: "p",
    checks: [],
    actions,
    commands: [],
    details: {},
    steps,
  });

  it("is every delete action of the plan, in order", () => {
    expect(deleteLines(plan(["delete /d/train/images (2 files, 1.0K)", "create /d", "delete /d/train/labels (2 files, 90B)"]))).toEqual([
      "delete /d/train/images (2 files, 1.0K)",
      "delete /d/train/labels (2 files, 90B)",
    ]);
  });

  it("includes the plans it holds", () => {
    expect(deleteLines(plan(["delete /a (1 file, 1B)"], [plan(["run X"]), plan(["delete /b (3 files, 2B)"])]))).toEqual([
      "delete /a (1 file, 1B)",
      "delete /b (3 files, 2B)",
    ]);
  });

  it("is nothing for a run that deletes nothing", () => {
    expect(deleteLines(plan(["keep /d: it holds its marker"]))).toEqual([]);
  });
});
