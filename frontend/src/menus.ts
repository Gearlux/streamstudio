/**
 * The menus the page shows, built as data (`menu/entries.ts`) from the command table (`commands.ts`) and the page's
 * state: the menu bar, and the three right-click menus of the approved scope —
 *
 *     empty canvas   Add Node ▸ (by kind, with search) · Add Group · Paste · Insert template ▸ · Insert steps file… · Arrange
 *     a node         Rename… · Delete · Copy · Group · Convert to subgraph · Save as template… · Run to here
 *                    (a node with a source slot — a Stream — also: Set source…, `slots.ts`;
 *                     a subgraph node also: Open · Move to its own file…; a subgraph's <in>/<out>: nothing)
 *     a group        Rename… · Colour ▸ · Ungroup · Convert group to subgraph
 *
 * A right-click on a node first makes it the selection (unless it already is part of it), so the node menu's items
 * are the SAME commands the menu bar runs on the selection — one implementation, one enabled rule. Nothing here draws;
 * `menu/MenuBar.tsx` and `menu/ContextMenu.tsx` do.
 */
import type { ReactNode } from "react";
import type { Template } from "./api";
import { COMMANDS, MENUS, checked, enabled, shortcutLabel, type CommandId, type CommandState, type MenuId, type MenuSlot } from "./commands";
import { COLOR_NAMES, GROUP_COLORS } from "./groups";
import { isBoundary, levelOfNode, shortName } from "./levels";
import { separator, type Entry, type ItemEntry, type SubmenuEntry } from "./menu/entries";
import type { ModelNode } from "./model";

export interface MenuContext {
  /** Spell shortcuts the Mac way. */
  mac: boolean;
  state: CommandState;
  run: (id: CommandId) => void;
  /** `GET api/templates`, or `null` while it loads. */
  templates: Template[] | null;
  insertTemplate: (name: string) => void;
  deleteTemplate: (name: string) => void;
}

/** A command as a menu item: the table's label (or `label`), its first shortcut, greyed when it cannot act. */
export function commandEntry(id: CommandId, context: Pick<MenuContext, "mac" | "state" | "run">, label?: string): ItemEntry {
  const command = COMMANDS[id];
  const shortcut = command.shortcuts[0];
  return {
    type: "item",
    key: id,
    label: label ?? command.label,
    shortcut: shortcut ? shortcutLabel(shortcut, context.mac) : undefined,
    hint: command.hint,
    disabled: !enabled(id, context.state),
    checked: checked(id, context.state),
    attrs: command.action ? { "data-command": id, "data-action": command.action } : { "data-command": id },
    onSelect: () => context.run(id),
  };
}

function templateItems(context: MenuContext, pick: (name: string) => void, verb: string): Entry[] {
  if (context.templates === null) return [{ type: "header", key: "loading", label: "loading…" }];
  if (context.templates.length === 0)
    return [{ type: "header", key: "none", label: "no templates yet — Save selection as template… makes one" }];
  return context.templates.map((template) => ({
    type: "item",
    key: template.name,
    label: template.name,
    hint: `${verb}: ${[template.doc, template.steps.join(" → ")].filter(Boolean).join(" — ")}`,
    attrs: { "data-template": template.name },
    onSelect: () => pick(template.name),
  }));
}

function slotEntry(slot: MenuSlot, context: MenuContext): SubmenuEntry {
  const insert = slot.slot === "insert-template";
  return {
    type: "submenu",
    key: slot.slot,
    label: slot.label,
    disabled: !context.state.graph,
    attrs: { "data-submenu": slot.slot },
    items: insert
      ? templateItems(context, context.insertTemplate, "adds a copy as a subgraph step")
      : templateItems(context, context.deleteTemplate, "deletes the template file"),
  };
}

export interface BarMenu {
  id: MenuId;
  label: string;
  entries: Entry[];
}

/** The menu bar: `MENUS` with every command resolved and the template slots filled. */
export function barMenus(context: MenuContext): BarMenu[] {
  return MENUS.map((menu) => ({
    id: menu.id,
    label: menu.label,
    entries: menu.items.map((item): Entry => {
      if (item === "-") return separator();
      if (typeof item === "object") return slotEntry(item, context);
      return commandEntry(item, context);
    }),
  }));
}

export interface PaneMenuContext extends MenuContext {
  /** Add Node's panel: search plus the kinds, each a list of classes. */
  addNode: (close: () => void) => ReactNode;
  /** An empty box where the canvas was right-clicked. */
  addGroup: () => void;
  /** The file dialog, then `POST …/include`. */
  insertStepsFile: () => void;
}

/** Right-click on empty canvas. */
export function paneMenu(context: PaneMenuContext): Entry[] {
  const graph = context.state.graph;
  return [
    { type: "submenu", key: "add-node", label: "Add Node", disabled: !graph, attrs: { "data-submenu": "add-node" }, panel: context.addNode },
    {
      type: "item",
      key: "add-group",
      label: "Add Group",
      hint: "an empty box here; drag nodes into it",
      disabled: !graph,
      attrs: { "data-command": "add-group" },
      onSelect: context.addGroup,
    },
    commandEntry("paste", context),
    separator(),
    slotEntry({ slot: "insert-template", label: "Insert template" }, context),
    {
      type: "item",
      key: "insert-steps",
      label: "Insert steps file…",
      hint: "a subgraph step that includes a *.steps.yaml",
      disabled: !graph,
      attrs: { "data-command": "insert-steps" },
      onSelect: context.insertStepsFile,
    },
    separator(),
    commandEntry("arrange", context),
  ];
}

export interface NodeMenuContext extends MenuContext {
  /** The node right-clicked (already made the selection). */
  node: ModelNode;
  /** Run one record, pausing before this node. */
  runTo: () => void;
  /** Move a subgraph's steps to their own file (the file dialog first). */
  extract: () => void;
  /** Show a subgraph's inside; absent when the server sent none for it (`model.inside`) — the item is then greyed. */
  openSubgraph?: () => void;
  /** The node's source slots (`slots.sourceSlots`): one Set <slot>… item each, opening the class picker and form. */
  slots?: { slot: string; filled: string | undefined; choose: () => void }[];
}

/** Right-click on a node. A subgraph's `<in>` / `<out>` is where the record enters or leaves it: nothing to run. */
export function nodeMenu(context: NodeMenuContext): Entry[] {
  if (isBoundary(context.node)) {
    const where = levelOfNode(context.node.name);
    const side = shortName(context.node.name) === "<out>" ? "leaves" : "enters";
    return [{ type: "header", key: "boundary", label: `${shortName(context.node.name)} — where the record ${side} ${where}; not a step` }];
  }
  const entries: Entry[] = [
    commandEntry("rename", context),
    commandEntry("delete", context),
    commandEntry("copy", context),
    commandEntry("group", context, "Group"),
    separator(),
    commandEntry("to-subgraph", context, "Convert to subgraph"),
    commandEntry("save-template", context, "Save as template…"),
    separator(),
    {
      type: "item",
      key: "run-to",
      label: "Run to here",
      hint: "run one record through every node before this one and pause there",
      disabled: context.state.busy,
      attrs: { "data-command": "run-to" },
      onSelect: context.runTo,
    },
  ];
  if (context.slots !== undefined && context.slots.length > 0) {
    entries.push(separator());
    for (const slot of context.slots)
      entries.push({
        type: "item",
        key: `set-slot:${slot.slot}`,
        label: `Set ${slot.slot}…`,
        hint: slot.filled === undefined ? `choose a class for the empty ${slot.slot} slot, and its values` : `replace ${slot.filled}: a class and its values`,
        attrs: { "data-command": "set-slot", "data-slot": slot.slot },
        onSelect: slot.choose,
      });
  }
  if (context.node.kind === "subgraph") {
    entries.push(
      separator(),
      {
        type: "item",
        key: "open",
        label: "Open",
        hint: context.openSubgraph ? "show its inside (double-click does the same)" : "the editor sent no inside for this subgraph",
        disabled: context.openSubgraph === undefined,
        attrs: { "data-command": "open-subgraph" },
        onSelect: () => context.openSubgraph?.(),
      },
      {
        type: "item",
        key: "extract",
        label: "Move to its own file…",
        hint: "its steps go to a .steps.yaml the graph includes",
        attrs: { "data-command": "extract" },
        onSelect: context.extract,
      },
    );
  }
  return entries;
}

export interface GroupMenuContext {
  /** The group's colour now — ticked in the Colour submenu. */
  color: string;
  rename: () => void;
  setColor: (color: string) => void;
  ungroup: () => void;
  convert: () => void;
}

/** Right-click on a group's title bar. */
export function groupMenu(context: GroupMenuContext): Entry[] {
  return [
    {
      type: "item",
      key: "rename",
      label: "Rename…",
      shortcut: "F2",
      attrs: { "data-command": "rename-group" },
      onSelect: context.rename,
    },
    {
      type: "submenu",
      key: "colour",
      label: "Colour",
      attrs: { "data-submenu": "colour" },
      items: GROUP_COLORS.map((color) => ({
        type: "item",
        key: color,
        label: COLOR_NAMES[color] ?? color,
        swatch: color,
        checked: color === context.color,
        attrs: { "data-color": color },
        onSelect: () => context.setColor(color),
      })),
    },
    {
      type: "item",
      key: "ungroup",
      label: "Ungroup",
      hint: "the members stay where they are",
      attrs: { "data-command": "ungroup" },
      onSelect: context.ungroup,
    },
    separator(),
    {
      type: "item",
      key: "convert",
      label: "Convert group to subgraph",
      hint: "its members become one subgraph step",
      attrs: { "data-command": "group-to-subgraph" },
      onSelect: context.convert,
    },
  ];
}
