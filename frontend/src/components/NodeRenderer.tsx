import { Handle, Position, type NodeProps } from "@xyflow/react";

export interface FluxNodeData extends Record<string, unknown> {
  label: string;
  paramSummary: string;
  active: boolean;
}

export default function NodeRenderer({ data, selected }: NodeProps) {
  const d = data as FluxNodeData;
  const border = d.active
    ? "2px solid var(--running)"
    : selected
      ? "2px solid var(--accent)"
      : "1px solid var(--border-strong)";
  const bg = d.active ? "var(--running-bg)" : selected ? "var(--accent-bg)" : "var(--bg-canvas)";
  return (
    <div
      style={{
        border,
        background: bg,
        color: "var(--text-primary)",
        padding: 12,
        borderRadius: 6,
        width: 280,
        fontSize: 12,
      }}
    >
      <Handle type="target" position={Position.Top} />
      <div style={{ fontWeight: 600, marginBottom: 4 }}>{d.label}</div>
      {d.paramSummary && <div style={{ color: "var(--text-muted)", fontSize: 11 }}>{d.paramSummary}</div>}
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}
