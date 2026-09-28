/**
 * The run panel's pure half — the debugger face of `recordstream.flow.Tracer` through the routes.
 *
 * A run is ONE record of the graph's own source traced node by node (`POST …/trace {record, until}`); "step"
 * re-posts it pausing one node further; "rerun this node" rebuilds one node through its constructor and
 * recomputes from there (`POST api/runs/<run>/rerun {node, params}`). The tracer rebuilds a node from its
 * CURRENT values plus the change, so a rerun sends only the values that differ from what the run's node holds —
 * the canvas's value after a document edit, or a trial value typed into the panel — and an unchanged form sends
 * nothing, which recomputes the node as it is.
 */
import type { Run } from "./api";
import type { Json, ModelNode } from "./model";

/** The node "step" pauses before next: `null` runs to the end, `undefined` when the run is not paused. */
export function nextNode(run: Run): string | null | undefined {
  if (run.paused_at === null) return undefined;
  const names = run.trace.nodes.map((node) => node.node);
  const index = names.indexOf(run.paused_at);
  return index >= 0 && index + 1 < names.length ? names[index + 1] : null;
}

const isScalar = (value: Json): boolean =>
  value === null || typeof value === "number" || typeof value === "string" || typeof value === "boolean";

/**
 * The node's values a copy carries and a rerun form starts from: the ones the document SPELLS, when JSON can
 * carry them (a scalar or a list of scalars). An unspelled one stays the constructor's default; a nested object
 * (a marker, a mapping) is the document's to spell and is never re-typed through a form.
 */
export function plainParams(node: ModelNode): Record<string, Json> {
  const plain: Record<string, Json> = {};
  for (const param of Object.values(node.params)) {
    if (!param.spelled) continue;
    const value = param.value;
    if (isScalar(value) || (Array.isArray(value) && value.every(isScalar))) plain[param.name] = value;
  }
  return plain;
}

/** The entries of `wanted` that differ from `current` (compared as JSON) — what a rerun sends. */
export function changedParams(wanted: Record<string, Json>, current: Record<string, Json>): Record<string, Json> {
  return Object.fromEntries(
    Object.entries(wanted).filter(([name, value]) => JSON.stringify(value) !== JSON.stringify(current[name])),
  );
}

export interface SummaryCells {
  type: string;
  shape: string;
  dtype: string;
  range: string;
  value: string;
}

const text = (value: unknown): string =>
  value === undefined || value === null ? "" : typeof value === "string" ? value : JSON.stringify(value);

/** A number to 4 significant digits (0.9882352948188782 → 0.9882): a summary is read, not recomputed from. */
const num = (value: unknown): string => (typeof value === "number" ? String(Number(value.toPrecision(4))) : text(value));

/**
 * One traced value (`Tracer.to_dict`'s summary: never the array itself) as the columns of a table row: its type,
 * shape and dtype, its range (`min … max`, the magnitude range of a complex array, a mask's true fraction) and,
 * for a scalar, a label or anything else, the value or its repr.
 */
export function summaryCells(summary: Record<string, unknown>): SummaryCells {
  const shape = Array.isArray(summary.shape) ? summary.shape.join("×") : "";
  let range = "";
  if (summary.min !== undefined || summary.max !== undefined) range = `${num(summary.min)} … ${num(summary.max)}`;
  else if (summary.abs_min !== undefined) range = `|${num(summary.abs_min)} … ${num(summary.abs_max)}|`;
  else if (typeof summary.true_fraction === "number") range = `${Math.round(summary.true_fraction * 1000) / 10}% true`;
  const value = summary.value !== undefined ? text(summary.value) : summary.repr !== undefined ? text(summary.repr) : "";
  return { type: text(summary.type), shape, dtype: text(summary.dtype), range, value };
}
