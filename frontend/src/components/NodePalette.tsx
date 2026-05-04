import { useMemo, useState } from "react";
import type { OpSchema } from "../types";

const CATEGORY_ORDER = ["DataSource", "DataOp", "DataSink", "Visualizer"] as const;

interface Props {
  ops: OpSchema[];
  showInternal: boolean;
  onToggleShowInternal: (value: boolean) => void;
}

export const PALETTE_DRAG_MIME = "application/fluxstudio-op";

export interface PaletteDragPayload {
  op_path: string;
  category: OpSchema["category"];
}

interface ModuleGroup {
  module: string;
  label: string;
  ops: OpSchema[];
}

interface CategoryGroup {
  category: OpSchema["category"];
  modules: ModuleGroup[];
  total: number;
}

// Build the two-level hierarchy: category → module → ops.
// Module label = last dotted component (`dataflux.ops.numpy` → `numpy`)
// unless that label collides with another module in the same category, in
// which case we fall back to the fully-qualified module string.
function bucketize(ops: OpSchema[]): CategoryGroup[] {
  const byCategory = new Map<OpSchema["category"], Map<string, OpSchema[]>>();
  for (const op of ops) {
    let perCat = byCategory.get(op.category);
    if (!perCat) {
      perCat = new Map();
      byCategory.set(op.category, perCat);
    }
    let perMod = perCat.get(op.module);
    if (!perMod) {
      perMod = [];
      perCat.set(op.module, perMod);
    }
    perMod.push(op);
  }

  const out: CategoryGroup[] = [];
  for (const cat of CATEGORY_ORDER) {
    const perCat = byCategory.get(cat);
    if (!perCat) continue;

    // Detect last-component collisions so we know when to fall back to FQN labels.
    const labelCounts = new Map<string, number>();
    for (const m of perCat.keys()) {
      const last = m.split(".").pop() ?? m;
      labelCounts.set(last, (labelCounts.get(last) ?? 0) + 1);
    }

    const modules: ModuleGroup[] = [];
    for (const [mod, list] of perCat.entries()) {
      const last = mod.split(".").pop() ?? mod;
      const label = (labelCounts.get(last) ?? 0) > 1 ? mod : last;
      modules.push({
        module: mod,
        label,
        ops: [...list].sort((a, b) => a.name.localeCompare(b.name)),
      });
    }
    modules.sort((a, b) => a.label.localeCompare(b.label));
    out.push({ category: cat, modules, total: modules.reduce((s, m) => s + m.ops.length, 0) });
  }
  return out;
}

export default function NodePalette({ ops, showInternal, onToggleShowInternal }: Props) {
  const filtered = useMemo(
    () => (showInternal ? ops : ops.filter((op) => !op.name.startsWith("_"))),
    [ops, showInternal],
  );
  const groups = useMemo(() => bucketize(filtered), [filtered]);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  const toggle = (key: string) => setCollapsed((s) => ({ ...s, [key]: !s[key] }));

  return (
    <div style={{ padding: 8, fontSize: 12 }}>
      <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 6 }}>Op palette</div>
      <label style={{ display: "block", marginBottom: 10, color: "var(--text-secondary)" }}>
        <input
          type="checkbox"
          checked={showInternal}
          onChange={(e) => onToggleShowInternal(e.target.checked)}
          style={{ marginRight: 6 }}
        />
        Show internal ops
      </label>

      {groups.map((cat) => (
        <div key={cat.category} style={{ marginBottom: 12 }}>
          <div
            style={{
              fontSize: 11,
              color: "var(--text-muted)",
              marginBottom: 4,
              textTransform: "uppercase",
              letterSpacing: 0.4,
            }}
          >
            {cat.category} ({cat.total})
          </div>
          {cat.modules.map((mod) => {
            const key = `${cat.category}/${mod.module}`;
            const isCollapsed = collapsed[key];
            return (
              <div key={key} style={{ marginBottom: 6 }}>
                <button
                  onClick={() => toggle(key)}
                  title={mod.module}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    width: "100%",
                    background: "none",
                    border: "none",
                    cursor: "pointer",
                    padding: "2px 0",
                    color: "var(--text-secondary)",
                    fontSize: 11,
                    fontWeight: 600,
                    textAlign: "left",
                  }}
                >
                  <span style={{ width: 10, color: "var(--text-faint)" }}>{isCollapsed ? "▸" : "▾"}</span>
                  <span style={{ flex: 1 }}>{mod.label}</span>
                  <span style={{ color: "var(--text-faint)" }}>{mod.ops.length}</span>
                </button>
                {!isCollapsed && (
                  <ul style={{ listStyle: "none", margin: "2px 0 0", padding: "0 0 0 12px" }}>
                    {mod.ops.map((op) => (
                      <li
                        key={op.path}
                        title={op.doc || op.path}
                        draggable
                        onDragStart={(e) => {
                          const payload: PaletteDragPayload = { op_path: op.path, category: op.category };
                          e.dataTransfer.setData(PALETTE_DRAG_MIME, JSON.stringify(payload));
                          e.dataTransfer.effectAllowed = "move";
                        }}
                        style={{
                          padding: "4px 6px",
                          margin: "2px 0",
                          border: "1px solid var(--border)",
                          borderRadius: 4,
                          cursor: "grab",
                          background: "var(--bg-canvas)",
                          color: "var(--text-primary)",
                        }}
                      >
                        {op.name}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            );
          })}
        </div>
      ))}

      {!ops.length && <div style={{ color: "var(--text-faint)" }}>Loading…</div>}
    </div>
  );
}
