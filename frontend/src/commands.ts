/**
 * The menu bar and the keyboard map as ONE table: what each command is called, the keys that run it, the menu that
 * shows it and when it can act. The menu bar (`menu/MenuBar.tsx`), the right-click menus and the page's key handler
 * all read it, so an item and its shortcut can never disagree, and an item is greyed exactly when its key does
 * nothing.
 *
 * The scope (the approved "menu scope", ComfyUI style — only entries that do something):
 *
 *     New       Flow graph… | Pipeline… | Source graph (with its contract)…
 *     File      Open… Ctrl+O · Save Ctrl+S · Save As… Ctrl+Shift+S · Revert
 *     Edit      Undo Ctrl+Z · Redo Ctrl+Shift+Z · Cut / Copy / Paste · Delete · Select all · Rename… F2
 *     View      Nodes sidebar N · Fit to view · Arrange · Minimap
 *     Graph     Add group · Group selection G · Convert selection to subgraph · templates · Check graph
 *     Run       Run Ctrl+Enter · Step · Rerun selected node
 *
 * Ctrl and Cmd are the same modifier (a Mac person types Cmd, anyone else Ctrl). Every key is ignored while a field
 * has the focus — the page's key handler checks that before it asks `matchShortcut`.
 */

export const MENU_IDS = ["new", "file", "edit", "view", "graph", "run"] as const;
export type MenuId = (typeof MENU_IDS)[number];

export const COMMAND_IDS = [
  "new-flow",
  "new-pipeline",
  "new-source",
  "open",
  "save",
  "save-as",
  "revert",
  "undo",
  "redo",
  "cut",
  "copy",
  "paste",
  "delete",
  "select-all",
  "rename",
  "toggle-nodes",
  "fit",
  "arrange",
  "arrange-server",
  "minimap",
  "add-group",
  "group",
  "to-subgraph",
  "save-template",
  "check",
  "run",
  "step",
  "rerun",
] as const;
export type CommandId = (typeof COMMAND_IDS)[number];

/**
 * One key chord. `key` is `KeyboardEvent.key` — a letter in lower case (compared case-blind, so Shift and caps lock
 * do not change it), or a named key (`Enter`, `Delete`, `Backspace`, `F2`). `mod` is Ctrl OR Cmd.
 */
export interface Shortcut {
  key: string;
  mod?: boolean;
  shift?: boolean;
}

export interface Command {
  id: CommandId;
  /** The menu label; `…` when the command asks something before it acts. */
  label: string;
  /** The chords that run it; the first is the one a menu shows. */
  shortcuts: Shortcut[];
  /** A `data-action` the item carries as well — the hook the browser tests used for the toolbar button it replaced. */
  action?: string;
  /** A line of help: the item's tooltip. */
  hint?: string;
}

const command = (id: CommandId, label: string, shortcuts: Shortcut[] = [], extra: Partial<Command> = {}): Command => ({
  id,
  label,
  shortcuts,
  ...extra,
});

export const COMMANDS: Record<CommandId, Command> = {
  "new-flow": command("new-flow", "Flow graph…", [], { hint: "a flow: of named steps, each reading the one before" }),
  "new-pipeline": command("new-pipeline", "Pipeline…", [], { hint: "a Stream with an ops: list" }),
  "new-source": command("new-source", "Source graph (with its contract)…", [], {
    hint: "a flow plus the contract naming what it reads and writes",
  }),
  open: command("open", "Open…", [{ key: "o", mod: true }], { hint: "open a .yaml on this machine" }),
  save: command("save", "Save", [{ key: "s", mod: true }], { hint: "write the unsaved changes to the file" }),
  "save-as": command("save-as", "Save As…", [{ key: "s", mod: true, shift: true }], {
    hint: "write this graph to another file; the old one is left as it is",
  }),
  revert: command("revert", "Revert", [], { hint: "drop the unsaved changes and read the file again" }),
  undo: command("undo", "Undo", [{ key: "z", mod: true }], { action: "undo" }),
  redo: command(
    "redo",
    "Redo",
    [
      { key: "z", mod: true, shift: true },
      { key: "y", mod: true },
    ],
    { action: "redo" },
  ),
  cut: command("cut", "Cut", [{ key: "x", mod: true }]),
  copy: command("copy", "Copy", [{ key: "c", mod: true }]),
  paste: command("paste", "Paste", [{ key: "v", mod: true }], { hint: "after the selected node" }),
  delete: command("delete", "Delete", [{ key: "Delete" }, { key: "Backspace" }]),
  "select-all": command("select-all", "Select all", [{ key: "a", mod: true }]),
  rename: command("rename", "Rename…", [{ key: "F2" }]),
  "toggle-nodes": command("toggle-nodes", "Nodes sidebar", [{ key: "n" }], { hint: "every class, by package and kind" }),
  fit: command("fit", "Fit to view"),
  arrange: command("arrange", "Arrange", [], { action: "arrange", hint: "left to right by the cards' sizes" }),
  "arrange-server": command("arrange-server", "Arrange in columns", [], {
    action: "arrange-server",
    hint: "the server's layered layout: a column per layer",
  }),
  minimap: command("minimap", "Minimap"),
  "add-group": command("add-group", "Add group", [], { hint: "an empty box; drag nodes into it" }),
  group: command("group", "Group selection", [{ key: "g" }], { action: "group" }),
  "to-subgraph": command("to-subgraph", "Convert selection to subgraph", [], {
    hint: "consecutive steps become one subgraph step",
  }),
  "save-template": command("save-template", "Save selection as template…", [], {
    hint: "a copy anyone can insert later; nothing links back",
  }),
  check: command("check", "Check graph", [], { hint: "every problem the document has, with its line" }),
  run: command("run", "Run", [{ key: "Enter", mod: true }], { hint: "one record, pausing before the selected node" }),
  step: command("step", "Step", [], { hint: "one node further" }),
  rerun: command("rerun", "Rerun selected node", [], { hint: "rebuild it and recompute from there" }),
};

/** A dynamic submenu the page fills (the template lists come from `GET api/templates`). */
export interface MenuSlot {
  slot: "insert-template" | "delete-template";
  label: string;
}

export type MenuItem = CommandId | "-" | MenuSlot;

export interface MenuSpec {
  id: MenuId;
  label: string;
  items: MenuItem[];
}

export const MENUS: MenuSpec[] = [
  { id: "new", label: "New", items: ["new-flow", "new-pipeline", "new-source"] },
  { id: "file", label: "File", items: ["open", "-", "save", "save-as", "-", "revert"] },
  {
    id: "edit",
    label: "Edit",
    items: ["undo", "redo", "-", "cut", "copy", "paste", "delete", "-", "select-all", "rename"],
  },
  { id: "view", label: "View", items: ["toggle-nodes", "-", "fit", "arrange", "arrange-server", "minimap"] },
  {
    id: "graph",
    label: "Graph",
    items: [
      "add-group",
      "group",
      "-",
      "to-subgraph",
      "-",
      { slot: "insert-template", label: "Insert template" },
      "save-template",
      { slot: "delete-template", label: "Delete template" },
      "-",
      "check",
    ],
  },
  { id: "run", label: "Run", items: ["run", "step", "rerun"] },
];

// ---------------------------------------------------------------------------------------------------------------
// keys
// ---------------------------------------------------------------------------------------------------------------

/** What `matchShortcut` reads of a `KeyboardEvent`. */
export interface KeyLike {
  key: string;
  ctrlKey: boolean;
  metaKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
}

const sameKey = (a: string, b: string): boolean => a.toLowerCase() === b.toLowerCase();

/** The command a key chord runs, or `undefined`. Alt never runs one; Shift and Ctrl/Cmd must be exactly as declared. */
export function matchShortcut(event: KeyLike): CommandId | undefined {
  if (event.altKey) return undefined;
  const mod = event.ctrlKey || event.metaKey;
  for (const id of COMMAND_IDS) {
    for (const shortcut of COMMANDS[id].shortcuts) {
      if (sameKey(event.key, shortcut.key) && mod === Boolean(shortcut.mod) && event.shiftKey === Boolean(shortcut.shift))
        return id;
    }
  }
  return undefined;
}

const MAC_KEYS: Record<string, string> = { Enter: "↩", Delete: "⌦", Backspace: "⌫" };

/** A chord as a menu shows it: `⇧⌘S` on a Mac, `Ctrl+Shift+S` elsewhere; a plain key is itself (`F2`, `N`). */
export function shortcutLabel(shortcut: Shortcut, mac: boolean): string {
  const named = shortcut.key.length > 1;
  const key = named ? shortcut.key : shortcut.key.toUpperCase();
  if (!shortcut.mod && !shortcut.shift) return key;
  if (mac) return `${shortcut.shift ? "⇧" : ""}${shortcut.mod ? "⌘" : ""}${MAC_KEYS[key] ?? key}`;
  return [shortcut.mod ? "Ctrl" : "", shortcut.shift ? "Shift" : "", key].filter(Boolean).join("+");
}

/** Whether the page runs on a Mac — which spelling `shortcutLabel` uses. */
export const isMac = (): boolean =>
  typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

// ---------------------------------------------------------------------------------------------------------------
// when a command can act
// ---------------------------------------------------------------------------------------------------------------

/** What `enabled` and `checked` read of the page. */
export interface CommandState {
  /** A graph is shown. */
  graph: boolean;
  /** It holds changes not yet written. */
  dirty: boolean;
  /** How many document nodes are selected. */
  selected: number;
  /** A group box is selected. */
  groupSelected: boolean;
  /** Something was copied. */
  clipboard: boolean;
  /** A run of this graph exists. */
  run: boolean;
  /** That run is paused. */
  paused: boolean;
  /** A run is in flight. */
  busy: boolean;
  /** The selected node is one the run traced. */
  traced: boolean;
  /** The Nodes sidebar is open. */
  sidebar: boolean;
  /** The minimap is shown. */
  minimap: boolean;
}

const ALWAYS: CommandId[] = ["new-flow", "new-pipeline", "new-source", "open", "toggle-nodes"];

export function enabled(id: CommandId, state: CommandState): boolean {
  if (ALWAYS.includes(id)) return true;
  if (!state.graph) return false;
  switch (id) {
    case "save":
    case "revert":
      return state.dirty;
    case "cut":
    case "copy":
    case "group":
    case "to-subgraph":
    case "save-template":
      return state.selected > 0;
    case "delete":
      return state.selected > 0 || state.groupSelected;
    case "rename":
      return state.selected === 1 || (state.selected === 0 && state.groupSelected);
    case "paste":
      return state.clipboard;
    case "run":
      return !state.busy;
    case "step":
      return state.run && state.paused && !state.busy;
    case "rerun":
      return state.run && state.traced && !state.busy;
    default:
      return true;
  }
}

/** A toggle's tick: `undefined` for a command that is not a toggle. */
export function checked(id: CommandId, state: CommandState): boolean | undefined {
  if (id === "toggle-nodes") return state.sidebar;
  if (id === "minimap") return state.minimap;
  return undefined;
}
