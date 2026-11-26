/**
 * The FULL run of a graph — the pure half: what the page says of a job (its count, its ending), which job belongs to
 * a graph, and what the scope boxes send. The server starts the graph's `runnable:` over every record as one job
 * (`POST …/run-all`), lists the jobs (`GET api/jobs`) and stops one (`POST api/jobs/<id>/stop`); the page polls the
 * list while a job runs. `panels/RunAll.tsx` draws the run tab's part, `App.tsx` the bar's button and the strip.
 */
import type { Job, RunPlan } from "./api";

/** How often the page asks for the jobs while one runs, in milliseconds. */
export const POLL_MS = 1000;

/** The job's count as a person reads it: `312 of 807 records`, `3 records` while the total is unknown. */
export function countLine(job: Pick<Job, "done" | "total" | "unit">): string {
  return job.total === null ? `${job.done} ${job.unit}` : `${job.done} of ${job.total} ${job.unit}`;
}

/** How far the job is, 0 to 1 — `null` while the total is unknown (the bar then shows no fill). */
export function fraction(job: Pick<Job, "done" | "total">): number | null {
  if (job.total === null || job.total <= 0) return null;
  return Math.min(1, Math.max(0, job.done / job.total));
}

/** A duration in seconds as `42 s`, `4 min 12 s`, `1 h 3 min`. */
export function elapsedLine(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  if (whole < 60) return `${whole} s`;
  if (whole < 3600) return `${Math.floor(whole / 60)} min ${whole % 60} s`;
  return `${Math.floor(whole / 3600)} h ${Math.floor((whole % 3600) / 60)} min`;
}

/** What the page says of a job that ended: the server's message, a failure in its own words, and how long it took. */
export function endingLine(job: Pick<Job, "status" | "message" | "error" | "title" | "elapsed_s">): string {
  const said = job.status === "failed" ? `${job.title} failed — ${job.error}` : job.message || `${job.title}: ${job.status}`;
  return `${said} (${elapsedLine(job.elapsed_s)})`;
}

/** The job running for `graph`, if any. */
export function runningFor(jobs: readonly Job[], graph: string): Job | undefined {
  return jobs.find((job) => job.running && job.detail.graph === graph);
}

/** The newest job of `graph`, running or ended — what the run tab shows. The list is the server's: running first,
 * then the ended ones newest first. */
export function latestFor(jobs: readonly Job[], graph: string): Job | undefined {
  return jobs.find((job) => job.detail.graph === graph);
}

/** The first running job of any graph — what the strip under the bar shows. */
export function firstRunning(jobs: readonly Job[]): Job | undefined {
  return jobs.find((job) => job.running);
}

/** The scopes to send: the ticked ones, in the order the graph declares them. */
export function tickedScopes(declared: readonly string[], ticked: ReadonlySet<string>): string[] {
  return declared.filter((scope) => ticked.has(scope));
}

/** A job's title beside its count: the graph, and the scopes it runs under. */
export function jobTitle(job: Pick<Job, "detail">): string {
  const scopes = job.detail.scopes ?? [];
  return scopes.length === 0 ? job.detail.graph : `${job.detail.graph}, ${scopes.join(" + ")}`;
}

/** What a run would DELETE, by its plan: every `delete …` action of the plan and of the plans it holds, in order —
 * the lines the page puts beside Start run. The words are the runnable's own; the page adds none. */
export function deleteLines(plan: RunPlan): string[] {
  const own = plan.actions.filter((action) => action.startsWith("delete "));
  return [...own, ...plan.steps.flatMap(deleteLines)];
}
