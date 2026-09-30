/**
 * The menus as data: what the menu bar and the three right-click menus (empty canvas, a node, a group) list, which
 * items are greyed, which hooks they carry. The DOM half — opening on click and hover, submenus, Escape — is measured
 * under playwright in `tests/test_canvas.py`.
 */
import { describe, expect, it, vi } from "vitest";
import type { CommandState } from "../commands";
import { GROUP_COLORS } from "../groups";
import type { Entry, ItemEntry, SubmenuEntry } from "../menu/entries";
import { barMenus, groupMenu, nodeMenu, paneMenu, type MenuContext } from "../menus";
import type { ModelNode } from "../model";

const state: CommandState = {
  graph: true,
  dirty: true,
  selected: 1,
  groupSelected: false,
  clipboard: false,
  run: false,
  paused: false,
  busy: false,
  traced: false,
  sidebar: false,
  minimap: true,
};

const context = (extra: Partial<MenuContext> = {}): MenuContext => ({
  mac: false,
  state,
  run: vi.fn(),
  templates: [
    { name: "prep", path: "/t/prep.steps.yaml", steps: ["grey", "scaled"], doc: "grey then scale" },
    { name: "mask", path: "/t/mask.steps.yaml", steps: ["threshold"], doc: "" },
  ],
  insertTemplate: vi.fn(),
  deleteTemplate: vi.fn(),
  ...extra,
});

const labels = (entries: Entry[]): string[] =>
  entries.filter((entry) => entry.type === "item" || entry.type === "submenu").map((entry) => (entry as ItemEntry).label);

const item = (entries: Entry[], label: string): ItemEntry =>
  entries.find((entry) => entry.type === "item" && entry.label === label) as ItemEntry;

const submenu = (entries: Entry[], label: string): SubmenuEntry =>
  entries.find((entry) => entry.type === "submenu" && entry.label === label) as SubmenuEntry;

const node = (kind: ModelNode["kind"]): ModelNode => ({
  name: "prep",
  class_path: "recordstream.flow.subgraph.Subgraph",
  kind,
  params: {},
  inputs: [],
  outputs: [],
  loc: "",
  source_file: "",
  note: "",
  alias_of: "",
});

describe("barMenus", () => {
  it("lists the six menus with their items, a separator where the table has one", () => {
    const menus = barMenus(context());
    expect(menus.map((menu) => menu.id)).toEqual(["new", "file", "edit", "view", "graph", "run"]);
    const file = menus.find((menu) => menu.id === "file")!;
    expect(file.entries.map((entry) => (entry.type === "separator" ? "-" : (entry as ItemEntry).label))).toEqual([
      "Open…",
      "-",
      "Save",
      "Save As…",
      "-",
      "Revert",
    ]);
  });

  it("an item carries data-command, the old data-action, its shortcut and whether it can act", () => {
    const edit = barMenus(context()).find((menu) => menu.id === "edit")!.entries;
    const undo = item(edit, "Undo");
    expect(undo.attrs).toEqual({ "data-command": "undo", "data-action": "undo" });
    expect(undo.shortcut).toBe("Ctrl+Z");
    expect(item(edit, "Paste").disabled).toBe(true); // nothing copied
    expect(item(edit, "Copy").disabled).toBe(false); // one node selected
  });

  it("runs the command when picked", () => {
    const run = vi.fn();
    const file = barMenus(context({ run })).find((menu) => menu.id === "file")!.entries;
    item(file, "Save").onSelect();
    expect(run).toHaveBeenCalledWith("save");
  });

  it("ticks the toggles", () => {
    const view = barMenus(context()).find((menu) => menu.id === "view")!.entries;
    expect(item(view, "Minimap").checked).toBe(true);
    expect(item(view, "Nodes sidebar").checked).toBe(false);
  });

  it("fills the template slots from GET api/templates: insert one, delete one", () => {
    const insertTemplate = vi.fn();
    const deleteTemplate = vi.fn();
    const graph = barMenus(context({ insertTemplate, deleteTemplate })).find((menu) => menu.id === "graph")!.entries;
    const insert = submenu(graph, "Insert template");
    expect(labels(insert.items!)).toEqual(["prep", "mask"]);
    expect((insert.items![0] as ItemEntry).attrs).toEqual({ "data-template": "prep" });
    (insert.items![1] as ItemEntry).onSelect();
    expect(insertTemplate).toHaveBeenCalledWith("mask");
    const remove = submenu(graph, "Delete template");
    (remove.items![0] as ItemEntry).onSelect();
    expect(deleteTemplate).toHaveBeenCalledWith("prep");
  });

  it("says so when there is no template yet, and while the list is loading", () => {
    const none = barMenus(context({ templates: [] })).find((menu) => menu.id === "graph")!.entries;
    expect(submenu(none, "Insert template").items).toEqual([
      { type: "header", key: "none", label: "no templates yet — Save selection as template… makes one" },
    ]);
    const loading = barMenus(context({ templates: null })).find((menu) => menu.id === "graph")!.entries;
    expect(submenu(loading, "Insert template").items).toEqual([{ type: "header", key: "loading", label: "loading…" }]);
  });
});

describe("paneMenu — right-click on empty canvas", () => {
  it("lists Add Node ▸, Add Group, Paste, Insert template ▸, Insert steps file…, Arrange", () => {
    const entries = paneMenu({ ...context(), addNode: () => null, addGroup: vi.fn(), insertStepsFile: vi.fn() });
    expect(labels(entries)).toEqual(["Add Node", "Add Group", "Paste", "Insert template", "Insert steps file…", "Arrange"]);
    expect(submenu(entries, "Add Node").panel).toBeTypeOf("function");
  });
});

describe("nodeMenu — right-click on a node", () => {
  const actions = { runTo: vi.fn(), extract: vi.fn() };

  it("acts on the node: rename, delete, copy, group, convert, template, run to it", () => {
    const entries = nodeMenu({ ...context(), node: node("op"), ...actions });
    expect(labels(entries)).toEqual([
      "Rename…",
      "Delete",
      "Copy",
      "Group",
      "Convert to subgraph",
      "Save as template…",
      "Run to here",
    ]);
  });

  it("offers Open and Move to its own file… on a subgraph node", () => {
    const openSubgraph = vi.fn();
    const entries = nodeMenu({ ...context(), node: node("subgraph"), ...actions, openSubgraph });
    expect(labels(entries).slice(-2)).toEqual(["Open", "Move to its own file…"]);
    item(entries, "Open").onSelect();
    expect(openSubgraph).toHaveBeenCalled();
    expect(item(entries, "Move to its own file…").attrs).toEqual({ "data-command": "extract" });
  });

  it("on a subgraph's <in> or <out> offers nothing to run — it is where the record enters or leaves, not a step", () => {
    const boundary = { ...node("boundary"), name: "prep/<in>", class_path: "" };
    const entries = nodeMenu({ ...context(), node: boundary, ...actions });
    expect(entries).toHaveLength(1);
    expect(entries[0].type).toBe("header");
    expect((entries[0] as { label: string }).label).toBe("<in> — where the record enters prep; not a step");
    const out = nodeMenu({ ...context(), node: { ...boundary, name: "prep/inner/<out>" }, ...actions });
    expect((out[0] as { label: string }).label).toBe("<out> — where the record leaves prep/inner; not a step");
  });

  it("greys Open, and says why, when the server sent no inside for the subgraph", () => {
    const entries = nodeMenu({ ...context(), node: node("subgraph"), ...actions });
    expect(item(entries, "Open").disabled).toBe(true);
    expect(item(entries, "Open").hint).toBe("the editor sent no inside for this subgraph");
  });
});

describe("groupMenu — right-click on a group's title", () => {
  it("renames, recolours, ungroups, converts", () => {
    const setColor = vi.fn();
    const entries = groupMenu({
      color: GROUP_COLORS[0],
      rename: vi.fn(),
      setColor,
      ungroup: vi.fn(),
      convert: vi.fn(),
    });
    expect(labels(entries)).toEqual(["Rename…", "Colour", "Ungroup", "Convert group to subgraph"]);
    const colours = submenu(entries, "Colour").items as ItemEntry[];
    expect(colours.map((entry) => entry.swatch)).toEqual(GROUP_COLORS);
    expect(colours.map((entry) => entry.label)).toEqual(["Blue", "Green", "Amber", "Red", "Violet", "Pink", "Teal", "Slate"]);
    expect(colours[0].checked).toBe(true);
    colours[2].onSelect();
    expect(setColor).toHaveBeenCalledWith(GROUP_COLORS[2]);
  });
});
