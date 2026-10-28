/**
 * The page's half of being MOUNTED in a host app (DESIGN5 step 1, items 1 and 7) — what a host provides the editor, and
 * what the page tells a host whose panel it sits in.
 *
 * **The host's record.** A pane graph has no source of its own: a host fills its record (the capture it shows, the
 * window and band selected there). When the editor is mounted with a host, `GET api/workspace` says so (`host`), and
 * the run panel's Run sends no record number for a graph without its own source (`POST …/trace {}`) — the server asks
 * the host for the record, fills the window and band into the ops that take them and traces the editor's UNSAVED text.
 * The answer names what ran (`run.host`: the host's `label`, and what it `filled` per node), and the panel shows it:
 *
 *     run_3fa2 · rec_0042 · 0.25–0.75 s · 1–2 MHz · 41.3 ms
 *
 * A graph WITH its own source (a Stream whose `source` slot is wired) runs record #N of that source, host or not.
 *
 * **The panel's messages** (`?embed=1` only, `link.ts`): the page posts to `window.parent`, same origin only —
 *
 *     {source: "streamstudio", type: "state", graph, dirty, unsaved: [...], tab}   on every change of these
 *     {source: "streamstudio", type: "saved", graph, path}                         after a Save / Save As wrote it
 *
 * — so a host can warn before it closes a panel holding unsaved work, and re-run its panes the moment a graph is saved
 * — and takes these commands from it:
 *
 *     {target: "streamstudio", type: "open", graph: "view_bte", tab?: "settings", level?: "prep"}
 *     {target: "streamstudio", type: "commit"}
 *     {target: "streamstudio", type: "values", graph: "view_detect", values: {rule: "cfar"}}
 *     {target: "streamstudio", type: "hints", graph: "export_wlan", hints: {level_db: "measured 14.6 dB"}}
 *     {target: "streamstudio", type: "style", font_size_px: 13, font_family: "-apple-system, sans-serif"}
 *
 * `open` shows that graph (and tab) without reloading the page — a reload of a page holding unsaved work asks
 * "Leave site?" first. `commit` writes the field being typed in (`CommitCommand`). `values` says what the host RUNS
 * the graph with over its settings — a pane's knobs, which the host applies by name and never writes — so the
 * settings form grays out what that run does not read (`settings.offReason`): the knob's rule, not the graph's.
 * `hints` gives a line to show under a setting, by parameter name (what the host measured for it: an export's level in
 * the box) — shown, never written; the hints it sent last stand until it sends others. `style` hands the settings form
 * the host page's font, so a panel inside the host reads at the host's size.
 *
 * When there is no page to show — not built, no Node at the server's start, a failed build — the server answers a
 * short page of its own instead, which posts `{source: "streamstudio", type: "unavailable"}` (`app._TELL_THE_HOST`):
 * no editor will answer `open` in that frame. This page never posts it.
 */
import type { Json, Model } from "./model";
import { SIDE_TABS, type SideTab } from "./link";

/** What `GET api/workspace` says of the app the editor is mounted in; `null` for a standalone editor. */
export interface HostInfo {
  /** How the page names the host: `annotaide`. */
  name: string;
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/**
 * The host `GET api/workspace` names — `{…, host: {name}}` (or the name alone, `host: "annotaide"`) — or `null`: a
 * standalone editor sends `host: null` or no key at all. A host the server names without a name is "the host".
 */
export function hostOf(workspace: unknown): HostInfo | null {
  if (!isObject(workspace)) return null;
  const host = workspace.host;
  if (host === null || host === undefined || host === false) return null;
  const named = typeof host === "string" ? host : isObject(host) && typeof host.name === "string" ? host.name : "";
  return { name: named.trim() === "" ? "the host" : named.trim() };
}

/**
 * The title of the host card (`<host>`) — what the host hands the graph when it runs it, never a node of the document:
 * `from annotaide` in a host's panel, `from the host` standalone.
 */
export function hostCardTitle(host: HostInfo | null): string {
  return `from ${host?.name ?? "the host"}`;
}

/** The socket a stream's records come from (`document.SOURCE`'s slot). */
const SOURCE_SLOT = "source";

/**
 * Whether the graph draws its records from a source of its OWN: an engine node (a `Stream`) whose `source` slot is
 * wired — the stream the server's trace draws record #N from (`app._fed_stream`). Used to choose what Run sends; the
 * server decides what runs, and refuses in its own words.
 */
export function hasOwnSource(model: Model): boolean {
  const engines = new Set(model.nodes.filter((node) => node.kind === "engine").map((node) => node.name));
  return model.wires.some((wire) => wire.target_socket === SOURCE_SLOT && engines.has(wire.target));
}

/**
 * The record number Run sends: `null` — none, the host's record — when a host is there and the graph has no source of
 * its own; else `record` (#N of the graph's own source).
 */
export function runRecord(host: HostInfo | null, model: Model, record: number): number | null {
  return host !== null && !hasOwnSource(model) ? null : record;
}

/**
 * What the trace answer says the host supplied (`run.host`): `label` in the host's own words, and `filled` — per node,
 * the values the host filled into it (`{window: {start_s: 0.25, stop_s: 0.75}}`).
 */
export interface HostRun {
  label?: string;
  filled?: Record<string, Record<string, Json>>;
}

const valueText = (value: unknown): string =>
  typeof value === "number" ? String(Number(value.toPrecision(6))) : typeof value === "string" ? value : JSON.stringify(value);

/**
 * What a run ran on, in a few words: the host's label (`rec_0042 · 0.25–0.75 s`), else what it filled in
 * (`the host's record · window: start_s = 0.25, stop_s = 0.75`), else `record #N` of the graph's own source.
 */
export function ranOn(run: { record?: Json; host?: unknown }): string {
  const host = run.host;
  if (isObject(host)) {
    if (typeof host.label === "string" && host.label.trim() !== "") return host.label.trim();
    return ["the host's record", ...filledLines(host.filled)].join(" · ");
  }
  return typeof run.record === "number" ? `record #${run.record}` : "the host's record";
}

/** What the host filled in, one entry per node: `window: start_s = 0.25, stop_s = 0.75`. */
export function filledLines(filled: unknown): string[] {
  if (!isObject(filled)) return [];
  return Object.entries(filled)
    .filter((entry): entry is [string, Record<string, unknown>] => isObject(entry[1]))
    .map(([node, values]) => `${node}: ${Object.entries(values).map(([key, value]) => `${key} = ${valueText(value)}`).join(", ")}`);
}

// ---------------------------------------------------------------------------------------------------------------
// the panel's messages
// ---------------------------------------------------------------------------------------------------------------

/** The `source` of every message the page posts, and the `target` of every command it takes. */
export const MESSAGE_SOURCE = "streamstudio";

/** What the page shows and holds: posted whenever one of these changes. */
export interface StateMessage {
  source: typeof MESSAGE_SOURCE;
  type: "state";
  /** The graph shown, `""` while none is. */
  graph: string;
  /** The graph shown holds unsaved changes. */
  dirty: boolean;
  /** Every listed graph holding unsaved changes — what closing the panel would leave unsaved. */
  unsaved: string[];
  tab: SideTab;
}

/** A Save or Save As wrote `path` (the graph's file). */
export interface SavedMessage {
  source: typeof MESSAGE_SOURCE;
  type: "saved";
  graph: string;
  path: string;
}

/** The answer to a host's `commit`: every field the person was typing in is written (and, on a settings-only page,
 * saved) — the host may read the files now. */
export interface CommittedMessage {
  source: typeof MESSAGE_SOURCE;
  type: "committed";
  graph: string;
}

export type PageMessage = StateMessage | SavedMessage | CommittedMessage;

export const stateMessage = (graph: string, dirty: boolean, unsaved: string[], tab: SideTab): StateMessage => ({
  source: MESSAGE_SOURCE,
  type: "state",
  graph,
  dirty,
  unsaved,
  tab,
});

export const savedMessage = (graph: string, path: string): SavedMessage => ({ source: MESSAGE_SOURCE, type: "saved", graph, path });

export const committedMessage = (graph: string): CommittedMessage => ({ source: MESSAGE_SOURCE, type: "committed", graph });

/** A command a host sends the page. */
export interface OpenCommand {
  type: "open";
  graph: string;
  tab?: SideTab;
  level?: string;
}

/**
 * A host about to READ what the page writes (a settings panel's run button): commit the field the person is typing in
 * — a field is otherwise written only when it is left — and answer `committed` once every write is done.
 */
export interface CommitCommand {
  type: "commit";
}

/**
 * What the host runs `graph` with over its settings, by parameter name (a pane's knobs: `{rule: "cfar"}`) — the values
 * it sent last stand until it sends others; `{}` says it overrides nothing.
 */
export interface ValuesCommand {
  type: "values";
  graph: string;
  values: Record<string, Json>;
}

/**
 * A line the host shows under each of `graph`'s settings, by parameter name (`{level_db: "measured 14.6 dB"}`) — what
 * it measured for that setting; `{}` shows none. Shown only, never written.
 */
export interface HintsCommand {
  type: "hints";
  graph: string;
  hints: Record<string, string>;
}

/** The host page's font, which the settings form takes — each part optional, a part left out keeps the page's own. */
export interface StyleCommand {
  type: "style";
  fontSizePx?: number;
  fontFamily?: string;
}

export type HostCommand = OpenCommand | CommitCommand | ValuesCommand | HintsCommand | StyleCommand;

/**
 * The command in a message posted to the page, or `undefined` for anything else — another sender's message, a typo'd
 * type, a graph that is not a non-empty string, a tab the panel does not have. The page also checks WHO sent it (its own
 * parent, same origin) before it acts.
 */
export function parseHostCommand(data: unknown): HostCommand | undefined {
  if (isObject(data) && data.target === MESSAGE_SOURCE && data.type === "commit") return { type: "commit" };
  if (isObject(data) && data.target === MESSAGE_SOURCE && data.type === "values") {
    if (typeof data.graph !== "string" || data.graph.trim() === "" || !isObject(data.values)) return undefined;
    return { type: "values", graph: data.graph.trim(), values: data.values as Record<string, Json> };
  }
  if (isObject(data) && data.target === MESSAGE_SOURCE && data.type === "hints") {
    if (typeof data.graph !== "string" || data.graph.trim() === "" || !isObject(data.hints)) return undefined;
    const hints: Record<string, string> = {};
    for (const [name, line] of Object.entries(data.hints)) if (typeof line === "string" && line !== "") hints[name] = line;
    return { type: "hints", graph: data.graph.trim(), hints };
  }
  if (isObject(data) && data.target === MESSAGE_SOURCE && data.type === "style") {
    const command: StyleCommand = { type: "style" };
    const size = data.font_size_px;
    if (size !== undefined) {
      if (typeof size !== "number" || !Number.isFinite(size) || size < 6 || size > 48) return undefined;
      command.fontSizePx = size;
    }
    if (data.font_family !== undefined) {
      if (typeof data.font_family !== "string" || data.font_family.trim() === "") return undefined;
      command.fontFamily = data.font_family;
    }
    return command;
  }
  if (!isObject(data) || data.target !== MESSAGE_SOURCE || data.type !== "open") return undefined;
  if (typeof data.graph !== "string" || data.graph.trim() === "") return undefined;
  const command: OpenCommand = { type: "open", graph: data.graph.trim() };
  if (data.tab !== undefined) {
    if (typeof data.tab !== "string" || !(SIDE_TABS as readonly string[]).includes(data.tab)) return undefined;
    command.tab = data.tab as SideTab;
  }
  if (data.level !== undefined) {
    if (typeof data.level !== "string") return undefined;
    command.level = data.level;
  }
  return command;
}
