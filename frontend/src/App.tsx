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
 * Wires (`wires.ts`): a wire drawn between sockets whose types meet is sent to `POST …/wires` and the server's answer is
 * drawn — the wire saved in the document's own spelling, one edit, one undo — or the server's one plain line is a
 * warning and nothing is drawn. Delete with wires selected (and no card) sends them all in one `POST …/wires/remove`.
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
 * `POST …/steps/remove`. Members are named by position, so after a remove nothing stays selected. A class that is not
 * an op is a CARD of its own (`cards.ts`, 2026-10-05): the server writes it as a top-level key above the root, wired to
 * a dropped wire's end, and the page draws it where the gesture was; the host card and the root are never an anchor.
 *
 * Unsaved work of the last session (DESIGN2 §1): before any graph is loaded the page asks `GET api/workspace`; when
 * the last stop kept unsaved work, the recovery dialog asks Restore / Open the saved files first (`recovery.ts`).
 *
 * `?graph=<name>` picks the graph on load (the first listed otherwise — after a Restore, the first restored),
 * `?level=<subgraph>` the level drawn and `?tab=settings|run` the side panel's tab; all three are kept in step with what
 * the page shows, so a reload or a link opens that graph at that level on that tab (a level the document no longer
 * holds falls back to the deepest one it does). A `?graph=` the editor does not list opens NOTHING: the canvas names the
 * graph and the graphs there are, one button each (`link.pickGraph`, `[data-graph-missing]`).
 *
 * Mounted in a host app (DESIGN5 step 1; `link.ts`, `host.ts`): the page answers under the host's prefix (`/studio/`),
 * `?embed=1` fits it into the host's panel — the menus and every function, no brand of its own, no minimap until asked
 * — and posts what it shows and holds to the host (its `state` and `saved` messages), taking the host's `open` command.
 * When `GET api/workspace` names a host, Run on a graph without its own source runs the record the host has selected.
 * The settings tab lists a graph's `app:` fields until "all parameters" is ticked (`settings.ts`); a field's pickers —
 * the folder chooser, the HuggingFace lists — are the ONE widget's (`pickers.ts`); Set source… puts a class and its
 * values into a Stream's source slot (`slots.ts`, `dialogs/SetSlotDialog.tsx`).
 *
 * The root carries `data-graph`, `data-ready`, `data-saves` (accepted writes so far), `data-dirty`, `data-level` (the
 * level drawn, `""` at the top), `data-palette-ready` (the catalogue has arrived — it is asked for on first need, never
 * at open: `catalogue.ts`), `data-selection` (the document nodes a gesture acts on),
 * `data-recovery` (`pending` | `asking` | `confirming` | `restored` | `discarded` | `none`), `data-side-tab` (the side panel's
 * tab), `data-embed` (`true` in a host's panel) and `data-host` (the host's name, `""` standalone) — what a test waits
 * on instead of timing.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ReactFlowProvider, useReactFlow } from "@xyflow/react";
import {
  ApiError,
  addStep,
  addWire,
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
  removeWires,
  renameNode,
  rerun as postRerun,
  restoreRecovery,
  revertGraph,
  saveGraph,
  saveSettings,
  saveGraphAs,
  saveTemplate,
  setParam as postParam,
  setSlot,
  trace,
  undo as postUndo,
  type Copied,
  type DraggedEnd,
  type NodeSpec,
  type Run,
  type Template,
} from "./api";
import { addPlan, draggedWire } from "./cards";
import { Canvas, type CanvasMenuRequest, type LayoutChange, type PaletteGesture } from "./canvas/Canvas";
import { Palette, type PaletteRequest } from "./canvas/Palette";
import { needsCatalogue, onFirstNeed } from "./catalogue";
import { enabled, isMac, matchShortcut, type CommandId, type CommandState, type MenuId } from "./commands";
import { EditorContext, type Editing, type EditorApi } from "./context";
import { AskDialog, type AskAnswer, type AskRequest } from "./dialogs/AskDialog";
import { CheckDialog } from "./dialogs/CheckDialog";
import { FileDialog, type FileAnswer, type FileRequest } from "./dialogs/FileDialog";
import { NewDialog, type NewAnswer } from "./dialogs/NewDialog";
import { RecoveryDialog } from "./dialogs/RecoveryDialog";
import { SetSlotDialog } from "./dialogs/SetSlotDialog";
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
import {
  hasOwnSource,
  hostOf,
  committedMessage,
  parseHostCommand,
  runRecord,
  savedMessage,
  stateMessage,
  type HostInfo,
  type OpenCommand,
  type PageMessage,
} from "./host";
import { type Direction, arrange } from "./layout";
import {
  EMPTY_MODEL,
  allNodeNames,
  existingLevel,
  findNode,
  followShifts,
  innerSteps as innerStepsOf,
  isHostCard,
  levelAnchor,
  levelFromSearch,
  levelModel,
  levelPredecessor,
  parentLevel,
  qualify,
  rekeyName,
  shiftKeys,
  shiftName,
  shortName,
  stepNames,
  withoutHost,
  type NameShift,
} from "./levels";
import {
  SIDE_TABS,
  embedFromSearch,
  graphFromSearch,
  missingGraphMessage,
  onlyFromSearch,
  pageHref,
  pickGraph,
  tabFromSearch,
  type SideTab,
} from "./link";
import { AddNodeMenu } from "./menu/AddNodeMenu";
import { ContextMenu } from "./menu/ContextMenu";
import type { Entry } from "./menu/entries";
import { MenuBar } from "./menu/MenuBar";
import { barMenus, groupMenu, nodeMenu, paneMenu, type MenuContext } from "./menus";
import type { CanvasNode, Form, Graph, GraphSummary, Json, Model, NewGraphKind, Positions, Problem, Supertypes } from "./model";
import { NodesSidebar } from "./panels/NodesSidebar";
import { RunPanel } from "./panels/RunPanel";
import { SettingsForm } from "./panels/SettingsForm";
import { addedMember, beside, inputTarget, insertTarget, shiftsMembers, wireTarget, type InsertTarget } from "./oplists";
import {
  answered as recoveryAnswered,
  discardQuestion,
  restoredNames as restoredIn,
  waitsForAnswer,
  type Recovery,
  type RecoveryPhase,
} from "./recovery";
import { nextNode } from "./run";
import { addableSpecs } from "./sidebar";
import { sourceSlots, type Slot } from "./slots";
import { selectedAnchor, spotBeside, stepName, uniqueName } from "./steps";
import { useToasts } from "./toast";
import { Toasts } from "./Toasts";
import { connectionEnds, deleteWires, drawWire, selectedWires, type Connected, type WireEnds } from "./wires";
import "./styles.css";

export default function App() {
  return (
    <ReactFlowProvider>
      <Editor />
    </ReactFlowProvider>
  );
}

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
  /** How many wires are selected — Delete takes them out when no card is selected. */
  const [wiresSelected, setWiresSelected] = useState(0);
  const [palette, setPalette] = useState<PaletteRequest | null>(null);
  /** In a host's panel (`?embed=1`): the menus and every function, no app chrome (`link.ts`, `host.ts`). */
  const [embed] = useState(() => embedFromSearch(window.location.search));
  /**
   * `?only=settings` (a host's settings sheet, `link.ts`): the graph's settings form and nothing else — no menu bar,
   * canvas, Nodes sidebar or tabs; toasts and dialogs as ever — and every change SAVED at once (`commitParam`).
   */
  const [settingsOnly] = useState(() => onlyFromSearch(window.location.search) === "settings");
  /** The side panel's tab — the one a link asks for (`?tab=settings`), then the person's. */
  const [tab, setTab] = useState<SideTab>(() => tabFromSearch(window.location.search));
  /** What the host runs each graph with over its settings (`values`, `host.ts`) — the settings form grays by it. */
  const [hostValues, setHostValues] = useState<Record<string, Record<string, Json>>>({});
  // What the host measured for each setting, by graph (`hints`): a line under the field, never written.
  const [hostHints, setHostHints] = useState<Record<string, Record<string, string>>>({});
  /** The tab the side panel shows: the settings, always, on a settings-only page. */
  const sideTab: SideTab = settingsOnly ? "settings" : tab;
  /**
   * The settings tab's "all parameters" tick, per graph, for the page's life — kept here, not in the tab, which unmounts
   * when another tab is shown (measured before: the tick was lost every time the person looked at the run tab).
   */
  const [allParams, setAllParams] = useState<Record<string, boolean>>({});
  const [run, setRun] = useState<Run | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [answers, setAnswers] = useState(0);
  const [record, setRecord] = useState(0);
  const [running, setRunning] = useState(false);
  const [inspected, setInspected] = useState<string | undefined>(undefined);
  const [sidebar, setSidebar] = useState(readSidebar);
  // a host's panel starts without the minimap — its corner of a small canvas (View ▸ Minimap shows it)
  const [minimap, setMinimap] = useState(() => !embedFromSearch(window.location.search));
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
  /** The app the editor is mounted in (`GET api/workspace`'s `host`): a graph without its own source runs ITS record. */
  const [host, setHost] = useState<HostInfo | null>(null);
  /** A link (or the host) named a graph the list does not hold: the page says so instead of opening another one. */
  const [missing, setMissing] = useState<string | null>(null);
  /** Set source…: the slot whose class picker and form are up (`slots.ts`). */
  const [slotAsked, setSlotAsked] = useState<Slot | null>(null);
  /** A Restore or a discard the server refused, in its words — shown in the dialog, which stays up. */
  const [recoveryRefused, setRecoveryRefused] = useState<string | null>(null);
  const [recoveryBusy, setRecoveryBusy] = useState(false);
  /** The graphs a Restore brought back: the first is shown when no link names a graph. */
  const restoredNames = useRef<string[]>([]);
  /** A Restore or a discard answered the graph list — the first `GET api/graphs`, if later, must not replace it. */
  const answeredList = useRef(false);
  const { toasts, push, dismiss } = useToasts();
  /**
   * Ask for the catalogue (`GET api/palette`) — the first call asks, later ones do nothing until an answer failed
   * (`catalogue.ts`). Called when something that lists it is shown, never as the page opens.
   */
  const [needCatalogue] = useState(() => onFirstNeed(getPalette, setSpecs, (exc) => push("error", errorMessage(exc))));
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
    // unsaved work the last session kept when it stopped: asked about before any graph is loaded
    getWorkspace().then(
      (workspace) => {
        joinSession(workspace.session); // every request from now on says which editor it was meant for (R1, P1)
        setFolder(workspace.folder);
        setHost(hostOf(workspace));
        setRecovery(workspace.recovery);
        setRecoveryPhase(workspace.recovery === null ? "none" : "asking");
      },
      (exc) => {
        setRecoveryPhase("none"); // the editor works without the answer; it only cannot offer the kept work
        push("warning", `could not ask the editor about unsaved work of the last session: ${errorMessage(exc)}`);
      },
    );
  }, [push]);

  // The catalogue on first need (`catalogue.ts`): the Nodes sidebar — open at start when the person left it open —,
  // the palette and Set source… ask here; Add Node ▸ asks when its menu is drawn. Never at open: building it imports
  // every installed package that declares classes, and the first graph list waited on those imports.
  useEffect(() => {
    if (needsCatalogue({ sidebar, palette: palette !== null, setSource: slotAsked !== null })) needCatalogue();
  }, [sidebar, palette, slotAsked, needCatalogue]);

  /** Show a graph the server answered, with its form: a fresh timeline, no run, nothing selected. */
  const present = useCallback((loaded: Graph, schema: Form) => {
    timeline.current.clear();
    setMissing(null);
    setForm(schema);
    setGraph(loaded);
    setGraphs((list) => markDirty(list, loaded.name, Boolean(loaded.dirty)));
    setRun(null);
    setRunError(null);
    setInspected(undefined);
    setSelection([]);
    setGroupsSelected([]);
    setWiresSelected(0);
    setPalette(null);
    setContext(null);
    setEditing(null);
    // Set source… is about the graph it was opened on: measured before, a host's `open` left it up over the next graph,
    // and Set wrote the source into THAT one (every Stream names its stream `stream`, so the slot is there too)
    setSlotAsked(null);
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

  // The graph a link names, once the list is known and the recovery question answered — and when the list does not
  // hold it, NOTHING: the canvas names it and the graphs there are (`link.pickGraph`; measured before, `?graph=nosuch`
  // opened the first graph of the folder without a word).
  useEffect(() => {
    if (graph !== null || missing !== null || !listed || !recoveryAnswered(recoveryPhase)) return;
    const wanted = graphFromSearch(window.location.search);
    const pick = pickGraph(graphs, wanted, restoredNames.current);
    if (pick.kind === "missing") {
      setMissing(pick.wanted);
      return;
    }
    if (pick.kind === "none") return;
    linkedLevel.current = pick.name === wanted ? levelFromSearch(window.location.search) : "";
    void load(pick.name);
  }, [graph, graphs, listed, load, missing, recoveryPhase]);

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
      setHost(hostOf(workspace));
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

  // `?graph=`, `?level=` and `?tab=` follow what the page shows, so a reload or a shared link opens the same graph, level
  // and tab (`link.pageHref`; `?embed=` is the host's and stays as given).
  const shownName = graph?.name;
  useEffect(() => {
    if (shownName === undefined) return;
    const href = pageHref(window.location.href, shownName, shown, tab);
    if (href !== window.location.href) window.history.replaceState(null, "", href);
  }, [shownName, shown, tab]);

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
  const accept = useCallback((next: Graph, entry?: TimelineEntry, count = true, replaces?: string) => {
    setGraphs((list) => markDirty(list, next.name, Boolean(next.dirty)));
    if (count) setSaves((saved) => saved + 1);
    // An answer for a graph no longer shown — the host opened another while the write was on its way (a field left as
    // the host switched, `openAsked`) — only marks the list: drawing it would switch the canvas back. `replaces` is the
    // name the answer takes over (Save As answers the graph under its new name).
    const showing = graphRef.current;
    if (showing !== null && showing.name !== next.name && showing.name !== replaces) return;
    graphRef.current = next;
    setGraph(next);
    if (entry) timeline.current.record(entry);
  }, []);

  /** Tell the host whose panel the page sits in (`?embed=1`, same origin only) — `host.ts`'s messages. */
  const postToHost = useCallback(
    (message: PageMessage) => {
      if (!embed || window.parent === window) return;
      window.parent.postMessage(message, window.location.origin);
    },
    [embed],
  );

  /**
   * Ask the editor where its folder is NOW and keep it — the server follows a host that moved its folder at every
   * request, the page only when it asks (measured before: read once at join, so after a move New… said it writes
   * `state/fresh.yaml` and wrote `saved.d/fresh.yaml`, and a missing graph was looked for in the old folder).
   */
  const refreshFolder = useCallback(async (): Promise<void> => {
    try {
      setFolder((await getWorkspace()).folder);
    } catch {
      // the page keeps the folder it knew: the dialog still works, its default is only a suggestion
    }
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
  /** A path field's `…` (`pickers.ts`): the file browser for a folder or any file — the path chosen, or `null`. */
  const browse = useCallback(
    (picker: "directory" | "file", start?: string) =>
      askFile(
        picker === "directory"
          ? { mode: "folder", title: "Choose a folder", confirm: "Choose", dir: start }
          : { mode: "file", title: "Choose a file", confirm: "Choose", dir: start },
      ).then((answer) => answer?.path ?? null),
    [askFile],
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

  /** The selected wires' ends, read from React Flow at the gesture (`selectedNow`'s reason). */
  const selectedWiresNow = useCallback((): WireEnds[] => selectedWires(flow.getEdges()), [flow]);

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
   * or the last node, and — inside a subgraph — the last inner step (`levels.levelAnchor`). The host card and the root
   * are never an anchor (`steps.selectedAnchor`).
   */
  const anchorNow = useCallback((): string | undefined => {
    if (graphRef.current === null) return undefined;
    const view = viewNow();
    return levelAnchor(view, levelRef.current, selectedAnchor(view, selectedNow()));
  }, [selectedNow, viewNow]);

  /**
   * Where an ADDED class goes (the Nodes sidebar, Add Node ▸, a drop): `anchorNow`'s rule on a level of flow steps,
   * and on an op list after the selected member, else at the end of the list (`oplists.insertTarget`).
   */
  const targetNow = useCallback((): InsertTarget => {
    const current = graphRef.current;
    if (current === null) return { kind: "none" };
    const view = viewNow();
    return insertTarget(view, levelRef.current, current.kind, selectedAnchor(view, selectedNow()));
  }, [selectedNow, viewNow]);

  // ---- levels: which subgraph's inside the canvas draws ----------------------------------------------------------

  /** Draw the level `next` (Open, a crumb, Escape): nothing stays selected, no menu or palette stays open. */
  const goTo = useCallback((next: string) => {
    levelRef.current = next;
    setLevel(next);
    setSelection([]);
    setGroupsSelected([]);
    setWiresSelected(0);
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

  // ---- the host's panel (`?embed=1`, `host.ts`): what the page tells it, and the one command it takes ---------------

  /**
   * The host asks for a graph (`{type: "open", graph, tab?, level?}`): the list is asked afresh (the host may just have
   * written the file), then the graph opens — or, when the list does not hold it, the canvas says so. Before the
   * recovery question is answered nothing loads: the link is pointed at the graph, which opens once it is.
   */
  const openAsked = useCallback(
    async (command: OpenCommand) => {
      // A field the person typed into and has not left is committed NOW, to the graph it was typed in: its blur writes
      // through `graphRef` before anything below changes the graph shown. Measured before: the card of a node of the
      // same name in the next graph kept the typed value and committed it THERE when the field was left.
      const typing = document.activeElement;
      if (typing instanceof HTMLElement && typing !== document.body) typing.blur();
      if (command.tab !== undefined) setTab(command.tab);
      // the link names what is asked for at once: a reload, or the load after the recovery question, opens it
      window.history.replaceState(null, "", pageHref(window.location.href, command.graph, command.level ?? "", command.tab ?? tab));
      if (!recoveryAnswered(recoveryPhase)) return;
      let list = graphs;
      try {
        // the list and the folder afresh: the host may just have written the file, or moved its folder
        const [listedNow] = await Promise.all([listGraphs(), refreshFolder()]);
        list = listedNow;
        setGraphs(list);
      } catch (exc) {
        push("error", errorMessage(exc));
      }
      const current = graphRef.current;
      if (current !== null && current.name === command.graph) {
        // The host may have rewritten the file: the editor reads a graph holding nothing unsaved again, so the answer
        // is what the file says now — shown when it differs (measured before: the panel kept the first text read).
        try {
          const fresh = await getGraph(command.graph);
          // the text, or the MODEL: a host saving a setting itself (a pane's knob) leaves the graph's text as it was
          const changed =
            graphRef.current !== null &&
            (fresh.text !== graphRef.current.text || JSON.stringify(fresh.model) !== JSON.stringify(graphRef.current.model));
          if (graphRef.current?.name === command.graph && changed) {
            linkedLevel.current = command.level ?? levelRef.current;
            present(fresh, await getForm(command.graph));
            return;
          }
        } catch (exc) {
          push("error", errorMessage(exc));
        }
        if (command.level !== undefined) goTo(existingLevel(current.model, command.level));
        return;
      }
      if (!list.some((entry) => entry.name === command.graph)) {
        graphRef.current = null;
        setGraph(null);
        setMissing(command.graph);
        return;
      }
      linkedLevel.current = command.level ?? "";
      await load(command.graph);
    },
    [goTo, graphs, load, present, push, recoveryPhase, refreshFolder, tab],
  );
  const openAskedRef = useRef(openAsked);
  openAskedRef.current = openAsked;

  /** The parameter writes on their way — `commitParam` adds each; a host's `commit` waits for them. */
  const writes = useRef(new Set<Promise<boolean>>());

  /**
   * The host asks the page to commit (`{type: "commit"}` — a settings panel's run button about to read the settings
   * file): the field being typed in is left, which writes it (and on a settings-only page saves it), and once every
   * write on its way is done the page answers `committed`. Measured before: a run pressed while a field still held
   * its typed value read the settings file BEFORE the field's save landed, and ran with the old value.
   */
  const commitAsked = useCallback(async () => {
    const typing = document.activeElement;
    if (typing instanceof HTMLElement && typing !== document.body) typing.blur();
    while (writes.current.size > 0) await Promise.allSettled([...writes.current]);
    postToHost(committedMessage(graphRef.current?.name ?? ""));
  }, [postToHost]);
  const commitAskedRef = useRef(commitAsked);
  commitAskedRef.current = commitAsked;
  useEffect(() => {
    if (!embed) return;
    const onMessage = (event: MessageEvent) => {
      if (event.source !== window.parent || event.origin !== window.location.origin) return; // its own host only
      const command = parseHostCommand(event.data);
      if (command?.type === "commit") void commitAskedRef.current();
      else if (command?.type === "values") setHostValues((all) => ({ ...all, [command.graph]: command.values }));
      else if (command?.type === "hints") setHostHints((all) => ({ ...all, [command.graph]: command.hints }));
      else if (command?.type === "style") {
        // the host page's font, as variables the settings form reads (styles.css `.app.settings-only`)
        const root = document.documentElement.style;
        if (command.fontSizePx !== undefined) root.setProperty("--host-font-size", `${command.fontSizePx}px`);
        if (command.fontFamily !== undefined) root.setProperty("--host-font-family", command.fontFamily);
      }
      else if (command !== undefined) void openAskedRef.current(command);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [embed]);

  // what the page shows and holds, posted to the host on every change — it warns before closing a panel with unsaved work,
  // and uncovers the panel while the recovery question waits for the person (no graph opens before it is answered)
  const unsavedKey = graphs
    .filter((entry) => entry.dirty)
    .map((entry) => entry.name)
    .join("\n");
  const shownDirty = Boolean(graph?.dirty);
  const askingAboutKeptWork = waitsForAnswer(recoveryPhase);
  useEffect(() => {
    const unsaved = unsavedKey === "" ? [] : unsavedKey.split("\n");
    postToHost(stateMessage(shownName ?? "", shownDirty, unsaved, tab, askingAboutKeptWork));
  }, [postToHost, shownName, shownDirty, unsavedKey, tab, askingAboutKeptWork]);

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
   * Add a class: an op where `target` says (`targetNow()` by default) — after a step or a list member, or at the end of
   * an op list — placed at `at`, else beside the node it follows (an append: the list's last member, else its holder);
   * any other class (its catalogue `kind`) as a CARD of its own, drawn at `at` and wired to the dropped `wire`
   * (`cards.ts` — the server places it by the kind the document derives).
   */
  const addClass = useCallback(
    async (spec: { class_path: string; kind?: string }, at?: XY, given?: InsertTarget, wire?: DraggedEnd): Promise<void> => {
      const current = graphRef.current;
      if (current === null) return;
      const target = given ?? targetNow();
      const view = viewNow();
      // named on the level drawn: `threshold` at the top, `prep/threshold` inside prep (a list member is named by
      // its position, a card after its class, whatever is sent)
      const taken = view.nodes.map((node) => shortName(node.name));
      const name = qualify(levelRef.current, stepName(spec.class_path, taken));
      const plan = addPlan(spec.kind, spec.class_path, name, target, wire);
      if ("refused" in plan) {
        push("error", `${fileOf(current)}: ${plan.refused}`);
        return;
      }
      const member = plan.card ? undefined : addedMember(view, target);
      const near = plan.card ? undefined : beside(view, target);
      try {
        const next = await addStep(current.name, plan.request);
        if (plan.card) {
          // drawn where the person asked, under the name the server gave it (`class_names_scan`)
          accept(await placeNew(current, next, at, next.added), { kind: "document" });
          return;
        }
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

  /** dagre over the MEASURED cards along the wires, in `direction` — then saved like a drag, and shown whole. */
  const arrangeHere = useCallback((direction: Direction) => {
    const current = graphRef.current;
    if (current === null) return;
    const drawn = flow.getNodes().filter((node) => node.type === "generic");
    const boxes = drawn.map((node) => ({
      id: node.id,
      width: node.measured?.width ?? CARD.width,
      height: node.measured?.height ?? CARD.height,
    }));
    const placed = arrange(boxes, viewNow().wires, direction);
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

  /**
   * A wire drawn between two sockets whose types meet: the server SAVES it (`POST …/wires`) and its answer is drawn —
   * or its one plain line is a warning and nothing is drawn (`wires.ts`).
   */
  const onWire = useCallback(
    (connection: Connected) => {
      const current = graphRef.current;
      if (current === null) return;
      void drawWire(connectionEnds(connection), {
        add: (wire) => addWire(current.name, wire),
        accept: (next) => accept(next, { kind: "document" }),
        refuse: (message) => push("warning", message),
      });
    },
    [accept, push],
  );

  /**
   * Delete: the selected cards (their wires go with them) — never the host card, which a mixed selection leaves where it
   * is — else the selected boxes, else the selected wires.
   */
  const deleteSelection = useCallback(() => {
    const current = graphRef.current;
    if (current === null) return;
    const nodes = withoutHost(viewNow(), selectedNow());
    if (nodes.length > 0) {
      void removeNodes(nodes);
      return;
    }
    const boxes = selectedGroupsNow();
    if (boxes.length > 0) {
      void writeGroups(current.groups.filter((group) => !boxes.includes(group.id)));
      return;
    }
    void deleteWires(selectedWiresNow(), {
      remove: (wires) => removeWires(current.name, wires),
      accept: (next) => accept(next, { kind: "document" }),
      refuse: (message) => push("warning", message),
    });
  }, [accept, push, removeNodes, selectedGroupsNow, selectedNow, selectedWiresNow, viewNow, writeGroups]);

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
      const { host_error: hostError, ...saved } = await saveGraph(current.name);
      accept(saved);
      postToHost(savedMessage(saved.name, saved.path)); // a host re-runs what reads this file at once
      // the file IS written; the host failed on the news of it — in its words
      if (hostError !== undefined) push("warning", `${fileOf(saved)} is saved, but ${host?.name ?? "the host"} said: ${hostError}`);
    } catch (exc) {
      push("error", errorMessage(exc));
    }
  }, [accept, host, postToHost, push]);

  /**
   * The settings sheet's Save (`?only=settings`): the graph's SETTINGS file alone (`api.saveSettings`) — a change of the
   * graph itself the editor's tab holds stays unsaved there (user pick 2026-10-05). The host hears it like any Save.
   */
  const saveSettingsAlone = useCallback(async () => {
    const current = graphRef.current;
    if (current === null) return;
    try {
      const { host_error: hostError, ...saved } = await saveSettings(current.name);
      accept(saved);
      postToHost(savedMessage(saved.name, saved.path)); // a host re-runs what reads this graph at once
      if (hostError !== undefined) push("warning", `${fileOf(saved)} is saved, but ${host?.name ?? "the host"} said: ${hostError}`);
    } catch (exc) {
      push("error", errorMessage(exc));
    }
  }, [accept, host, postToHost, push]);

  /**
   * How the page writes a parameter: `setParam`, and on a settings-only page (`?only=settings`) the settings' Save right
   * after — the sheet holds no unsaved state, and the host hears every change at once (its pane asks again). A setting
   * the server refuses saves nothing.
   */
  const commitParam = useCallback(
    (node: string, param: string, value: Json): Promise<boolean> => {
      const writing = (async () => {
        const done = await setParam(node, param, value);
        if (done && settingsOnly) await saveSettingsAlone();
        return done;
      })();
      // a host's `commit` waits for every write on its way (`commitAsked`)
      writes.current.add(writing);
      void writing.finally(() => writes.current.delete(writing));
      return writing;
    },
    [saveSettingsAlone, setParam, settingsOnly],
  );

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
      const { host_error: hostError, ...next } = await saveGraphAs(current.name, answer.path, answer.overwrite);
      setGraphs(await listGraphs());
      timeline.current.clear(); // the page's layout entries were the old entry's
      accept(next, undefined, true, current.name); // ?graph= follows the new name (the URL effect)
      postToHost(savedMessage(next.name, next.path));
      if (hostError !== undefined) push("warning", `${fileOf(next)} is saved, but ${host?.name ?? "the host"} said: ${hostError}`);
    } catch (exc) {
      push("error", errorMessage(exc));
    }
  }, [accept, askFile, host, postToHost, push]);

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

  /** New ▸ <kind>: the dialog opens once the page has asked where the editor's folder is now (`refreshFolder`). */
  const openNew = useCallback(
    (newKind: NewGraphKind) => void refreshFolder().then(() => setDialog({ kind: "new", newKind })),
    [refreshFolder],
  );

  /**
   * New…'s Create (DESIGN3): the server WRITES the starter file and answers the graph, not dirty; the dialog closes and
   * the graph opens. A refusal writes nothing — the file exists, the folder does not, the name is refused — and goes
   * back to the dialog, which stays up showing the server's words.
   */
  const create = useCallback(
    async (answer: NewAnswer): Promise<string | null> => {
      let made: Graph;
      let hostError: string | undefined;
      try {
        ({ host_error: hostError, ...made } = await newGraph(answer.name, answer.kind, answer.dir));
      } catch (exc) {
        return errorMessage(exc);
      }
      setDialog(null);
      postToHost(savedMessage(made.name, made.path)); // New… WROTE a graph file: the host hears it as it hears a Save
      if (hostError !== undefined) push("warning", `${fileOf(made)} is written, but ${host?.name ?? "the host"} said: ${hostError}`);
      await show(made); // the file is written: whatever this says, the dialog has done its part
      return null;
    },
    [host, postToHost, push, show],
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

  // ---- set source… (`slots.ts`): a class and its values into a source slot ---------------------------------------------

  /** The source slot `slot` of `node` on the level drawn, with what fills it now; `undefined` when it is not one. */
  const slotOf = useCallback(
    (node: string, slot: string): Slot | undefined => {
      const view = viewNow();
      const found = view.nodes.find((candidate) => candidate.name === node);
      return found === undefined ? undefined : sourceSlots(view, found).find((candidate) => candidate.slot === slot);
    },
    [viewNow],
  );

  /** Open the class picker and form for a slot (its card's `set…`, the node menu, a wire dropped out of it). */
  const chooseSlot = useCallback(
    (node: string, slot: string) => {
      const found = slotOf(node, slot);
      setContext(null);
      setPalette(null);
      if (found !== undefined) setSlotAsked(found);
    },
    [slotOf],
  );

  /** Set: ONE edit (`POST …/slot`), one undo entry; a refusal goes back to the dialog, which shows it. */
  const writeSlot = useCallback(
    async (classPath: string, params: Record<string, Json>): Promise<string | null> => {
      const current = graphRef.current;
      const slot = slotAsked;
      if (current === null || slot === null) return null;
      try {
        const { set: _set, ...next } = await setSlot(current.name, slot.node, slot.slot, classPath, params);
        setSlotAsked(null);
        accept(next, { kind: "document" });
        return null;
      } catch (exc) {
        return errorMessage(exc);
      }
    },
    [accept, slotAsked],
  );

  // ---- the palette ---------------------------------------------------------------------------------------------

  const onPalette = useCallback((gesture: PaletteGesture) => {
    const current = graphRef.current;
    if (current === null) return;
    const view = viewNow();
    const level = levelRef.current;
    // a wire dragged OUT of a source slot and dropped on nothing: the slot is filled by a class and its values (Set
    // source…), never by an inserted step — a Stream's source is not a step of its ops
    if (gesture.from?.kind === "in" && slotOf(gesture.from.node, gesture.from.socket) !== undefined) {
      chooseSlot(gesture.from.node, gesture.from.socket);
      return;
    }
    if (!gesture.from) {
      setPalette({
        screen: gesture.screen,
        openedAt: gesture.openedAt,
        target: insertTarget(view, level, current.kind, selectedAnchor(view, gesture.selected ?? [])),
      });
      return;
    }
    const { node, kind, type, socket } = gesture.from;
    // from an OUTPUT: the new node takes the type and goes after the node dragged from; from an INPUT: it gives the
    // type and goes before that node — after whatever feeds its record (inside a subgraph, never after its <in>); a
    // Stream's member goes into its list the same way (`oplists.wireTarget`). From an op-list item input (a Pipeline's
    // `transforms[k]`) the pick becomes that list's item k (`oplists.inputTarget` — P3). The host card is no anchor; a
    // CARD picked here is wired to the end the wire was dragged out of (`cards.draggedWire`)
    const listed = kind === "in" ? inputTarget(view, node, socket) : undefined;
    const after = kind === "out" ? node : levelPredecessor(view, node);
    const anchor = after === undefined ? undefined : selectedAnchor(view, [after]);
    const why =
      after === undefined
        ? `nothing feeds ${node} — a step can only be inserted after another one`
        : `${after} is the host's side of the graph — an op is inserted after a step`;
    setPalette({
      screen: gesture.screen,
      openedAt: gesture.openedAt,
      filter: { type, side: kind === "out" ? "inputs" : "outputs" },
      target: listed ?? (anchor === undefined ? { kind: "none", why } : wireTarget(view, level, current.kind, anchor)),
      wire: draggedWire(gesture.from),
    });
  }, [chooseSlot, slotOf, viewNow]);

  const pick = useCallback(
    (spec: NodeSpec) => {
      const request = palette;
      setPalette(null);
      if (request === null) return;
      // the new node lands where the gesture was (the double-click, the wire's release); an op with nowhere to go is
      // refused there, a card never is
      void addClass(spec, flow.screenToFlowPosition(request.screen), request.target, request.wire);
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
        // the host's record for a graph without its own source when a host is there, else record #N (`host.runRecord`)
        answered(await trace(current.name, runRecord(host, current.model, record), until), null);
      } catch (exc) {
        answered(null, errorMessage(exc));
      }
    },
    [answered, host, record],
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
          return openNew("flow");
        case "new-pipeline":
          return openNew("pipeline");
        case "new-source":
          return openNew("source");
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
          return arrangeHere("LR");
        case "arrange-diagonal":
          return arrangeHere("diagonal");
        case "arrange-down":
          return arrangeHere("TB");
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
      openNew,
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

  /** The model of the level drawn, as this render draws it (`viewNow` reads it at a gesture). */
  const shownView = graph === null ? EMPTY_MODEL : (levelModel(graph.model, shown) ?? EMPTY_MODEL);
  const commandState: CommandState = {
    graph: graph !== null,
    dirty: dirtyShown,
    selected: selection.length,
    // the host card is drawn, never written: Rename… and Delete leave it (`commands.enabled`)
    hostSelected: withoutHost(shownView, selection).length < selection.length,
    groupSelected: groupsSelected.length > 0,
    wireSelected: wiresSelected > 0,
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
      hostSelected: withoutHost(viewNow(), selected).length < selected.length,
      groupSelected: selectedGroupsNow().length > 0,
      wireSelected: selectedWiresNow().length > 0,
      // a copy pressed a moment ago is on its way: Paste waits for it (the state above renders a frame later)
      clipboard: clipboard.current !== null || copying.current !== null,
      traced: run !== null && last !== undefined && run.trace.nodes.some((node) => node.node === last),
    };
  }, [commandState, run, selectedGroupsNow, selectedNow, selectedWiresNow, viewNow]);

  // the recovery question is answered first: no shortcut acts while it is asked (or still on its way), and the bar and
  // the canvas are INERT — out of reach of the mouse and of Tab (P5: measured before, Tab reached the New menu and the
  // graph picker, and a graph loaded before the answer stayed drawn from disk after a Restore)
  const recovering = !recoveryAnswered(recoveryPhase);
  const bar = useRef<HTMLElement>(null);
  const body = useRef<HTMLElement>(null);
  useEffect(() => {
    for (const part of [bar.current, body.current]) part?.toggleAttribute("inert", recovering);
  }, [recovering]);
  const asking = dialog !== null || slotAsked !== null;
  const keys = useRef({ runCommand, stateNow, menuOpen, context, asking, palette, level: shown, goTo, recovering });
  keys.current = { runCommand, stateNow, menuOpen, context, asking, palette, level: shown, goTo, recovering };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const now = keys.current;
      if (event.key === "Escape" && (now.menuOpen !== null || now.context !== null)) {
        event.preventDefault();
        setMenuOpen(null);
        setContext(null);
        return;
      }
      if (now.asking || now.palette !== null || now.recovering || isTyping(event.target)) return;
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
            onShown={needCatalogue}
            close={close}
            onPick={(spec) => {
              close();
              void addClass(spec, request.flow);
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
    const state = selection.includes(node.name) ? commandState : { ...commandState, selected: 1, hostSelected: isHostCard(node) };
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
      slots: (graph === null ? [] : sourceSlots(levelModel(graph.model, shown) ?? EMPTY_MODEL, node)).map((slot) => ({
        slot: slot.slot,
        filled: slot.filled,
        choose: () => chooseSlot(node.name, slot.slot),
      })),
      // Open shows the inside the server sent; without one it stays greyed
      openSubgraph: graph && levelModel(graph.model, node.name) !== undefined ? () => openSubgraph(node.name) : undefined,
    });
  };

  const model = graph?.model;
  const shownGraph = graph?.name ?? "";
  const editor = useMemo<EditorApi>(
    () => ({
      graph: shownGraph,
      host,
      form,
      types,
      run,
      setParam: commitParam,
      rename,
      updateGroup,
      ungroup,
      resizeGroup,
      editing,
      setEditing,
      openSubgraph,
      innerSteps: (name: string) => (model === undefined ? [] : innerStepsOf(model, name)),
      browse,
      chooseSlot,
    }),
    [shownGraph, host, form, types, run, commitParam, rename, updateGroup, ungroup, resizeGroup, editing, openSubgraph, model, browse, chooseSlot],
  );

  const inspectedNode = graph !== null && inspected !== undefined ? findNode(graph.model, inspected) : undefined;
  /** Run sends the host's record: a host is there and the graph has no source of its own (`host.runRecord`). */
  const fromHost = graph !== null && host !== null && !hasOwnSource(graph.model);
  const targetShown: InsertTarget = graph ? insertTarget(shownView, shown, graph.kind, selectedAnchor(shownView, selection)) : { kind: "none" };

  return (
    <EditorContext.Provider value={editor}>
      <div
        className={`app${embed ? " embed" : ""}${settingsOnly ? " settings-only" : ""}`}
        data-embed={embed ? "true" : "false"}
        data-only={settingsOnly ? "settings" : ""}
        data-host={host?.name ?? ""}
        data-side-tab={sideTab}
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
        {!settingsOnly && <header className="bar" ref={bar}>
          {/* in a host's panel the host's own page says what app this is: no second brand */}
          {!embed && (
            <span className="brand" title="streamstudio">
              <svg viewBox="0 0 20 20" aria-hidden="true">
                <circle cx="4" cy="10" r="2.6" />
                <circle cx="16" cy="4.5" r="2.6" />
                <circle cx="16" cy="15.5" r="2.6" />
                <path d="M6.3 9 13.7 5.5M6.3 11l7.4 3.5" strokeWidth="1.6" fill="none" />
              </svg>
              streamstudio
            </span>
          )}
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
              {graph === null && <option value="">{missing !== null ? `no graph '${missing}'` : listed ? "no graph" : "loading…"}</option>}
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
        </header>}
        <main className={`body${sidebar && !settingsOnly ? " with-nodes" : ""}`} ref={body}>
          {sidebar && !settingsOnly && (
            <NodesSidebar
              specs={addable}
              target={targetShown}
              onAdd={(spec) => void addClass(spec)}
              onClose={toggleSidebar}
            />
          )}
          {settingsOnly ? null : graph !== null ? (
            <Canvas
              graph={graph}
              types={types}
              level={shown}
              showMinimap={minimap}
              fitOnOpen={embed}
              onLayout={onLayout}
              onWire={onWire}
              onSelection={(names, groups, wires) => {
                setSelection(names);
                setGroupsSelected(groups);
                setWiresSelected(wires);
              }}
              onPalette={onPalette}
              onNodeClick={onNodeClick}
              onContextMenu={onContextMenu}
              onDropClass={(classPath, at) => void addClass(specs?.find((spec) => spec.class_path === classPath) ?? { class_path: classPath }, at)}
              onOpenSubgraph={openSubgraph}
              onLevel={goTo}
            />
          ) : (
            <div className="canvas empty-canvas">
              {missing !== null && (
                <div className="empty-state graph-missing" data-graph-missing={missing}>
                  <h2>No graph '{missing}' here</h2>
                  <p data-graph-missing-message>{missingGraphMessage(missing, graphs, folder)}</p>
                  <div>
                    {graphs.map((entry) => (
                      <button key={entry.name} type="button" className="button" data-graph-choice={entry.name} onClick={() => void load(entry.name)}>
                        {pickerLabel(entry)}
                      </button>
                    ))}
                    {graphs.length === 0 && (
                      <>
                        <button type="button" className="button primary" onClick={() => runCommand("new-flow")}>
                          New flow graph…
                        </button>
                        <button type="button" className="button" onClick={() => runCommand("open")}>
                          Open…
                        </button>
                      </>
                    )}
                  </div>
                </div>
              )}
              {missing === null && listed && graphs.length === 0 && (
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
            {!settingsOnly && (
              <nav className="tabs">
                {SIDE_TABS.map((name) => (
                  <button key={name} data-tab={name} aria-selected={tab === name} onClick={() => setTab(name)}>
                    {name}
                  </button>
                ))}
              </nav>
            )}
            {settingsOnly && graph === null && (
              <p className="dim" data-graph-missing={missing ?? undefined}>
                {missing !== null ? missingGraphMessage(missing, graphs, folder) : "reading the graph…"}
              </p>
            )}
            {sideTab === "text" && (
              <>
                <h2 title={graph?.path}>{graph ? fileTitle(graph) : ""}</h2>
                <pre data-yaml>{graph?.text ?? ""}</pre>
              </>
            )}
            {sideTab === "settings" && graph !== null && (
              <SettingsForm
                key={graph.name}
                graph={graph}
                level={shown}
                all={allParams[graph.name] === true}
                onAll={(all) => setAllParams((ticked) => ({ ...ticked, [graph.name]: all }))}
                fixed={settingsOnly}
                hostValues={hostValues[graph.name]}
                hints={hostHints[graph.name]}
              />
            )}
            {sideTab === "run" && graph !== null && (
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
                host={host}
                fromHost={fromHost}
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
        {slotAsked !== null && graph !== null && (
          <SetSlotDialog
            slot={slotAsked}
            specs={addable}
            types={types}
            model={levelModel(graph.model, shown) ?? EMPTY_MODEL}
            onSet={writeSlot}
            onCancel={() => setSlotAsked(null)}
          />
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
        <Toasts toasts={toasts} dismiss={dismiss} />
      </div>
    </EditorContext.Provider>
  );
}
