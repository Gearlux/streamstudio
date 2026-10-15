/**
 * A menu as data — what `MenuList` draws, for the menu bar and the right-click menus alike. An item runs `onSelect`
 * and closes the whole menu; a submenu opens on hover (or click) to the right, with a ▸; a separator is a rule; a
 * header is a greyed line (an empty list says so rather than showing nothing).
 *
 * `attrs` are the `data-*` hooks the element carries (`data-command="save"`, `data-action="undo"`,
 * `data-template="prep"`), so a browser test clicks by meaning, never by label.
 */
import type { ReactNode } from "react";

export interface ItemEntry {
  type: "item";
  key: string;
  label: string;
  /** Right-aligned: the chord that runs the same command. */
  shortcut?: string;
  /** The tooltip. */
  hint?: string;
  disabled?: boolean;
  /** A toggle's tick; `undefined` for an item that is not a toggle. */
  checked?: boolean;
  /** A colour square before the label (the group colours). */
  swatch?: string;
  attrs?: Record<string, string>;
  onSelect: () => void;
}

export interface SubmenuEntry {
  type: "submenu";
  key: string;
  label: string;
  hint?: string;
  disabled?: boolean;
  attrs?: Record<string, string>;
  /** The submenu's entries… */
  items?: Entry[];
  /** …or a panel of its own (Add Node's search), given the function that closes the whole menu. */
  panel?: (close: () => void) => ReactNode;
}

export interface SeparatorEntry {
  type: "separator";
  key: string;
}

export interface HeaderEntry {
  type: "header";
  key: string;
  label: string;
}

export type Entry = ItemEntry | SubmenuEntry | SeparatorEntry | HeaderEntry;

let separators = 0;
/** A rule between two runs of items. */
export const separator = (): SeparatorEntry => ({ type: "separator", key: `sep${++separators}` });
