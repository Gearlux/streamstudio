/**
 * Explicit Save, the page's half. An edit changes the document the server holds IN MEMORY; nothing reaches the file
 * until Save (`POST …/save`). The server says, per graph, whether it holds changes not yet written (`dirty` on every
 * graph answer and on each `GET api/graphs` entry); the page shows it — ● on the page title, on the bar's file name
 * and in the graph dropdown, `data-dirty` on the root — and asks before the page is closed or reloaded while any
 * graph is dirty (`beforeunload`).
 *
 * A layout write (`PUT …/positions`, `PUT …/groups`) answers only the layout, not the graph, so after one the page
 * marks the graph dirty itself — the server's rule says positions or groups set since the last Save make it dirty.
 */
import type { GraphSummary } from "./model";

export const DIRTY_MARK = "●";

/** The list with one entry's flag set; the SAME list when nothing changes (no redraw) or the name is not listed. */
export function markDirty(list: GraphSummary[], name: string, dirty: boolean): GraphSummary[] {
  const index = list.findIndex((entry) => entry.name === name);
  if (index < 0 || list[index].dirty === dirty) return list;
  return list.map((entry, i) => (i === index ? { ...entry, dirty } : entry));
}

/** Whether leaving the page loses something: a listed graph is dirty (every graph the page shows is listed). */
export function anyDirty(list: GraphSummary[]): boolean {
  return list.some((entry) => entry.dirty);
}

/** The browser tab's title. */
export const pageTitle = (dirty: boolean): string => `${dirty ? `${DIRTY_MARK} ` : ""}streamstudio editor`;

/** A graph dropdown option. */
export const pickerLabel = (entry: GraphSummary): string => `${entry.dirty ? `${DIRTY_MARK} ` : ""}${entry.name} · ${entry.kind}`;

/** The file name the bar shows. */
export const fileTitle = (graph: { path: string; dirty: boolean }): string =>
  `${graph.dirty ? `${DIRTY_MARK} ` : ""}${graph.path.split("/").pop() ?? graph.path}`;
