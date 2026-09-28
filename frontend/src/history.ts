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
 * edit is recorded as a bare marker (its content is the server's), a layout change with its before and after.
 * When the page recorded nothing — after a reload, or for an edit an agent made — the next undo still asks the
 * server, whose history may hold it. A new entry drops what could be redone on the PAGE's side; the server keeps
 * its own redo until its next edit, so a redo past the page's entries still asks it.
 */
import type { Group } from "./groups";
import type { Positions } from "./model";

/** How many entries the page keeps — the server's own depth is `history_depth` (50 by default). */
export const HISTORY_DEPTH = 100;

export type TimelineEntry =
  | { kind: "document" }
  | { kind: "positions"; before: Positions; after: Positions }
  | { kind: "groups"; before: Group[]; after: Group[] };

const DOCUMENT: TimelineEntry = { kind: "document" };

const rekeyPositions = (positions: Positions, old: string, name: string): Positions =>
  Object.fromEntries(Object.entries(positions).map(([key, xy]) => [key === old ? name : key, xy]));

const rekeyGroups = (groups: Group[], old: string, name: string): Group[] =>
  groups.map((group) => (group.nodes ? { ...group, nodes: group.nodes.map((n) => (n === old ? name : n)) } : group));

function rekeyEntry(entry: TimelineEntry, old: string, name: string): TimelineEntry {
  if (entry.kind === "positions")
    return { kind: "positions", before: rekeyPositions(entry.before, old, name), after: rekeyPositions(entry.after, old, name) };
  if (entry.kind === "groups")
    return { kind: "groups", before: rekeyGroups(entry.before, old, name), after: rekeyGroups(entry.after, old, name) };
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

  /** A node was renamed (the server re-keyed the sidecar): every layout entry follows it. */
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
