/**
 * Shift+click ADDS a card to the selection, the pure half (DESIGN2 §4 — the user's "match": ComfyUI's README,
 * "`Ctrl`/`Shift` + `Click` — Add clicked node to selection", Ctrl read as Cmd on macOS).
 *
 * React Flow's Shift is its box-select key, and while Shift is held its pane takes EVERY press — one on a card too —
 * to start a box there (measured on a Mac: click `mask`, Shift+click `grey` → the selection stayed `mask`). The canvas
 * therefore takes a Shift+press on a card's body FIRST (a capture listener outside React Flow's pane) and adds that
 * card; every other press stays React Flow's:
 *
 *     Shift + click on a card          → added (never removed — Shift+click on a selected card leaves it selected)
 *     Cmd (macOS) / Ctrl + click       → React Flow's toggle (a second one removes the card: the one way out)
 *     Shift + drag on EMPTY canvas     → React Flow's box
 *     drag a selected card             → React Flow moves the whole selection — Shift still held, too (P9: the press
 *                                        on a selected card goes on to React Flow's drag; the canvas only keeps the
 *                                        pane from taking it for a box)
 *     Ctrl + click on macOS            → the browser's right-click: the node menu
 */

/** The buttons and keys of a press. */
export interface PressKeys {
  shiftKey: boolean;
  button: number;
  metaKey?: boolean;
  ctrlKey?: boolean;
}

/** Where a press landed. */
export interface PressTarget {
  /** On a card (a document node's box). */
  onCard: boolean;
  /** On a card's handle (a socket): a wire starts there, never a selection. */
  onHandle: boolean;
  /** In a field, a select or a button on the card (a value typed, a name renamed). */
  inField: boolean;
}

/** Whether the canvas takes this press from React Flow and ADDS the card under it to the selection. */
export const shiftAddsCard = (keys: PressKeys, target: PressTarget): boolean =>
  keys.shiftKey && keys.button === 0 && target.onCard && !target.onHandle && !target.inField;

/**
 * The drawn nodes with `id` selected as well: the SAME array when it already is (or no node carries it), so React
 * Flow has nothing to redraw; untouched nodes stay the same objects.
 */
export function addToSelection<N extends { id: string; selected?: boolean }>(nodes: N[], id: string): N[] {
  const index = nodes.findIndex((node) => node.id === id);
  if (index < 0 || nodes[index].selected) return nodes;
  return nodes.map((node, i) => (i === index ? { ...node, selected: true } : node));
}
