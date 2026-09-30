/**
 * The run panel — the debugger face of `recordstream.flow.Tracer`, through the routes.
 *
 * "run" traces ONE record of the graph's own source (`POST …/trace {record, until}`), pausing BEFORE the selected
 * node when one is selected; every traced node then carries its status and wall time on the canvas, and the table
 * here lists them in schedule order. "step" re-posts the trace pausing one node further. Clicking a node opens its
 * entry: the input and output SUMMARIES the tracer keeps (type, shape, dtype, range — never the arrays), what it
 * changed or removed, whether it edited its input in place, and a rerun form.
 *
 * The rerun form starts from the node's current values (the canvas widgets' — the document's), and a value typed
 * here is a TRIAL: it is never written to the document. "rerun this node" sends what differs from what the run's
 * node holds now (`POST api/runs/<run>/rerun`), the tracer rebuilds the node through its constructor and recomputes
 * it and everything downstream; the canvas repaints the statuses and generations. A value the constructor refuses
 * shows its message here and leaves the run as it was. A graph with no source of its own is refused by the
 * server in words — a view chain is fed by a host — and that text is what the panel shows.
 *
 * A subgraph is traced INTO (DESIGN §1.3): its inner nodes are rows of their own, named qualified (`prep/grey`),
 * listed right under the subgraph's row and indented one step per level (`run.runRows`); each carries
 * `data-run-parent` (the subgraph it sits in) and `data-depth`. "step" goes through them in that order.
 */
import { useEffect, useState } from "react";
import type { Run, TraceNode } from "../api";
import { ParamWidget } from "../canvas/ParamWidget";
import { shortName } from "../levels";
import type { Form, Json, ModelNode, Param } from "../model";
import { changedParams, nextNode, plainParams, runRows, summaryCells } from "../run";

export interface RunPanelProps {
  file: string;
  run: Run | null;
  error: string | null;
  answers: number;
  record: number;
  onRecord: (record: number) => void;
  /** The node the next run pauses before (the selection), if any. */
  until: string | undefined;
  inspected: ModelNode | undefined;
  form: Form;
  busy: boolean;
  onRun: () => void;
  onStep: () => void;
  onRerun: (node: string, params: Record<string, Json>) => void;
}

export function RunPanel(props: RunPanelProps) {
  const { run, error, answers, record, onRecord, until, inspected, busy } = props;
  const next = run ? nextNode(run) : undefined;
  const traced = inspected && run ? run.trace.nodes.find((node) => node.node === inspected.name) : undefined;
  return (
    <section className="run-panel" data-run-panel data-answers={answers} data-run={run?.run ?? ""}>
      <div className="run-controls">
        <label>
          record
          <input
            type="number"
            min={0}
            step={1}
            data-run-record
            value={record}
            onChange={(event) => onRecord(Math.max(0, Math.trunc(Number(event.target.value) || 0)))}
          />
        </label>
        <button data-action="run" disabled={busy} onClick={props.onRun}>
          {until ? `run, pausing before ${until}` : "run"}
        </button>
        <button data-action="step" disabled={busy || next === undefined} onClick={props.onStep}>
          {next === undefined ? "step" : next === null ? "step to the end" : `step to ${next}`}
        </button>
      </div>
      {error && (
        <div className="run-error" data-run-error>
          {error}
        </div>
      )}
      {run && (
        <>
          <div className="run-summary">
            {run.run} · record #{run.record} · {typeof run.trace.total_ms === "number" ? `${run.trace.total_ms.toFixed(2)} ms` : ""}
            {run.paused_at ? ` · paused before ${run.paused_at}` : ""}
          </div>
          <table className="run-nodes">
            <tbody>
              {runRows(run.trace.nodes).map(({ node, depth, parent }) => (
                <tr
                  key={node.node}
                  data-run-row={node.node}
                  data-run-parent={parent}
                  data-depth={depth}
                  className={`run-${node.status.replace(" ", "-")}${depth > 0 ? " run-inner" : ""}`}
                >
                  <td className="run-name" style={depth > 0 ? { paddingLeft: 6 + depth * 14 } : undefined} title={node.node}>
                    {depth > 0 ? <span className="run-indent">└ </span> : null}
                    {depth > 0 ? shortName(node.node) : node.node}
                    {Array.isArray(node.inner) && <span className="run-sub"> subgraph</span>}
                  </td>
                  <td data-field="status">{node.status}</td>
                  <td data-field="ms">{typeof node.ms === "number" ? `${node.ms.toFixed(2)} ms` : ""}</td>
                  <td data-field="generation">{run.generations[node.node] ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      {inspected && run && !traced && (
        <p className="dim">{inspected.name} is not a node this run traced — click one of the rows above.</p>
      )}
      {inspected && traced && run && (
        <Inspector
          key={`${inspected.name}`}
          node={inspected}
          traced={traced}
          form={props.form}
          busy={busy}
          onRerun={(params) => props.onRerun(inspected.name, params)}
        />
      )}
      {!run && !error && <p className="dim">Run one record of {props.file}'s own source through the graph.</p>}
    </section>
  );
}

function SummaryTable({ side, summaries }: { side: "input" | "output"; summaries: unknown }) {
  if (typeof summaries !== "object" || summaries === null) return null;
  const entries = Object.entries(summaries as Record<string, Record<string, unknown>>);
  return (
    <table className="summary" data-summary={side}>
      <caption>{side}</caption>
      <tbody>
        {entries.map(([entry, summary]) => {
          const cells = summaryCells(summary);
          return (
            <tr key={entry} data-side={side} data-entry={entry}>
              <th>{entry}</th>
              <td data-field="type">{cells.type}</td>
              <td data-field="shape">{cells.shape}</td>
              <td data-field="dtype">{cells.dtype}</td>
              <td data-field="range">{cells.range}</td>
              <td data-field="value" title={cells.value}>
                {cells.value}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

const names = (value: unknown): string => (Array.isArray(value) ? value.join(", ") : "");

function Inspector({
  node,
  traced,
  form,
  busy,
  onRerun,
}: {
  node: ModelNode;
  traced: TraceNode;
  form: Form;
  busy: boolean;
  onRerun: (params: Record<string, Json>) => void;
}) {
  // What a rerun starts from: the values the document spells (the canvas's), and — for a value it leaves to the
  // constructor — what the run's node actually used. Only a spelled value or one typed here is ever SENT: a value
  // the host set on the node after construction must not be overwritten by a default the page never showed.
  const spelled = plainParams(node);
  const spelledKey = JSON.stringify(spelled);
  const recorded = (traced.params ?? {}) as Record<string, Json>;
  const [touched, setTouched] = useState<Record<string, Json>>({});
  // A document edit (on the canvas, an undo) is the new starting point: trials typed before it are dropped.
  useEffect(() => setTouched({}), [spelledKey]);
  const schemas = form[node.name] ?? {};
  const editable = Object.values(node.params).filter((param) => {
    const schema = { ...param.schema, ...(schemas[param.name] ?? {}) };
    return Array.isArray(schema.enum) || ["number", "integer", "string", "boolean"].includes(param.type);
  });
  const valueOf = (param: Param): Json =>
    param.name in touched
      ? touched[param.name]
      : param.name in spelled
        ? spelled[param.name]
        : recorded[param.name] !== undefined && typeof recorded[param.name] !== "object"
          ? recorded[param.name]
          : param.value;
  return (
    <div className="inspect" data-inspect={node.name}>
      <h3>
        {node.name} <span className={`run-${traced.status.replace(" ", "-")}`}>{traced.status}</span>
      </h3>
      <div className="dim">
        {traced.op} {typeof traced.ms === "number" ? `· ${traced.ms.toFixed(3)} ms` : ""}
        {traced.in_place === true ? " · edited its input IN PLACE" : ""}
      </div>
      {typeof traced.error === "string" && <pre className="run-error">{traced.error}</pre>}
      <SummaryTable side="input" summaries={traced.input} />
      <SummaryTable side="output" summaries={traced.output} />
      {(names(traced.changed) || names(traced.removed)) && (
        <div className="changes">
          <span data-changed>changed: {names(traced.changed) || "—"}</span>
          <span data-removed>removed: {names(traced.removed) || "—"}</span>
        </div>
      )}
      {editable.length > 0 && (
        <div className="rerun-form" data-rerun-form>
          {editable.map((param) => {
            const shown: Param = { ...param, value: valueOf(param) };
            return (
              <label className="field" key={param.name}>
                <span title={param.doc}>{param.name}</span>
                <ParamWidget
                  param={shown}
                  schema={{ ...param.schema, ...(schemas[param.name] ?? {}) }}
                  onCommit={async (value) => {
                    setTouched((all) => ({ ...all, [param.name]: value }));
                    return true;
                  }}
                />
              </label>
            );
          })}
        </div>
      )}
      <button data-action="rerun" disabled={busy} onClick={() => onRerun(changedParams({ ...spelled, ...touched }, recorded))}>
        rerun this node
      </button>
    </div>
  );
}
