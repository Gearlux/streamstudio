/**
 * The editor page: a graph picker and a toolbar over the canvas, a sidebar with three tabs (the document's text,
 * the settings form, the run panel), the palette and the toasts. The app owns the graph state and every route
 * call; the canvas, the nodes and the panels ask for what they need through props and the editor context.
 *
 * Every write goes to the server and the server's answer is drawn — the page never shows an edit the document did
 * not accept. A refusal is a toast carrying the server's located message verbatim. Undo/redo follows `history.ts`:
 * a document edit is undone by the SERVER's text history (the one truth an agent's edits share), a layout change
 * (a move, an arrange, a group) by the page, which wrote it; one Ctrl/Cmd+Z reverses whichever came last.
 *
 * Shortcuts (ignored while typing in a field): Ctrl/Cmd+Z undo, Shift+Ctrl/Cmd+Z or Ctrl/Cmd+Y redo, Ctrl/Cmd+C
 * copy the selected nodes, Ctrl/Cmd+V paste them as steps after the selection, G group the selection.
 *
 * `?graph=<name>` picks the graph on load (the first listed otherwise) and is kept in step with the picker, so a
 * link opens a graph directly. The root carries `data-graph`, `data-ready`, `data-saves` (accepted writes so far),
 * `data-palette-ready` and `data-selection` (the document nodes a gesture acts on) — what a test waits on instead
 * of timing.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ReactFlowProvider, useReactFlow } from "@xyflow/react";
import {
  addStep,
  errorMessage,
  getForm,
  getGraph,
  getPalette,
  getTypes,
  listGraphs,
  putGroups,
  putPositions,
  redo as postRedo,
  relayout,
  renameNode,
  rerun as postRerun,
  setParam as postParam,
  trace,
  undo as postUndo,
  type NodeSpec,
  type Run,
} from "./api";
import { Canvas, type PaletteGesture } from "./canvas/Canvas";
import { Palette, type PaletteRequest } from "./canvas/Palette";
import { EditorContext, type EditorApi } from "./context";
import { groupTogether, type Group } from "./groups";
import { Timeline, type TimelineEntry } from "./history";
import { arrange } from "./layout";
import type { CanvasNode, Form, Graph, GraphSummary, Json, Positions, Supertypes } from "./model";
import { SettingsForm } from "./panels/SettingsForm";
import { RunPanel } from "./panels/RunPanel";
import { nextNode, plainParams } from "./run";
import { recordPredecessor, stepAnchor, stepName } from "./steps";
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

/** What a copy carries: the class and the values the document spells for it. */
interface Copied {
  class_path: string;
  params: Record<string, Json>;
}

const fileOf = (graph: Graph): string => graph.path.split("/").pop() ?? graph.path;

const isTyping = (target: EventTarget | null): boolean =>
  target instanceof HTMLElement && (/^(INPUT|SELECT|TEXTAREA)$/.test(target.tagName) || target.isContentEditable);

function Editor() {
  const flow = useReactFlow<CanvasNode>();
  const [graphs, setGraphs] = useState<GraphSummary[]>([]);
  const [graph, setGraph] = useState<Graph | null>(null);
  const [form, setForm] = useState<Form>({});
  const [types, setTypes] = useState<Supertypes>({});
  const [specs, setSpecs] = useState<NodeSpec[] | null>(null);
  const [saves, setSaves] = useState(0);
  const [selection, setSelection] = useState<string[]>([]);
  const [palette, setPalette] = useState<PaletteRequest | null>(null);
  const [tab, setTab] = useState<SideTab>("text");
  const [run, setRun] = useState<Run | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [answers, setAnswers] = useState(0);
  const [record, setRecord] = useState(0);
  const [running, setRunning] = useState(false);
  const [inspected, setInspected] = useState<string | undefined>(undefined);
  const { toasts, push, dismiss } = useToasts();
  const timeline = useRef(new Timeline());
  const clipboard = useRef<Copied[]>([]);
  // The latest graph for handlers that run after an await — a closure's copy would be one answer behind.
  const graphRef = useRef(graph);
  graphRef.current = graph;

  useEffect(() => {
    listGraphs().then(setGraphs, (exc) => push("error", errorMessage(exc)));
    getTypes().then(setTypes, (exc) => push("error", errorMessage(exc)));
    // the catalogue is scanned once per server (seconds on a cold one): fetched in the background, never awaited
    getPalette().then(setSpecs, (exc) => push("error", errorMessage(exc)));
  }, [push]);

  const load = useCallback(
    async (name: string) => {
      try {
        const [loaded, schema] = await Promise.all([getGraph(name), getForm(name)]);
        timeline.current.clear();
        setForm(schema);
        setGraph(loaded);
        setRun(null);
        setRunError(null);
        setInspected(undefined);
        setSelection([]);
        setPalette(null);
        const url = new URL(window.location.href);
        url.searchParams.set("graph", name);
        window.history.replaceState(null, "", url);
      } catch (exc) {
        push("error", errorMessage(exc));
      }
    },
    [push],
  );

  useEffect(() => {
    if (graph !== null || graphs.length === 0) return;
    const wanted = new URLSearchParams(window.location.search).get("graph");
    void load(graphs.some((entry) => entry.name === wanted) && wanted ? wanted : graphs[0].name);
  }, [graph, graphs, load]);

  // A node the form has never described (a step just added, a node renamed) gets its schema from the server.
  useEffect(() => {
    if (graph === null || graph.model.nodes.every((node) => node.name in form)) return;
    getForm(graph.name).then(setForm, (exc) => push("error", errorMessage(exc)));
  }, [graph, form, push]);

  /** An accepted write: draw the server's answer, count it, and remember what to undo. */
  const accept = useCallback((next: Graph, entry?: TimelineEntry) => {
    setGraph(next);
    setSaves((count) => count + 1);
    if (entry) timeline.current.record(entry);
  }, []);

  // ---- document edits: the server's text history holds them ---------------------------------------------------

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
        accept(next, { kind: "document" });
        setSelection((names) => names.map((n) => (n === node ? name : n)));
        setInspected((was) => (was === node ? name : was));
        return true;
      } catch (exc) {
        push("error", errorMessage(exc));
        return false;
      }
    },
    [accept, push],
  );

  /** Insert steps one after another, starting after `anchor`; stops at the first refusal, which is toasted. */
  const insertSteps = useCallback(
    async (steps: Copied[], anchor: string | undefined, noAnchor: string): Promise<void> => {
      let current = graphRef.current;
      if (current === null) return;
      if (anchor === undefined) {
        push("error", `${fileOf(current)}: ${noAnchor}`);
        return;
      }
      let after = anchor;
      for (const step of steps) {
        const name = stepName(step.class_path, current.model.nodes.map((node) => node.name));
        try {
          current = await addStep(current.name, { name, after, class_path: step.class_path, params: step.params });
          accept(current, { kind: "document" });
          after = name;
        } catch (exc) {
          push("error", errorMessage(exc));
          return;
        }
      }
    },
    [accept, push],
  );

  // ---- layout: the sidecar; the page keeps the before/after ---------------------------------------------------

  const writePositions = useCallback(
    async (positions: Positions, record: boolean): Promise<boolean> => {
      const current = graphRef.current;
      if (current === null) return false;
      const before = Object.fromEntries(
        Object.keys(positions).map((name) => [name, current.positions[name]] as const).filter(([, xy]) => xy !== undefined),
      ) as Positions;
      try {
        const all = await putPositions(current.name, positions);
        accept({ ...(graphRef.current ?? current), positions: all }, record ? { kind: "positions", before, after: positions } : undefined);
        return true;
      } catch (exc) {
        push("error", errorMessage(exc));
        return false;
      }
    },
    [accept, push],
  );

  const writeGroups = useCallback(
    async (groups: Group[], record: boolean): Promise<boolean> => {
      const current = graphRef.current;
      if (current === null) return false;
      try {
        const answer = await putGroups(current.name, groups);
        accept(
          { ...(graphRef.current ?? current), groups: answer.groups },
          record ? { kind: "groups", before: current.groups, after: answer.groups } : undefined,
        );
        return true;
      } catch (exc) {
        push("error", errorMessage(exc));
        return false;
      }
    },
    [accept, push],
  );

  const onPositions = useCallback((positions: Positions) => void writePositions(positions, true), [writePositions]);

  /**
   * The selected document nodes, in the model's order, read from React Flow AT THE GESTURE. The `selection` state
   * (fed by React Flow's selection listener) arrives one effect after a click — measured: a Ctrl+C typed right
   * after a click read an empty selection — so it only drives what the toolbar shows; every gesture reads this.
   */
  const selectedNow = useCallback((): string[] => {
    const current = graphRef.current;
    if (current === null) return [];
    const picked = new Set(flow.getNodes().filter((node) => node.selected && node.type === "generic").map((node) => node.id));
    return current.model.nodes.map((node) => node.name).filter((name) => picked.has(name));
  }, [flow]);

  const groupSelection = useCallback(() => {
    const current = graphRef.current;
    const members = selectedNow();
    if (current === null || members.length === 0) return;
    void writeGroups(groupTogether(current.groups, members), true);
  }, [selectedNow, writeGroups]);

  const updateGroup = useCallback(
    (id: string, patch: Partial<Group>) => {
      const current = graphRef.current;
      if (current === null) return Promise.resolve(false);
      return writeGroups(current.groups.map((group) => (group.id === id ? { ...group, ...patch } : group)), true);
    },
    [writeGroups],
  );

  const ungroup = useCallback(
    (id: string) => {
      const current = graphRef.current;
      if (current === null) return Promise.resolve(false);
      return writeGroups(current.groups.filter((group) => group.id !== id), true);
    },
    [writeGroups],
  );

  /** dagre over the MEASURED cards, left to right along the wires — then saved like a drag. */
  const arrangeHere = useCallback(() => {
    const current = graphRef.current;
    if (current === null) return;
    const drawn = flow.getNodes().filter((node) => node.type === "generic");
    const boxes = drawn.map((node) => ({
      id: node.id,
      width: node.measured?.width ?? 240,
      height: node.measured?.height ?? 120,
    }));
    const placed = arrange(boxes, current.model.wires);
    void writePositions(Object.fromEntries(Object.entries(placed).map(([name, { x, y }]) => [name, [x, y]])), true);
  }, [flow, writePositions]);

  /** The server's own layered first layout (`streamstudio/layout.py`). */
  const arrangeOnServer = useCallback(async () => {
    const current = graphRef.current;
    if (current === null) return;
    try {
      const all = await relayout(current.name);
      accept({ ...(graphRef.current ?? current), positions: all }, { kind: "positions", before: current.positions, after: all });
    } catch (exc) {
      push("error", errorMessage(exc));
    }
  }, [accept, push]);

  // ---- undo / redo --------------------------------------------------------------------------------------------

  const step = useCallback(
    async (direction: "undo" | "redo") => {
      const current = graphRef.current;
      if (current === null) return;
      const entry = direction === "undo" ? timeline.current.nextUndo() : timeline.current.nextRedo();
      const done = () => (direction === "undo" ? timeline.current.undone() : timeline.current.redone());
      if (entry.kind === "positions") {
        if (await writePositions(direction === "undo" ? entry.before : entry.after, false)) done();
        return;
      }
      if (entry.kind === "groups") {
        if (await writeGroups(direction === "undo" ? entry.before : entry.after, false)) done();
        return;
      }
      try {
        const answer = await (direction === "undo" ? postUndo : postRedo)(current.name);
        done();
        if (answer.changed) {
          const { changed: _changed, ...next } = answer;
          accept(next);
        } else push("info", `${fileOf(current)}: nothing to ${direction} — the editor holds no ${direction === "undo" ? "earlier" : "later"} edit of this document`);
      } catch (exc) {
        push("error", errorMessage(exc));
      }
    },
    [accept, push, writeGroups, writePositions],
  );

  // ---- copy / paste -------------------------------------------------------------------------------------------

  const copy = useCallback(() => {
    const current = graphRef.current;
    if (current === null) return false;
    const selected = selectedNow();
    const picked = current.model.nodes.filter((node) => selected.includes(node.name) && node.class_path);
    if (picked.length === 0) return false;
    clipboard.current = picked.map((node) => ({ class_path: node.class_path, params: plainParams(node) }));
    push("info", `copied ${picked.map((node) => node.name).join(", ")} — Ctrl/Cmd+V adds ${picked.length === 1 ? "it" : "them"} after the selection`);
    return true;
  }, [push, selectedNow]);

  const paste = useCallback(() => {
    const current = graphRef.current;
    if (current === null || clipboard.current.length === 0) return false;
    const selected = selectedNow();
    const anchor = stepAnchor(current, selected[selected.length - 1]);
    void insertSteps(clipboard.current, anchor, "nothing to paste after — select a node first");
    return true;
  }, [insertSteps, selectedNow]);

  // ---- the palette --------------------------------------------------------------------------------------------

  const onPalette = useCallback((gesture: PaletteGesture) => {
    const current = graphRef.current;
    if (current === null) return;
    if (!gesture.from) {
      const selected = gesture.selected ?? [];
      setPalette({
        screen: gesture.screen,
        openedAt: gesture.openedAt,
        anchor: stepAnchor(current, selected[selected.length - 1]),
      });
      return;
    }
    const { node, kind, type } = gesture.from;
    // from an OUTPUT: the new node takes the type and goes after the node dragged from; from an INPUT: it gives the
    // type and goes before that node — after whatever feeds its record
    const anchor = kind === "out" ? node : recordPredecessor(current, node);
    setPalette({
      screen: gesture.screen,
      openedAt: gesture.openedAt,
      filter: { type, side: kind === "out" ? "inputs" : "outputs" },
      anchor,
      noAnchor: anchor === undefined ? `nothing feeds ${node} — a step can only be inserted after another one` : undefined,
    });
  }, []);

  const pick = useCallback(
    (spec: NodeSpec) => {
      const request = palette;
      setPalette(null);
      if (request === null) return;
      void insertSteps([{ class_path: spec.class_path, params: {} }], request.anchor, request.noAnchor ?? "nothing to add after");
    },
    [insertSteps, palette],
  );

  // ---- runs ---------------------------------------------------------------------------------------------------

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

  // ---- the keyboard -------------------------------------------------------------------------------------------

  const keys = useRef({ step, copy, paste, groupSelection });
  keys.current = { step, copy, paste, groupSelection };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (isTyping(event.target)) return;
      const mod = event.metaKey || event.ctrlKey;
      const key = event.key.toLowerCase();
      if (mod && key === "z") {
        event.preventDefault();
        void keys.current.step(event.shiftKey ? "redo" : "undo");
      } else if (mod && key === "y") {
        event.preventDefault();
        void keys.current.step("redo");
      } else if (mod && key === "c") {
        if (window.getSelection()?.toString()) return; // a text selection is the browser's to copy
        if (keys.current.copy()) event.preventDefault();
      } else if (mod && key === "v") {
        if (keys.current.paste()) event.preventDefault();
      } else if (!mod && !event.altKey && key === "g") {
        event.preventDefault();
        keys.current.groupSelection();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const editor = useMemo<EditorApi>(
    () => ({ form, types, run, setParam, rename, updateGroup, ungroup }),
    [form, types, run, setParam, rename, updateGroup, ungroup],
  );

  const inspectedNode = graph?.model.nodes.find((node) => node.name === inspected);

  return (
    <EditorContext.Provider value={editor}>
      <div
        className="app"
        data-graph={graph?.name ?? ""}
        data-ready={graph === null ? "false" : "true"}
        data-saves={saves}
        data-palette-ready={specs === null ? "false" : "true"}
        data-selection={selection.join(",")}
      >
        <header className="bar">
          <span className="brand">streamstudio</span>
          <select data-graph-picker value={graph?.name ?? ""} onChange={(event) => void load(event.target.value)}>
            {graph === null && <option value="">loading…</option>}
            {graphs.map((entry) => (
              <option key={entry.name} value={entry.name}>
                {entry.name} · {entry.kind}
              </option>
            ))}
          </select>
          <nav className="tools">
            <button data-action="undo" title="undo (Ctrl/Cmd+Z)" disabled={graph === null} onClick={() => void step("undo")}>
              ↶ undo
            </button>
            <button data-action="redo" title="redo (Shift+Ctrl/Cmd+Z)" disabled={graph === null} onClick={() => void step("redo")}>
              ↷ redo
            </button>
            <button data-action="group" title="group the selection (G)" disabled={selection.length === 0} onClick={groupSelection}>
              ▢ group
            </button>
            <button data-action="arrange" title="arrange left to right by the cards' sizes (dagre)" disabled={graph === null} onClick={arrangeHere}>
              ⇶ arrange
            </button>
            <button
              data-action="arrange-server"
              title="the server's layered layout: a column per layer"
              disabled={graph === null}
              onClick={() => void arrangeOnServer()}
            >
              ▦ columns
            </button>
            <span className="hint">double-click the canvas to add a step</span>
          </nav>
        </header>
        <main className="body">
          {graph !== null && (
            <Canvas
              graph={graph}
              types={types}
              onPositions={onPositions}
              onProposal={(message) => push("warning", message)}
              onSelection={setSelection}
              onPalette={onPalette}
              onNodeClick={onNodeClick}
            />
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
                <h2 title={graph?.path}>{graph ? fileOf(graph) : ""}</h2>
                <pre data-yaml>{graph?.text ?? ""}</pre>
              </>
            )}
            {tab === "settings" && graph !== null && <SettingsForm graph={graph} />}
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
          <Palette request={palette} specs={specs} types={types} onPick={pick} onClose={() => setPalette(null)} />
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
