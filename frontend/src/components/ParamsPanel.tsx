import { useState } from "react";
import type { GraphNode, OpSchema } from "../types";

interface Props {
  node: GraphNode | null;
  schema: OpSchema | null;
  onChange: (paramName: string, value: unknown) => void;
}

// Maps the stringified Python annotation we get from the backend
// (`<class 'float'>`, `<class 'int'>`, etc) to a UI widget.
function widgetKindFor(typeStr: string): "number" | "int" | "bool" | "string" | "json" {
  const t = typeStr.toLowerCase();
  if (t.includes("'float'") || t.includes("'numbers.real'")) return "number";
  if (t.includes("'int'")) return "int";
  if (t.includes("'bool'")) return "bool";
  if (t.includes("'str'")) return "string";
  return "json";
}

export default function ParamsPanel({ node, schema, onChange }: Props) {
  if (!node) {
    return (
      <div style={{ color: "var(--text-faint)", fontSize: 13 }}>
        Select a node on the canvas to edit its parameters.
      </div>
    );
  }
  return (
    <div style={{ fontSize: 12 }}>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>{node.op_path}</div>
      <div style={{ color: "var(--text-muted)", fontSize: 11, marginBottom: 10 }}>id: {node.id}</div>
      {!schema && (
        <div style={{ color: "var(--danger-text)", fontSize: 11, marginBottom: 8 }}>
          No schema discovered for this op (palette out of sync?). You can still edit existing params below.
        </div>
      )}
      {(schema?.parameters ?? Object.keys(node.params).map((k) => ({ name: k, type: "", default: null, required: false }))).map(
        (p) => (
          <ParamRow
            key={p.name}
            name={p.name}
            kind={widgetKindFor(p.type)}
            value={node.params[p.name]}
            onChange={(v) => onChange(p.name, v)}
          />
        ),
      )}
    </div>
  );
}

interface RowProps {
  name: string;
  kind: ReturnType<typeof widgetKindFor>;
  value: unknown;
  onChange: (v: unknown) => void;
}

function ParamRow({ name, kind, value, onChange }: RowProps) {
  const [textBuffer, setTextBuffer] = useState<string>(() =>
    kind === "json" && value !== undefined ? JSON.stringify(value) : "",
  );
  const [jsonError, setJsonError] = useState<string | null>(null);

  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ marginBottom: 2, color: "var(--text-secondary)" }}>{name}</div>
      {kind === "bool" ? (
        <input
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => onChange(e.target.checked)}
        />
      ) : kind === "number" || kind === "int" ? (
        <input
          type="number"
          step={kind === "int" ? 1 : "any"}
          value={value === undefined || value === null ? "" : String(value)}
          onChange={(e) => {
            const raw = e.target.value;
            if (raw === "") {
              onChange(undefined);
              return;
            }
            const parsed = kind === "int" ? parseInt(raw, 10) : parseFloat(raw);
            if (!Number.isNaN(parsed)) onChange(parsed);
          }}
          style={{ width: "100%", padding: 4, fontSize: 12 }}
        />
      ) : kind === "string" ? (
        <input
          type="text"
          value={value === undefined || value === null ? "" : String(value)}
          onChange={(e) => onChange(e.target.value)}
          style={{ width: "100%", padding: 4, fontSize: 12 }}
        />
      ) : (
        <>
          <textarea
            value={textBuffer}
            onChange={(e) => setTextBuffer(e.target.value)}
            onBlur={() => {
              if (textBuffer.trim() === "") {
                onChange(undefined);
                setJsonError(null);
                return;
              }
              try {
                onChange(JSON.parse(textBuffer));
                setJsonError(null);
              } catch (err) {
                setJsonError(String(err));
              }
            }}
            rows={3}
            style={{ width: "100%", padding: 4, fontSize: 12, fontFamily: "monospace" }}
          />
          {jsonError && <div style={{ color: "var(--danger-text)", fontSize: 11 }}>{jsonError}</div>}
        </>
      )}
    </div>
  );
}
