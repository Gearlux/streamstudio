import { useEffect, useState } from "react";
import GraphCanvas from "./components/GraphCanvas";
import NodePalette from "./components/NodePalette";
import StepControls from "./components/StepControls";
import VisualizerPane from "./components/VisualizerPane";
import * as api from "./api";
import type { GraphSpec, OpSchema, StepEvent } from "./types";

const STYLE: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "240px 1fr 360px",
  gridTemplateRows: "1fr 100px",
  gridTemplateAreas: `"palette canvas viz" "palette controls viz"`,
  height: "100vh",
  fontFamily: "-apple-system, system-ui, sans-serif",
};

const TINY_PIPELINE_YAML = `!class:dataflux.core.Flux()
source: !class:fluxstudio._fixtures.NumericArraySource()
  count: 4
  size: 8
ops:
  - !class:dataflux.ops.numpy.RescaleOp()
    in_min: 0.0
    in_max: 255.0
    out_min: 0.0
    out_max: 1.0
  - !class:dataflux.ops.numpy.StandardizeOp()
    mean: 0.5
    std: 0.5
`;

export default function App() {
  const [ops, setOps] = useState<OpSchema[]>([]);
  const [graph, setGraph] = useState<GraphSpec | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [activeNodeId, setActiveNodeId] = useState<string | null>(null);
  const [latestEvent, setLatestEvent] = useState<StepEvent | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.listOps().then(setOps).catch((e) => setError(String(e)));
  }, []);

  async function loadTinyPipeline() {
    try {
      const g = await api.graphFromYaml(TINY_PIPELINE_YAML);
      setGraph(g);
      setActiveNodeId(null);
      setLatestEvent(null);
    } catch (e) {
      setError(String(e));
    }
  }

  async function startRun(mode: "step" | "continue") {
    if (!graph) return;
    try {
      const yaml = await api.graphToYaml(graph);
      const id = await api.startRun(yaml, mode);
      setRunId(id);
      setLatestEvent(null);
      setActiveNodeId(graph.nodes[0]?.id ?? null);
      api.openRunStream(
        id,
        (event) => {
          setLatestEvent(event);
          // Map sample_index/op_index → node id (n{op_index+1}, since n0 is the source).
          setActiveNodeId(`n${event.op_index + 1}`);
        },
        () => {
          /* stream ended */
        },
      );
    } catch (e) {
      setError(String(e));
    }
  }

  return (
    <div style={STYLE}>
      <div style={{ gridArea: "palette", borderRight: "1px solid #ddd", overflow: "auto" }}>
        <NodePalette ops={ops} />
      </div>
      <div style={{ gridArea: "canvas", position: "relative" }}>
        <GraphCanvas graph={graph} activeNodeId={activeNodeId} />
        {!graph && (
          <button
            style={{ position: "absolute", top: 12, left: 12, padding: "6px 12px" }}
            onClick={loadTinyPipeline}
          >
            Load tiny_pipeline.yaml
          </button>
        )}
      </div>
      <div style={{ gridArea: "viz", borderLeft: "1px solid #ddd", overflow: "auto", padding: 12 }}>
        <VisualizerPane event={latestEvent} />
      </div>
      <div style={{ gridArea: "controls", borderTop: "1px solid #ddd", padding: 12 }}>
        <StepControls
          runId={runId}
          canStart={!!graph}
          onStart={startRun}
          onCommand={(cmd) => runId && api.controlRun(runId, cmd)}
        />
        {error && <div style={{ color: "crimson", fontSize: 12, marginTop: 8 }}>{error}</div>}
      </div>
    </div>
  );
}
