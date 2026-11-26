/**
 * The run panel — the debugger face of `recordstream.flow.Tracer`, through the routes.
 *
 * "run" traces ONE record of the graph's own source (`POST …/trace {record, until}`), pausing BEFORE the selected
 * node when one is selected; every traced node then carries its status and wall time on the canvas, and the table
 * here lists them in schedule order. Mounted in a host (`host.ts`), a graph WITHOUT a source of its own runs the record
 * the host has selected instead (`POST …/trace {}`, no record number — a pane graph runs on the capture and window the host shows): the
 * record box gives way to `record: annotaide's selection` (`data-run-from="host"`), the summary names what ran in the
 * host's words (`data-run-ran-on`: `rec_7 · 0.25–0.75 s · 1–2 MHz`) and lists what it filled into the graph, one node a
 * line (`data-run-filled`: `window: start_s = 0.25, stop_s = 0.75`). "step" re-posts the trace pausing one node further. Clicking a node — its
 * card on the canvas, or its ROW in the table here (a step traced inside a subgraph may have no card; a row never
 * changes the selection, which is where the next run pauses) — opens its
 * entry: the input and output SUMMARIES the tracer keeps (type, shape, dtype, range — never the arrays), what it
 * changed or removed, whether it edited its input in place, and a rerun form. An entry the trace says has a PICTURE
 * (an image, a mask, a 2-D array of numbers — `run.hasPicture`) gets a row under its summary: the picture the server
 * draws (`GET api/runs/<run>/picture`), fitted to the panel, open for what the node made (`run.pictureOpen`) and
 * behind "show" for the rest; a click on it opens every pixel of it in a dialog that scrolls.
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
import type { ReactNode } from "react";
import type { Run, TraceNode } from "../api";
import { ParamWidget, valuesOf } from "../canvas/ParamWidget";
import { filledLines, ranOn, type HostInfo } from "../host";
import { shortName } from "../levels";
import type { Form, Json, ModelNode, Param } from "../model";
import { Modal } from "../dialogs/Modal";
import {
  PANEL_PICTURE_WIDTH,
  hasPicture,
  nextNode,
  pictureHref,
  pictureOpen,
  plainParams,
  rerunSends,
  rerunStart,
  runRows,
  summaryCells,
  type Side,
} from "../run";

export interface RunPanelProps {
  file: string;
  run: Run | null;
  error: string | null;
  answers: number;
  record: number;
  onRecord: (record: number) => void;
  /** The node the next run pauses before (the selection), if any. */
  until: string | undefined;
  /** The traced step whose entry is open: the card selected last, or the row clicked last. */
  inspectedName: string | undefined;
  /** Its node of the drawing — `undefined` for a step the canvas draws no card for (no rerun form then). */
  inspected: ModelNode | undefined;
  /** A row of the table was clicked: open that step's entry. */
  onInspect: (node: string) => void;
  form: Form;
  busy: boolean;
  /** The app the editor is mounted in, or `null` (`GET api/workspace`'s `host`). */
  host: HostInfo | null;
  /** Run sends the host's record (a host is there and the graph has no source of its own), not a record number. */
  fromHost: boolean;
  onRun: () => void;
  onStep: () => void;
  onRerun: (node: string, params: Record<string, Json>) => void;
  /** The full run's part of the tab (`RunAll`), or `null` where a full run is not offered (a host's panel). */
  runAll: ReactNode;
}

export function RunPanel(props: RunPanelProps) {
  const { run, error, answers, record, onRecord, until, inspected, inspectedName, busy, host, fromHost } = props;
  const next = run ? nextNode(run) : undefined;
  const traced = inspectedName !== undefined && run ? run.trace.nodes.find((node) => node.node === inspectedName) : undefined;
  return (
    <section className="run-panel" data-run-panel data-answers={answers} data-run={run?.run ?? ""}>
      <div className="run-controls" data-run-from={fromHost ? "host" : "source"}>
        {fromHost && host !== null ? (
          <span className="run-from-host" title={`the record and the window ${host.name} has selected; the editor's values run, saved or not`}>
            record: {host.name}'s selection
          </span>
        ) : (
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
        )}
        <button data-action="run" disabled={busy} onClick={props.onRun}>
          {until ? `run, pausing before ${until}` : "run"}
        </button>
        <button data-action="step" disabled={busy || next === undefined} onClick={props.onStep}>
          {next === undefined ? "step" : next === null ? "step to the end" : `step to ${next}`}
        </button>
      </div>
      {props.runAll}
      {error && (
        <div className="run-error" data-run-error>
          {error}
        </div>
      )}
      {run && (
        <>
          <div className="run-summary">
            {run.run} · <span data-run-ran-on>{ranOn(run)}</span>
            {typeof run.trace.total_ms === "number" ? ` · ${run.trace.total_ms.toFixed(2)} ms` : ""}
            {run.paused_at ? ` · paused before ${run.paused_at}` : ""}
          </div>
          {run.host && filledLines(run.host.filled).length > 0 && (
            <ul className="run-filled" data-run-filled title="what the host filled into the graph before the run">
              {filledLines(run.host.filled).map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          )}
          <table className="run-nodes">
            <tbody>
              {runRows(run.trace.nodes).map(({ node, depth, parent }) => (
                <tr
                  key={node.node}
                  data-run-row={node.node}
                  data-run-parent={parent}
                  data-depth={depth}
                  data-inspected={node.node === inspectedName ? "true" : undefined}
                  className={`run-${node.status.replace(" ", "-")}${depth > 0 ? " run-inner" : ""}`}
                  title="open this step's entry"
                  tabIndex={0}
                  onClick={() => props.onInspect(node.node)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") props.onInspect(node.node);
                  }}
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
      {inspectedName !== undefined && run && !traced && (
        <p className="dim">{inspectedName} is not a node this run traced — click one of the rows above.</p>
      )}
      {traced && run && (
        <Inspector
          key={traced.node}
          node={inspected}
          traced={traced}
          run={run.run}
          generation={run.generations[traced.node]}
          filled={run.host?.filled?.[traced.node] ?? NOTHING_FILLED}
          form={props.form}
          busy={busy}
          onRerun={(params) => props.onRerun(traced.node, params)}
        />
      )}
      {!run && !error && (
        <p className="dim">
          {fromHost && host !== null
            ? `Run the record ${host.name} has selected through ${props.file} — the values shown here run, saved or not.`
            : `Run one record of ${props.file}'s own source through the graph.`}
        </p>
      )}
    </section>
  );
}

/** What a picture is of, and where it is asked: the dialog's title and the address without a width. */
interface Enlarged {
  title: string;
  href: string;
}

interface SummaryTableProps {
  side: Side;
  summaries: unknown;
  traced: TraceNode;
  run: string;
  generation: number | null | undefined;
  onEnlarge: (picture: Enlarged) => void;
}

function SummaryTable({ side, summaries, traced, run, generation, onEnlarge }: SummaryTableProps) {
  // The entries whose picture the person opened or closed themselves; the rest follow `pictureOpen`.
  const [chosen, setChosen] = useState<Record<string, boolean>>({});
  if (typeof summaries !== "object" || summaries === null) return null;
  const entries = Object.entries(summaries as Record<string, Record<string, unknown>>);
  return (
    <table className="summary" data-summary={side}>
      <caption>{side}</caption>
      <tbody>
        {entries.flatMap(([entry, summary]) => {
          const cells = summaryCells(summary);
          const pictured = hasPicture(summary);
          const open = pictured && (chosen[entry] ?? pictureOpen(traced.changed, side, entry));
          const rows = [
            <tr key={entry} data-side={side} data-entry={entry}>
              <th>{entry}</th>
              <td data-field="type">{cells.type}</td>
              <td data-field="shape">{cells.shape}</td>
              <td data-field="dtype">{cells.dtype}</td>
              <td data-field="range">{cells.range}</td>
              {/* a raster has no value to print: its cell holds the picture's toggle, so the table gains no column */}
              <td data-field="value" title={pictured ? undefined : cells.value}>
                {pictured ? (
                  <button
                    className="picture-toggle"
                    data-picture-toggle={entry}
                    aria-expanded={open}
                    title={open ? "hide the picture of this entry" : "show the picture of this entry"}
                    onClick={() => setChosen((all) => ({ ...all, [entry]: !open }))}
                  >
                    {open ? "hide" : "show"}
                  </button>
                ) : (
                  cells.value
                )}
              </td>
            </tr>,
          ];
          if (open) {
            rows.push(
              <tr key={`${entry}:picture`} data-side={side} data-picture-row={entry}>
                <td colSpan={6}>
                  <button
                    className="picture"
                    title="every pixel of it, in a dialog"
                    onClick={() =>
                      onEnlarge({
                        title: `${traced.node} · ${side} · ${entry} — ${cells.shape}`,
                        href: pictureHref(run, traced.node, entry, side, generation),
                      })
                    }
                  >
                    <img
                      data-picture={entry}
                      alt={`${entry}, the ${side} of ${traced.node}`}
                      src={pictureHref(run, traced.node, entry, side, generation, PANEL_PICTURE_WIDTH)}
                    />
                  </button>
                </td>
              </tr>,
            );
          }
          return rows;
        })}
      </tbody>
    </table>
  );
}

const names = (value: unknown): string => (Array.isArray(value) ? value.join(", ") : "");

const NOTHING_FILLED: Record<string, Json> = {};

function Inspector({
  node,
  traced,
  run,
  generation,
  filled,
  form,
  busy,
  onRerun,
}: {
  /** The step's node of the drawing; `undefined` for a step the canvas draws no card for: its entry has no rerun form. */
  node: ModelNode | undefined;
  traced: TraceNode;
  /** The run the node was traced in, and the node's generation in it: what its pictures are asked by. */
  run: string;
  generation: number | null | undefined;
  /** What the host filled into this node before the run (a window, a band): the form starts there, and sends it only typed. */
  filled: Record<string, Json>;
  form: Form;
  busy: boolean;
  onRerun: (params: Record<string, Json>) => void;
}) {
  const [enlarged, setEnlarged] = useState<Enlarged | null>(null);
  return (
    <div className="inspect" data-inspect={traced.node}>
      <h3>
        {traced.node} <span className={`run-${traced.status.replace(" ", "-")}`}>{traced.status}</span>
      </h3>
      <div className="dim">
        {traced.op} {typeof traced.ms === "number" ? `· ${traced.ms.toFixed(3)} ms` : ""}
        {traced.in_place === true ? " · edited its input IN PLACE" : ""}
      </div>
      {typeof traced.error === "string" && <pre className="run-error">{traced.error}</pre>}
      <SummaryTable side="input" summaries={traced.input} traced={traced} run={run} generation={generation} onEnlarge={setEnlarged} />
      <SummaryTable side="output" summaries={traced.output} traced={traced} run={run} generation={generation} onEnlarge={setEnlarged} />
      {enlarged && (
        <Modal
          title={enlarged.title}
          kind="picture"
          wide
          onCancel={() => setEnlarged(null)}
          footer={
            <button data-action="close-picture" autoFocus onClick={() => setEnlarged(null)}>
              Close
            </button>
          }
        >
          <div className="picture-full" data-picture-full>
            <img alt={enlarged.title} src={enlarged.href} />
          </div>
        </Modal>
      )}
      {(names(traced.changed) || names(traced.removed)) && (
        <div className="changes">
          <span data-changed>changed: {names(traced.changed) || "—"}</span>
          <span data-removed>removed: {names(traced.removed) || "—"}</span>
        </div>
      )}
      {node !== undefined && <RerunForm node={node} traced={traced} filled={filled} form={form} busy={busy} onRerun={onRerun} />}
    </div>
  );
}

function RerunForm({
  node,
  traced,
  filled,
  form,
  busy,
  onRerun,
}: {
  node: ModelNode;
  traced: TraceNode;
  filled: Record<string, Json>;
  form: Form;
  busy: boolean;
  onRerun: (params: Record<string, Json>) => void;
}) {
  // What a rerun starts from (`run.rerunStart`): a trial typed here, the value the HOST filled in (a window, a band —
  // what ran), the document's value, else what the run's node actually used. Only a spelled value or one typed here is
  // ever SENT (`run.rerunSends`), and a value the host filled only when typed: neither a default the page never showed
  // nor the document's `0` may overwrite the host's window.
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
  const values = { touched, filled, spelled, recorded };
  const valueOf = (param: Param): Json => {
    if (param.name in touched || param.name in filled || param.name in spelled) return rerunStart(param.name, values) as Json;
    const used = recorded[param.name]; // a scalar the run used; a container is the document's to spell
    return used !== undefined && typeof used !== "object" ? used : param.value;
  };
  return (
    <>
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
                  values={valuesOf(node.params)}
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
      <button data-action="rerun" disabled={busy} onClick={() => onRerun(rerunSends(values))}>
        rerun this node
      </button>
    </>
  );
}
