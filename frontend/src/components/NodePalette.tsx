import { useMemo } from "react";
import type { OpSchema } from "../types";

const CATEGORY_ORDER = ["DataSource", "DataOp", "DataSink", "Visualizer"] as const;

interface Props {
  ops: OpSchema[];
}

export default function NodePalette({ ops }: Props) {
  const grouped = useMemo(() => {
    const out: Record<string, OpSchema[]> = {};
    for (const op of ops) {
      (out[op.category] ??= []).push(op);
    }
    return out;
  }, [ops]);

  return (
    <div style={{ padding: 8, fontSize: 12 }}>
      <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 6 }}>Op palette</div>
      {CATEGORY_ORDER.map((cat) => {
        const items = grouped[cat] ?? [];
        if (!items.length) return null;
        return (
          <div key={cat} style={{ marginBottom: 12 }}>
            <div style={{ fontSize: 11, color: "#666", marginBottom: 4, textTransform: "uppercase" }}>
              {cat} ({items.length})
            </div>
            <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
              {items.map((op) => (
                <li
                  key={op.path}
                  title={op.doc}
                  style={{
                    padding: "4px 6px",
                    margin: "2px 0",
                    border: "1px solid #ddd",
                    borderRadius: 4,
                    cursor: "default",
                  }}
                >
                  {op.name}
                </li>
              ))}
            </ul>
          </div>
        );
      })}
      {!ops.length && <div style={{ color: "#999" }}>Loading…</div>}
    </div>
  );
}
