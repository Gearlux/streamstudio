/**
 * The page's own timeline: what Ctrl/Cmd+Z reverses next. A DOCUMENT edit is only a marker here — its text lives
 * in the server's history (`POST …/undo`), the one truth an agent's edits share — while a move, an arrange and a
 * grouping are LAYOUT, kept here with their before/after because the document never saw them. The keyboard
 * gestures are measured under playwright in `tests/test_canvas.py`.
 */
import { describe, expect, it } from "vitest";
import { HISTORY_DEPTH, Timeline } from "../history";

const moved = (name: string, from: [number, number], to: [number, number]) => ({
  kind: "positions" as const,
  before: { [name]: from },
  after: { [name]: to },
});

describe("Timeline", () => {
  it("asks the SERVER when the page recorded nothing — an agent's edit or one made before a reload is still undoable", () => {
    const timeline = new Timeline();
    expect(timeline.nextUndo()).toEqual({ kind: "document" });
    expect(timeline.nextRedo()).toEqual({ kind: "document" });
  });

  it("reverses the latest entry first, a move before the document edit made ahead of it", () => {
    const timeline = new Timeline();
    timeline.record({ kind: "document" });
    timeline.record(moved("gain", [40, 40], [160, 120]));
    expect(timeline.nextUndo()).toEqual(moved("gain", [40, 40], [160, 120]));
    timeline.undone();
    expect(timeline.nextUndo()).toEqual({ kind: "document" });
    timeline.undone();
    expect(timeline.depth()).toEqual({ past: 0, future: 2 });
    expect(timeline.nextRedo()).toEqual({ kind: "document" });
    timeline.redone();
    expect(timeline.nextRedo()).toEqual(moved("gain", [40, 40], [160, 120]));
  });

  it("drops what could be redone once something new is recorded", () => {
    const timeline = new Timeline();
    timeline.record(moved("a", [0, 0], [10, 0]));
    timeline.undone();
    timeline.record(moved("b", [0, 0], [0, 10]));
    expect(timeline.depth()).toEqual({ past: 1, future: 0 });
  });

  it("undone/redone on an empty side change nothing (the server answered for an entry the page never held)", () => {
    const timeline = new Timeline();
    timeline.undone();
    timeline.redone();
    expect(timeline.depth()).toEqual({ past: 0, future: 0 });
  });

  it("re-keys a renamed node in every position and every group it holds, on both sides", () => {
    const timeline = new Timeline();
    timeline.record(moved("gain", [0, 0], [10, 10]));
    timeline.record({
      kind: "groups",
      before: [],
      after: [{ id: "g1", title: "t", color: "#fff", nodes: ["decode", "gain"] }],
    });
    timeline.undone();
    timeline.rekey("gain", "amp");
    expect(timeline.nextUndo()).toEqual(moved("amp", [0, 0], [10, 10]));
    expect(timeline.nextRedo()).toEqual({
      kind: "groups",
      before: [],
      after: [{ id: "g1", title: "t", color: "#fff", nodes: ["decode", "amp"] }],
    });
  });

  it("keeps at most HISTORY_DEPTH entries, the oldest dropped", () => {
    const timeline = new Timeline();
    for (let i = 0; i < HISTORY_DEPTH + 5; i++) timeline.record(moved(`n${i}`, [0, 0], [i, 0]));
    expect(timeline.depth()).toEqual({ past: HISTORY_DEPTH, future: 0 });
  });

  it("clear forgets both sides (a graph switch)", () => {
    const timeline = new Timeline();
    timeline.record({ kind: "document" });
    timeline.clear();
    expect(timeline.depth()).toEqual({ past: 0, future: 0 });
  });
});
