/**
 * The typed client for every phase-2 route of `streamstudio/app.py` — one function per route.
 *
 * Paths are RELATIVE (`api/graphs`, never `/api/graphs`): the page is built with `base: "./"`, so the same
 * bundle works served at `/` by `streamstudio editor` and under a consuming app's prefix (`/editor/`),
 * as long as that app registers the API blueprint under the same prefix.
 *
 * A refusal is what the routes answer: `400 {error}` with the LOCATED message (`file:line:col: …`), a
 * name that does not exist `404 {error}` naming what there is. Both surface as an `ApiError` whose
 * `error` is that message verbatim, so the page shows the server's words and never paraphrases them.
 */
import type { Group } from "./groups";
import type { Form, Graph, GraphSummary, Json, Positions, Supertypes } from "./model";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly error: string,
  ) {
    super(error);
    this.name = "ApiError";
  }
}

/** The message to show for anything a call threw: the server's located message, else the failure itself. */
export function errorMessage(exc: unknown): string {
  if (exc instanceof ApiError) return exc.error;
  if (exc instanceof Error) return exc.message;
  return String(exc);
}

type Method = "GET" | "PUT" | "POST" | "DELETE";

// `object`, not `Record<string, Json>`: a group carries keys the page never declared (`Group`), and a body is
// only ever JSON.stringify-ed.
async function call<T>(method: Method, path: string, body?: object): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data: unknown = await response.json().catch(() => undefined);
  if (!response.ok) {
    const error =
      typeof data === "object" && data !== null && typeof (data as { error?: unknown }).error === "string"
        ? (data as { error: string }).error
        : `${response.status} ${response.statusText} on ${method} ${path}`;
    throw new ApiError(response.status, error);
  }
  return data as T;
}

const graphPath = (name: string): string => `api/graphs/${encodeURIComponent(name)}`;

// ---------------------------------------------------------------------------------------------------
// graphs
// ---------------------------------------------------------------------------------------------------

/** `GET api/graphs` — every `*.yaml` of the directory as `{name, path, kind}`. */
export const listGraphs = (): Promise<GraphSummary[]> => call("GET", "api/graphs");

/** `GET api/graphs/<name>` — `{name, path, kind, text, model, positions, groups, history_depth}`. */
export const getGraph = (name: string): Promise<Graph> => call("GET", graphPath(name));

/** `PUT api/graphs/<name>` `{text}` — replace the whole document (refused when it gains a problem). */
export const putText = (name: string, text: string): Promise<Graph> => call("PUT", graphPath(name), { text });

// ---------------------------------------------------------------------------------------------------
// editing — each answers the whole graph after the edit
// ---------------------------------------------------------------------------------------------------

/** `POST api/graphs/<name>/param` `{node, param, value}` — patch one value on its own line. */
export const setParam = (name: string, node: string, param: string, value: Json): Promise<Graph> =>
  call("POST", `${graphPath(name)}/param`, { node, param, value });

export interface NewStep {
  name: string;
  after: string;
  class_path: string;
  params: Record<string, Json>;
}

/** `POST api/graphs/<name>/steps` — insert a `flow:` step after `after`. */
export const addStep = (name: string, step: NewStep): Promise<Graph> =>
  call("POST", `${graphPath(name)}/steps`, { ...step });

/** `DELETE api/graphs/<name>/steps/<node>` — remove a step; what read it now reads what it read from. */
export const removeStep = (name: string, node: string): Promise<Graph> =>
  call("DELETE", `${graphPath(name)}/steps/${encodeURIComponent(node)}`);

/** `POST api/graphs/<name>/rename` `{node, new}` — the key, every reference to it, and the sidecar entry. */
export const renameNode = (name: string, node: string, newName: string): Promise<Graph> =>
  call("POST", `${graphPath(name)}/rename`, { node, new: newName });

export type HistoryStep = Graph & { changed: boolean };

/** `POST api/graphs/<name>/undo` — one step back in the text history; `changed` says whether there was one. */
export const undo = (name: string): Promise<HistoryStep> => call("POST", `${graphPath(name)}/undo`);

/** `POST api/graphs/<name>/redo`. */
export const redo = (name: string): Promise<HistoryStep> => call("POST", `${graphPath(name)}/redo`);

// ---------------------------------------------------------------------------------------------------
// positions
// ---------------------------------------------------------------------------------------------------

/** `PUT api/graphs/<name>/positions` `{positions}` — written to the sidecar; answers every node's position. */
export const putPositions = (name: string, positions: Positions): Promise<Positions> =>
  call("PUT", `${graphPath(name)}/positions`, { positions });

/** `POST api/graphs/<name>/layout` — lay the graph out afresh on the server and save it. */
export const relayout = (name: string): Promise<Positions> => call("POST", `${graphPath(name)}/layout`);

/** `PUT api/graphs/<name>/groups` `{groups}` — the sidecar's `groups:` block, replaced whole; the document is untouched. */
export const putGroups = (name: string, groups: Group[]): Promise<{ groups: Group[] }> =>
  call("PUT", `${graphPath(name)}/groups`, { groups });

// ---------------------------------------------------------------------------------------------------
// the palette, the form, the types
// ---------------------------------------------------------------------------------------------------

/** One class as a node, as `streamstudio/palette.py`'s `NodeSpec` serialises — what the palette lists. */
export interface NodeSpec {
  class_path: string;
  name: string;
  category: string;
  group: string;
  kind: string;
  params_schema: Record<string, Json>;
  params_defs: Record<string, Json>;
  inputs: { name: string; type: string; required: boolean; doc: string }[];
  outputs: { name: string; type: string; required: boolean; doc: string }[];
  consumes: Record<string, string>;
  produces: Record<string, string>;
  consumes_types: string[];
  produces_types: string[];
  flags: string[];
  reports: string[];
  doc: string;
  tuple_form: boolean;
  problem: string;
}

/** `GET api/palette` — every configurable class as a node spec; `refresh` rescans the registry. */
export const getPalette = (refresh = false): Promise<NodeSpec[]> =>
  call("GET", refresh ? "api/palette?refresh=1" : "api/palette");

/** `GET api/graphs/<name>/form` — `{node: JSON-schema properties}`. */
export const getForm = (name: string): Promise<Form> => call("GET", `${graphPath(name)}/form`);

/** `GET api/types` — `{socket type: [the types it may be wired into]}`, the meet rule's table. */
export const getTypes = (): Promise<Supertypes> => call("GET", "api/types");

// ---------------------------------------------------------------------------------------------------
// runs
// ---------------------------------------------------------------------------------------------------

/**
 * One traced node, as `recordstream.flow.Tracer.to_dict` spells it (arrays summarised, never dumped). The
 * fields `app.py` guarantees are named; the tracer's other entries (`input`, `output`, `bound`, `changed`, …)
 * ride along untyped and the run panel reads them defensively — a node never reached carries none of them.
 */
export type TraceNode = {
  node: string;
  op: string;
  status: string;
  ms: number;
  params: Record<string, Json>;
} & Record<string, unknown>;

export interface Run {
  run: string;
  graph: string;
  record: number;
  trace: { nodes: TraceNode[]; check: ({ node: string } & Record<string, unknown>)[]; paused_at: string | null } & Record<
    string,
    unknown
  >;
  statuses: Record<string, string>;
  generations: Record<string, number>;
  paused_at: string | null;
}

/** `POST api/graphs/<name>/trace` `{record, until}` — run one record of the graph's own source, pausing before `until`. */
export const trace = (name: string, record = 0, until?: string): Promise<Run> =>
  call("POST", `${graphPath(name)}/trace`, until === undefined ? { record } : { record, until });

/** `POST api/runs/<run>/rerun` `{node, params}` — rebuild one node through its constructor and rerun from it. */
export const rerun = (run: string, node: string, params: Record<string, Json>): Promise<Run> =>
  call("POST", `api/runs/${encodeURIComponent(run)}/rerun`, { node, params });

/** `GET api/runs/<run>`. */
export const getRun = (run: string): Promise<Run> => call("GET", `api/runs/${encodeURIComponent(run)}`);
