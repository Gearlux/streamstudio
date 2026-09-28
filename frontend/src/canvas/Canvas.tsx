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
 *   table) WHILE dragging, so a mismatch never becomes an edge. A drop on a handle whose type meets becomes a dashed
 *   PROPOSAL edge and a warning naming the spelling the document would need (no route writes a wire yet). A drop on
 *   EMPTY canvas opens the palette narrowed to the classes that can take (or give) the dragged type.
 * - **dragging a node or a group** — on release, every document node whose ABSOLUTE position changed (a dragged
 *   group moves its members) goes to `PUT …/positions`.
 * - **double-clicking empty canvas** — the palette. React Flow 12 has no pane double-click, so the wrapper listens;
 *   zoom-on-double-click is off. The first click of a double-click deselects (React Flow's pane click), so the
 *   selection is remembered at that first press: "after the selected node" means the node selected BEFORE it.
 * - **clicking a node** — the app shows its run entry.
 *
 * Zoom, pan, minimap, snap to a 10-px grid and box select (Shift + drag) are React Flow's own (`START` says where
 * the viewport begins and why).
 */
import { useCallback, useRef, useState, type MouseEvent as ReactMouseEvent } from "react";
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  useNodesInitialized,
  useOnSelectionChange,
  useReactFlow,
  type Connection,
  type EdgeChange,
  type NodeChange,
  type NodeTypes,
  type OnConnectEnd,
  type OnNodeDrag,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { GROUP_HEAD, GROUP_PAD, absolutePositions, refitGroups, withGroups, type Size } from "../groups";
import {
  compatible,
  modelToFlow,
  parseHandle,
  socketType,
  type CanvasEdge,
  type CanvasNode,
  type Graph,
  type HandleKind,
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
  /** The handle a dropped wire was dragged from. */
  from?: { node: string; kind: HandleKind; type: SocketType };
}

export interface CanvasProps {
  graph: Graph;
  types: Supertypes;
  /** Nodes whose drawn position changed — the app PUTs them and refreshes the graph. */
  onPositions: (positions: Positions) => void;
  /** A wire was drawn that the document cannot express yet; `message` says why and what it would be spelled as. */
  onProposal: (message: string) => void;
  /** The selected DOCUMENT nodes (a group box is not one), in the model's order — for what the toolbar shows. */
  onSelection: (names: string[]) => void;
  onPalette: (gesture: PaletteGesture) => void;
  onNodeClick: (name: string) => void;
}

/** Keep what React Flow learnt about a node (selected, measured) across a redraw from a new graph. */
function redraw(graph: Graph, previous: CanvasNode[]): { nodes: CanvasNode[]; edges: CanvasEdge[] } {
  const known = new Map(previous.map((node) => [node.id, node]));
  const sizes: Record<string, Size> = {};
  for (const node of previous)
    if (node.measured?.width !== undefined && node.measured.height !== undefined)
      sizes[node.id] = { width: node.measured.width, height: node.measured.height };
  const flow = modelToFlow(graph.model, graph.positions);
  const nodes = withGroups(flow.nodes, graph.groups, sizes).map((node) => {
    const before = known.get(node.id);
    return before ? { ...node, selected: before.selected, measured: before.measured } : node;
  });
  return { nodes, edges: flow.edges };
}

export function Canvas({ graph, types, onPositions, onProposal, onSelection, onPalette, onNodeClick }: CanvasProps) {
  const [drawnFrom, setDrawnFrom] = useState<Graph | null>(null);
  const [nodes, setNodes] = useState<CanvasNode[]>([]);
  const [edges, setEdges] = useState<CanvasEdge[]>([]);
  if (drawnFrom !== graph) {
    // derived state: React re-renders this component at once, before committing — never a frame behind the graph
    const flow = redraw(graph, drawnFrom !== null && drawnFrom.name === graph.name ? nodes : []);
    setDrawnFrom(graph);
    setNodes(flow.nodes);
    setEdges(flow.edges);
  }
  const nodesRef = useRef(nodes);
  nodesRef.current = nodes;
  const flow = useReactFlow<CanvasNode, CanvasEdge>();
  const pressedSelection = useRef<string[]>([]);
  const proposals = useRef(0);
  const initialized = useNodesInitialized();

  useOnSelectionChange<CanvasNode, CanvasEdge>({
    onChange: useCallback(
      ({ nodes: picked }: { nodes: CanvasNode[] }) => {
        const ids = new Set(picked.filter((node) => node.type === "generic").map((node) => node.id));
        onSelection(graph.model.nodes.map((node) => node.name).filter((name) => ids.has(name)));
      },
      [graph.model.nodes, onSelection],
    ),
  });

  const typeOf = useCallback((nodeId: string, handle: string | null | undefined) => {
    const node = nodesRef.current.find((candidate) => candidate.id === nodeId);
    return node && node.type === "generic" ? socketType(node.data.node, handle) : undefined;
  }, []);

  const isValidConnection = useCallback(
    (connection: Connection | CanvasEdge) => {
      if (connection.source === connection.target) return false;
      const source = typeOf(connection.source, connection.sourceHandle);
      const target = typeOf(connection.target, connection.targetHandle);
      return source !== undefined && target !== undefined && compatible(source, target, types);
    },
    [typeOf, types],
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      const type = typeOf(connection.source, connection.sourceHandle) ?? "*";
      const edge: CanvasEdge = {
        ...connection,
        id: `proposal${++proposals.current}`,
        className: "wire wire-proposal",
        animated: true,
        data: { kind: "proposal", type },
      };
      setEdges((current) => addEdge(edge, current));
      onProposal(proposalMessage(graph, connection));
    },
    [graph, onProposal, typeOf],
  );

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
        from: { node: state.fromNode.id, kind: handle.kind, type },
      });
    },
    [onPalette, typeOf],
  );

  const onNodesChange = useCallback((changes: NodeChange<CanvasNode>[]) => {
    setNodes((current) => {
      const next = applyNodeChanges(changes, current);
      return changes.some((change) => change.type === "dimensions") ? refitGroups(next) : next;
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
      const overrides = Object.fromEntries(dragged.map((node) => [node.id, node.position]));
      const placed = absolutePositions(nodesRef.current, overrides);
      const moved = Object.entries(placed).filter(([name, { x, y }]) => {
        const [x0, y0] = graph.positions[name] ?? [NaN, NaN];
        return x !== x0 || y !== y0;
      });
      if (moved.length === 0) return;
      onPositions(Object.fromEntries(moved.map(([name, { x, y }]) => [name, [x, y] as [number, number]])));
    },
    [graph.positions, onPositions],
  );

  const onPointerDownCapture = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (event.detail !== 1 || !(event.target as HTMLElement).classList.contains("react-flow__pane")) return;
    // read React Flow's own flags, not the listener's copy — that one arrives an effect after a click
    const picked = new Set(flow.getNodes().filter((node) => node.selected && node.type === "generic").map((node) => node.id));
    pressedSelection.current = graph.model.nodes.map((node) => node.name).filter((name) => picked.has(name));
  };
  const onDoubleClick = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (!(event.target as HTMLElement).classList.contains("react-flow__pane")) return;
    onPalette({ screen: { x: event.clientX, y: event.clientY }, openedAt: performance.now(), selected: pressedSelection.current });
  };

  return (
    <div
      className="canvas"
      data-graph={drawnFrom?.name ?? ""}
      data-initialized={initialized && drawnFrom === graph ? "true" : "false"}
      onMouseDownCapture={onPointerDownCapture}
      onDoubleClick={onDoubleClick}
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
        defaultViewport={START}
        minZoom={0.1}
        snapToGrid
        snapGrid={[10, 10]}
        nodeDragThreshold={0}
        zoomOnDoubleClick={false}
        deleteKeyCode={null}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={20} />
        <Controls />
        <MiniMap pannable zoomable bgColor="#181822" nodeColor="#3949ab" maskColor="rgba(0, 0, 0, 0.55)" />
      </ReactFlow>
      <footer className="status" data-status>
        <span data-status-graph>{graph.name}</span>
        <span className="dim">{graph.kind}</span>
        <span className="dim path" title={graph.path}>
          {graph.path}
        </span>
        <span className="dim">
          {graph.model.nodes.length} nodes · {graph.model.wires.length} wires
        </span>
        {graph.model.problems.length > 0 ? (
          <ul className="problems">
            {graph.model.problems.map((problem, index) => (
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

/** Why a drawn wire is not written, in the document's own spelling — per document form (docs/rewrite.md §5). */
export function proposalMessage(graph: Graph, connection: Connection): string {
  const file = graph.path.split("/").pop() ?? graph.path;
  const from = parseHandle(connection.sourceHandle)?.socket ?? "?";
  const to = parseHandle(connection.targetHandle)?.socket ?? "?";
  const why =
    graph.kind === "flow"
      ? `on ${connection.target} it would be spelled 'from: ${connection.source}' (a record wire) or ` +
        `'bind: {${to}: ${connection.source}.${from}}' (a value), and no route writes those yet`
      : graph.kind === "graph"
        ? "in a Stream the record wires ARE the ops order, and the editor cannot reorder ops yet"
        : `a slot is wired by '${to}: !ref:${connection.source}', which the editor cannot write yet`;
  return (
    `${file}: the wire ${connection.source}.${from} → ${connection.target}.${to} is drawn as a proposal, ` +
    `not written — ${why}. The editor writes parameters, positions, names, groups and steps.`
  );
}
