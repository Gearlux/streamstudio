/**
 * The React Flow canvas over one graph, plus its status bar.
 *
 * The arrays are DERIVED from the graph every time the app hands a new one (an accepted edit answers the whole
 * graph): document nodes by name at the sidecar's positions, a box per sidecar group around its members, edges
 * from the document's wires. They are derived DURING render (the "adjust state when a prop changes" pattern, not
 * an effect), so the commit that shows an answer's `data-saves` also hands React Flow the answer's nodes. A
 * node's selection and measured size survive a redraw — an edit must not deselect what the person is working on.
 *
 * The canvas owns the gestures and hands each to the app, which owns the routes:
 *
 * - **dragging a wire** — `isValidConnection` applies the model's meet rule (`compatible`, with the server's type
 *   table) WHILE dragging, so a mismatch never becomes an edge. A drop on a handle whose type meets is handed to the
 *   app (`onWire`), which asks the server to SAVE it (`wires.ts`) and draws the answer — the canvas draws no edge of its
 *   own, so a wire the server refuses is never drawn. A drop on EMPTY canvas opens the palette narrowed to the classes
 *   that can take (or give) the dragged type.
 * - **clicking a wire** — React Flow selects it; Delete (the app's command) takes the selected wires out.
 * - **dragging a node or a group** — on release, every document node whose ABSOLUTE position changed (a dragged
 *   group moves its members) goes to `PUT …/positions`; a node whose centre lands in a group's box joins it, one
 *   that lands outside its box leaves it, and a sized box moved by its title takes its new corner — handed over as
 *   ONE layout change (`onLayout`), so one undo reverses the whole drag.
 * - **right-clicking** — the pane, a node (made the selection first, unless it is already part of it) or a group's
 *   title bar: the app opens that menu at the pointer (`onContextMenu`), with the flow point under it.
 * - **dropping a class from the Nodes sidebar** — the drop point, converted to flow coordinates, is where the app
 *   places the new step (`onDropClass`).
 * - **double-clicking empty canvas** — the palette. React Flow 12 has no pane double-click, so the wrapper listens;
 *   zoom-on-double-click is off. The first click of a double-click deselects (React Flow's pane click), so the
 *   selection is remembered at that first press: "after the selected node" means the node selected BEFORE it.
 * - **clicking a node** — the app shows its run entry.
 * - **double-clicking a subgraph node** (anywhere on its card but its name, which renames) — the app shows its
 *   inside (`onOpenSubgraph`).
 *
 * The canvas draws ONE LEVEL of the document (`level`, DESIGN §3 F2): `""` is the top; `prep` is the inside of the
 * subgraph node `prep` — its nodes and wires are `graph.model.inside["prep"]` (`levels.levelModel`), placed from the
 * same `positions` map under their qualified names (`prep/grey`), with only that level's groups. Inside a subgraph a
 * breadcrumb `demo › prep` sits above the drawing; a click on a crumb shows that level (`onLevel`). Each level keeps
 * its own viewport for the page's life: going up puts back the view the level was left at. Entering a subgraph the
 * first time shows its inside at ZOOM 1 — fitted when it fits, else panned so its `<in>` shows — never a fit that
 * shrinks the cards (measured: a four-card inside fitted at 0.66 drew its text at 6–8 px). In a host's panel
 * (`fitOnOpen`) a graph's TOP opens fitted, the whole graph in view (never above zoom 1). `data-view-level` names the
 * level once its viewport is set — what a test waits on.
 *
 * Zoom, pan, minimap, snap to a 10-px grid and box select (Shift + drag on empty canvas) are React Flow's own
 * (`START` says where the viewport begins and why). One press is taken from it: Shift + click on a card ADDS that card
 * to the selection, ComfyUI's way (`selection.ts`) — React Flow would start a box there. Cmd (macOS) / Ctrl + click
 * keeps React Flow's toggle, and dragging a selected card still moves the whole selection.
 */
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type DragEvent,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  applyEdgeChanges,
  applyNodeChanges,
  useNodesInitialized,
  useOnSelectionChange,
  useReactFlow,
  useStoreApi,
  type EdgeChange,
  type NodeChange,
  type NodeTypes,
  type OnConnectEnd,
  type OnNodeDrag,
  type Viewport,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  GROUP_HEAD,
  GROUP_PAD,
  absolutePositions,
  moveGroup,
  refitGroups,
  regroupAfterDrop,
  withGroups,
  type Box,
  type Group,
  type Rect,
  type Size,
  type XY,
} from "../groups";
import { EMPTY_MODEL, breadcrumb, isBoundary, levelModel, levelPositions } from "../levels";
import { CLASS_MIME } from "../panels/NodesSidebar";
import { addToSelection, shiftAddsCard } from "../selection";
import type { Connected } from "../wires";
import {
  compatible,
  modelToFlow,
  parseHandle,
  socketType,
  type CanvasEdge,
  type CanvasNode,
  type Graph,
  type HandleKind,
  type Model,
  type Positions,
  type SocketType,
  type Supertypes,
} from "../model";
import { GenericNodeView } from "./GenericNode";
import { GroupNodeView } from "./GroupNode";

const nodeTypes = { generic: GenericNodeView, group: GroupNodeView } satisfies NodeTypes;

/**
 * Zoom 1 — a screen pixel is a flow pixel, which is what makes a drag's effect measurable — and the flow's origin
 * pushed down by a group's title room: the first layout row sits 40 px from the top, and a box around it needs
 * `GROUP_PAD + GROUP_HEAD` above its members, so at y 0 its title bar (and its buttons) would be off the canvas.
 */
const START = { x: 0, y: GROUP_PAD + GROUP_HEAD, zoom: 1 };

/** What opened the palette: a double-click (with the selection before it), or a wire dropped on nothing. */
export interface PaletteGesture {
  screen: { x: number; y: number };
  openedAt: number;
  /** The selected document nodes when the double-click began, in the model's order. */
  selected?: string[];
  /** The handle a dropped wire was dragged from: its node, side, type and socket (`transforms[1]`). */
  from?: { node: string; kind: HandleKind; type: SocketType; socket: string };
}

/** What a drag changed: the nodes whose absolute place moved, and the groups when a membership or a box moved. */
export interface LayoutChange {
  positions: Positions;
  groups?: Group[];
}

/** What was right-clicked, where on the screen, and the flow point under the pointer. */
export type CanvasMenuRequest = { screen: XY; flow: XY } & (
  | { kind: "pane" }
  | { kind: "node"; node: string }
  | { kind: "group"; group: string }
);

export interface CanvasProps {
  graph: Graph;
  types: Supertypes;
  /** The subgraph level drawn (`""` = the top): only its groups are drawn and changed. */
  level?: string;
  showMinimap?: boolean;
  /**
   * Show a graph's top level WHOLE the first time it is drawn (a fit, never above zoom 1) — a host's panel (`?embed=1`):
   * measured before, zoom 1 from the top-left showed 1 of the Bluetooth pane's 30 cards in a 900x700 panel. The
   * standalone page keeps `START` (zoom 1), where a drag's effect is measured.
   */
  fitOnOpen?: boolean;
  /** A drag changed the layout — the app writes it (positions and, when they changed, groups) as one gesture. */
  onLayout: (change: LayoutChange) => void;
  /** A wire was drawn between sockets whose types meet: the app asks the server to save it and draws the answer. */
  onWire: (connection: Connected) => void;
  /**
   * The selected DOCUMENT nodes (a group box is not one), in the model's order, the selected group ids, and how many
   * wires are selected.
   */
  onSelection: (names: string[], groups: string[], wires: number) => void;
  onPalette: (gesture: PaletteGesture) => void;
  onNodeClick: (name: string) => void;
  onContextMenu: (request: CanvasMenuRequest) => void;
  /** A class from the Nodes sidebar was dropped at `at` (flow coordinates). */
  onDropClass: (classPath: string, at: XY) => void;
  /** A subgraph node was double-clicked: show its inside. */
  onOpenSubgraph?: (name: string) => void;
  /** A breadcrumb was clicked: show that level. */
  onLevel?: (level: string) => void;
}

/** The model a level draws — nothing when the document holds no such subgraph (the app falls back a level). */
const viewOf = (graph: Graph, level: string): Model => levelModel(graph.model, level) ?? EMPTY_MODEL;

/** How far inside the canvas's edge a subgraph's `<in>` is placed when its inside is wider than the canvas. */
const INLET_MARGIN = 40;
/** The smallest zoom a subgraph's inside opens at: the cards' text stays readable (about 9 px) there. */
const INSIDE_MIN_ZOOM = 0.8;

/** A double-click in a widget selects its text; it must not open the subgraph the widget sits on. */
const inField = (target: EventTarget | null): boolean =>
  target instanceof HTMLElement && target.closest("input, select, textarea, button") !== null;

/** Keep what React Flow learnt about a node (selected, measured) across a redraw from a new graph. */
function redraw(graph: Graph, previous: CanvasNode[], level: string): { nodes: CanvasNode[]; edges: CanvasEdge[] } {
  const view = viewOf(graph, level);
  const known = new Map(previous.map((node) => [node.id, node]));
  const sizes: Record<string, Size> = {};
  for (const node of previous)
    if (node.measured?.width !== undefined && node.measured.height !== undefined)
      sizes[node.id] = { width: node.measured.width, height: node.measured.height };
  const flow = modelToFlow(view, levelPositions(view, graph.positions));
  const nodes = withGroups(flow.nodes, graph.groups, sizes, level).map((node) => {
    const before = known.get(node.id);
    return before ? { ...node, selected: before.selected, measured: before.measured } : node;
  });
  return { nodes, edges: flow.edges };
}

/** A drawn node's size: what React Flow measured, else what it was given, else a card's assumed size. */
const sizeOf = (node: CanvasNode): Size => ({
  width: node.measured?.width ?? node.width ?? 240,
  height: node.measured?.height ?? node.height ?? 120,
});

export function Canvas({
  graph,
  types,
  level = "",
  showMinimap = true,
  fitOnOpen = false,
  onLayout,
  onWire,
  onSelection,
  onPalette,
  onNodeClick,
  onContextMenu,
  onDropClass,
  onOpenSubgraph,
  onLevel,
}: CanvasProps) {
  const [drawnFrom, setDrawnFrom] = useState<Graph | null>(null);
  const [drawnLevel, setDrawnLevel] = useState(level);
  const [nodes, setNodes] = useState<CanvasNode[]>([]);
  const [edges, setEdges] = useState<CanvasEdge[]>([]);
  if (drawnFrom !== graph || drawnLevel !== level) {
    // derived state: React re-renders this component at once, before committing — never a frame behind the graph
    const same = drawnFrom !== null && drawnFrom.name === graph.name && drawnLevel === level;
    const flow = redraw(graph, same ? nodes : [], level);
    setDrawnFrom(graph);
    setDrawnLevel(level);
    setNodes(flow.nodes);
    setEdges(flow.edges);
  }
  const view = viewOf(graph, level);
  const nodesRef = useRef(nodes);
  nodesRef.current = nodes;
  const flow = useReactFlow<CanvasNode, CanvasEdge>();
  const store = useStoreApi<CanvasNode, CanvasEdge>();
  const pressedSelection = useRef<string[]>([]);
  const initialized = useNodesInitialized();

  // Each level keeps its viewport: leaving one remembers where it was looked at, entering one puts that back — or,
  // for a subgraph shown the first time, fits its inside (React Flow queues the fit until the new cards are measured).
  // The top's first view is START, never a fit: a drag's effect stays measurable at zoom 1.
  const viewports = useRef<Record<string, Viewport>>({});
  const viewKey = `${graph.name}\u0000${level}`;
  const wrapper = useRef<HTMLDivElement>(null);
  const [viewLevel, setViewLevel] = useState<string | null>(null);

  /**
   * A subgraph's inside shown the first time: the whole inside when it fits at a readable zoom (INSIDE_MIN_ZOOM to 1),
   * else at that zoom, moved until its `<in>` shows. Measured 2026-09-29: a fit at zoom 1 left a two-step inside's
   * `<out>` off the pane, a plain fit (0.66) drew the cards' text at 6-8 px.
   */
  const openInside = useCallback(
    async (inside: string) => {
      await flow.fitView({ padding: 0.05, minZoom: INSIDE_MIN_ZOOM, maxZoom: 1 });
      const inlet = flow.getInternalNode(`${inside}/<in>`);
      const pane = wrapper.current?.querySelector(".react-flow")?.getBoundingClientRect();
      if (!inlet || !pane) return;
      const at = flow.flowToScreenPosition(inlet.internals.positionAbsolute);
      const width = inlet.measured.width ?? 0;
      const height = inlet.measured.height ?? 0;
      const shift = (start: number, size: number, low: number, high: number): number =>
        start < low + INLET_MARGIN ? low + INLET_MARGIN - start : start + size > high - INLET_MARGIN ? high - INLET_MARGIN - start - size : 0;
      const dx = shift(at.x, width, pane.left, pane.right);
      const dy = shift(at.y, height, pane.top, pane.bottom);
      if (dx === 0 && dy === 0) return;
      const { x, y, zoom } = flow.getViewport();
      await flow.setViewport({ x: x + dx, y: y + dy, zoom });
    },
    [flow],
  );

  useEffect(() => {
    let live = true;
    setViewLevel(null);
    const saved = viewports.current[viewKey];
    const set = saved
      ? flow.setViewport(saved)
      : level !== ""
        ? openInside(level)
        : fitOnOpen
          ? flow.fitView({ padding: 0.05, maxZoom: 1 })
          : Promise.resolve(true);
    void set.then(() => {
      if (live) setViewLevel(level);
    });
    return () => {
      live = false;
      viewports.current[viewKey] = flow.getViewport();
    };
  }, [fitOnOpen, flow, level, openInside, viewKey]);

  useOnSelectionChange<CanvasNode, CanvasEdge>({
    onChange: useCallback(
      ({ nodes: picked, edges: wires }: { nodes: CanvasNode[]; edges: CanvasEdge[] }) => {
        const ids = new Set(picked.filter((node) => node.type === "generic").map((node) => node.id));
        const groups = picked.filter((node) => node.type === "group").map((node) => (node.type === "group" ? node.data.group.id : ""));
        // the level's steps, in the model's order — a subgraph's <in>/<out> is drawn, never acted on
        onSelection(
          view.nodes.filter((node) => !isBoundary(node) && ids.has(node.name)).map((node) => node.name),
          groups,
          wires.length,
        );
      },
      [view.nodes, onSelection],
    ),
  });

  const typeOf = useCallback((nodeId: string, handle: string | null | undefined) => {
    const node = nodesRef.current.find((candidate) => candidate.id === nodeId);
    return node && node.type === "generic" ? socketType(node.data.node, handle) : undefined;
  }, []);

  const isValidConnection = useCallback(
    (connection: Connected | CanvasEdge) => {
      if (connection.source === connection.target) return false;
      const source = typeOf(connection.source, connection.sourceHandle);
      const target = typeOf(connection.target, connection.targetHandle);
      return source !== undefined && target !== undefined && compatible(source, target, types);
    },
    [typeOf, types],
  );

  // the server saves it, or refuses it in one line; either way the canvas draws only what the document holds
  const onConnect = useCallback((connection: Connected) => onWire(connection), [onWire]);

  // A wire released over NOTHING (no handle within reach: `isValid` is null) opens the palette; a release on a
  // handle whose type does not meet is simply refused, as it was while dragging.
  const onConnectEnd: OnConnectEnd = useCallback(
    (event, state) => {
      if (state.isValid !== null || state.toHandle || !state.fromNode || !state.fromHandle) return;
      const handle = parseHandle(state.fromHandle.id);
      const type = typeOf(state.fromNode.id, state.fromHandle.id);
      if (!handle || type === undefined) return;
      const point = "changedTouches" in event ? event.changedTouches[0] : event;
      onPalette({
        screen: { x: point.clientX, y: point.clientY },
        openedAt: performance.now(),
        from: { node: state.fromNode.id, kind: handle.kind, type, socket: handle.socket },
      });
    },
    [onPalette, typeOf],
  );

  const onNodesChange = useCallback((changes: NodeChange<CanvasNode>[]) => {
    setNodes((current) => {
      const next = applyNodeChanges(changes, current);
      // a box being resized by hand follows the hand, not its members (the resize's end writes its bounds)
      const resizing = changes.some((change) => change.type === "dimensions" && change.resizing !== undefined);
      return !resizing && changes.some((change) => change.type === "dimensions") ? refitGroups(next) : next;
    });
  }, []);
  const onEdgesChange = useCallback(
    (changes: EdgeChange<CanvasEdge>[]) => setEdges((current) => applyEdgeChanges(changes, current)),
    [],
  );
  const onNodeDragStop: OnNodeDrag<CanvasNode> = useCallback(
    (_event, _node, dragged) => {
      // nodeDragThreshold is 0 (the drag starts on mousedown, so the first pixel of movement is not lost), which
      // means a plain click is also a zero-length drag: only a node whose absolute place changed is written.
      const current = nodesRef.current;
      const overrides = Object.fromEntries(dragged.map((node) => [node.id, node.position]));
      const placed = absolutePositions(current, overrides);
      // compared with where the level DREW them (a node the sidecar does not place yet is drawn at a filled-in
      // place), so a click on such a node is not a move
      const drawnAt = levelPositions(view, graph.positions);
      const moved = Object.entries(placed).filter(([name, { x, y }]) => {
        const [x0, y0] = drawnAt[name] ?? [NaN, NaN];
        return x !== x0 || y !== y0;
      });

      // membership: the boxes as drawn BEFORE the drop (a dragged box is where it was dropped), the nodes dragged on
      // their own (a member dragged along with its box stays in it)
      const draggedIds = new Set(dragged.map((node) => node.id));
      const boxes: Box[] = current
        .filter((node) => node.type === "group")
        .map((node) => ({
          id: node.type === "group" ? node.data.group.id : "",
          rect: { ...(overrides[node.id] ?? node.position), ...sizeOf(node) },
        }));
      const dropped = dragged
        .filter((node) => node.type === "generic" && !(node.parentId && draggedIds.has(node.parentId)) && placed[node.id])
        .map((node) => ({ name: node.id, rect: { ...placed[node.id], ...sizeOf(node) } as Rect }));
      let groups = graph.groups;
      for (const node of dragged) {
        if (node.type !== "group") continue;
        const was = current.find((drawn) => drawn.id === node.id)?.position;
        if (!was || was.x !== node.position.x || was.y !== node.position.y)
          groups = moveGroup(groups, node.data.group.id, node.position); // a sized box stores its corner
      }
      groups = regroupAfterDrop(groups, boxes, dropped, level);

      if (moved.length === 0 && groups === graph.groups) return;
      onLayout({
        positions: Object.fromEntries(moved.map(([name, { x, y }]) => [name, [x, y] as [number, number]])),
        ...(groups === graph.groups ? {} : { groups }),
      });
    },
    [graph.groups, graph.positions, level, onLayout, view],
  );

  const menuAt = (event: ReactMouseEvent | MouseEvent) => ({
    screen: { x: event.clientX, y: event.clientY },
    flow: flow.screenToFlowPosition({ x: event.clientX, y: event.clientY }),
  });

  const onDragOver = (event: DragEvent<HTMLDivElement>) => {
    if (!event.dataTransfer.types.includes(CLASS_MIME)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  };
  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    const classPath = event.dataTransfer.getData(CLASS_MIME);
    if (!classPath) return;
    event.preventDefault();
    onDropClass(classPath, flow.screenToFlowPosition({ x: event.clientX, y: event.clientY }));
  };

  /**
   * Shift + press on a card ADDS it to the selection (`selection.ts`, DESIGN2 §4). React Flow's pane would take the
   * press to start a box (Shift is its box-select key, and its capture listener starts one even over a card), so this
   * listener — outside the pane, so it runs first — acts before it:
   *
   * - a card NOT yet selected: added, and the press stops there (no box, no drag; the click that follows only shows the
   *   card's run entry);
   * - a card already selected: the press goes on to React Flow's node drag, which moves the WHOLE selection (P9 —
   *   measured before: with Shift still held, dragging a selected card did nothing). React Flow's pane starts no box
   *   for a press inside a `nokey` element, so the card is marked `nokey` for this one press.
   *
   * Every other press is left to React Flow.
   */
  const onShiftPress = (event: ReactPointerEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement;
    const card = target.closest<HTMLElement>(".react-flow__node-generic");
    const on = { onCard: card !== null, onHandle: target.closest(".react-flow__handle") !== null, inField: inField(target) };
    if (!shiftAddsCard(event, on) || card === null) return;
    const id = card.dataset.id;
    if (id === undefined) return;
    if (flow.getNode(id)?.selected === true) {
      card.classList.add("nokey");
      const done = () => {
        card.classList.remove("nokey");
        window.removeEventListener("pointerup", done, true);
        window.removeEventListener("pointercancel", done, true);
      };
      window.addEventListener("pointerup", done, true);
      window.addEventListener("pointercancel", done, true);
      return;
    }
    event.stopPropagation();
    event.preventDefault(); // no compatibility mousedown: React Flow's node drag does not start either
    store.setState({ nodesSelectionActive: false }); // as React Flow's own click on a node does
    flow.setNodes((current) => addToSelection(current, id)); // as Select all / selectOnly do: React Flow's own path
  };

  const onPointerDownCapture = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (event.detail !== 1 || !(event.target as HTMLElement).classList.contains("react-flow__pane")) return;
    // read React Flow's own flags, not the listener's copy — that one arrives an effect after a click
    const picked = new Set(flow.getNodes().filter((node) => node.selected && node.type === "generic").map((node) => node.id));
    pressedSelection.current = view.nodes.filter((node) => !isBoundary(node) && picked.has(node.name)).map((node) => node.name);
  };
  const onDoubleClick = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (!(event.target as HTMLElement).classList.contains("react-flow__pane")) return;
    onPalette({ screen: { x: event.clientX, y: event.clientY }, openedAt: performance.now(), selected: pressedSelection.current });
  };

  const crumbs = breadcrumb(graph.name, level);

  return (
    <div
      ref={wrapper}
      className="canvas"
      data-graph={drawnFrom?.name ?? ""}
      data-level={level}
      data-view-level={viewLevel ?? undefined}
      data-initialized={initialized && drawnFrom === graph ? "true" : "false"}
      onPointerDownCapture={onShiftPress}
      onMouseDownCapture={onPointerDownCapture}
      onDoubleClick={onDoubleClick}
      onDragOver={onDragOver}
      onDrop={onDrop}
    >
      <ReactFlow<CanvasNode, CanvasEdge>
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onConnectEnd={onConnectEnd}
        isValidConnection={isValidConnection}
        onNodeDragStop={onNodeDragStop}
        onNodeClick={(_event, node) => node.type === "generic" && onNodeClick(node.id)}
        onNodeDoubleClick={(event, node) => {
          if (node.type !== "generic" || node.data.node.kind !== "subgraph" || inField(event.target)) return;
          onOpenSubgraph?.(node.id);
        }}
        onPaneContextMenu={(event) => {
          event.preventDefault();
          onContextMenu({ kind: "pane", ...menuAt(event) });
        }}
        onNodeContextMenu={(event, node) => {
          event.preventDefault();
          if (node.type === "group") {
            onContextMenu({ kind: "group", group: node.data.group.id, ...menuAt(event) });
            return;
          }
          // a right-click acts on the selection when the node is part of it, else on the node alone
          if (!node.selected)
            flow.setNodes((all) => all.map((other) => (other.selected !== (other.id === node.id) ? { ...other, selected: other.id === node.id } : other)));
          onContextMenu({ kind: "node", node: node.id, ...menuAt(event) });
        }}
        onSelectionContextMenu={(event, picked) => {
          event.preventDefault();
          const first = picked.find((node) => node.type === "generic");
          if (first) onContextMenu({ kind: "node", node: first.id, ...menuAt(event) });
          else onContextMenu({ kind: "pane", ...menuAt(event) });
        }}
        defaultViewport={START}
        // low enough for a host's panel to show a large graph whole (`fitOnOpen`): the Bluetooth pane's 30 cards fit a
        // 580-px canvas at about 0.06, and React Flow clamps a fit to this bound (measured at 0.1: 17 of the 30 cards)
        minZoom={0.02}
        snapToGrid
        snapGrid={[10, 10]}
        nodeDragThreshold={0}
        zoomOnDoubleClick={false}
        deleteKeyCode={null}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={20} />
        <Controls />
        {showMinimap && <MiniMap pannable zoomable bgColor="#181822" nodeColor="#3949ab" maskColor="rgba(0, 0, 0, 0.55)" />}
      </ReactFlow>
      {level !== "" && (
        <nav className="crumbs" data-breadcrumb aria-label="the subgraph shown">
          {crumbs.map((crumb, index) => (
            <span key={crumb.level} className="crumb-wrap">
              {index > 0 && <span className="crumb-sep">›</span>}
              <button
                type="button"
                className={`crumb${crumb.level === level ? " here" : ""}`}
                data-crumb={crumb.level}
                aria-current={crumb.level === level ? "location" : undefined}
                title={crumb.level === "" ? `the top of ${graph.name}` : crumb.level}
                onClick={() => onLevel?.(crumb.level)}
              >
                {crumb.label}
              </button>
            </span>
          ))}
          <span className="crumb-hint">Esc goes up</span>
        </nav>
      )}
      <footer className="status" data-status>
        <span data-status-graph>{graph.name}</span>
        <span className="dim">{graph.kind}</span>
        <span className="dim path" title={graph.path}>
          {graph.path}
        </span>
        <span className="dim">
          {level !== "" ? `${level} · ` : ""}
          {view.nodes.length} nodes · {view.wires.length} wires
        </span>
        {view.problems.length > 0 ? (
          <ul className="problems">
            {view.problems.map((problem, index) => (
              <li key={index} data-problem title={problem.loc}>
                {problem.message}
              </li>
            ))}
          </ul>
        ) : (
          <span className="ok">no problems</span>
        )}
      </footer>
    </div>
  );
}
