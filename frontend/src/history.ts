/**
 * Undo and redo: TWO histories, one gesture.
 *
 * - A DOCUMENT edit (a parameter, a step, a rename) is undone by the SERVER (`POST …/undo`): the document's text
 *   history lives beside the document (`Document.undo`), shared by the canvas and by an agent's tools, so it is the
 *   one truth — the page never keeps a copy of a text it could restore out of step with the file.
 * - A LAYOUT change (a node moved, an arrange, a group made or edited) never reaches the document (§2.1: the
 *   sidecar holds it), so the server has no history of it; the page keeps its before/after here and undoes it
 *   by writing the before back.
 *
 * This timeline orders the two so Ctrl/Cmd+Z reverses the LATEST thing the person did, whichever kind: a document
 * edit is recorded as a bare marker (its content is the server's), a layout change with its before and after — ONE
 * entry for a gesture that changed positions and groups together (a node dragged into a box), so one undo reverses it.
 * When the page recorded nothing — after a reload, or for an edit an agent made — the next undo still asks the
 * server, whose history may hold it. A new entry drops what could be redone on the PAGE's side; the server keeps
 * its own redo until its next edit, so a redo past the page's entries still asks it.
 */
import type { Group } from "./groups";
import { rekeyName } from "./levels";
import type { Positions } from "./model";

/** How many entries the page keeps — the server's own depth is `history_depth` (50 by default). */
export const HISTORY_DEPTH = 100;

/** Both halves of the sidecar, for a gesture that changed both (a node dragged into or out of a group). */
export interface Layout {
  positions: Positions;
  groups: Group[];
}

export type TimelineEntry =
  /**
   * A document edit; `groups` when the same gesture also re-named group members (a conversion into a subgraph names
   * the new node where its steps were): undo asks the server and then writes `before` back, redo `after` — one entry,
   * so one Ctrl/Cmd+Z reverses the whole gesture and no box is left naming nodes the undo took away.
   */
  | { kind: "document"; groups?: { before: Group[]; after: Group[] } }
  | { kind: "positions"; before: Positions; after: Positions }
  | { kind: "groups"; before: Group[]; after: Group[] }
  | { kind: "layout"; before: Layout; after: Layout };

const DOCUMENT: TimelineEntry = { kind: "document" };

// A rename of a subgraph node moves every name inside it (`prep/grey` → `pre/grey`, `levels.rekeyName`), and every
// group living on its level (`parent: prep`) with it.
const rekeyPositions = (positions: Positions, old: string, name: string): Positions =>
  Object.fromEntries(Object.entries(positions).map(([key, xy]) => [rekeyName(key, old, name), xy]));

const rekeyGroups = (groups: Group[], old: string, name: string): Group[] =>
  groups.map((group) => {
    const nodes = group.nodes ? { nodes: group.nodes.map((n) => rekeyName(n, old, name)) } : {};
    const parent = typeof group.parent === "string" && group.parent !== "" ? { parent: rekeyName(group.parent, old, name) } : {};
    return { ...group, ...nodes, ...parent };
  });

function rekeyEntry(entry: TimelineEntry, old: string, name: string): TimelineEntry {
  if (entry.kind === "positions")
    return { kind: "positions", before: rekeyPositions(entry.before, old, name), after: rekeyPositions(entry.after, old, name) };
  if (entry.kind === "groups")
    return { kind: "groups", before: rekeyGroups(entry.before, old, name), after: rekeyGroups(entry.after, old, name) };
  if (entry.kind === "layout") {
    const rekey = (layout: Layout): Layout => ({
      positions: rekeyPositions(layout.positions, old, name),
      groups: rekeyGroups(layout.groups, old, name),
    });
    return { kind: "layout", before: rekey(entry.before), after: rekey(entry.after) };
  }
  if (entry.groups)
    return { kind: "document", groups: { before: rekeyGroups(entry.groups.before, old, name), after: rekeyGroups(entry.groups.after, old, name) } };
  return entry;
}

export class Timeline {
  private past: TimelineEntry[] = [];
  private future: TimelineEntry[] = [];

  /** Something was done: it becomes the next undo, and the page's redo side is dropped. */
  record(entry: TimelineEntry): void {
    this.past.push(entry);
    if (this.past.length > HISTORY_DEPTH) this.past.splice(0, this.past.length - HISTORY_DEPTH);
    this.future = [];
  }

  /** What the next undo reverses — the server's document history when the page holds nothing. */
  nextUndo(): TimelineEntry {
    return this.past[this.past.length - 1] ?? DOCUMENT;
  }

  /** What the next redo re-applies — the server's document history when the page holds nothing. */
  nextRedo(): TimelineEntry {
    return this.future[this.future.length - 1] ?? DOCUMENT;
  }

  /** The entry `nextUndo` named was reversed. */
  undone(): void {
    const entry = this.past.pop();
    if (entry) this.future.push(entry);
  }

  /** The entry `nextRedo` named was re-applied. */
  redone(): void {
    const entry = this.future.pop();
    if (entry) this.past.push(entry);
  }

  /** A node was renamed (the server re-keyed the sidecar): every layout entry follows it — a subgraph with its inside. */
  rekey(old: string, name: string): void {
    this.past = this.past.map((entry) => rekeyEntry(entry, old, name));
    this.future = this.future.map((entry) => rekeyEntry(entry, old, name));
  }

  clear(): void {
    this.past = [];
    this.future = [];
  }

  depth(): { past: number; future: number } {
    return { past: this.past.length, future: this.future.length };
  }
}
