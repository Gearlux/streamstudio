/**
 * What a link asks of the page — the query a person or a HOST opens it with — and the graph it ends up showing.
 *
 *     /studio/?graph=view_bte                    the graph view_bte, its text tab
 *     /studio/?graph=view_bte&tab=settings       the same graph, the settings tab open (a host's "settings" button)
 *     /studio/?graph=view_bte&embed=1            in a host's panel: the menus and every function, no app chrome
 *     /studio/?graph=demo&level=prep             the inside of the subgraph prep (`levels.ts`)
 *
 * The page is served at `/` by the editor itself or under a mount prefix (`/studio/`) by a host app; every request it
 * sends is RELATIVE (`api/graphs`, `api.ts`) and so is every asset the build references (`base: "./"`), so the same
 * build answers under both. `?graph=`, `?level=` and `?tab=` follow what the page shows (`pageHref`), so a reload or a
 * shared link opens the same view; `?embed=` is the host's and is kept as it was given.
 *
 * A link naming a graph the editor does not list is ANSWERED, not swallowed (`pickGraph`): measured before, `?graph=nosuch`
 * opened the first graph of the folder with no message, so a host button pointing at a graph that is not there yet
 * showed a different graph to edit.
 */
import { levelHref } from "./levels";
import type { GraphSummary } from "./model";

/** The side panel's tabs, in the order the panel shows them. */
export const SIDE_TABS = ["text", "settings", "run"] as const;
export type SideTab = (typeof SIDE_TABS)[number];

/** The tab a page opens on when the link names none. */
export const DEFAULT_TAB: SideTab = "text";

export const GRAPH_PARAM = "graph";
export const TAB_PARAM = "tab";
export const EMBED_PARAM = "embed";

/** `?tab=settings` → `settings`; a tab the panel does not have (or none) is the default. */
export function tabFromSearch(search: string): SideTab {
  const asked = new URLSearchParams(search).get(TAB_PARAM);
  return (SIDE_TABS as readonly string[]).includes(asked ?? "") ? (asked as SideTab) : DEFAULT_TAB;
}

/** `?embed=1` (or `true`, or the bare `?embed`) puts the page in a host's panel; `?embed=0` / `false` / none does not. */
export function embedFromSearch(search: string): boolean {
  const asked = new URLSearchParams(search).get(EMBED_PARAM);
  return asked !== null && !["0", "false", "no"].includes(asked.trim().toLowerCase());
}

/** The graph a link names (`?graph=`), or `null`. */
export const graphFromSearch = (search: string): string | null => new URLSearchParams(search).get(GRAPH_PARAM);

/**
 * `href` naming what the page shows: the graph, the level (`levels.levelHref`) and the tab — the default tab drops the
 * parameter, so a page that never left the text tab keeps the link it was opened with. Every other parameter
 * (`embed`) is kept as it was.
 */
export function pageHref(href: string, graph: string, level: string, tab: SideTab): string {
  const url = new URL(levelHref(href, graph, level));
  if (tab === DEFAULT_TAB) url.searchParams.delete(TAB_PARAM);
  else url.searchParams.set(TAB_PARAM, tab);
  return url.toString();
}

/** Which graph the page opens once the graph list is known. */
export type GraphPick =
  /** A listed graph: the one the link names, else the first restored one, else the first listed. */
  | { kind: "open"; name: string }
  /** The link names a graph the list does not hold: the page says so and opens nothing (`missingGraphMessage`). */
  | { kind: "missing"; wanted: string }
  /** The link names none and the list is empty. */
  | { kind: "none" };

/**
 * The graph to show once the recovery question is answered: the one a link names when it is listed — and when it is
 * NOT, nothing (`missing`): the page names the graph and the graphs there are, and the person picks one. Without a
 * link, the first RESTORED graph (the work just brought back), else the first listed.
 */
export function pickGraph(list: GraphSummary[], wanted: string | null, restored: string[]): GraphPick {
  const listed = new Set(list.map((entry) => entry.name));
  if (wanted !== null && wanted !== "") return listed.has(wanted) ? { kind: "open", name: wanted } : { kind: "missing", wanted };
  const first = restored.find((name) => listed.has(name)) ?? list[0]?.name;
  return first === undefined ? { kind: "none" } : { kind: "open", name: first };
}

/** `a`, `a and b`, `a, b and c`. */
function sayList(names: string[]): string {
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/**
 * What the page says for a link naming a graph it does not list:
 *
 *     There is no graph 'nosuch' in /w/state — the graphs there are: source, view_bte and view_fft.
 *     There is no graph 'view_bte' in /w/state — it holds no graph yet.
 *
 * `folder` is the editor's (`GET api/workspace`); the names are the list's, which also holds graphs opened from other
 * folders — every one a link can name.
 */
export function missingGraphMessage(wanted: string, list: GraphSummary[], folder: string): string {
  const where = folder === "" ? "here" : `in ${folder}`;
  const names = list.map((entry) => entry.name);
  return names.length === 0
    ? `There is no graph '${wanted}' ${where} — it holds no graph yet.`
    : `There is no graph '${wanted}' ${where} — the graphs there are: ${sayList(names)}.`;
}
