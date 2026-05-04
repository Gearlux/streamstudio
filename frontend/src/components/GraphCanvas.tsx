import { useCallback, useMemo, useState } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  type Edge,
  type Node,
  type NodeChange,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { GraphNode, GraphSpec } from "../types";
import NodeRenderer, { type FluxNodeData } from "./NodeRenderer";
import { PALETTE_DRAG_MIME, type PaletteDragPayload } from "./NodePalette";

interface Props {
  graph: GraphSpec | null;
  nodePositions: Record<string, { x: number; y: number }>;
  activeNodeId: string | null;
  selectedNodeId: string | null;
  paramSummaryFor: (node: GraphNode) => string;
  onDropOp: (
    payload: PaletteDragPayload,
    position: { x: number; y: number },
    hoveredEdgeId: string | null,
  ) => void;
  onPositionChange: (id: string, position: { x: number; y: number }) => void;
  onSelectNode: (id: string | null) => void;
  onConnect: (source: string, target: string) => void;
  onNodesDelete: (ids: string[]) => void;
  onEdgesDelete: (edgeIds: string[]) => void;
}

const nodeTypes = { flux: NodeRenderer };

// Stable edge id: deletion-safe and trivially mappable back to a GraphEdge.
export const edgeId = (source: string, target: string): string => `${source}--${target}`;

export default function GraphCanvas(props: Props) {
  return (
    <ReactFlowProvider>
      <GraphCanvasInner {...props} />
    </ReactFlowProvider>
  );
}

function GraphCanvasInner({
  graph,
  nodePositions,
  activeNodeId,
  selectedNodeId,
  paramSummaryFor,
  onDropOp,
  onPositionChange,
  onSelectNode,
  onConnect,
  onNodesDelete,
  onEdgesDelete,
}: Props) {
  const { screenToFlowPosition } = useReactFlow();
  const [hoveredEdgeId, setHoveredEdgeId] = useState<string | null>(null);

  const nodes = useMemo<Node<FluxNodeData>[]>(() => {
    if (!graph) return [];
    return graph.nodes.map((n, i) => {
      const pos = nodePositions[n.id] ?? { x: 60, y: 60 + i * 120 };
      const data: FluxNodeData = {
        label: n.op_path,
        paramSummary: paramSummaryFor(n),
        active: activeNodeId === n.id,
      };
      return {
        id: n.id,
        type: "flux",
        position: pos,
        selected: selectedNodeId === n.id,
        data,
      };
    });
  }, [graph, nodePositions, activeNodeId, selectedNodeId, paramSummaryFor]);

  const edges = useMemo<Edge[]>(() => {
    if (!graph) return [];
    return graph.edges.map((e) => {
      const id = edgeId(e.source, e.target);
      const isHover = hoveredEdgeId === id;
      return {
        id,
        source: e.source,
        target: e.target,
        style: {
          stroke: isHover ? "var(--running)" : "var(--edge-stroke)",
          strokeWidth: isHover ? 3 : 1.5,
        },
      };
    });
  }, [graph, hoveredEdgeId]);

  const handleNodesChange = useCallback(
    (changes: NodeChange[]) => {
      for (const c of changes) {
        if (c.type === "position" && c.position && !c.dragging) {
          onPositionChange(c.id, c.position);
        }
      }
    },
    [onPositionChange],
  );

  return (
    <div
      style={{ width: "100%", height: "100%" }}
      onDragOver={(e) => {
        if (!e.dataTransfer.types.includes(PALETTE_DRAG_MIME)) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
      }}
      onDrop={(e) => {
        const raw = e.dataTransfer.getData(PALETTE_DRAG_MIME);
        if (!raw) return;
        e.preventDefault();
        const payload = JSON.parse(raw) as PaletteDragPayload;
        const position = screenToFlowPosition({ x: e.clientX, y: e.clientY });
        onDropOp(payload, position, hoveredEdgeId);
        setHoveredEdgeId(null);
      }}
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={handleNodesChange}
        onEdgeMouseEnter={(_, edge) => setHoveredEdgeId(edge.id)}
        onEdgeMouseLeave={() => setHoveredEdgeId(null)}
        onConnect={(c) => {
          if (c.source && c.target) onConnect(c.source, c.target);
        }}
        onNodesDelete={(ns) => onNodesDelete(ns.map((n) => n.id))}
        onEdgesDelete={(es) => onEdgesDelete(es.map((e) => e.id))}
        onSelectionChange={(sel) => onSelectNode(sel.nodes[0]?.id ?? null)}
        fitView={!!graph && graph.nodes.length > 0}
      >
        <Background variant={BackgroundVariant.Dots} gap={16} />
        <Controls />
      </ReactFlow>
      {(!graph || graph.nodes.length === 0) && (
        <div
          style={{
            position: "absolute",
            top: "50%",
            left: "50%",
            transform: "translate(-50%, -50%)",
            color: "var(--text-placeholder)",
            fontSize: 13,
            pointerEvents: "none",
          }}
        >
          Drag ops from the palette to start.
        </div>
      )}
    </div>
  );
}
