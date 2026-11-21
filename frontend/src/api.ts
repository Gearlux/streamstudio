/**
 * The typed client for every phase-2 route of `streamstudio/app.py` — one function per route.
 *
 * Paths are RELATIVE (`api/graphs`, never `/api/graphs`): the page is built with `base: "./"`, so the same
 * bundle works served at `/` by `streamstudio` (the canvas editor) and under a consuming app's prefix (`/editor/`),
 * as long as that app registers the API blueprint under the same prefix.
 *
 * A refusal is what the routes answer: `400 {error}` with the LOCATED message (`file:line:col: …`), a
 * name that does not exist `404 {error}` naming what there is. Both surface as an `ApiError` whose
 * `error` is that message verbatim, so the page shows the server's words and never paraphrases them.
 *
 * Explicit Save (DESIGN §2.2): every editing route changes the document the server holds IN MEMORY and answers it
 * with `dirty`; only `save` / `save-as` write files. A node name in a URL may be QUALIFIED (`prep/grey`, a node
 * inside a subgraph): the routes take it as `<path:node>`, so each segment is encoded on its own (`nodePath`).
 */
import { oncePerClass } from "./catalogue";
import type { Group } from "./groups";
import type { HostRun } from "./host";
import type { Form, Graph, GraphSummary, Json, NewGraphKind, Positions, Problem, Supertypes } from "./model";
import type { NameShift } from "./levels";
import type { RestoredSummary, Workspace } from "./recovery";
import type { WireEnds } from "./wires";

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

/**
 * The editor SESSION this page joined (`GET api/workspace` answers it), sent with every request as
 * `X-Streamstudio-Session` (R1, P1): the server refuses a request of an earlier editor's session — a tab left open
 * across `streamstudio restart` — with `409`, changing nothing, and the page joins the new editor again
 * (`onRestarted`). Measured before: such a tab edited the NEW editor's document, read from disk, so the kept edit
 * vanished on the next change, Save wrote the file without it, and the next stop replaced the kept version.
 */
let session: string | null = null;
let restarted: ((message: string) => void) | null = null;

/** Send `joined` with every request from now on (`null`: none — a request without it is served as ever). */
export function joinSession(joined: string | null): void {
  session = joined;
}

/** Hear every `409` — the editor restarted since this page joined it; `null` stops listening. */
export function onRestarted(listener: ((message: string) => void) | null): void {
  restarted = listener;
}

/** The status the server refuses a request of another session with. */
export const RESTARTED_STATUS = 409;

// `object`, not `Record<string, Json>`: a group carries keys the page never declared (`Group`), and a body is
// only ever JSON.stringify-ed.
async function call<T>(method: Method, path: string, body?: object): Promise<T> {
  const headers: Record<string, string> = body === undefined ? {} : { "content-type": "application/json" };
  if (session !== null) headers["x-streamstudio-session"] = session;
  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data: unknown = await response.json().catch(() => undefined);
  if (!response.ok) {
    const error =
      typeof data === "object" && data !== null && typeof (data as { error?: unknown }).error === "string"
        ? (data as { error: string }).error
        : `${response.status} ${response.statusText} on ${method} ${path}`;
    if (response.status === RESTARTED_STATUS) restarted?.(error);
    throw new ApiError(response.status, error);
  }
  return data as T;
}

const graphPath = (name: string): string => `api/graphs/${encodeURIComponent(name)}`;

/** A (possibly qualified) node name as a `<path:node>` URL part: `prep/grey` → `prep/grey`, each segment encoded. */
export const nodePath = (node: string): string => node.split("/").map(encodeURIComponent).join("/");

// ---------------------------------------------------------------------------------------------------
// graphs
// ---------------------------------------------------------------------------------------------------

/**
 * `GET api/graphs` — `[{name, path, kind, dirty}]`: every `*.yaml` of the graph folder (sidecars and `*.steps.yaml`
 * left out), and every file opened with Open… or made with New… in another folder.
 */
export const listGraphs = (): Promise<GraphSummary[]> => call("GET", "api/graphs");

/** `GET api/graphs/<name>` — `{name, path, kind, text, model, positions, groups, history_depth, dirty}`. */
export const getGraph = (name: string): Promise<Graph> => call("GET", graphPath(name));

/** `PUT api/graphs/<name>` `{text}` — replace the whole document in memory (refused when it gains a problem). */
export const putText = (name: string, text: string): Promise<Graph> => call("PUT", graphPath(name), { text });

/**
 * `POST api/graphs` `{name, kind, dir?}` — New…: WRITES the starter of `kind` to `<dir or graph folder>/<name>.yaml` at
 * once and answers the graph, not dirty (a folder other than the editor's joins the list like a file opened). Refused,
 * writing nothing, when that file exists, the folder does not exist or cannot be written, or the name is one the
 * server's rule refuses — the refusal names the file or the rule.
 */
export const newGraph = (name: string, kind: NewGraphKind, dir?: string): Promise<Saved> =>
  call("POST", "api/graphs", dir === undefined ? { name, kind } : { name, kind, dir });

/** `POST api/graphs/open` `{path}` — open any `.yaml` on the editor's machine and list it (a taken name → `demo~2`). */
export const openGraph = (path: string): Promise<Graph> => call("POST", "api/graphs/open", { path });

/**
 * What a Save answers: the graph as written — plus, in an editor mounted in a host, `host_error` when the host failed on
 * the news of the files written (the files ARE written; the page shows the host's words).
 */
export type Saved = Graph & { host_error?: string };

/**
 * `POST api/graphs/<name>/save` — write every changed file and the sidecar. Refused, writing nothing, when a file
 * changed on disk since the editor read it (the message says to Revert or Save As).
 */
export const saveGraph = (name: string): Promise<Saved> => call("POST", `${graphPath(name)}/save`);

/**
 * `POST api/graphs/<name>/param` `{node, param, value, save: "settings"}` — set one value AND write the graph's SETTINGS
 * file alone, in one request (a settings sheet's change, `?only=settings`): a change of the graph itself the editor
 * holds stays unsaved. A refused save keeps nothing: the server undoes the change before it answers.
 */
export const setParamAndSaveSettings = (name: string, node: string, param: string, value: Json): Promise<Saved> =>
  call("POST", `${graphPath(name)}/param`, { node, param, value, save: "settings" });

/** `POST api/graphs/<name>/save-as` `{path, overwrite?}` — write it to `path`; the list entry now names the new file. */
export const saveGraphAs = (name: string, path: string, overwrite = false): Promise<Saved> =>
  call("POST", `${graphPath(name)}/save-as`, overwrite ? { path, overwrite } : { path });

/** `POST api/graphs/<name>/revert` — read the file again; the in-memory history, positions and groups are dropped. */
export const revertGraph = (name: string): Promise<Graph> => call("POST", `${graphPath(name)}/revert`);

/** `GET api/graphs/<name>/check` — every problem of the document, each with its `file:line:col`. */
export const checkGraph = (name: string): Promise<{ problems: Problem[] }> => call("GET", `${graphPath(name)}/check`);

// ---------------------------------------------------------------------------------------------------
// the workspace — the folder the editor was started on, and the unsaved work a stop kept there (DESIGN2 §1)
// ---------------------------------------------------------------------------------------------------

/**
 * `GET api/workspace` — `{folder, session, recovery, host}`: `session` is the editor's (the page joins it,
 * `joinSession`); `recovery` is `null`, or the unsaved work the last session kept in the folder's hidden
 * `.streamstudio-workspace.json` when it stopped (`recovery.ts`); `host` is `{name}` when the editor is mounted in an
 * app that supplies the record a graph without its own source runs on (`host.ts`), else `null` or absent. Answered
 * whatever session a page sends: it is how a page joins again after a restart.
 */
export const getWorkspace = (): Promise<Workspace> => call("GET", "api/workspace");

/**
 * `POST api/workspace/recovery/restore` — open the kept graphs with their unsaved texts, positions and groups (held in
 * the editor, dirty; nothing is written until Save) and remove the recovery file. Answers the graph list, each entry
 * with `restored`: a file this editor holds unsaved edits of, or one kept twice, opens a second entry beside the
 * first (`demo~2`) — no version is dropped.
 */
export const restoreRecovery = (): Promise<RestoredSummary[]> => call("POST", "api/workspace/recovery/restore");

/** `POST api/workspace/recovery/discard` — drop the kept work (the recovery file goes). Answers the graph list. */
export const discardRecovery = (): Promise<GraphSummary[]> => call("POST", "api/workspace/recovery/discard");

// ---------------------------------------------------------------------------------------------------
// the file browser — the editor's machine, never the browser's
// ---------------------------------------------------------------------------------------------------

export interface FileEntry {
  name: string;
  path: string;
  /** A folder, a graph file (the graph browser's listing), or any file (`show=all`, a file picker's listing). */
  kind: "dir" | "yaml" | "file";
}

/** What a listing shows besides folders: the graph files (`graphs`, the default), or every file (`all`). */
export type FileShow = "graphs" | "all";

export interface FileListing {
  dir: string;
  /** The folder above, or `null` at the root. */
  parent: string | null;
  /** Folders first; hidden entries and `*.layout.yaml` left out. */
  entries: FileEntry[];
  /** The dialog's shortcut buttons: the graph folder, then each place variable (`HOME`, `DATA_ROOT` …) set to a folder. */
  places: FilePlace[];
}

export interface FilePlace {
  /** `Graphs`, or the variable's name. */
  name: string;
  /** Resolved — the spelling a listing answers as `dir`. */
  path: string;
}

/**
 * `GET api/files?dir=<dir>&show=<graphs|all>` — no `dir` is the graph folder (a `$VAR` in it is expanded by the server);
 * a missing one is a 404 naming it. `show=all` lists every file — a file picker's listing (`pickers.ts`).
 */
export const listFiles = (dir?: string, show: FileShow = "graphs"): Promise<FileListing> => {
  const query = new URLSearchParams();
  if (dir !== undefined) query.set("dir", dir);
  if (show !== "graphs") query.set("show", show);
  const asked = query.toString();
  return call("GET", asked === "" ? "api/files" : `api/files?${asked}`);
};

/** `POST api/files/folder` `{dir, name}` — make the folder `name` inside `dir` and answer ITS listing (New folder). */
export const makeFolder = (dir: string, name: string): Promise<FileListing> => call("POST", "api/files/folder", { dir, name });

// ---------------------------------------------------------------------------------------------------
// editing — each answers the whole graph after the edit
// ---------------------------------------------------------------------------------------------------

/** `POST api/graphs/<name>/param` `{node, param, value}` — patch one value on its own line. */
export const setParam = (name: string, node: string, param: string, value: Json): Promise<Graph> =>
  call("POST", `${graphPath(name)}/param`, { node, param, value });

/**
 * Where a new step goes: after a step or a list member (`after`), or at the END of an op list (`list`, the list node
 * `<holder>.<slot>` — `stream.ops`; sent without `after`). Exactly one of the two.
 */
export type StepPlace = { after: string; list?: undefined } | { list: string; after?: undefined };

/**
 * A wire dragged out of a socket and dropped on empty canvas, by the end it was dragged out of: the card added for it is
 * the other end — fed by that output (`{source, source_socket}`), or feeding that input (`{target, target_socket}`).
 */
export type DraggedEnd = { target: string; target_socket: string } | { source: string; source_socket: string };

export type NewStep = {
  name: string;
  class_path: string;
  params: Record<string, Json>;
  /** The dropped wire a CARD is picked from — saved to the card in the same edit (an op's place is its wire). */
  wire?: DraggedEnd;
} & (StepPlace | { after?: undefined; list?: undefined });

/**
 * `POST api/graphs/<name>/steps` — insert a `flow:` step after `after`. Inside a subgraph both names are QUALIFIED:
 * `{name: "prep/threshold", after: "prep/grey"}` inserts into prep's `steps:`. On an op list (DESIGN2 §3) `after` names
 * a MEMBER — `{after: "stream.ops[1]"}` inserts the class as `stream.ops[2]` — and `{list: "stream.ops"}` appends it as
 * the list's last member (an empty `ops: []` becomes a block list); `name` is the step name a flow step would take,
 * sent either way (a member is named by its position). A class that is not an op (2026-10-05, `cards.ts`) is a CARD of
 * its own — a top-level key named after its class, above the graph's root, the place unused — with the dropped `wire`
 * saved to it. The answer is the graph plus `added`: the new node's name as the server made it (`stream.ops[2]`; a flow
 * step's own, qualified name; a card's key, `class_names_scan`).
 */
export const addStep = (name: string, step: NewStep): Promise<Graph & { added?: string }> =>
  call("POST", `${graphPath(name)}/steps`, { ...step });

/**
 * `POST api/graphs/<name>/nodes` `{class_path, params, wire?}` — a class that is not an op as a CARD of its own, the
 * route an agent names it by (the page's adds go through `addStep`, which hands such a class over): `class_names_scan:
 * !class:…ClassNamesScan {}` above the graph's root, the dropped `wire` saved to it in the same edit. Refused in one
 * plain line on a flow graph. The answer is the graph plus `added`, the card's name.
 */
export const addNode = (
  name: string,
  classPath: string,
  params: Record<string, Json>,
  wire?: DraggedEnd,
): Promise<Graph & { added?: string }> =>
  call("POST", `${graphPath(name)}/nodes`, wire === undefined ? { class_path: classPath, params } : { class_path: classPath, params, wire });

/** `DELETE api/graphs/<name>/steps/<path:node>` — remove a step; what read it now reads what it read from. */
export const removeStep = (name: string, node: string): Promise<Graph> =>
  call("DELETE", `${graphPath(name)}/steps/${nodePath(node)}`);

/**
 * `POST api/graphs/<name>/steps/remove` `{nodes}` — remove several steps as ONE edit (one undo entry): what Cut and
 * Delete send for the whole selection, so one Ctrl/Cmd+Z brings every removed step back. Op list MEMBERS are removed
 * the same way (`{nodes: ["stream.ops[1]", "stream.ops[3]"]}`); the later members are renumbered by the server, their
 * positions and group entries with them; removing every member leaves `ops: []`.
 */
export const removeSteps = (name: string, nodes: string[]): Promise<Graph> =>
  call("POST", `${graphPath(name)}/steps/remove`, { nodes });

/**
 * `POST api/graphs/<name>/slot` `{node, slot, class_path, params}` — Set source… (`slots.ts`): put ONE marker of
 * `class_path` built with `params` into the slot `slot` of `node` (`{node: "stream", slot: "source"}`), replacing what
 * fills it now; held until Save like every edit, one undo entry. The answer is the graph, plus `set` — the node the new
 * marker is drawn as (`stream.source`) — when the server names it.
 */
export const setSlot = (
  name: string,
  node: string,
  slot: string,
  classPath: string,
  params: Record<string, Json>,
): Promise<Graph & { set?: string }> => call("POST", `${graphPath(name)}/slot`, { node, slot, class_path: classPath, params });

/**
 * `POST api/graphs/<name>/wires` `{source, source_socket, target, target_socket}` — SAVE a wire drawn on the canvas, in
 * the document's own spelling (`wires.ts`); held until Save, one undo entry. The answer is the graph plus `wired`: the card
 * the wire comes from now (`source` for a `stream.source` the server moved out of its slot). A wire the editor cannot save
 * yet is a `400` with one plain line (`This wire would change the order of the steps — the editor can't save that yet.`).
 */
export const addWire = (name: string, wire: WireEnds): Promise<Graph & { wired?: string }> =>
  call("POST", `${graphPath(name)}/wires`, { ...wire });

/** `POST api/graphs/<name>/wires/remove` `{wires}` — take every wire listed out as ONE edit (Delete on selected wires). */
export const removeWires = (name: string, wires: WireEnds[]): Promise<Graph> =>
  call("POST", `${graphPath(name)}/wires/remove`, { wires });

/** `POST api/graphs/<name>/rename` `{node, new}` — the key, every reference to it, and the sidecar entry. */
export const renameNode = (name: string, node: string, newName: string): Promise<Graph> =>
  call("POST", `${graphPath(name)}/rename`, { node, new: newName });

/**
 * An undo or redo: the graph, whether there was a step, and how it renamed op list members (`shifts`, in order —
 * a page inside a subgraph MEMBER follows it, `levels.followShifts`).
 */
export type HistoryStep = Graph & { changed: boolean; shifts?: NameShift[] };

/** `POST api/graphs/<name>/undo` — one step back in the text history; `changed` says whether there was one. */
export const undo = (name: string): Promise<HistoryStep> => call("POST", `${graphPath(name)}/undo`);

/** `POST api/graphs/<name>/redo`. */
export const redo = (name: string): Promise<HistoryStep> => call("POST", `${graphPath(name)}/redo`);

/** What `copy` answers: the YAML text of the copied steps — a steps mapping, or a sequence of `ops` list members. */
export interface Copied {
  text: string;
  kind: "steps" | "ops";
}

/** `POST api/graphs/<name>/copy` `{nodes}` — the nodes' own lines (comments kept, dedented), as text. */
export const copyNodes = (name: string, nodes: string[]): Promise<Copied> => call("POST", `${graphPath(name)}/copy`, { nodes });

/**
 * `POST api/graphs/<name>/paste` `{text, after}` — insert copied text after `after`: a taken name is suffixed
 * (`grey` → `grey_2`) and the references among the pasted steps follow; ONE undo entry.
 */
export const pasteText = (name: string, text: string, after: string): Promise<Graph & { pasted?: string[] }> =>
  call("POST", `${graphPath(name)}/paste`, { text, after });

// ---------------------------------------------------------------------------------------------------
// subgraphs (DESIGN §2.6) and templates (§2.5)
// ---------------------------------------------------------------------------------------------------

/**
 * `POST api/graphs/<name>/subgraph` `{nodes, name}` — consecutive steps of one level become one subgraph step; `name`
 * is qualified on that level like the steps (`{nodes: ["prep/grey", "prep/scaled"], name: "prep/subgraph"}`). The
 * answer's `subgraph` is the new node's name — `name` for flow steps, the first member's POSITION for list members.
 */
export const convertToSubgraph = (name: string, nodes: string[], subgraph: string): Promise<Graph & { subgraph?: string }> =>
  call("POST", `${graphPath(name)}/subgraph`, { nodes, name: subgraph });

/** `POST api/graphs/<name>/subgraph/<path:node>/extract` `{path?}` — Move to its own file: `steps: {include: …}`. */
export const extractSubgraph = (name: string, node: string, path?: string): Promise<Graph> =>
  call("POST", `${graphPath(name)}/subgraph/${nodePath(node)}/extract`, path === undefined ? {} : { path });

/** `POST api/graphs/<name>/include` `{path, after, name?}` — Insert steps file: a subgraph step that includes it. */
export const includeSteps = (name: string, path: string, after: string, step?: string): Promise<Graph> =>
  call("POST", `${graphPath(name)}/include`, step === undefined ? { path, after } : { path, after, name: step });

/** A template: a steps file in the templates folder (`~/.streamstudio/templates` by default). */
export interface Template {
  name: string;
  path: string;
  steps: string[];
  /** The file's first comment line. */
  doc: string;
}

/** `GET api/templates`. */
export const listTemplates = (): Promise<Template[]> => call("GET", "api/templates");

/**
 * `POST api/templates` `{name, graph, nodes, overwrite?}` — a subgraph node saves its inner steps, flow steps save
 * themselves. Written at once (a template is not part of any document, so no Save is needed).
 */
export const saveTemplate = (template: string, graph: string, nodes: string[], overwrite = false): Promise<unknown> =>
  call("POST", "api/templates", overwrite ? { name: template, graph, nodes, overwrite } : { name: template, graph, nodes });

/** `DELETE api/templates/<name>`. */
export const deleteTemplate = (template: string): Promise<unknown> =>
  call("DELETE", `api/templates/${encodeURIComponent(template)}`);

/** `POST api/graphs/<name>/templates/<template>/insert` `{after}` — a COPY of the template as a new subgraph step. */
export const insertTemplate = (name: string, template: string, after: string): Promise<Graph> =>
  call("POST", `${graphPath(name)}/templates/${encodeURIComponent(template)}/insert`, { after });

// ---------------------------------------------------------------------------------------------------
// positions
// ---------------------------------------------------------------------------------------------------

/**
 * `PUT api/graphs/<name>/positions` `{positions}` — held until Save (then the sidecar); answers every node's position.
 * The server MERGES what it is sent over the positions it holds, so a partial map is safe; the page sends the whole
 * map it draws all the same.
 */
export const putPositions = (name: string, positions: Positions): Promise<Positions> =>
  call("PUT", `${graphPath(name)}/positions`, { positions });

/** `POST api/graphs/<name>/layout` — lay the graph out afresh on the server (held until Save). */
export const relayout = (name: string): Promise<Positions> => call("POST", `${graphPath(name)}/layout`);

/**
 * `PUT api/graphs/<name>/groups` `{groups}` — the sidecar's `groups:` block, replaced whole and held until Save;
 * the document is untouched. Answers the groups and whether the graph is now dirty.
 */
export const putGroups = (name: string, groups: Group[]): Promise<{ groups: Group[]; dirty?: boolean }> =>
  call("PUT", `${graphPath(name)}/groups`, { groups });

// ---------------------------------------------------------------------------------------------------
// the palette, the form, the types
// ---------------------------------------------------------------------------------------------------

/**
 * One class as a node, as the catalogue's list carries it (`streamstudio/palette.py`'s `listed`) — what the palette,
 * the Nodes sidebar, Add Node ▸ and Set source… list. Its settings are not in it: they are asked for when the class is
 * selected (`settingsOf`).
 */
export interface NodeSpec {
  class_path: string;
  name: string;
  category: string;
  group: string;
  kind: string;
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

/** One class with its settings: the constructor's JSON-schema `properties` (a `picker` marked where one applies) and
 * the `$defs` they refer to — `streamstudio/palette.py`'s whole `NodeSpec`. */
export interface NodeSpecWithSettings extends NodeSpec {
  params_schema: Record<string, Json>;
  params_defs: Record<string, Json>;
}

/** `GET api/palette` — every configurable class as a node spec, without its settings; `refresh` runs a new build. */
export const getPalette = (refresh = false): Promise<NodeSpec[]> =>
  call("GET", refresh ? "api/palette?refresh=1" : "api/palette");

/** `GET api/palette/<class path>` — one class with its settings. */
export const getNodeSpec = (classPath: string): Promise<NodeSpecWithSettings> =>
  call("GET", `api/palette/${encodeURIComponent(classPath)}`);

/** A class's settings, asked when it is selected — once per class for the page's life (`catalogue.oncePerClass`). */
export const settingsOf = oncePerClass(getNodeSpec);

/**
 * `GET api/graphs/<name>/form` — `{node: JSON-schema properties}`: every parameter of every node (a step's gate,
 * `requires`, is never one); a fragment the palette marked with `picker` gets that picker (`pickers.ts`).
 */
export const getForm = (name: string): Promise<Form> => call("GET", `${graphPath(name)}/form`);

/** One field a graph's `app:` block names: a node (each node of a class it names) and its parameter. */
export interface AppField {
  node: string;
  param: string;
  /** The `app:` entry's `file:line:col`. */
  loc: string;
}

/**
 * `GET api/graphs/<name>/app` — the graph's SETTINGS, its `app:` block resolved against what the editor holds:
 * `declared` (the document has an `app:` block), the `fields` in the block's order (a `__lo`/`__hi` pair is one
 * field; a class two nodes share gives one field each), their `form` in `GET …/form`'s shape, and every entry that
 * names nothing as a located `problem`. `{declared: false, fields: [], form: {}, problems: []}` without a block.
 */
/** What a graph lets a person change (`app: {graph: …}`): everything, or its settings alone. */
export type GraphMode = "editable" | "settings-only";

export interface AppForm {
  declared: boolean;
  /** `settings-only`: the server refuses every edit but a setting (the graph is fixed where it is shipped). */
  graph: GraphMode;
  fields: AppField[];
  form: Form;
  problems: Problem[];
}

export const getApp = (name: string): Promise<AppForm> => call("GET", `${graphPath(name)}/app`);

/** `GET api/types` — `{socket type: [the types it may be wired into]}`, the meet rule's table. */
export const getTypes = (): Promise<Supertypes> => call("GET", "api/types");

// ---------------------------------------------------------------------------------------------------
// the HuggingFace pickers (`pickers.ts`) — asked of the EDITOR's machine, whose cache a source reads
// ---------------------------------------------------------------------------------------------------

/** A lookup's list, whether the server answers it bare or under its key (`{datasets: […]}`); anything else is `[]`. */
function names(answer: unknown, key: string): string[] {
  const list = Array.isArray(answer)
    ? answer
    : typeof answer === "object" && answer !== null && Array.isArray((answer as Record<string, unknown>)[key])
      ? ((answer as Record<string, unknown>)[key] as unknown[])
      : [];
  return list.filter((item): item is string => typeof item === "string");
}

const query = (params: Record<string, string>): string => {
  const given = Object.entries(params).filter(([, value]) => value !== "");
  return given.length === 0 ? "" : `?${new URLSearchParams(given).toString()}`;
};

/**
 * What a lookup answers: the names to offer, and — when there are none — why, in the server's words (`""` when it gave
 * no reason). An empty list that explains nothing looks like a broken one (`streamstudio.cached_hf_datasets`: "no
 * datasets cached at <the cache this process reads>").
 */
export interface Listed {
  names: string[];
  reason: string;
}

/** The `reason` an answer carries, when it carries one as text. */
const reasonOf = (answer: unknown): string =>
  typeof answer === "object" && answer !== null && typeof (answer as Record<string, unknown>).reason === "string"
    ? ((answer as Record<string, unknown>).reason as string)
    : "";

const listed = (answer: unknown, key: string): Listed => ({ names: names(answer, key), reason: reasonOf(answer) });

/** `GET api/hf/datasets` → `{datasets: [repo ids], reason}`: the datasets cached on the editor's machine, and why none. */
export const hfDatasets = async (): Promise<Listed> => listed(await call<unknown>("GET", "api/hf/datasets"), "datasets");

/**
 * `GET api/hf/features?path=&name=&split=` → `{features: [columns], all_columns: "*"}`: the columns of one dataset
 * (`[]` when unknown, and when no dataset is named).
 */
export const hfFeatures = async (path: string, name = "", split = ""): Promise<Listed> =>
  listed(await call<unknown>("GET", `api/hf/features${query({ path, name, split })}`), "features");

/** `GET api/hf/splits?path=&name=` → `{splits: [names]}`: the splits of one dataset (`[]` when unknown). */
export const hfSplits = async (path: string, name = ""): Promise<Listed> =>
  listed(await call<unknown>("GET", `api/hf/splits${query({ path, name })}`), "splits");

/**
 * `GET api/lookups/<tag>?<the node's values>` → `{names, reason}`: the list an installed plugin supplies for its own
 * picker tag, asked with the node's current values (an empty value is left out); never an error — an empty list says why.
 */
export const pluginLookup = async (tag: string, values: Record<string, string>): Promise<Listed> =>
  listed(await call<unknown>("GET", `api/lookups/${encodeURIComponent(tag)}${query(values)}`), "names");

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
  /** Which record of the graph's own source ran; `null` when the host supplied it. */
  record: number | null;
  /**
   * What the host supplied, when the record was the host's (`host.ts`): `label` in its own words (`rec_0042 · 0.25–0.75
   * s · 1–2 MHz`) and `filled` — per node, the values it filled in (the window, the band); `null` for a record of the
   * graph's own source.
   */
  host?: HostRun | null;
  trace: { nodes: TraceNode[]; check: ({ node: string } & Record<string, unknown>)[]; paused_at: string | null } & Record<
    string,
    unknown
  >;
  statuses: Record<string, string>;
  generations: Record<string, number>;
  paused_at: string | null;
}

/**
 * `POST api/graphs/<name>/trace` — run ONE record, pausing before `until`: `{record: N}` draws record #N of the graph's
 * own source; with `record` `null` the body carries no record number (`{}`) and a graph WITHOUT a source of its own runs
 * the record the host the editor is mounted in supplies — its selection, with the window and band it fills in. Either
 * way the editor's UNSAVED text is what runs.
 */
export const trace = (name: string, record: number | null = 0, until?: string): Promise<Run> =>
  call("POST", `${graphPath(name)}/trace`, { ...(record === null ? {} : { record }), ...(until === undefined ? {} : { until }) });

/** `POST api/runs/<run>/rerun` `{node, params}` — rebuild one node through its constructor and rerun from it. */
export const rerun = (run: string, node: string, params: Record<string, Json>): Promise<Run> =>
  call("POST", `api/runs/${encodeURIComponent(run)}/rerun`, { node, params });

/** `GET api/runs/<run>`. */
export const getRun = (run: string): Promise<Run> => call("GET", `api/runs/${encodeURIComponent(run)}`);
