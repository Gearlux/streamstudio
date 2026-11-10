/**
 * What a node and a group box need from the page: the form schema per node (`GET api/graphs/<name>/form`), the
 * meet-rule table (`GET api/types`), the latest run (its statuses, wall times and generations are painted on the
 * nodes), the mutations a node or a box performs itself — a parameter, a rename, a group's title/colour, an ungroup,
 * a resize — which title is being renamed in place (F2 and the menus' Rename… open the same field a double-click
 * does), a subgraph node's two needs: opening its inside and naming the steps it holds, a path field's file browser,
 * and a source slot's Set source… (the dialogs are the app's). Everything else
 * (positions, wires, steps, the graph switch, the level shown) is the canvas's or the app's, so a node stays ONE
 * component for every class (docs/rewrite.md §2.3) with no knowledge of routes.
 *
 * Each mutation answers whether the server accepted it; a refusal has already been shown as a toast carrying the
 * server's located message, so a caller only reverts what it drew.
 */
import { createContext, useContext } from "react";
import type { Run } from "./api";
import type { Group, Rect } from "./groups";
import type { HostInfo } from "./host";
import type { Form, Json, Supertypes } from "./model";

/** What is being renamed in place: `node:<name>` (a node's title) or `group:<id>` (a box's title). */
export type Editing = `node:${string}` | `group:${string}` | null;

export interface EditorApi {
  /**
   * The graph shown (`""` while none is) — a node's widgets are keyed by it, so a host's `open` of another graph that
   * holds a node of the same name draws FRESH widgets: measured before, the card kept a value typed in the first graph
   * and committed it into the second when the field was left.
   */
  graph: string;
  /** The app the editor is mounted in (`GET api/workspace`), `null` standalone — the host card's title names it. */
  host: HostInfo | null;
  form: Form;
  types: Supertypes;
  /** The latest run of this graph, or `null` — what the nodes paint their status from. */
  run: Run | null;
  /** `POST api/graphs/<name>/param`. */
  setParam: (node: string, param: string, value: Json) => Promise<boolean>;
  /** `POST api/graphs/<name>/rename` — the key, every reference, the sidecar entry and group membership. */
  rename: (node: string, name: string) => Promise<boolean>;
  /** `PUT api/graphs/<name>/groups` with one group changed (its title or colour). */
  updateGroup: (id: string, patch: Partial<Group>) => Promise<boolean>;
  /** `PUT api/graphs/<name>/groups` without the group; its members stay where they are drawn. */
  ungroup: (id: string) => Promise<boolean>;
  /** A box was resized: it takes the bounds and holds the nodes whose centre it now covers (`groups.resizeGroup`). */
  resizeGroup: (id: string, rect: Rect) => Promise<boolean>;
  /** The title being edited in place (F2, Rename…, a double-click), or `null`. */
  editing: Editing;
  setEditing: (editing: Editing) => void;
  /** Show the inside of the subgraph node `name` (its open button, a double-click, Open in its menu). */
  openSubgraph: (name: string) => void;
  /** The steps inside the subgraph node `name`, by their own names — what its card lists. */
  innerSteps: (name: string) => string[];
  /**
   * The editor's file browser for a field (`…`, `pickers.ts`): a folder (`directory`) or any file (`file`), starting at
   * `start` when given — the path chosen, or `null` when the person cancelled.
   */
  browse: (picker: "directory" | "file", start?: string) => Promise<string | null>;
  /** Set source…: the class picker and form for the slot `slot` of the node `node` (`slots.ts`). */
  chooseSlot: (node: string, slot: string) => void;
}

const refused = async () => false;

export const EditorContext = createContext<EditorApi>({
  graph: "",
  host: null,
  form: {},
  types: {},
  run: null,
  setParam: refused,
  rename: refused,
  updateGroup: refused,
  ungroup: refused,
  resizeGroup: refused,
  editing: null,
  setEditing: () => undefined,
  openSubgraph: () => undefined,
  innerSteps: () => [],
  browse: async () => null,
  chooseSlot: () => undefined,
});

export const useEditor = (): EditorApi => useContext(EditorContext);

/**
 * Whether the level shown is drawn top to bottom (`layout.runsDown`): the canvas provides it, and every card then
 * draws its inputs on its top edge and its outputs on its bottom edge instead of on its sides.
 */
export const DownContext = createContext(false);
