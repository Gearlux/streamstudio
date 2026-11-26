/**
 * The run tab's part of a FULL run: "run all…" asks the server for the plan (what a run would do, under the ticked
 * scopes), "Start run" starts it, and the graph's job is shown with its count and Stop, then with its ending. The
 * rules of what is said are `runall.ts`; the app keeps the jobs (it polls them) and starts and stops them.
 *
 * Hooks: `data-run-all` (the section; `data-plan` = `closed` | `asking` | `shown` | `refused`),
 * `data-action="plan"`, `data-scope=<name>` (a scope's tick box), `data-plan-lines`, `data-plan-error`,
 * `data-action="start-run"`, `data-action="close-plan"`, `data-job=<id>` with `data-job-status`, `data-job-count`,
 * `data-action="stop-run"`, `data-job-ending`.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage, planRun } from "../api";
import type { Job, PlanAnswer } from "../api";
import { countLine, endingLine, fraction, jobTitle, tickedScopes } from "../runall";

type Props = {
  graph: string;
  /** Why a full run cannot be asked for (no `runnable:`), else `""`. */
  unavailable: string;
  /** The newest job of this graph, running or ended. */
  job: Job | undefined;
  /** Whether the bar's "Run all…" asked for the plan; answered through `onAsked`. */
  asked: boolean;
  onAsked: () => void;
  onStart: (scopes: string[]) => Promise<void>;
  onStop: (job: string) => void;
};

export function JobBar({ job, onStop }: { job: Job; onStop: (job: string) => void }): JSX.Element {
  const done = fraction(job);
  return (
    <div className="job-bar" data-job={job.id} data-job-status={job.status}>
      <span className="job-title">{jobTitle(job)}</span>
      <span className="job-track" role="progressbar" aria-valuemin={0} aria-valuemax={1} aria-valuenow={done ?? undefined}>
        <span className="job-fill" style={{ width: `${Math.round((done ?? 0) * 100)}%` }} />
      </span>
      <span className="job-count" data-job-count>
        {countLine(job)}
      </span>
      <button type="button" data-action="stop-run" disabled={job.stop} onClick={() => onStop(job.id)}>
        {job.stop ? "stopping…" : "Stop"}
      </button>
    </div>
  );
}

export function RunAll({ graph, unavailable, job, asked, onAsked, onStart, onStop }: Props): JSX.Element {
  const [open, setOpen] = useState(false);
  const [ticked, setTicked] = useState<ReadonlySet<string>>(new Set());
  const [plan, setPlan] = useState<PlanAnswer | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);
  const latest = useRef(0);

  const ask = useCallback(
    async (scopes: string[]) => {
      const mine = ++latest.current;
      setOpen(true);
      setAsking(true);
      try {
        const answer = await planRun(graph, scopes);
        if (mine !== latest.current) return;
        setPlan(answer);
        setError(null);
      } catch (exc) {
        if (mine !== latest.current) return;
        setPlan(null);
        setError(errorMessage(exc));
      } finally {
        if (mine === latest.current) setAsking(false);
      }
    },
    [graph],
  );

  // another graph: its own plan, its own scopes
  useEffect(() => {
    latest.current += 1;
    setOpen(false);
    setPlan(null);
    setError(null);
    setTicked(new Set());
    setAsking(false);
  }, [graph]);

  // the bar's "Run all…" opens the plan here
  useEffect(() => {
    if (!asked) return;
    onAsked();
    if (unavailable === "") void ask([]);
  }, [asked, onAsked, ask, unavailable]);

  const tick = (scope: string, on: boolean): void => {
    const next = new Set(ticked);
    if (on) next.add(scope);
    else next.delete(scope);
    setTicked(next);
    void ask(tickedScopes(plan?.declared ?? [scope], next));
  };
  const start = async (): Promise<void> => {
    if (plan === null) return;
    try {
      await onStart(plan.scopes);
      setOpen(false);
    } catch (exc) {
      setError(errorMessage(exc));
    }
  };
  const running = job !== undefined && job.running;
  const state = !open ? "closed" : asking ? "asking" : error !== null ? "refused" : "shown";

  return (
    <div className="run-all" data-run-all data-plan={state}>
      <div className="run-controls">
        <button
          type="button"
          data-action="plan"
          disabled={unavailable !== "" || running}
          title={unavailable || "what a run of every record would do — nothing runs until Start run"}
          onClick={() => void ask(tickedScopes(plan?.declared ?? [], ticked))}
        >
          run all…
        </button>
        {unavailable !== "" && <span className="dim run-all-why">{unavailable}</span>}
      </div>
      {open && (
        <div className="run-plan">
          {plan !== null && plan.declared.length > 0 && (
            <div className="run-scopes">
              scope
              {plan.declared.map((scope) => (
                <label key={scope}>
                  <input
                    type="checkbox"
                    data-scope={scope}
                    checked={ticked.has(scope)}
                    onChange={(event) => tick(scope, event.target.checked)}
                  />
                  {scope}
                </label>
              ))}
            </div>
          )}
          {error !== null && (
            <div className="run-error" data-plan-error>
              {error}
            </div>
          )}
          {plan !== null && error === null && (
            <pre className="run-plan-lines" data-plan-lines>
              {plan.lines.join("\n")}
            </pre>
          )}
          <div className="run-controls">
            <button
              type="button"
              data-action="start-run"
              disabled={plan === null || error !== null || asking || plan.would_stop || running}
              title={plan?.would_stop ? "a check of the plan failed: a run would stop" : "run every record of the saved graph"}
              onClick={() => void start()}
            >
              Start run
            </button>
            <button type="button" data-action="close-plan" onClick={() => setOpen(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}
      {job !== undefined && job.running && <JobBar job={job} onStop={onStop} />}
      {job !== undefined && !job.running && (
        <div className={`job-ending job-${job.status}`} data-job={job.id} data-job-status={job.status} data-job-ending>
          {endingLine(job)}
        </div>
      )}
    </div>
  );
}
