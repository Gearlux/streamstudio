import { useEffect, useState } from "react";
import type { GraphNode, OpSchema, StepEvent } from "../types";
import OpInfoPanel from "./OpInfoPanel";
import ParamsPanel from "./ParamsPanel";
import VisualizerPane from "./VisualizerPane";

type Tab = "params" | "info" | "viz";

interface Props {
  selectedNode: GraphNode | null;
  selectedSchema: OpSchema | null;
  onParamChange: (paramName: string, value: unknown) => void;
  latestEvent: StepEvent | null;
}

export default function RightPane({ selectedNode, selectedSchema, onParamChange, latestEvent }: Props) {
  const [tab, setTab] = useState<Tab>("params");
  const [manualOverride, setManualOverride] = useState(false);

  // Auto-switch tabs based on context unless the user has clicked one
  // explicitly this render-cycle (manualOverride). Selecting a node →
  // Params; receiving a run event → Visualizer.
  useEffect(() => {
    if (manualOverride) return;
    if (selectedNode) setTab("params");
  }, [selectedNode, manualOverride]);
  useEffect(() => {
    if (manualOverride) return;
    if (latestEvent) setTab("viz");
  }, [latestEvent, manualOverride]);

  const select = (next: Tab) => {
    setTab(next);
    setManualOverride(true);
  };

  return (
    <div>
      <div style={{ display: "flex", borderBottom: "1px solid var(--border)", marginBottom: 10 }}>
        <TabButton active={tab === "params"} onClick={() => select("params")}>
          Params
        </TabButton>
        <TabButton active={tab === "info"} onClick={() => select("info")} title="Op metadata: docstring, module, parameter table.">
          Info
        </TabButton>
        <TabButton active={tab === "viz"} onClick={() => select("viz")}>
          Visualizer
        </TabButton>
      </div>
      {tab === "params" && <ParamsPanel node={selectedNode} schema={selectedSchema} onChange={onParamChange} />}
      {tab === "info" && <OpInfoPanel node={selectedNode} schema={selectedSchema} />}
      {tab === "viz" && <VisualizerPane event={latestEvent} />}
    </div>
  );
}

function TabButton({
  active,
  onClick,
  title,
  children,
}: {
  active: boolean;
  onClick: () => void;
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      style={{
        background: "none",
        border: "none",
        borderBottom: active ? "2px solid var(--accent)" : "2px solid transparent",
        color: active ? "var(--accent)" : "var(--text-secondary)",
        fontWeight: active ? 600 : 400,
        padding: "6px 12px",
        cursor: "pointer",
        fontSize: 12,
      }}
    >
      {children}
    </button>
  );
}
