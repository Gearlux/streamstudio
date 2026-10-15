/**
 * Shift+click ADDS a card to the selection (DESIGN2 §4, the user's "match" — ComfyUI's README: "`Ctrl`/`Shift` +
 * `Click` — Add clicked node to selection"). Measured before (Mac): click `mask`, Shift+click `grey` → the selection
 * stayed `mask` — React Flow's Shift is its box-select key, and its pane swallows a Shift+press even on a card to
 * start a box there. The canvas now takes a Shift+press ON A CARD first and adds that card; everything else stays
 * React Flow's: Shift+drag on empty canvas draws a box, Cmd/Ctrl+click toggles, a plain drag of a selected card
 * moves the selection. The DOM half is measured under playwright.
 */
import { describe, expect, it } from "vitest";
import { addToSelection, shiftAddsCard } from "../selection";

describe("shiftAddsCard — which presses the canvas takes from React Flow", () => {
  const card = { onCard: true, onHandle: false, inField: false };

  it("Shift + the main button on a card's body: added", () => {
    expect(shiftAddsCard({ shiftKey: true, button: 0 }, card)).toBe(true);
  });

  it("Shift+Cmd / Shift+Ctrl on a card still ADDS (Shift never removes)", () => {
    expect(shiftAddsCard({ shiftKey: true, button: 0, metaKey: true }, card)).toBe(true);
    expect(shiftAddsCard({ shiftKey: true, button: 0, ctrlKey: true }, card)).toBe(true);
  });

  it("CON — no Shift: React Flow's own click (select, or Cmd/Ctrl's toggle) and drag", () => {
    expect(shiftAddsCard({ shiftKey: false, button: 0 }, card)).toBe(false);
    expect(shiftAddsCard({ shiftKey: false, button: 0, metaKey: true }, card)).toBe(false);
  });

  it("CON — Shift on empty canvas: React Flow's box select", () => {
    expect(shiftAddsCard({ shiftKey: true, button: 0 }, { onCard: false, onHandle: false, inField: false })).toBe(false);
  });

  it("CON — Shift on a handle, in a field (a typed value, a name being renamed), or with another button", () => {
    expect(shiftAddsCard({ shiftKey: true, button: 0 }, { ...card, onHandle: true })).toBe(false);
    expect(shiftAddsCard({ shiftKey: true, button: 0 }, { ...card, inField: true })).toBe(false);
    expect(shiftAddsCard({ shiftKey: true, button: 2 }, card)).toBe(false);
  });
});

describe("addToSelection — the nodes after a Shift+click", () => {
  const drawn = [
    { id: "mask", type: "generic", selected: true },
    { id: "grey", type: "generic", selected: false },
    { id: "g1", type: "group", selected: false },
  ];

  it("the clicked card joins what is selected; nothing else changes", () => {
    const next = addToSelection(drawn, "grey");
    expect(next.filter((node) => node.selected).map((node) => node.id)).toEqual(["mask", "grey"]);
    expect(next[0]).toBe(drawn[0]); // untouched nodes are the same objects (no redraw for them)
    expect(next[2]).toBe(drawn[2]);
  });

  it("a card already selected stays selected — Shift+click never removes (the SAME array: nothing to redraw)", () => {
    expect(addToSelection(drawn, "mask")).toBe(drawn);
  });

  it("a name no card carries changes nothing", () => {
    expect(addToSelection(drawn, "nope")).toBe(drawn);
  });
});
