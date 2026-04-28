import { useMemo } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  Edge,
  Node,
  ReactFlow,
} from "@xyflow/react";
import type { GraphSpec } from "../types";

interface Props {
  graph: GraphSpec | null;
  activeNodeId: string | null;
}

export default function GraphCanvas({ graph, activeNodeId }: Props) {
  const { nodes, edges } = useMemo(() => {
    if (!graph) return { nodes: [], edges: [] };
    const nodes: Node[] = graph.nodes.map((n, i) => ({
      id: n.id,
      data: { label: `${n.op_path}` },
      position: { x: 60, y: 60 + i * 120 },
      style: {
        border: activeNodeId === n.id ? "2px solid #ff6a00" : "1px solid #888",
        background: activeNodeId === n.id ? "#fff3e6" : "#fff",
        padding: 12,
        borderRadius: 6,
        width: 280,
        fontSize: 12,
      },
    }));
    const edges: Edge[] = graph.edges.map((e, i) => ({
      id: `e${i}`,
      source: e.source,
      target: e.target,
    }));
    return { nodes, edges };
  }, [graph, activeNodeId]);

  return (
    <ReactFlow nodes={nodes} edges={edges} fitView>
      <Background variant={BackgroundVariant.Dots} gap={16} />
      <Controls />
    </ReactFlow>
  );
}
