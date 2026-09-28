/**
 * What a node and a group box need from the page: the form schema per node (`GET api/graphs/<name>/form`), the
 * meet-rule table (`GET api/types`), the latest run (its statuses, wall times and generations are painted on the
 * nodes), and the mutations a node or a box performs itself — a parameter, a rename, a group's title/colour, an
 * ungroup. Everything else (positions, wires, steps, the graph switch) is the canvas's or the app's, so a node
 * stays ONE component for every class (docs/rewrite.md §2.3) with no knowledge of routes.
 *
 * Each mutation answers whether the server accepted it; a refusal has already been shown as a toast carrying the
 * server's located message, so a caller only reverts what it drew.
 */
import { createContext, useContext } from "react";
import type { Run } from "./api";
import type { Group } from "./groups";
import type { Form, Json, Supertypes } from "./model";

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
});

export const useEditor = (): EditorApi => useContext(EditorContext);
