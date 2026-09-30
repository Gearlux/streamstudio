/**
 * The editor page: a menu bar over the canvas (New / File / Edit / View / Graph / Run, ComfyUI style), the Nodes
 * sidebar on the left, a side panel with three tabs (the document's text, the settings form, the run panel), the
 * right-click menus, the dialogs and the toasts. The app owns the graph state and every route call; the canvas, the
 * nodes and the panels ask for what they need through props and the editor context.
 *
 * Every write goes to the server and the server's answer is drawn — the page never shows an edit the document did
 * not accept. A refusal is a toast carrying the server's located message verbatim. Edits change the document the
 * server holds IN MEMORY; File ▸ Save (Ctrl/Cmd+S) writes it (Explicit Save, `dirty.ts`): ● marks the title and the
 * dropdown while a graph holds unsaved changes, and leaving the page asks first. Undo/redo follows `history.ts`: a
 * document edit is undone by the SERVER's text history, a layout change (a move, an arrange, a group) by the page,
 * which wrote it; one Ctrl/Cmd+Z reverses whichever came last.
 *
 * One command table (`commands.ts`) drives the menu bar, the right-click menus and the keyboard, so a shortcut and a
 * menu item always do the same thing and are greyed together; keys are ignored while typing in a field.
 *
 * Subgraphs (DESIGN §3 F2): the canvas draws ONE level — the top, or the inside of a subgraph node (`prep`,
 * `prep/inner`, `levels.ts`). Open (the node's button, a double-click, its menu) enters one; a breadcrumb crumb or
 * Escape goes up. Inside, every gesture targets that level: the nodes it acts on are the level's (qualified,
 * `prep/grey`), a step added from the Nodes sidebar or the palette, a paste, a template and a steps file go after the
 * selected inner node (else the last inner step), a new step or subgraph is named on the level (`prep/threshold`),
 * and groups are made with `parent` = the level. When an edit or an undo takes the level away, the canvas climbs to
 * the deepest level still there; a rename of a subgraph the canvas is inside follows it.
 *
 * Op lists (DESIGN2 §3): on a `Stream` (a plain graph whose level holds an `ops` / `transforms` list) a class added
 * from the Nodes sidebar, Add Node ▸ or either palette becomes a list MEMBER — after the selected member, else at the
 * end of the list (`oplists.insertTarget`, `POST …/steps {list}`); Delete and Cut remove members like steps, in ONE
 * `POST …/steps/remove`. Members are named by position, so after a remove nothing stays selected.
 *
 * Unsaved work of the last session (DESIGN2 §1): before any graph is loaded the page asks `GET api/workspace`; when
 * the last stop kept unsaved work, the recovery dialog asks Restore / Open the saved files first (`recovery.ts`).
 *
 * `?graph=<name>` picks the graph on load (the first listed otherwise — after a Restore, the first restored) and
 * `?level=<subgraph>` the level drawn; both are kept in step with what the canvas shows, so a reload or a link opens
 * that graph at that level (a level the document no longer holds falls back to the deepest one it does). The root
 * carries `data-graph`, `data-ready`, `data-saves` (accepted writes so far), `data-dirty`, `data-level` (the level
 * drawn, `""` at the top), `data-palette-ready`, `data-selection` (the document nodes a gesture acts on) and
 * `data-recovery` (`pending` | `asking` | `confirming` | `restored` | `discarded` | `none`) — what a test waits on
 * instead of timing.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ReactFlowProvider, useReactFlow } from "@xyflow/react";
import {
  ApiError,
  addStep,
  checkGraph,
  convertToSubgraph,
  copyNodes,
  deleteTemplate as deleteTemplateRoute,
  discardRecovery,
  errorMessage,
  extractSubgraph,
  getForm,
  getGraph,
  getPalette,
  getTypes,
  getWorkspace,
  includeSteps,
  insertTemplate as insertTemplateRoute,
  joinSession,
  listGraphs,
  listTemplates,
  newGraph,
  onRestarted,
  openGraph,
  pasteText,
  putGroups,
  putPositions,
  redo as postRedo,
  relayout,
  removeSteps,
  renameNode,
  rerun as postRerun,
  restoreRecovery,
  revertGraph,
  saveGraph,
  saveGraphAs,
  saveTemplate,
  setParam as postParam,
  trace,
  undo as postUndo,
  type Copied,
  type NodeSpec,
  type Run,
  type Template,
} from "./api";
import { Canvas, type CanvasMenuRequest, type LayoutChange, type PaletteGesture } from "./canvas/Canvas";
import { Palette, type PaletteRequest } from "./canvas/Palette";
import { enabled, isMac, matchShortcut, type CommandId, type CommandState, type MenuId } from "./commands";
import { EditorContext, type Editing, type EditorApi } from "./context";
import { AskDialog, type AskAnswer, type AskRequest } from "./dialogs/AskDialog";
import { CheckDialog } from "./dialogs/CheckDialog";
import { FileDialog, type FileAnswer, type FileRequest } from "./dialogs/FileDialog";
import { NewDialog, type NewAnswer } from "./dialogs/NewDialog";
import { RecoveryDialog } from "./dialogs/RecoveryDialog";
import { anyDirty, fileTitle, markDirty, pageTitle, pickerLabel } from "./dirty";
import { baseName, dirName, stepsFileName } from "./files";
import {
  DEFAULT_BOX,
  absolutePositions,
  addGroupAt,
  groupNodeId,
  groupTogether,
  placeBox,
  replaceConverted,
  resizeGroup as resizeGroups,
  type Group,
  type Rect,
  type XY,
} from "./groups";
import { Timeline, type TimelineEntry } from "./history";
import { arrange } from "./layout";
import {
  EMPTY_MODEL,
  allNodeNames,
  existingLevel,
  findNode,
  followShifts,
  innerSteps as innerStepsOf,
  levelAnchor,
  levelFromSearch,
  levelHref,
  levelModel,
  levelPredecessor,
  parentLevel,
  qualify,
  rekeyName,
  shiftKeys,
  shiftName,
  shortName,
  stepNames,
  type NameShift,
} from "./levels";
import { AddNodeMenu } from "./menu/AddNodeMenu";
import { ContextMenu } from "./menu/ContextMenu";
import type { Entry } from "./menu/entries";
import { MenuBar } from "./menu/MenuBar";
import { barMenus, groupMenu, nodeMenu, paneMenu, type MenuContext } from "./menus";
import type { CanvasNode, Form, Graph, GraphSummary, Json, Model, NewGraphKind, Positions, Problem, Supertypes } from "./model";
import { NodesSidebar } from "./panels/NodesSidebar";
import { RunPanel } from "./panels/RunPanel";
import { SettingsForm } from "./panels/SettingsForm";
import { addedMember, beside, inputTarget, insertTarget, shiftsMembers, stepFields, wireTarget, type InsertTarget } from "./oplists";
import {
  answered as recoveryAnswered,
  discardQuestion,
  firstGraph,
  restoredNames as restoredIn,
  type Recovery,
  type RecoveryPhase,
} from "./recovery";
import { nextNode } from "./run";
import { addableSpecs } from "./sidebar";
import { spotBeside, stepName, uniqueName } from "./steps";
import { useToasts } from "./toast";
import "./styles.css";

export default function App() {
  return (
    <ReactFlowProvider>
      <Editor />
    </ReactFlowProvider>
  );
}

const SIDE_TABS = ["text", "settings", "run"] as const;
type SideTab = (typeof SIDE_TABS)[number];

/** A card's size until React Flow has measured it. */
const CARD = { width: 240, height: 120 };

const SIDEBAR_KEY = "streamstudio.nodes.open";

type Dialog =
  | { kind: "file"; request: FileRequest; resolve: (answer: FileAnswer | null) => void }
  | { kind: "ask"; request: AskRequest; resolve: (answer: AskAnswer | null) => void }
  | { kind: "new"; newKind: NewGraphKind }
  | { kind: "check"; file: string; problems: Problem[] };

const fileOf = (graph: Pick<Graph, "path">): string => graph.path.split("/").pop() ?? graph.path;

const isTyping = (target: EventTarget | null): boolean =>
  target instanceof HTMLElement && (/^(INPUT|SELECT|TEXTAREA)$/.test(target.tagName) || target.isContentEditable);

const readSidebar = (): boolean => {
  try {
    return window.localStorage.getItem(SIDEBAR_KEY) === "1";
  } catch {
    return false;
  }
};

const writeSidebar = (open: boolean): void => {
  try {
    window.localStorage.setItem(SIDEBAR_KEY, open ? "1" : "0");
  } catch {
    // storage may be blocked; the sidebar still opens for this page
  }
};

/** A slug a subgraph step can be named by: a group title `front end` → `front_end`. */
const slug = (text: string): string => text.trim().toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "") || "subgraph";

function Editor() {
  const flow = useReactFlow<CanvasNode>();
  const mac = useMemo(isMac, []);
  const [graphs, setGraphs] = useState<GraphSummary[]>([]);
  const [listed, setListed] = useState(false);
  const [graph, setGraph] = useState<Graph | null>(null);
  const [form, setForm] = useState<Form>({});
  const [types, setTypes] = useState<Supertypes>({});
  const [specs, setSpecs] = useState<NodeSpec[] | null>(null);
  /** What the Nodes sidebar, the palette and Add Node ▸ offer: the catalogue less the Subgraph class (made, not added). */
  const addable = useMemo(() => (specs === null ? null : addableSpecs(specs)), [specs]);
  const [saves, setSaves] = useState(0);
  const [selection, setSelection] = useState<string[]>([]);
  const [groupsSelected, setGroupsSelected] = useState<string[]>([]);
  const [palette, setPalette] = useState<PaletteRequest | null>(null);
  const [tab, setTab] = useState<SideTab>("text");
  const [run, setRun] = useState<Run | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [answers, setAnswers] = useState(0);
  const [record, setRecord] = useState(0);
  const [running, setRunning] = useState(false);
  const [inspected, setInspected] = useState<string | undefined>(undefined);
  const [sidebar, setSidebar] = useState(readSidebar);
  const [minimap, setMinimap] = useState(true);
  const [menuOpen, setMenuOpen] = useState<MenuId | null>(null);
  const [context, setContext] = useState<CanvasMenuRequest | null>(null);
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [templates, setTemplates] = useState<Template[] | null>(null);
  const [hasClip, setHasClip] = useState(false);
  /** A copy is on its way (`POST …/copy`): Paste acts already — it waits for the copy (`copying`) instead of dropping. */
  const [copyPending, setCopyPending] = useState(false);
  const [editing, setEditing] = useState<Editing>(null);
  /** The subgraph level asked for (`""` = the top); what is DRAWN is the deepest part of it the document holds. */
  const [level, setLevel] = useState("");
  /**
   * The unsaved work the last session kept (`recovery.ts`): no graph is loaded until the question is answered — a
   * graph loaded first would be read from the disk the recovered texts are meant to replace.
   */
  const [recoveryPhase, setRecoveryPhase] = useState<RecoveryPhase>("pending");
  const [recovery, setRecovery] = useState<Recovery | null>(null);
  /** The folder the editor serves (`GET api/workspace`): the recovery question names files by their way from it. */
  const [folder, setFolder] = useState("");
  /** A Restore or a discard the server refused, in its words — shown in the dialog, which stays up. */
  const [recoveryRefused, setRecoveryRefused] = useState<string | null>(null);
  const [recoveryBusy, setRecoveryBusy] = useState(false);
  /** The graphs a Restore brought back: the first is shown when no link names a graph. */
  const restoredNames = useRef<string[]>([]);
  /** A Restore or a discard answered the graph list — the first `GET api/graphs`, if later, must not replace it. */
  const answeredList = useRef(false);
  const { toasts, push, dismiss } = useToasts();
  const timeline = useRef(new Timeline());
  /** The page's clipboard: the TEXT `POST …/copy` answered — a subgraph's whole block, pastable into any graph. */
  const clipboard = useRef<Copied | null>(null);
  /**
   * The copy on its way, or `null`. Ctrl/Cmd+V pressed right after Ctrl/Cmd+C arrives before the copy's answer
   * (measured: Paste stayed greyed and the key did nothing); the paste AWAITS this, then runs.
   */
  const copying = useRef<Promise<Copied | null> | null>(null);
  /** The level a link asked for (`?level=prep`), handed to the first graph shown and then forgotten. */
  const linkedLevel = useRef("");
  // The latest graph for handlers that run after an await — a closure's copy would be one answer behind.
  const graphRef = useRef(graph);
  graphRef.current = graph;
  // The level drawn: an undo, a delete or a rename elsewhere may have taken the asked-for one away.
  const shown = graph === null ? "" : existingLevel(graph.model, level);
  const levelRef = useRef(shown);
  levelRef.current = shown;
  useEffect(() => {
    if (shown !== level) setLevel(shown);
  }, [shown, level]);

  /** The model of the level drawn, read at the gesture (after an await the closure's copy is one answer behind). */
  const viewNow = useCallback((): Model => {
    const current = graphRef.current;
    return current === null ? EMPTY_MODEL : (levelModel(current.model, levelRef.current) ?? EMPTY_MODEL);
  }, []);

  useEffect(() => {
    listGraphs().then(
      (list) => {
        if (answeredList.current) return; // a Restore / discard answered a newer list (with the restored graphs)
        setGraphs(list);
        setListed(true);
      },
      (exc) => push("error", errorMessage(exc)),
    );
    getTypes().then(setTypes, (exc) => push("error", errorMessage(exc)));
    // the catalogue is scanned once per server (seconds on a cold one): fetched in the background, never awaited
    getPalette().then(setSpecs, (exc) => push("error", errorMessage(exc)));
    // unsaved work the last session kept when it stopped: asked about before any graph is loaded
    getWorkspace().then(
      (workspace) => {
        joinSession(workspace.session); // every request from now on says which editor it was meant for (R1, P1)
        setFolder(workspace.folder);
        setRecovery(workspace.recovery);
        setRecoveryPhase(workspace.recovery === null ? "none" : "asking");
      },
      (exc) => {
        setRecoveryPhase("none"); // the editor works without the answer; it only cannot offer the kept work
        push("warning", `could not ask the editor about unsaved work of the last session: ${errorMessage(exc)}`);
      },
    );
  }, [push]);

  /** Show a graph the server answered, with its form: a fresh timeline, no run, nothing selected. */
  const present = useCallback((loaded: Graph, schema: Form) => {
    timeline.current.clear();
    setForm(schema);
    setGraph(loaded);
    setGraphs((list) => markDirty(list, loaded.name, Boolean(loaded.dirty)));
    setRun(null);
    setRunError(null);
    setInspected(undefined);
    setSelection([]);
    setGroupsSelected([]);
    setPalette(null);
    setContext(null);
    setEditing(null);
    // a link's ?level= opens that level once (it falls back to the deepest one the document holds); any other load
    // starts at the top
    setLevel(linkedLevel.current);
    levelRef.current = linkedLevel.current;
    linkedLevel.current = "";
  }, []);

  const load = useCallback(
    async (name: string) => {
      try {
        const [loaded, schema] = await Promise.all([getGraph(name), getForm(name)]);
        present(loaded, schema);
      } catch (exc) {
        push("error", errorMessage(exc));
      }
    },
    [present, push],
  );

  /** A graph an answer brought (New, Open): the list first — it may be new to it — then the graph. */
  const show = useCallback(
    async (loaded: Graph) => {
      try {
        const [schema, list] = await Promise.all([getForm(loaded.name), listGraphs()]);
        setGraphs(list);
        present(loaded, schema);
      } catch (exc) {
        push("error", errorMessage(exc));
      }
    },
    [present, push],
  );

  useEffect(() => {
    if (graph !== null || graphs.length === 0 || !recoveryAnswered(recoveryPhase)) return;
    const wanted = new URLSearchParams(window.location.search).get("graph");
    const first = firstGraph(graphs, wanted, restoredNames.current);
    if (first === undefined) return;
    linkedLevel.current = first === wanted ? levelFromSearch(window.location.search) : "";
    void load(first);
  }, [graph, graphs, load, recoveryPhase]);

  // ---- the unsaved work of the last session (DESIGN2 §1) ---------------------------------------------------------

  /**
   * The question is answered: `list` is the graph list the answer brought, and the graph to show is picked afresh (the
   * load effect) — a graph shown before the answer would be the file on disk, not the work the answer restored.
   */
  const settleRecovery = useCallback((list: GraphSummary[], phase: RecoveryPhase) => {
    answeredList.current = true;
    graphRef.current = null;
    setGraph(null);
    setGraphs(list);
    setListed(true);
    setRecoveryPhase(phase);
  }, []);

  /**
   * A Restore or a discard the server refused: when there is no kept work any more — another tab answered the
   * question (R12, P4) — the question goes and the graphs load (measured before: both buttons were refused and the
   * dialog could not be left); else the dialog shows the refusal in the server's words, with what the file now says.
   */
  const recoveryRefusal = useCallback(
    async (exc: unknown) => {
      if (exc instanceof ApiError && exc.status === 404) {
        try {
          const workspace = await getWorkspace();
          if (workspace.recovery === null) {
            settleRecovery(await listGraphs(), "none");
            push("info", "the unsaved work of the last session was already answered (in another tab) — the graphs open as the editor holds them");
            return;
          }
          setRecovery(workspace.recovery);
        } catch (again) {
          exc = again;
        }
      }
      setRecoveryRefused(errorMessage(exc));
      setRecoveryPhase("asking");
    },
    [push, settleRecovery],
  );

  /** Restore unsaved work: the kept graphs open with their texts, positions and groups (dirty; Save writes them). */
  const restoreWork = useCallback(async () => {
    setRecoveryBusy(true);
    setRecoveryRefused(null);
    try {
      const list = await restoreRecovery();
      restoredNames.current = recovery === null ? [] : restoredIn(list, recovery);
      settleRecovery(list, "restored");
    } catch (exc) {
      await recoveryRefusal(exc);
    } finally {
      setRecoveryBusy(false);
    }
  }, [recovery, recoveryRefusal, settleRecovery]);

  /**
   * The editor this page talked to was restarted (a `409` — R1, P1): nothing the page sent was done. The page forgets
   * what it drew, joins the new editor and asks it everything again — the list, and the recovery question, which now
   * offers the work the stop kept. Measured before: the page kept editing the NEW editor's document (read from disk),
   * the kept edit vanished on the next change, and Save wrote the file without it and said clean.
   */
  const rejoining = useRef(false);
  const rejoin = useCallback(async () => {
    if (rejoining.current) return;
    rejoining.current = true;
    joinSession(null);
    timeline.current.clear();
    graphRef.current = null;
    setGraph(null);
    setRun(null);
    setRunError(null);
    setPalette(null);
    setContext(null);
    setDialog(null);
    answeredList.current = false;
    restoredNames.current = [];
    setRecoveryRefused(null);
    setRecoveryPhase("pending");
    try {
      const [workspace, list] = await Promise.all([getWorkspace(), listGraphs()]);
      joinSession(workspace.session);
      setFolder(workspace.folder);
      setGraphs(list);
      setListed(true);
      setRecovery(workspace.recovery);
      setRecoveryPhase(workspace.recovery === null ? "none" : "asking");
    } catch (exc) {
      setRecoveryPhase("none");
      push("error", errorMessage(exc));
    } finally {
      rejoining.current = false;
    }
  }, [push]);
  useEffect(() => {
    onRestarted(() => void rejoin());
    return () => onRestarted(null);
  }, [rejoin]);

  // `?graph=` and `?level=` follow what the canvas shows, so a reload or a shared link opens the same graph and level.
  const shownName = graph?.name;
  useEffect(() => {
    if (shownName === undefined) return;
    const href = levelHref(window.location.href, shownName, shown);
    if (href !== window.location.href) window.history.replaceState(null, "", href);
  }, [shownName, shown]);

  // A node the form has never described (a step just added, a node renamed — on ANY level, the settings tab lists the
  // level shown) gets its schema from the server; once per answer, so a name the form never holds cannot loop.
  const formAsked = useRef<Graph | null>(null);
  useEffect(() => {
    if (graph === null || formAsked.current === graph || allNodeNames(graph.model).every((name) => name in form)) return;
    formAsked.current = graph;
    getForm(graph.name).then(setForm, (exc) => push("error", errorMessage(exc)));
  }, [graph, form, push]);

  // Explicit Save: the tab says ●, and leaving asks first, while anything is unsaved.
  const dirtyShown = Boolean(graph?.dirty);
  useEffect(() => {
    document.title = pageTitle(dirtyShown);
  }, [dirtyShown]);
  const leaving = anyDirty(graphs);
  useEffect(() => {
    if (!leaving) return;
    const onLeave = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = ""; // Chrome shows its own "Leave site?" text; the value only arms it
    };
    window.addEventListener("beforeunload", onLeave);
    return () => window.removeEventListener("beforeunload", onLeave);
  }, [leaving]);

  /**
   * An accepted write: draw the server's answer, mark the list, count it, and remember what to undo. `graphRef` takes
   * the answer AT ONCE: a gesture that awaits another route after this one (Convert group to subgraph writes the
   * layout after the conversion) must build on this answer, not on the render before it (measured: it drew the steps
   * the conversion had removed, and the next edit named them).
   */
  const accept = useCallback((next: Graph, entry?: TimelineEntry, count = true) => {
    graphRef.current = next;
    setGraph(next);
    setGraphs((list) => markDirty(list, next.name, Boolean(next.dirty)));
    if (count) setSaves((saved) => saved + 1);
    if (entry) timeline.current.record(entry);
  }, []);

  // ---- dialogs as promises ---------------------------------------------------------------------------------------

  const askFile = useCallback(
    (request: FileRequest) => new Promise<FileAnswer | null>((resolve) => setDialog({ kind: "file", request, resolve })),
    [],
  );
  const ask = useCallback(
    (request: AskRequest) => new Promise<AskAnswer | null>((resolve) => setDialog({ kind: "ask", request, resolve })),
    [],
  );

  /**
   * Open the saved files: asked once more — naming the files whose changes are lost (P10), or saying that an unreadable
   * recovery file is only moved aside (R9) — then the kept work is dropped (the recovery file goes).
   */
  const discardWork = useCallback(async () => {
    if (recovery === null) return;
    setRecoveryPhase("confirming");
    const sure = await ask(discardQuestion(recovery, folder));
    if (sure === null) {
      setRecoveryPhase("asking");
      return;
    }
    setRecoveryBusy(true);
    setRecoveryRefused(null);
    try {
      settleRecovery(await discardRecovery(), "discarded");
    } catch (exc) {
      await recoveryRefusal(exc);
    } finally {
      setRecoveryBusy(false);
    }
  }, [ask, folder, recovery, recoveryRefusal, settleRecovery]);

  // ---- the selection, read at the gesture ------------------------------------------------------------------------

  /**
   * The selected document nodes, in the model's order, read from React Flow AT THE GESTURE. The `selection` state
   * (fed by React Flow's selection listener) arrives one effect after a click — measured: a Ctrl+C typed right
   * after a click read an empty selection — so it only drives what the menus grey; every gesture reads this.
   */
  const selectedNow = useCallback((): string[] => {
    if (graphRef.current === null) return [];
    const picked = flow.getNodes().filter((node) => node.selected && node.type === "generic").map((node) => node.id);
    return stepNames(viewNow(), picked); // the level's steps — a subgraph's <in>/<out> is drawn, never acted on
  }, [flow, viewNow]);

  const selectedGroupsNow = useCallback(
    (): string[] =>
      flow
        .getNodes()
        .filter((node) => node.selected && node.type === "group")
        .map((node) => (node.type === "group" ? node.data.group.id : "")),
    [flow],
  );

  /** Make exactly `names` the selection (on the canvas; the listener then updates `data-selection`). */
  const selectOnly = useCallback(
    (names: string[]) => {
      const wanted = new Set(names);
      flow.setNodes((all) =>
        all.map((node) => {
          const on = node.type === "generic" && wanted.has(node.id);
          return node.selected === on ? node : { ...node, selected: on };
        }),
      );
    },
    [flow],
  );

  /**
   * Where a step is inserted, on the level drawn: after the last selected node, else — at the top — the flow's output
   * or the last node, and — inside a subgraph — the last inner step (`levels.levelAnchor`).
   */
  const anchorNow = useCallback((): string | undefined => {
    if (graphRef.current === null) return undefined;
    const selected = selectedNow();
    return levelAnchor(viewNow(), levelRef.current, selected[selected.length - 1]);
  }, [selectedNow, viewNow]);

  /**
   * Where an ADDED class goes (the Nodes sidebar, Add Node ▸, a drop): `anchorNow`'s rule on a level of flow steps,
   * and on an op list after the selected member, else at the end of the list (`oplists.insertTarget`).
   */
  const targetNow = useCallback((): InsertTarget => {
    const current = graphRef.current;
    if (current === null) return { kind: "none" };
    const selected = selectedNow();
    return insertTarget(viewNow(), levelRef.current, current.kind, selected[selected.length - 1]);
  }, [selectedNow, viewNow]);

  // ---- levels: which subgraph's inside the canvas draws ----------------------------------------------------------

  /** Draw the level `next` (Open, a crumb, Escape): nothing stays selected, no menu or palette stays open. */
  const goTo = useCallback((next: string) => {
    levelRef.current = next;
    setLevel(next);
    setSelection([]);
    setGroupsSelected([]);
    setPalette(null);
    setContext(null);
    setEditing(null);
  }, []);

  /** Show the inside of the subgraph node `name` — the server must have sent it (`model.inside`). */
  const openSubgraph = useCallback(
    (name: string) => {
      const current = graphRef.current;
      if (current === null) return;
      if (levelModel(current.model, name) === undefined) {
        push("error", `${fileOf(current)}: the editor sent no inside for ${name}, so it cannot be shown`);
        return;
      }
      goTo(name);
    },
    [goTo, push],
  );

  // ---- document edits: the server's text history holds them ------------------------------------------------------

  const setParam = useCallback(
    async (node: string, param: string, value: Json): Promise<boolean> => {
      const current = graphRef.current;
      if (current === null) return false;
      try {
        accept(await postParam(current.name, node, param, value), { kind: "document" });
        return true;
      } catch (exc) {
        push("error", errorMessage(exc));
        return false;
      }
    },
    [accept, push],
  );

  const rename = useCallback(
    async (node: string, name: string): Promise<boolean> => {
      const current = graphRef.current;
      if (current === null) return false;
      try {
        const next = await renameNode(current.name, node, name);
        timeline.current.rekey(node, name);
        // a subgraph renamed moves every name inside it — and the level drawn, when the canvas is inside it
        const moved = rekeyName(levelRef.current, node, name);
        if (moved !== levelRef.current) {
          levelRef.current = moved;
          setLevel(moved);
        }
        accept(next, { kind: "document" });
        setSelection((names) => names.map((n) => rekeyName(n, node, name)));
        setInspected((was) => (was === undefined ? was : rekeyName(was, node, name)));
        return true;
      } catch (exc) {
        push("error", errorMessage(exc));
        return false;
      }
    },
    [accept, push],
  );

  /** The rectangle a drawn node covers, in flow coordinates. */
  const rectOf = useCallback(
    (name: string): Rect | undefined => {
      const drawn = flow.getNodes();
      const at = absolutePositions(drawn)[name];
      const node = drawn.find((candidate) => candidate.id === name);
      if (!at || !node) return undefined;
      return { ...at, width: node.measured?.width ?? CARD.width, height: node.measured?.height ?? CARD.height };
    },
    [flow],
  );

  /**
   * Where a node added by a click goes (P8): below the card it follows (`near`), clear of every card of the level
   * drawn — at their places in the ANSWER (`next.positions`), since the server lays the later members of a list out
   * again when one is inserted before them. Measured before: the spot was found against the places before the add and
   * the new card landed on top of the member that had moved down. The drawn cards' measured sizes are carried to the
   * names the add gave them (`shift`: members are named by position).
   */
  const spotAfterAdd = useCallback(
    (next: Graph, near: string, added: string | undefined, shift: NameShift | undefined): XY | undefined => {
      const drawn = Object.fromEntries(
        flow
          .getNodes()
          .filter((node) => node.type === "generic")
          .map((node) => [node.id, { width: node.measured?.width ?? CARD.width, height: node.measured?.height ?? CARD.height }]),
      );
      const sizes = shift === undefined ? drawn : shiftKeys(drawn, shift);
      const rect = (name: string): Rect | undefined => {
        const xy = next.positions[name];
        return xy === undefined ? undefined : { x: xy[0], y: xy[1], ...(sizes[name] ?? CARD) };
      };
      const anchor = rect(near);
      if (anchor === undefined) return undefined;
      const view = levelModel(next.model, levelRef.current) ?? EMPTY_MODEL;
      const cards = view.nodes
        .filter((node) => node.name !== added)
        .map((node) => rect(node.name))
        .filter((card): card is Rect => card !== undefined);
      // the new card is not measured yet: it is taken as large as the largest card of the level (measured: a
      // PrintRecordOp card is ~190 px tall, the default guess 120 — a spot kept clear of a 120 px card overlapped)
      const size = {
        width: Math.max(CARD.width, ...cards.map((card) => card.width)),
        height: Math.max(CARD.height, ...cards.map((card) => card.height)),
      };
      return spotBeside(anchor, cards, size);
    },
    [flow],
  );

  /**
   * Place the nodes an edit added (the ones `next` has and `before` had not) at `at`, keeping their offsets; the
   * position write rides on the SAME gesture (one count, one undo — the document entry). A list MEMBER is named by
   * its position, so the members after it were renamed and a diff of names would pick the LAST one (`stream.ops[5]`),
   * not the one added (`stream.ops[3]`): `member` names it (`oplists.addedMember`).
   */
  const placeNew = useCallback(
    async (before: Graph, next: Graph, at: XY | undefined, member?: string): Promise<Graph> => {
      if (at === undefined) return next;
      let moved: Positions;
      if (member !== undefined) {
        if (!allNodeNames(next.model).includes(member)) return next;
        moved = { [member]: [at.x, at.y] };
      } else {
        // every level: a step added inside a subgraph is in `model.inside[…]`, not in the top's nodes
        const known = new Set(allNodeNames(before.model));
        const added = allNodeNames(next.model).filter((name) => !known.has(name) && next.positions[name]);
        if (added.length === 0) return next;
        const [x0, y0] = next.positions[added[0]];
        moved = Object.fromEntries(
          added.map((name) => [name, [next.positions[name][0] - x0 + at.x, next.positions[name][1] - y0 + at.y] as [number, number]]),
        );
      }
      try {
        return { ...next, positions: await putPositions(next.name, { ...next.positions, ...moved }), dirty: true };
      } catch (exc) {
        push("warning", errorMessage(exc));
        return next;
      }
    },
    [push],
  );

  /**
   * Add a class where `target` says (`targetNow()` by default): after a step or a list member, or at the end of an op
   * list. Placed at `at`, else beside the node it follows (an append: the list's last member, else its holder).
   */
  const addClass = useCallback(
    async (classPath: string, at?: XY, given?: InsertTarget): Promise<void> => {
      const current = graphRef.current;
      if (current === null) return;
      const target = given ?? targetNow();
      const where = stepFields(target);
      if (where === undefined) {
        const why = target.kind === "none" ? target.why : undefined;
        push("error", `${fileOf(current)}: ${why ?? "nothing to add after — a step is inserted after another one"}`);
        return;
      }
      const view = viewNow();
      // named on the level drawn: `threshold` at the top, `prep/threshold` inside prep (a list member is named by
      // its position whatever is sent)
      const taken = view.nodes.map((node) => shortName(node.name));
      const name = qualify(levelRef.current, stepName(classPath, taken));
      const member = addedMember(view, target);
      const near = beside(view, target);
      try {
        const next = await addStep(current.name, { name, ...where, class_path: classPath, params: {} });
        // a member: the server's `added` names it (the page's own reckoning only when an answer lacks it)
        const placed = member === undefined ? undefined : (next.added ?? member);
        const cut = placed === undefined ? -1 : placed.lastIndexOf("[");
        const shift = placed === undefined ? undefined : { list: placed.slice(0, cut), first: Number(placed.slice(cut + 1, -1)), by: 1 };
        const spot = at ?? (near === undefined ? undefined : spotAfterAdd(next, near, next.added ?? placed, shift));
        accept(await placeNew(current, next, spot, placed), { kind: "document" });
      } catch (exc) {
        push("error", errorMessage(exc));
      }
    },
    [accept, placeNew, push, spotAfterAdd, targetNow, viewNow],
  );

  // ---- layout: the sidecar's two blocks; the page keeps the before/after ----------------------------------------

  /** Write positions and/or groups as ONE gesture: one count, one timeline entry (when `record`). */
  const writeLayout = useCallback(
    async (positions: Positions | undefined, groups: Group[] | undefined, record: boolean): Promise<boolean> => {
      const current = graphRef.current;
      if (current === null) return false;
      const hasPositions = positions !== undefined && Object.keys(positions).length > 0;
      const before = Object.fromEntries(
        Object.keys(positions ?? {})
          .map((name) => [name, current.positions[name]] as const)
          .filter(([, xy]) => xy !== undefined),
      ) as Positions;
      try {
        let next: Graph = current;
        let dirty = true; // positions set since the last Save make the graph dirty (the answer is the bare map)
        // the WHOLE map the page draws; the server merges it over what it holds until Save (a partial map is safe too)
        if (hasPositions)
          next = { ...(graphRef.current ?? next), positions: await putPositions(current.name, { ...current.positions, ...positions! }) };
        if (groups !== undefined) {
          const answer = await putGroups(current.name, groups);
          next = { ...next, groups: answer.groups };
          if (!hasPositions) dirty = answer.dirty ?? true;
        }
        let entry: TimelineEntry | undefined;
        if (record && hasPositions && groups !== undefined)
          entry = {
            kind: "layout",
            before: { positions: before, groups: current.groups },
            after: { positions: positions!, groups: next.groups },
          };
        else if (record && groups !== undefined) entry = { kind: "groups", before: current.groups, after: next.groups };
        else if (record && hasPositions) entry = { kind: "positions", before, after: positions! };
        accept({ ...next, dirty }, entry);
        return true;
      } catch (exc) {
        push("error", errorMessage(exc));
        return false;
      }
    },
    [accept, push],
  );

  const writeGroups = useCallback((groups: Group[], record = true) => writeLayout(undefined, groups, record), [writeLayout]);

  const onLayout = useCallback((change: LayoutChange) => void writeLayout(change.positions, change.groups, true), [writeLayout]);

  const groupSelection = useCallback(() => {
    const current = graphRef.current;
    const members = selectedNow();
    if (current === null || members.length === 0) return;
    void writeGroups(groupTogether(current.groups, members, levelRef.current));
  }, [selectedNow, writeGroups]);

  const updateGroup = useCallback(
    (id: string, patch: Partial<Group>) => {
      const current = graphRef.current;
      if (current === null) return Promise.resolve(false);
      return writeGroups(current.groups.map((group) => (group.id === id ? { ...group, ...patch } : group)));
    },
    [writeGroups],
  );

  const ungroup = useCallback(
    (id: string) => {
      const current = graphRef.current;
      if (current === null) return Promise.resolve(false);
      return writeGroups(current.groups.filter((group) => group.id !== id));
    },
    [writeGroups],
  );

  const resizeGroup = useCallback(
    (id: string, rect: Rect) => {
      const current = graphRef.current;
      if (current === null) return Promise.resolve(false);
      const nodes = flow
        .getNodes()
        .filter((node) => node.type === "generic")
        .map((node) => ({ name: node.id, rect: rectOf(node.id) }))
        .filter((node): node is { name: string; rect: Rect } => node.rect !== undefined);
      return writeGroups(resizeGroups(current.groups, id, rect, nodes));
    },
    [flow, rectOf, writeGroups],
  );

  const addGroup = useCallback(
    (at?: XY) => {
      const current = graphRef.current;
      if (current === null) return;
      let point = at;
      if (point === undefined) {
        // from the menu bar: the middle of what the canvas shows when the box covers no card there, else the nearest
        // free place — an EMPTY box is never drawn over cards it does not hold (measured: centred, it hid two)
        const pane = document.querySelector(".canvas .react-flow")?.getBoundingClientRect();
        const screen = pane ?? { left: 0, top: 0, width: window.innerWidth, height: window.innerHeight };
        const corner = flow.screenToFlowPosition({ x: screen.left, y: screen.top });
        const far = flow.screenToFlowPosition({ x: screen.left + screen.width, y: screen.top + screen.height });
        const view: Rect = { x: corner.x, y: corner.y, width: far.x - corner.x, height: far.y - corner.y };
        const obstacles = flow
          .getNodes()
          .map((node): Rect | undefined =>
            node.type === "group"
              ? { ...node.position, width: node.measured?.width ?? node.width ?? 0, height: node.measured?.height ?? node.height ?? 0 }
              : rectOf(node.id),
          )
          .filter((rect): rect is Rect => rect !== undefined);
        const middle = { x: view.x + view.width / 2 - DEFAULT_BOX.width / 2, y: view.y + view.height / 2 - DEFAULT_BOX.height / 2 };
        point = placeBox(middle, DEFAULT_BOX, obstacles, view);
        if (point.y + DEFAULT_BOX.height > view.y + view.height) // no room in view: below every card, shown
          void flow.setCenter(point.x + DEFAULT_BOX.width / 2, point.y + DEFAULT_BOX.height / 2, { zoom: flow.getZoom(), duration: 200 });
      }
      void writeGroups(addGroupAt(current.groups, point, levelRef.current));
    },
    [flow, rectOf, writeGroups],
  );

  // After an arrange the level is shown whole (a fit, never above zoom 1) — measured before: the arranged demo stream
  // stepped down to the bottom-right corner, half behind the side panel and the minimap. The fit waits for the commit
  // that draws the new places: this effect runs after the canvas's own (a child's effects run first), so React Flow
  // already holds them.
  const [fitRequests, setFitRequests] = useState(0);
  const showWhole = useCallback(() => setFitRequests((count) => count + 1), []);
  useEffect(() => {
    if (fitRequests > 0) void flow.fitView({ padding: 0.1, maxZoom: 1, duration: 200 });
  }, [fitRequests, flow]);

  /** dagre over the MEASURED cards, left to right along the wires — then saved like a drag, and shown whole. */
  const arrangeHere = useCallback(() => {
    const current = graphRef.current;
    if (current === null) return;
    const drawn = flow.getNodes().filter((node) => node.type === "generic");
    const boxes = drawn.map((node) => ({
      id: node.id,
      width: node.measured?.width ?? CARD.width,
      height: node.measured?.height ?? CARD.height,
    }));
    const placed = arrange(boxes, viewNow().wires);
    void writeLayout(Object.fromEntries(Object.entries(placed).map(([name, { x, y }]) => [name, [x, y]])), undefined, true).then(
      (written) => {
        if (written) showWhole();
      },
    );
  }, [flow, showWhole, viewNow, writeLayout]);

  /** The server's own layered first layout (`streamstudio/layout.py`). */
  const arrangeOnServer = useCallback(async () => {
    const current = graphRef.current;
    if (current === null) return;
    try {
      const all = await relayout(current.name);
      accept(
        { ...(graphRef.current ?? current), positions: all, dirty: true },
        { kind: "positions", before: current.positions, after: all },
      );
      showWhole();
    } catch (exc) {
      push("error", errorMessage(exc));
    }
  }, [accept, push, showWhole]);

  // ---- undo / redo -----------------------------------------------------------------------------------------------

  const step = useCallback(
    async (direction: "undo" | "redo") => {
      const current = graphRef.current;
      if (current === null) return;
      const entry = direction === "undo" ? timeline.current.nextUndo() : timeline.current.nextRedo();
      const done = () => (direction === "undo" ? timeline.current.undone() : timeline.current.redone());
      const side = direction === "undo" ? "before" : "after";
      if (entry.kind === "positions" || entry.kind === "groups" || entry.kind === "layout") {
        const ok =
          entry.kind === "positions"
            ? await writeLayout(entry[side], undefined, false)
            : entry.kind === "groups"
              ? await writeLayout(undefined, entry[side], false)
              : await writeLayout(entry[side].positions, entry[side].groups, false);
        if (ok) done();
        return;
      }
      try {
        const answer = await (direction === "undo" ? postUndo : postRedo)(current.name);
        done();
        if (answer.changed) {
          const { changed: _changed, shifts, ...next } = answer;
          if (shifts !== undefined && shifts.length > 0) {
            // members are named by POSITION: the level drawn follows its subgraph member to its new name, or — when the
            // step took that member away — climbs to the level holding the list (P7)
            const was = levelRef.current;
            const now = followShifts(was, shifts);
            if (now !== was) {
              levelRef.current = now;
              setLevel(now);
              let followed: string | undefined = was;
              for (const shift of shifts) followed = followed === undefined ? undefined : shiftName(followed, shift);
              if (followed === undefined)
                push("info", `${fileOf(current)}: the ${direction} took ${was} away — showing ${now === "" ? "the top" : now}`);
            }
          }
          // a conversion re-named group members in the same gesture: those go back (or forward) with the text
          const groups = entry.kind === "document" && entry.groups ? entry.groups[side] : undefined;
          let drawn: Graph = next;
          if (groups !== undefined)
            try {
              drawn = { ...next, groups: (await putGroups(current.name, groups)).groups };
            } catch (exc) {
              push("warning", errorMessage(exc)); // the text moved; only the boxes could not follow
            }
          accept(drawn);
        } else
          push(
            "info",
            `${fileOf(current)}: nothing to ${direction} — the editor holds no ${direction === "undo" ? "earlier" : "later"} edit of this document`,
          );
      } catch (exc) {
        push("error", errorMessage(exc));
      }
    },
    [accept, push, writeLayout],
  );

  // ---- copy / cut / paste as TEXT (§2.4), delete, select all, rename ---------------------------------------------

  const copy = useCallback(async (): Promise<boolean> => {
    const current = graphRef.current;
    const nodes = selectedNow();
    if (current === null || nodes.length === 0) return false;
    // `copying` is set before the first await, so a paste key pressed while the copy is on its way finds it
    const request = copyNodes(current.name, nodes).then(
      (copied): Copied => {
        clipboard.current = copied;
        setHasClip(true);
        push("info", `copied ${nodes.join(", ")} — Ctrl/Cmd+V pastes after the selected node, in any graph`);
        return copied;
      },
      (exc): null => {
        push("error", errorMessage(exc));
        return null;
      },
    );
    copying.current = request;
    setCopyPending(true);
    const copied = await request;
    if (copying.current === request) {
      copying.current = null;
      setCopyPending(false);
    }
    return copied !== null;
  }, [push, selectedNow]);

  /**
   * Cut and Delete: every node in ONE request (`POST …/steps/remove`) — one edit, one undo brings them all back; op
   * list members too. A member is named by its POSITION: after the remove the later members carry the removed ones'
   * names, and a selection kept by name would jump onto them — so nothing stays selected (a refusal puts it back).
   */
  const removeNodes = useCallback(
    async (nodes: string[]) => {
      const current = graphRef.current;
      if (current === null || nodes.length === 0) return;
      const shifting = shiftsMembers(viewNow(), nodes);
      if (shifting) {
        selectOnly([]);
        setInspected(undefined);
      }
      try {
        accept(await removeSteps(current.name, nodes), { kind: "document" });
      } catch (exc) {
        if (shifting) selectOnly(nodes);
        push("error", errorMessage(exc));
      }
    },
    [accept, push, selectOnly, viewNow],
  );

  const cut = useCallback(async () => {
    const nodes = selectedNow();
    if (await copy()) await removeNodes(nodes);
  }, [copy, removeNodes, selectedNow]);

  const paste = useCallback(
    async (at?: XY) => {
      const current = graphRef.current;
      if (current === null) return;
      const after = anchorNow(); // where it goes is decided at the key press, before any wait
      // a copy still on its way is waited for — the paste is never dropped; a failed copy said why in its own toast
      const copied = copying.current !== null ? await copying.current : clipboard.current;
      if (copied === null) return;
      if (after === undefined) {
        push("error", `${fileOf(current)}: nothing to paste after — select a node first`);
        return;
      }
      try {
        const next = await pasteText(current.name, copied.text, after);
        accept(await placeNew(current, next, at), { kind: "document" });
      } catch (exc) {
        push("error", errorMessage(exc));
      }
    },
    [accept, anchorNow, placeNew, push],
  );

  const deleteSelection = useCallback(() => {
    const current = graphRef.current;
    if (current === null) return;
    const nodes = selectedNow();
    if (nodes.length > 0) {
      void removeNodes(nodes);
      return;
    }
    const boxes = selectedGroupsNow();
    if (boxes.length > 0) void writeGroups(current.groups.filter((group) => !boxes.includes(group.id)));
  }, [removeNodes, selectedGroupsNow, selectedNow, writeGroups]);

  const selectAll = useCallback(() => {
    flow.setNodes((all) => all.map((node) => (node.type === "generic" && !node.selected ? { ...node, selected: true } : node)));
  }, [flow]);

  const renameSelected = useCallback(() => {
    const nodes = selectedNow();
    if (nodes.length === 1) setEditing(`node:${nodes[0]}`);
    else if (nodes.length === 0) {
      const boxes = selectedGroupsNow();
      if (boxes.length > 0) setEditing(`group:${boxes[0]}`);
    }
  }, [selectedGroupsNow, selectedNow]);

  // ---- files: New, Open, Save, Save As, Revert, Check ------------------------------------------------------------

  const save = useCallback(async () => {
    const current = graphRef.current;
    if (current === null) return;
    try {
      accept(await saveGraph(current.name));
    } catch (exc) {
      push("error", errorMessage(exc));
    }
  }, [accept, push]);

  const saveAs = useCallback(async () => {
    const current = graphRef.current;
    if (current === null) return;
    const answer = await askFile({
      mode: "save",
      title: `Save ${fileOf(current)} as`,
      confirm: "Save",
      dir: dirName(current.path),
      name: baseName(current.path),
    });
    if (answer === null) return;
    try {
      const next = await saveGraphAs(current.name, answer.path, answer.overwrite);
      setGraphs(await listGraphs());
      timeline.current.clear(); // the page's layout entries were the old entry's
      accept(next); // ?graph= follows the new name (the URL effect)
    } catch (exc) {
      push("error", errorMessage(exc));
    }
  }, [accept, askFile, push]);

  const revert = useCallback(async () => {
    const current = graphRef.current;
    if (current === null) return;
    const sure = await ask({
      title: `Revert ${fileOf(current)}?`,
      message: "The changes made since it was last saved are dropped and the file is read again. This cannot be undone.",
      confirm: "Revert",
      danger: true,
    });
    if (sure === null) return;
    try {
      const next = await revertGraph(current.name);
      timeline.current.clear();
      accept(next);
      getForm(next.name).then(setForm, (exc) => push("error", errorMessage(exc)));
    } catch (exc) {
      push("error", errorMessage(exc));
    }
  }, [accept, ask, push]);

  const open = useCallback(async () => {
    const current = graphRef.current;
    const answer = await askFile({
      mode: "open",
      title: "Open a graph",
      confirm: "Open",
      dir: current ? dirName(current.path) : undefined,
    });
    if (answer === null) return;
    try {
      await show(await openGraph(answer.path));
    } catch (exc) {
      push("error", errorMessage(exc));
    }
  }, [askFile, push, show]);

  /**
   * New…'s Create (DESIGN3): the server WRITES the starter file and answers the graph, not dirty; the dialog closes and
   * the graph opens. A refusal writes nothing — the file exists, the folder does not, the name is refused — and goes
   * back to the dialog, which stays up showing the server's words.
   */
  const create = useCallback(
    async (answer: NewAnswer): Promise<string | null> => {
      let made: Graph;
      try {
        made = await newGraph(answer.name, answer.kind, answer.dir);
      } catch (exc) {
        return errorMessage(exc);
      }
      setDialog(null);
      await show(made); // the file is written: whatever this says, the dialog has done its part
      return null;
    },
    [show],
  );

  const check = useCallback(async () => {
    const current = graphRef.current;
    if (current === null) return;
    try {
      const { problems } = await checkGraph(current.name);
      if (problems.length === 0) push("info", `${fileOf(current)}: no problems`);
      else setDialog({ kind: "check", file: fileOf(current), problems });
    } catch (exc) {
      push("error", errorMessage(exc));
    }
  }, [push]);

  // ---- subgraphs and templates -----------------------------------------------------------------------------------

  const refreshTemplates = useCallback(() => {
    listTemplates().then(setTemplates, (exc) => {
      setTemplates([]);
      push("error", errorMessage(exc));
    });
  }, [push]);

  /**
   * Convert selection (or a group) to subgraph, as ONE gesture: the conversion, then the layout it implies — a group
   * whose members became the subgraph names the subgraph instead (`groups.replaceConverted`), a converted BOX goes and
   * the new node takes its corner — drawn once, counted once, undone by one Ctrl/Cmd+Z (the groups ride on the
   * document entry). Nothing stays selected: list members are named by POSITION, so after the conversion the old
   * names belong to other nodes (measured: Threshold stayed highlighted and Run paused before it).
   */
  const toSubgraph = useCallback(
    async (nodes: string[], base = "subgraph", box?: { id: string; corner?: XY }): Promise<string | undefined> => {
      const current = graphRef.current;
      if (current === null || nodes.length === 0) return undefined;
      const level = levelRef.current;
      // named on the level of the steps it takes: `subgraph` at the top, `prep/subgraph` inside prep
      const taken = viewNow().nodes.map((node) => shortName(node.name));
      const name = qualify(level, uniqueName(slug(base), taken));
      selectOnly([]);
      let next: Graph;
      let made: string;
      try {
        const { subgraph, ...answer } = await convertToSubgraph(current.name, nodes, name);
        next = answer;
        // the server names the new node: `name` for flow steps, the first member's POSITION for list members
        made = subgraph ?? name;
      } catch (exc) {
        selectOnly(nodes); // refused: nothing changed, the selection comes back
        push("error", errorMessage(exc));
        return undefined;
      }
      const present = new Set((levelModel(next.model, level) ?? EMPTY_MODEL).nodes.map((node) => node.name));
      let groups = replaceConverted(next.groups, nodes, made, (node) => !present.has(node), level);
      if (box) groups = groups.filter((group) => group.id !== box.id);
      let regrouped = false;
      try {
        if (groups !== next.groups) {
          next = { ...next, groups: (await putGroups(current.name, groups)).groups };
          regrouped = true;
        }
        if (box?.corner && next.positions[made])
          next = { ...next, positions: await putPositions(current.name, { ...next.positions, [made]: [box.corner.x, box.corner.y] }) };
      } catch (exc) {
        push("warning", errorMessage(exc)); // the conversion stands; only its layout could not be written
      }
      accept(
        { ...next, dirty: true },
        regrouped ? { kind: "document", groups: { before: current.groups, after: next.groups } } : { kind: "document" },
      );
      return made;
    },
    [accept, push, selectOnly, viewNow],
  );

  const convertGroup = useCallback(
    async (id: string) => {
      const current = graphRef.current;
      const group = current?.groups.find((candidate) => candidate.id === id);
      if (current === null || !group) return;
      const members = stepNames(viewNow(), group.nodes ?? []);
      if (members.length === 0) {
        push("error", `${fileOf(current)}: ${group.title} holds no node to convert`);
        return;
      }
      // the box becomes the subgraph: the box goes, the new node takes its corner
      await toSubgraph(members, group.title, { id, corner: flow.getNode(groupNodeId(id))?.position });
    },
    [flow, push, toSubgraph, viewNow],
  );

  const saveAsTemplate = useCallback(async () => {
    const current = graphRef.current;
    const nodes = selectedNow();
    if (current === null || nodes.length === 0) return;
    const known = (templates ?? []).map((template) => template.name);
    const suggestion = nodes.length === 1 ? (nodes[0].split("/").pop() ?? nodes[0]) : uniqueName("template", known);
    const answer = await ask({
      title: "Save as template",
      message: `${nodes.join(", ")} — a copy anyone can insert later with Insert template; nothing links back to this graph.`,
      label: "Template name",
      value: suggestion,
      option: "replace a template of the same name",
      confirm: "Save template",
    });
    if (answer === null) return;
    let overwrite = answer.option;
    if (!overwrite && known.includes(answer.value)) {
      const sure = await ask({ title: `Replace template ${answer.value}?`, message: "A template of that name exists.", confirm: "Replace", danger: true });
      if (sure === null) return;
      overwrite = true;
    }
    try {
      await saveTemplate(answer.value, current.name, nodes, overwrite);
      push("info", `template ${answer.value} saved — Graph ▸ Insert template adds a copy`);
      refreshTemplates();
    } catch (exc) {
      push("error", errorMessage(exc));
    }
  }, [ask, push, refreshTemplates, selectedNow, templates]);

  const insertTemplate = useCallback(
    async (name: string, at?: XY) => {
      const current = graphRef.current;
      if (current === null) return;
      const after = anchorNow();
      if (after === undefined) {
        push("error", `${fileOf(current)}: nothing to insert after — select a node first`);
        return;
      }
      try {
        const next = await insertTemplateRoute(current.name, name, after);
        accept(await placeNew(current, next, at), { kind: "document" });
      } catch (exc) {
        push("error", errorMessage(exc));
      }
    },
    [accept, anchorNow, placeNew, push],
  );

  const deleteTemplate = useCallback(
    async (name: string) => {
      const sure = await ask({
        title: `Delete template ${name}?`,
        message: "Its file is removed from the templates folder. Graphs that inserted it keep their copy.",
        confirm: "Delete",
        danger: true,
      });
      if (sure === null) return;
      try {
        await deleteTemplateRoute(name);
        refreshTemplates();
      } catch (exc) {
        push("error", errorMessage(exc));
      }
    },
    [ask, push, refreshTemplates],
  );

  const insertStepsFile = useCallback(
    async (at?: XY) => {
      const current = graphRef.current;
      if (current === null) return;
      const after = anchorNow();
      if (after === undefined) {
        push("error", `${fileOf(current)}: nothing to insert after — select a node first`);
        return;
      }
      const answer = await askFile({ mode: "open", title: "Insert a steps file", confirm: "Insert", dir: dirName(current.path) });
      if (answer === null) return;
      try {
        const next = await includeSteps(current.name, answer.path, after);
        accept(await placeNew(current, next, at), { kind: "document" });
      } catch (exc) {
        push("error", errorMessage(exc));
      }
    },
    [accept, anchorNow, askFile, placeNew, push],
  );

  const extract = useCallback(
    async (node: string) => {
      const current = graphRef.current;
      if (current === null) return;
      const answer = await askFile({
        mode: "save",
        title: `Move ${node}'s steps to their own file`,
        confirm: "Move",
        dir: dirName(current.path),
        name: stepsFileName(node),
        normalise: (name) => (/\.ya?ml$/.test(name.trim()) ? name.trim() : `${name.trim()}.steps.yaml`),
      });
      if (answer === null) return;
      try {
        accept(await extractSubgraph(current.name, node, answer.path), { kind: "document" });
      } catch (exc) {
        push("error", errorMessage(exc));
      }
    },
    [accept, askFile, push],
  );

  // ---- the palette ---------------------------------------------------------------------------------------------

  const onPalette = useCallback((gesture: PaletteGesture) => {
    const current = graphRef.current;
    if (current === null) return;
    const view = viewNow();
    const level = levelRef.current;
    if (!gesture.from) {
      const selected = gesture.selected ?? [];
      setPalette({
        screen: gesture.screen,
        openedAt: gesture.openedAt,
        target: insertTarget(view, level, current.kind, selected[selected.length - 1]),
      });
      return;
    }
    const { node, kind, type, socket } = gesture.from;
    // from an OUTPUT: the new node takes the type and goes after the node dragged from; from an INPUT: it gives the
    // type and goes before that node — after whatever feeds its record (inside a subgraph, never after its <in>); a
    // Stream's member goes into its list the same way (`oplists.wireTarget`). From an op-list item input (a Pipeline's
    // `transforms[k]`) the pick becomes that list's item k (`oplists.inputTarget` — P3)
    const listed = kind === "in" ? inputTarget(view, node, socket) : undefined;
    const after = kind === "out" ? node : levelPredecessor(view, node);
    setPalette({
      screen: gesture.screen,
      openedAt: gesture.openedAt,
      filter: { type, side: kind === "out" ? "inputs" : "outputs" },
      target:
        listed ??
        (after === undefined
          ? { kind: "none", why: `nothing feeds ${node} — a step can only be inserted after another one` }
          : wireTarget(view, level, current.kind, after)),
    });
  }, [viewNow]);

  const pick = useCallback(
    (spec: NodeSpec) => {
      const request = palette;
      setPalette(null);
      if (request === null) return;
      // the new node lands where the gesture was (the double-click, the wire's release); `none` is refused there
      void addClass(spec.class_path, flow.screenToFlowPosition(request.screen), request.target);
    },
    [addClass, flow, palette],
  );

  // ---- runs ------------------------------------------------------------------------------------------------------

  const answered = useCallback((next: Run | null, error: string | null) => {
    if (next !== null) setRun(next);
    setRunError(error);
    setAnswers((count) => count + 1);
    setRunning(false);
  }, []);

  const startRun = useCallback(
    async (until: string | undefined) => {
      const current = graphRef.current;
      if (current === null) return;
      setRunning(true);
      try {
        answered(await trace(current.name, record, until), null);
      } catch (exc) {
        answered(null, errorMessage(exc));
      }
    },
    [answered, record],
  );

  const until = selection[selection.length - 1];
  const onRun = useCallback(() => {
    const selected = selectedNow();
    void startRun(selected[selected.length - 1]);
  }, [selectedNow, startRun]);
  const onStep = useCallback(() => {
    if (run === null) return;
    const next = nextNode(run);
    if (next !== undefined) void startRun(next ?? undefined);
  }, [run, startRun]);
  const onRerun = useCallback(
    async (node: string, params: Record<string, Json>) => {
      if (run === null) return;
      setRunning(true);
      try {
        answered(await postRerun(run.run, node, params), null);
      } catch (exc) {
        answered(null, errorMessage(exc));
      }
    },
    [answered, run],
  );

  const onNodeClick = useCallback(
    (name: string) => {
      setInspected(name);
      if (run !== null) setTab("run");
    },
    [run],
  );

  // ---- the commands: the menu bar, the right-click menus and the keyboard run THESE ------------------------------

  const toggleSidebar = useCallback(() => {
    setSidebar((was) => {
      writeSidebar(!was);
      return !was;
    });
  }, []);

  const runCommand = useCallback(
    (id: CommandId) => {
      setMenuOpen(null);
      setContext(null);
      switch (id) {
        case "new-flow":
          return setDialog({ kind: "new", newKind: "flow" });
        case "new-pipeline":
          return setDialog({ kind: "new", newKind: "pipeline" });
        case "new-source":
          return setDialog({ kind: "new", newKind: "source" });
        case "open":
          return void open();
        case "save":
          return void save();
        case "save-as":
          return void saveAs();
        case "revert":
          return void revert();
        case "undo":
        case "redo":
          return void step(id);
        case "cut":
          return void cut();
        case "copy":
          return void copy();
        case "paste":
          return void paste();
        case "delete":
          return deleteSelection();
        case "select-all":
          return selectAll();
        case "rename":
          return renameSelected();
        case "toggle-nodes":
          return toggleSidebar();
        case "fit":
          return void flow.fitView({ padding: 0.15, duration: 200 });
        case "arrange":
          return arrangeHere();
        case "arrange-server":
          return void arrangeOnServer();
        case "minimap":
          return setMinimap((shown) => !shown);
        case "add-group":
          return addGroup();
        case "group":
          return groupSelection();
        case "to-subgraph":
          return void toSubgraph(selectedNow());
        case "save-template":
          return void saveAsTemplate();
        case "check":
          return void check();
        case "run":
          setTab("run");
          return onRun();
        case "step":
          setTab("run");
          return onStep();
        case "rerun": {
          const selected = selectedNow();
          if (selected.length > 0) void onRerun(selected[selected.length - 1], {});
          return;
        }
      }
    },
    [
      addGroup,
      arrangeHere,
      arrangeOnServer,
      check,
      copy,
      cut,
      deleteSelection,
      flow,
      groupSelection,
      onRerun,
      onRun,
      onStep,
      open,
      paste,
      renameSelected,
      revert,
      save,
      saveAs,
      saveAsTemplate,
      selectAll,
      selectedNow,
      step,
      toSubgraph,
      toggleSidebar,
    ],
  );

  const commandState: CommandState = {
    graph: graph !== null,
    dirty: dirtyShown,
    selected: selection.length,
    groupSelected: groupsSelected.length > 0,
    clipboard: hasClip || copyPending,
    run: run !== null,
    paused: run !== null && run.paused_at !== null && nextNode(run) !== undefined,
    busy: running,
    traced: run !== null && until !== undefined && run.trace.nodes.some((node) => node.node === until),
    sidebar,
    minimap,
  };

  // ---- the keyboard ------------------------------------------------------------------------------------------------

  /**
   * The state a KEY is judged by: the menus' state with the selection read from React Flow now — the listener's
   * copy lags one effect behind a click (measured: Delete pressed right after a click found nothing selected).
   */
  const stateNow = useCallback((): CommandState => {
    const selected = selectedNow();
    const last = selected[selected.length - 1];
    return {
      ...commandState,
      selected: selected.length,
      groupSelected: selectedGroupsNow().length > 0,
      // a copy pressed a moment ago is on its way: Paste waits for it (the state above renders a frame later)
      clipboard: clipboard.current !== null || copying.current !== null,
      traced: run !== null && last !== undefined && run.trace.nodes.some((node) => node.node === last),
    };
  }, [commandState, run, selectedGroupsNow, selectedNow]);

  // the recovery question is answered first: no shortcut acts while it is asked (or still on its way), and the bar and
  // the canvas are INERT — out of reach of the mouse and of Tab (P5: measured before, Tab reached the New menu and the
  // graph picker, and a graph loaded before the answer stayed drawn from disk after a Restore)
  const recovering = !recoveryAnswered(recoveryPhase);
  const bar = useRef<HTMLElement>(null);
  const body = useRef<HTMLElement>(null);
  useEffect(() => {
    for (const part of [bar.current, body.current]) part?.toggleAttribute("inert", recovering);
  }, [recovering]);
  const keys = useRef({ runCommand, stateNow, menuOpen, context, dialog, palette, level: shown, goTo, recovering });
  keys.current = { runCommand, stateNow, menuOpen, context, dialog, palette, level: shown, goTo, recovering };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const now = keys.current;
      if (event.key === "Escape" && (now.menuOpen !== null || now.context !== null)) {
        event.preventDefault();
        setMenuOpen(null);
        setContext(null);
        return;
      }
      if (now.dialog !== null || now.palette !== null || now.recovering || isTyping(event.target)) return;
      if (event.key === "Escape" && now.level !== "") {
        event.preventDefault(); // inside a subgraph, Escape goes up one level (the breadcrumb's crumb before it)
        now.goTo(parentLevel(now.level));
        return;
      }
      const id = matchShortcut(event);
      if (id === undefined) return;
      if (id === "copy" && window.getSelection()?.toString()) return; // a text selection is the browser's to copy
      event.preventDefault(); // Ctrl+S / Ctrl+O never reach the browser's own dialogs, acting or not
      if (enabled(id, now.stateNow())) now.runCommand(id);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // ---- the menus -------------------------------------------------------------------------------------------------

  const menuContext: MenuContext = {
    mac,
    state: commandState,
    run: runCommand,
    templates,
    insertTemplate: (name) => void insertTemplate(name),
    deleteTemplate: (name) => void deleteTemplate(name),
  };

  const openMenu = useCallback(
    (id: MenuId | null) => {
      setMenuOpen(id);
      setContext(null);
      if (id === "graph") refreshTemplates();
    },
    [refreshTemplates],
  );

  const onContextMenu = useCallback(
    (request: CanvasMenuRequest) => {
      setMenuOpen(null);
      setPalette(null);
      setContext(request);
      if (request.kind === "pane") refreshTemplates();
    },
    [refreshTemplates],
  );

  const closeContext = useCallback(() => setContext(null), []);

  const contextEntries = (request: CanvasMenuRequest): Entry[] => {
    if (request.kind === "pane")
      return paneMenu({
        ...menuContext,
        insertTemplate: (name) => void insertTemplate(name, request.flow),
        addNode: (close) => (
          <AddNodeMenu
            specs={addable}
            close={close}
            onPick={(spec) => {
              close();
              void addClass(spec.class_path, request.flow);
            }}
          />
        ),
        addGroup: () => addGroup(request.flow),
        insertStepsFile: () => void insertStepsFile(request.flow),
      });
    if (request.kind === "group") {
      const group = graph?.groups.find((candidate) => candidate.id === request.group);
      return groupMenu({
        color: group?.color ?? "",
        rename: () => setEditing(`group:${request.group}`),
        setColor: (color) => void updateGroup(request.group, { color }),
        ungroup: () => void ungroup(request.group),
        convert: () => void convertGroup(request.group),
      });
    }
    const node = graph ? findNode(graph.model, request.node) : undefined;
    if (!node) return [];
    // the right-clicked node was made the selection a moment ago: the commands see it through the state below
    const state = selection.includes(node.name) ? commandState : { ...commandState, selected: 1 };
    return nodeMenu({
      ...menuContext,
      state,
      node,
      runTo: () => {
        setContext(null);
        setTab("run");
        void startRun(node.name);
      },
      extract: () => void extract(node.name),
      // Open shows the inside the server sent; without one it stays greyed
      openSubgraph: graph && levelModel(graph.model, node.name) !== undefined ? () => openSubgraph(node.name) : undefined,
    });
  };

  const model = graph?.model;
  const editor = useMemo<EditorApi>(
    () => ({
      form,
      types,
      run,
      setParam,
      rename,
      updateGroup,
      ungroup,
      resizeGroup,
      editing,
      setEditing,
      openSubgraph,
      innerSteps: (name: string) => (model === undefined ? [] : innerStepsOf(model, name)),
    }),
    [form, types, run, setParam, rename, updateGroup, ungroup, resizeGroup, editing, openSubgraph, model],
  );

  const inspectedNode = graph !== null && inspected !== undefined ? findNode(graph.model, inspected) : undefined;
  const targetShown: InsertTarget = graph
    ? insertTarget(levelModel(graph.model, shown) ?? EMPTY_MODEL, shown, graph.kind, until)
    : { kind: "none" };

  return (
    <EditorContext.Provider value={editor}>
      <div
        className="app"
        data-graph={graph?.name ?? ""}
        data-ready={graph === null ? "false" : "true"}
        data-saves={saves}
        data-dirty={dirtyShown ? "true" : "false"}
        data-level={shown}
        data-palette-ready={specs === null ? "false" : "true"}
        data-selection={selection.join(",")}
        data-recovery={recoveryPhase}
        style={{ ["--nodes-width" as string]: sidebar ? "300px" : "0px" }}
      >
        <header className="bar" ref={bar}>
          <span className="brand" title="streamstudio">
            <svg viewBox="0 0 20 20" aria-hidden="true">
              <circle cx="4" cy="10" r="2.6" />
              <circle cx="16" cy="4.5" r="2.6" />
              <circle cx="16" cy="15.5" r="2.6" />
              <path d="M6.3 9 13.7 5.5M6.3 11l7.4 3.5" strokeWidth="1.6" fill="none" />
            </svg>
            streamstudio
          </span>
          <MenuBar menus={barMenus(menuContext)} open={menuOpen} onOpen={openMenu} />
          <div className="bar-doc">
            {graph && (
              <span className={`doc-title${dirtyShown ? " dirty" : ""}`} data-title title={graph.path}>
                {fileTitle(graph)}
              </span>
            )}
            <select
              data-graph-picker
              value={graph?.name ?? ""}
              title="the graphs of this folder, and every one opened"
              onChange={(event) => !recovering && void load(event.target.value)}
            >
              {graph === null && <option value="">{listed ? "no graph" : "loading…"}</option>}
              {graphs.map((entry) => (
                <option key={entry.name} value={entry.name}>
                  {pickerLabel(entry)}
                </option>
              ))}
            </select>
          </div>
          <div className="bar-tools">
            <button
              type="button"
              className={`bar-button${sidebar ? " on" : ""}`}
              data-command="toggle-nodes"
              aria-pressed={sidebar}
              title="the Nodes sidebar (N)"
              onClick={toggleSidebar}
            >
              <svg viewBox="0 0 16 16" aria-hidden="true">
                <rect x="1.5" y="2" width="13" height="12" rx="2" fill="none" stroke="currentColor" strokeWidth="1.3" />
                <path d="M6 2v12" stroke="currentColor" strokeWidth="1.3" />
              </svg>
              Nodes
            </button>
            <button
              type="button"
              className="bar-button run"
              data-command="run"
              disabled={!enabled("run", commandState)}
              title={`run one record${until ? `, pausing before ${until}` : ""} (${mac ? "⌘↩" : "Ctrl+Enter"})`}
              onClick={() => runCommand("run")}
            >
              <svg viewBox="0 0 16 16" aria-hidden="true">
                <path d="M4 2.5v11l9-5.5z" fill="currentColor" />
              </svg>
              Run
            </button>
          </div>
        </header>
        <main className={`body${sidebar ? " with-nodes" : ""}`} ref={body}>
          {sidebar && (
            <NodesSidebar
              specs={addable}
              target={targetShown}
              onAdd={(spec) => void addClass(spec.class_path)}
              onClose={toggleSidebar}
            />
          )}
          {graph !== null ? (
            <Canvas
              graph={graph}
              types={types}
              level={shown}
              showMinimap={minimap}
              onLayout={onLayout}
              onProposal={(message) => push("warning", message)}
              onSelection={(names, groups) => {
                setSelection(names);
                setGroupsSelected(groups);
              }}
              onPalette={onPalette}
              onNodeClick={onNodeClick}
              onContextMenu={onContextMenu}
              onDropClass={(classPath, at) => void addClass(classPath, at)}
              onOpenSubgraph={openSubgraph}
              onLevel={goTo}
            />
          ) : (
            <div className="canvas empty-canvas">
              {listed && graphs.length === 0 && (
                <div className="empty-state">
                  <h2>No graph in this folder yet</h2>
                  <p>Make one with New, or open a .yaml from anywhere on this machine.</p>
                  <div>
                    <button type="button" className="button primary" onClick={() => runCommand("new-flow")}>
                      New flow graph…
                    </button>
                    <button type="button" className="button" onClick={() => runCommand("open")}>
                      Open…
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
          <aside className="side">
            <nav className="tabs">
              {SIDE_TABS.map((name) => (
                <button key={name} data-tab={name} aria-selected={tab === name} onClick={() => setTab(name)}>
                  {name}
                </button>
              ))}
            </nav>
            {tab === "text" && (
              <>
                <h2 title={graph?.path}>{graph ? fileTitle(graph) : ""}</h2>
                <pre data-yaml>{graph?.text ?? ""}</pre>
              </>
            )}
            {tab === "settings" && graph !== null && <SettingsForm graph={graph} level={shown} />}
            {tab === "run" && graph !== null && (
              <RunPanel
                file={fileOf(graph)}
                run={run}
                error={runError}
                answers={answers}
                record={record}
                onRecord={setRecord}
                until={until}
                inspected={inspectedNode}
                form={form}
                busy={running}
                onRun={onRun}
                onStep={onStep}
                onRerun={(node, params) => void onRerun(node, params)}
              />
            )}
          </aside>
        </main>
        {palette !== null && (
          <Palette request={palette} specs={addable} types={types} onPick={pick} onClose={() => setPalette(null)} />
        )}
        {context !== null && (
          <ContextMenu at={context.screen} kind={context.kind} entries={contextEntries(context)} onClose={closeContext} />
        )}
        {dialog?.kind === "file" && (
          <FileDialog
            request={dialog.request}
            onDone={(answer) => {
              setDialog(null);
              dialog.resolve(answer);
            }}
          />
        )}
        {dialog?.kind === "ask" && (
          <AskDialog
            request={dialog.request}
            onDone={(answer) => {
              setDialog(null);
              dialog.resolve(answer);
            }}
          />
        )}
        {dialog?.kind === "new" && (
          <NewDialog kind={dialog.newKind} folder={folder} onCreate={create} onCancel={() => setDialog(null)} />
        )}
        {dialog?.kind === "check" && <CheckDialog file={dialog.file} problems={dialog.problems} onClose={() => setDialog(null)} />}
        {recoveryPhase === "asking" && recovery !== null && (
          <RecoveryDialog
            recovery={recovery}
            folder={folder}
            refused={recoveryRefused}
            busy={recoveryBusy}
            onRestore={() => void restoreWork()}
            onDiscard={() => void discardWork()}
          />
        )}
        <div className="toasts">
          {toasts.map((toast) => (
            <div key={toast.id} className={`toast toast-${toast.level}`} data-toast={toast.level} onClick={() => dismiss(toast.id)}>
              {toast.text}
            </div>
          ))}
        </div>
      </div>
    </EditorContext.Provider>
  );
}
