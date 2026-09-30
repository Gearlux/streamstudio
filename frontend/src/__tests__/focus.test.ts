/**
 * The dialogs keep the keyboard (P5): Tab and Shift+Tab cycle through the dialog's own controls, so nothing behind
 * the question — the menu bar, the graph picker — can be reached before it is answered. Measured before: the fourth
 * Tab left the recovery question for the New menu, and a graph was made while the question was still asked.
 */
import { describe, expect, it } from "vitest";
import { nextFocus } from "../focus";

describe("nextFocus — which control of a dialog Tab moves to", () => {
  it("forward from the last wraps to the first; backward from the first wraps to the last", () => {
    expect(nextFocus(3, 2, false)).toBe(0);
    expect(nextFocus(3, 0, true)).toBe(2);
    expect(nextFocus(3, 0, false)).toBe(1);
    expect(nextFocus(3, 2, true)).toBe(1);
  });

  it("focus outside the dialog comes back in: to the first (Tab) or the last (Shift+Tab)", () => {
    expect(nextFocus(3, -1, false)).toBe(0);
    expect(nextFocus(3, -1, true)).toBe(2);
  });

  it("a dialog with nothing to focus: nowhere", () => {
    expect(nextFocus(0, -1, false)).toBe(-1);
  });
});
