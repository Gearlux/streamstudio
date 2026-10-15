/**
 * A dialog keeps the keyboard (P5): Tab and Shift+Tab cycle through the dialog's OWN controls. Without it the fourth
 * Tab of the recovery question reached the menu bar behind it (measured: New ▸ Flow graph… made a graph while the
 * question was still asked). The shell (`dialogs/Modal.tsx`) finds the controls; this says which one is next.
 */

/**
 * The index Tab (or Shift+Tab, `backwards`) moves to among `count` controls, from `current` (`-1`: the focus is not on
 * one of them — it comes back in). `-1` when there is nothing to focus.
 */
export function nextFocus(count: number, current: number, backwards: boolean): number {
  if (count === 0) return -1;
  if (current < 0) return backwards ? count - 1 : 0;
  return (current + (backwards ? count - 1 : 1)) % count;
}

/** What a dialog can put the focus on — its buttons, fields and links that are not disabled. */
export const FOCUSABLE = "button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex='-1'])";
