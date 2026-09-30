/**
 * What a node and a group box need from the page: the form schema per node (`GET api/graphs/<name>/form`), the
 * meet-rule table (`GET api/types`), the latest run (its statuses, wall times and generations are painted on the
 * nodes), the mutations a node or a box performs itself — a parameter, a rename, a group's title/colour, an ungroup,
 * a resize — which title is being renamed in place (F2 and the menus' Rename… open the same field a double-click
 * does), and a subgraph node's two needs: opening its inside and naming the steps it holds. Everything else
 * (positions, wires, steps, the graph switch, the level shown) is the canvas's or the app's, so a node stays ONE
 * component for every class (docs/rewrite.md §2.3) with no knowledge of routes.
 *
 * Each mutation answers whether the server accepted it; a refusal has already been shown as a toast carrying the
 * server's located message, so a caller only reverts what it drew.
 */
import { createContext, useContext } from "react";
import type { Run } from "./api";
import type { Group, Rect } from "./groups";
import type { Form, Json, Supertypes } from "./model";

/** What is being renamed in place: `node:<name>` (a node's title) or `group:<id>` (a box's title). */
export type Editing = `node:${string}` | `group:${string}` | null;

export interface EditorApi {
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
}

const refused = async () => false;

export const EditorContext = createContext<EditorApi>({
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
});

export const useEditor = (): EditorApi => useContext(EditorContext);
