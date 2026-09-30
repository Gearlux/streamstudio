/**
 * Unsaved work survives a stop, the page's half (DESIGN2 §1 — the user's item 1: "streamstudio stop or restart should
 * save the file to a tmp workspace file (hidden), when opening that same workspace streamstudio should ask to start
 * from the tmp file or the original").
 *
 * When the editor stops (`streamstudio stop`, `restart`, Ctrl+C) with unsaved work, the server keeps it in the
 * workspace folder's hidden `.streamstudio-workspace.json`. On the next start `GET api/workspace` says so, and the page
 * asks BEFORE it loads any graph:
 *
 *     Unsaved work from the last session (29 Sep 13:25)
 *       demo.yaml         changed
 *       radio_view.yaml   changed — the file on disk changed after the editor stopped
 *     [Restore unsaved work]   [Open the saved files]
 *
 * Every kept graph is a FILE with changes: New… writes its file at once, so no graph exists only in the editor.
 *
 * Restore → `POST api/workspace/recovery/restore`: those graphs open with the kept texts, positions and groups,
 * marked ● (nothing is written until Save). Open the saved files → a second question (`Discard the unsaved work of
 * the last session? This cannot be undone.`) → `POST api/workspace/recovery/discard`. Either answers the graph list;
 * either removes the recovery file. A recovery file the server could not read comes with an `error` in its words
 * (naming the file), which the dialog shows instead of the rows; only discarding is offered then.
 *
 * The phases (`data-recovery` on the page's root): `pending` (asking the server), `asking` (the dialog is up),
 * `confirming` (the discard question is up), then `restored` / `discarded` / `none` (nothing to ask) — only those
 * three let the editor load a graph.
 */
import type { GraphSummary } from "./model";

/**
 * What the disk holds of the files a Save of a kept graph would write, against what the editor last read or wrote
 * (`document.DiskState`): the same, another text (Save refuses it), no file where there was one (deleted or moved
 * away — Save writes it again), or a file that cannot be read (`reason` says which and why).
 */
export const DISK_STATES = ["unchanged", "changed", "gone", "unreadable"] as const;
export type DiskState = (typeof DISK_STATES)[number];

/** One graph with unsaved work, as `GET api/workspace` lists it — every key always present. */
export interface RecoveredGraph {
  name: string;
  path: string;
  disk: DiskState;
  /** For `unreadable`: the file and why, in the server's words (`/x/away.yaml cannot be read: Permission denied`). */
  reason: string | null;
  /** The file on disk changed after the editor stopped (a later Save is refused until Revert or Save As). */
  changed_on_disk: boolean;
  /** When THIS graph was kept — two versions of one file, kept by two stops, are two rows with two times. */
  saved_at: string;
}

/**
 * `GET api/workspace`'s `recovery`: the unsaved work of the last session, exactly as `app.py::Studio.workspace`
 * sends it — every key always present. A readable file: `{saved_at, graphs, error: null, aside: null}`. One the editor
 * cannot use: `{saved_at: null, graphs: [], error, aside}`, `error` in the server's words, naming the file
 * (`/w/.streamstudio-workspace.json cannot be read: …`), `aside` where Open the saved files moves it.
 */
export interface Recovery {
  /** When the editor stopped (ISO time with its offset); `null` when the file could not be read. */
  saved_at: string | null;
  graphs: RecoveredGraph[];
  /** Why the recovery file cannot be used, in the server's words; `null` when it can. */
  error: string | null;
  /** Where discarding moves a file that cannot be used (never deleted: it may be the only copy); `null` otherwise. */
  aside: string | null;
}

/** `GET api/workspace`. */
export interface Workspace {
  /** The folder the editor was started on. */
  folder: string;
  /** Which editor process answered: the page sends it back with every request (`api.joinSession`). */
  session: string;
  recovery: Recovery | null;
}

export const RECOVERY_PHASES = ["pending", "asking", "confirming", "restored", "discarded", "none"] as const;
export type RecoveryPhase = (typeof RECOVERY_PHASES)[number];

/** Whether the question is answered — only then does the editor load a graph (it would read the file on disk). */
export const answered = (phase: RecoveryPhase): boolean => phase === "restored" || phase === "discarded" || phase === "none";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** `2026-09-29T13:25:07` → `29 Sep 13:25`, in the page's own time; a time it cannot read is shown as sent. */
export function savedAtLabel(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  const two = (value: number) => String(value).padStart(2, "0");
  return `${at.getDate()} ${MONTHS[at.getMonth()]} ${two(at.getHours())}:${two(at.getMinutes())}`;
}

/** The dialog's title. */
export const recoveryTitle = (recovery: Recovery): string =>
  `Unsaved work from the last session${recovery.saved_at ? ` (${savedAtLabel(recovery.saved_at)})` : ""}`;

/** One row of the dialog. */
export interface RecoveryRow {
  name: string;
  /** The file by its way from the workspace folder (`demo.yaml`, `../other/flow.yaml`). */
  file: string;
  disk: DiskState;
  changedOnDisk: boolean;
  /** What is unsaved about it, in the dialog's words. */
  note: string;
}

/**
 * Why the recovery file cannot be used — the server's words, which the dialog shows instead of the rows — or
 * `undefined` for a file it can restore. (The server sends `error: null` for a good file; a check against `undefined`
 * alone would take every good file for a broken one.)
 */
export const recoveryError = (recovery: Recovery): string | undefined =>
  typeof recovery.error === "string" && recovery.error !== "" ? recovery.error : undefined;

/**
 * `path` by its way from `folder` (R11): `/w/demo.yaml` → `demo.yaml`, `/r/other/flow.yaml` from `/r/ws` →
 * `../other/flow.yaml` — measured before, two kept `flow.yaml` rows (one of another folder) could not be told apart.
 */
export function relativePath(path: string, folder: string): string {
  const parts = path.split("/").filter(Boolean);
  const base = folder.split("/").filter(Boolean);
  let common = 0;
  while (common < base.length && common < parts.length - 1 && parts[common] === base[common]) common++;
  return [...Array(base.length - common).fill(".."), ...parts.slice(common)].join("/");
}

/** What the disk says of a kept graph, after "changed": nothing, or ` — …` in the dialog's words. */
function diskNote(graph: RecoveredGraph): string {
  if (graph.disk === "unreadable") return ` — ${graph.reason ?? `${graph.path} cannot be read`}`;
  if (graph.disk === "gone") return " — the file was deleted or moved away after the editor stopped; Save writes it again";
  if (graph.disk !== "changed") return "";
  return " — the file on disk changed after the editor stopped";
}

/** The dialog's rows, in the order the server listed them; two versions of one file each say when they were kept. */
export function recoveryRows(recovery: Recovery, folder: string): RecoveryRow[] {
  const perPath = new Map<string, number>();
  for (const graph of recovery.graphs) perPath.set(graph.path, (perPath.get(graph.path) ?? 0) + 1);
  return recovery.graphs.map((graph) => {
    const when = (perPath.get(graph.path) ?? 0) > 1 ? ` (kept ${savedAtLabel(graph.saved_at)})` : "";
    return {
      name: graph.name,
      file: relativePath(graph.path, folder),
      disk: graph.disk,
      changedOnDisk: graph.changed_on_disk,
      note: "changed" + when + diskNote(graph),
    };
  });
}

/** The question Open the saved files asks before it drops the kept work (an `AskRequest` — `dialogs/AskDialog`). */
export interface DiscardQuestion {
  title: string;
  message: string;
  confirm: string;
  danger?: boolean;
}

/**
 * What Open the saved files asks: naming the files whose changes are lost (P10 — measured before, the question named
 * none), or — for a recovery file the editor cannot read — that it is only moved aside, and where (R9 — measured
 * before, it said "This cannot be undone." of a file that is never deleted).
 */
export function discardQuestion(recovery: Recovery, folder: string): DiscardQuestion {
  if (recoveryError(recovery) !== undefined) {
    const file = `${folder.replace(/\/$/, "")}/.streamstudio-workspace.json`;
    return {
      title: "Set the unreadable unsaved work aside?",
      message: `${file} is moved to ${recovery.aside ?? "a file beside it"} — nothing is deleted — and the saved files open.`,
      confirm: "Set it aside",
    };
  }
  const files = recoveryRows(recovery, folder).map((row) => row.file);
  const named = files.length > 1 ? `${files.slice(0, -1).join(", ")} and ${files[files.length - 1]}` : files.join("");
  return {
    title: "Discard the unsaved work of the last session?",
    message: `The unsaved changes of ${named} are lost. This cannot be undone.`,
    confirm: "Discard",
    danger: true,
  };
}

/**
 * The graph shown once the question is answered: the one a link names (`?graph=`) when listed, else the first
 * RESTORED one (the work just brought back), else the first listed.
 */
export function firstGraph(list: GraphSummary[], wanted: string | null, restored: string[]): string | undefined {
  const listed = new Set(list.map((entry) => entry.name));
  if (wanted !== null && listed.has(wanted)) return wanted;
  return restored.find((name) => listed.has(name)) ?? list[0]?.name;
}

/** An entry of the list `POST api/workspace/recovery/restore` answers: whether the restore opened it. */
export type RestoredSummary = GraphSummary & { restored?: boolean };

/**
 * The names the restored graphs have in the list `restore` answered, in the recovery file's order — the entries the
 * server flagged `restored`, matched by PATH (a name taken since the stop is restored under a free one, `demo~2`, and
 * a file kept twice, or held unsaved by this editor too, opens beside the other version).
 */
export function restoredNames(list: RestoredSummary[], recovery: Recovery): string[] {
  const names: string[] = [];
  for (const kept of recovery.graphs) {
    const entry = list.find((candidate) => candidate.restored === true && candidate.path === kept.path && !names.includes(candidate.name));
    if (entry !== undefined) names.push(entry.name);
  }
  return names;
}
