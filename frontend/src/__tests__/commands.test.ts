/**
 * The menu bar and the keyboard map, the pure half: ONE table says what each command is called, which key runs it
 * and which menu shows it, so a menu item and its shortcut can never disagree. The DOM half (a menu opening, an
 * item greyed) is measured under playwright in `tests/test_canvas.py`.
 */
import { describe, expect, it } from "vitest";
import {
  COMMANDS,
  COMMAND_IDS,
  MENUS,
  MENU_IDS,
  checked,
  enabled,
  matchShortcut,
  shortcutLabel,
  type CommandId,
  type CommandState,
} from "../commands";

const idle: CommandState = {
  graph: true,
  dirty: false,
  selected: 0,
  groupSelected: false,
  clipboard: false,
  run: false,
  paused: false,
  busy: false,
  traced: false,
  sidebar: false,
  minimap: true,
};

const key = (k: string, mods: Partial<{ ctrl: boolean; meta: boolean; shift: boolean; alt: boolean }> = {}) => ({
  key: k,
  ctrlKey: mods.ctrl ?? false,
  metaKey: mods.meta ?? false,
  shiftKey: mods.shift ?? false,
  altKey: mods.alt ?? false,
});

describe("the menu layout", () => {
  it("has the six menus of the approved scope, in order", () => {
    expect(MENU_IDS).toEqual(["new", "file", "edit", "view", "graph", "run"]);
    expect(MENUS.map((menu) => menu.id)).toEqual([...MENU_IDS]);
    expect(MENUS.map((menu) => menu.label)).toEqual(["New", "File", "Edit", "View", "Graph", "Run"]);
  });

  it("shows every command exactly once — nothing unreachable, nothing twice", () => {
    const shown = MENUS.flatMap((menu) => menu.items).filter((item): item is CommandId => typeof item === "string" && item !== "-");
    expect([...shown].sort()).toEqual([...COMMAND_IDS].sort());
    expect(new Set(shown).size).toBe(shown.length);
  });

  it("puts each command where the scope says", () => {
    const where = (id: CommandId) => MENUS.find((menu) => menu.items.includes(id))?.id;
    expect(where("save")).toBe("file");
    expect(where("undo")).toBe("edit");
    expect(where("rename")).toBe("edit");
    expect(where("toggle-nodes")).toBe("view");
    expect(where("arrange")).toBe("view");
    expect(where("group")).toBe("graph");
    expect(where("to-subgraph")).toBe("graph");
    expect(where("check")).toBe("graph");
    expect(where("run")).toBe("run");
    expect(where("new-source")).toBe("new");
  });

  it("names the template submenus as slots the page fills from GET api/templates", () => {
    const graph = MENUS.find((menu) => menu.id === "graph")!;
    const slots = graph.items.filter((item) => typeof item === "object").map((item) => (item as { slot: string }).slot);
    expect(slots).toEqual(["insert-template", "delete-template"]);
  });

  it("ends a label with … exactly when the command asks something first", () => {
    const asking: CommandId[] = ["new-flow", "new-pipeline", "new-source", "open", "save-as", "rename", "save-template"];
    for (const id of COMMAND_IDS) expect(COMMANDS[id].label.endsWith("…"), id).toBe(asking.includes(id));
  });

  it("keeps the toolbar's data-action hooks on the commands that replaced its buttons", () => {
    const actions = Object.fromEntries(COMMAND_IDS.filter((id) => COMMANDS[id].action).map((id) => [id, COMMANDS[id].action]));
    expect(actions).toEqual({
      undo: "undo",
      redo: "redo",
      group: "group",
      arrange: "arrange",
      "arrange-server": "arrange-server",
    });
  });
});

describe("matchShortcut — the keyboard map of the scope", () => {
  it("Ctrl or Cmd run the same command", () => {
    expect(matchShortcut(key("s", { ctrl: true }))).toBe("save");
    expect(matchShortcut(key("s", { meta: true }))).toBe("save");
    expect(matchShortcut(key("o", { ctrl: true }))).toBe("open");
  });

  it("Shift tells Save As from Save, and redo from undo (the key arrives upper-cased)", () => {
    expect(matchShortcut(key("S", { ctrl: true, shift: true }))).toBe("save-as");
    expect(matchShortcut(key("z", { ctrl: true }))).toBe("undo");
    expect(matchShortcut(key("Z", { meta: true, shift: true }))).toBe("redo");
    expect(matchShortcut(key("y", { ctrl: true }))).toBe("redo");
  });

  it("maps the edit keys", () => {
    expect(matchShortcut(key("x", { ctrl: true }))).toBe("cut");
    expect(matchShortcut(key("c", { meta: true }))).toBe("copy");
    expect(matchShortcut(key("v", { ctrl: true }))).toBe("paste");
    expect(matchShortcut(key("a", { ctrl: true }))).toBe("select-all");
    expect(matchShortcut(key("Delete"))).toBe("delete");
    expect(matchShortcut(key("Backspace"))).toBe("delete");
    expect(matchShortcut(key("F2"))).toBe("rename");
  });

  it("maps the plain letters and Ctrl+Enter", () => {
    expect(matchShortcut(key("n"))).toBe("toggle-nodes");
    expect(matchShortcut(key("g"))).toBe("group");
    expect(matchShortcut(key("G"))).toBe("group"); // caps lock
    expect(matchShortcut(key("Enter", { ctrl: true }))).toBe("run");
  });

  it("answers nothing for a key the map does not hold, or with a modifier it does not ask for", () => {
    expect(matchShortcut(key("Enter"))).toBeUndefined();
    expect(matchShortcut(key("g", { alt: true }))).toBeUndefined();
    expect(matchShortcut(key("g", { ctrl: true }))).toBeUndefined();
    expect(matchShortcut(key("N", { shift: true }))).toBeUndefined();
    expect(matchShortcut(key("q", { ctrl: true }))).toBeUndefined();
  });
});

describe("shortcutLabel — right-aligned in a menu item", () => {
  it("spells the modifiers the platform's way", () => {
    expect(shortcutLabel(COMMANDS.save.shortcuts[0], false)).toBe("Ctrl+S");
    expect(shortcutLabel(COMMANDS.save.shortcuts[0], true)).toBe("⌘S");
    expect(shortcutLabel(COMMANDS["save-as"].shortcuts[0], false)).toBe("Ctrl+Shift+S");
    expect(shortcutLabel(COMMANDS["save-as"].shortcuts[0], true)).toBe("⇧⌘S");
    expect(shortcutLabel(COMMANDS.run.shortcuts[0], false)).toBe("Ctrl+Enter");
    expect(shortcutLabel(COMMANDS.run.shortcuts[0], true)).toBe("⌘↩");
  });

  it("shows a plain key as itself", () => {
    expect(shortcutLabel(COMMANDS.rename.shortcuts[0], true)).toBe("F2");
    expect(shortcutLabel(COMMANDS["toggle-nodes"].shortcuts[0], false)).toBe("N");
    expect(shortcutLabel(COMMANDS.delete.shortcuts[0], false)).toBe("Delete");
  });
});

describe("enabled — an item is greyed when it cannot act", () => {
  it("Save and Revert need unsaved changes; Save As does not", () => {
    expect(enabled("save", idle)).toBe(false);
    expect(enabled("revert", idle)).toBe(false);
    expect(enabled("save-as", idle)).toBe(true);
    expect(enabled("save", { ...idle, dirty: true })).toBe(true);
    expect(enabled("revert", { ...idle, dirty: true })).toBe(true);
  });

  it("a selection gates what acts on it", () => {
    for (const id of ["cut", "copy", "group", "to-subgraph", "save-template", "delete"] as CommandId[]) {
      expect(enabled(id, idle), id).toBe(false);
      expect(enabled(id, { ...idle, selected: 2 }), id).toBe(true);
    }
    expect(enabled("delete", { ...idle, groupSelected: true })).toBe(true);
  });

  it("rename acts on exactly one node, or on a selected group", () => {
    expect(enabled("rename", { ...idle, selected: 1 })).toBe(true);
    expect(enabled("rename", { ...idle, selected: 2 })).toBe(false);
    expect(enabled("rename", { ...idle, groupSelected: true })).toBe(true);
  });

  it("paste needs something copied", () => {
    expect(enabled("paste", idle)).toBe(false);
    expect(enabled("paste", { ...idle, clipboard: true })).toBe(true);
  });

  it("the run commands follow the run", () => {
    expect(enabled("run", idle)).toBe(true);
    expect(enabled("run", { ...idle, busy: true })).toBe(false);
    expect(enabled("step", idle)).toBe(false);
    expect(enabled("step", { ...idle, run: true, paused: true })).toBe(true);
    expect(enabled("rerun", { ...idle, run: true })).toBe(false);
    expect(enabled("rerun", { ...idle, run: true, traced: true })).toBe(true);
  });

  it("with no graph loaded only New and Open act", () => {
    const none = { ...idle, graph: false };
    const acting = COMMAND_IDS.filter((id) => enabled(id, none));
    expect(acting).toEqual(["new-flow", "new-pipeline", "new-source", "open", "toggle-nodes"]);
  });
});

describe("checked — the two toggles show a tick", () => {
  it("follows the sidebar and the minimap", () => {
    expect(checked("toggle-nodes", idle)).toBe(false);
    expect(checked("toggle-nodes", { ...idle, sidebar: true })).toBe(true);
    expect(checked("minimap", idle)).toBe(true);
    expect(checked("minimap", { ...idle, minimap: false })).toBe(false);
    expect(checked("save", idle)).toBeUndefined();
  });
});
