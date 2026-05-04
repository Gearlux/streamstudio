import type { GraphNode, OpSchema } from "../types";

interface Props {
  node: GraphNode | null;
  schema: OpSchema | null;
}

const ROW: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "90px 1fr",
  rowGap: 4,
  columnGap: 8,
  fontSize: 12,
  marginBottom: 12,
};

const KEY: React.CSSProperties = { color: "var(--text-muted)", fontSize: 11 };

export default function OpInfoPanel({ node, schema }: Props) {
  if (!node) {
    return (
      <div style={{ color: "var(--text-faint)", fontSize: 13 }}>
        Select a node on the canvas to see details about its op.
      </div>
    );
  }
  if (!schema) {
    return (
      <div style={{ fontSize: 12 }}>
        <div style={{ fontWeight: 600, marginBottom: 4 }}>{node.op_path}</div>
        <div style={{ color: "var(--danger-text)" }}>
          No schema discovered for this op (palette out of sync with the running backend).
        </div>
      </div>
    );
  }

  return (
    <div style={{ fontSize: 12 }}>
      <div style={{ fontWeight: 600, marginBottom: 8 }}>{schema.name}</div>

      <div style={ROW}>
        <div style={KEY}>category</div>
        <div>{schema.category}</div>
        <div style={KEY}>module</div>
        <div style={{ fontFamily: "monospace" }}>{schema.module}</div>
        <div style={KEY}>path</div>
        <div style={{ fontFamily: "monospace" }}>{schema.path}</div>
        <div style={KEY}>node id</div>
        <div style={{ fontFamily: "monospace" }}>{node.id}</div>
      </div>

      {schema.doc && (
        <div style={{ marginBottom: 12 }}>
          <div style={KEY}>docstring</div>
          <pre
            style={{
              whiteSpace: "pre-wrap",
              fontFamily: "inherit",
              margin: "4px 0 0",
              padding: 6,
              background: "var(--bg-subtle)",
              borderRadius: 4,
              fontSize: 11,
              color: "var(--text-secondary)",
            }}
          >
            {schema.doc.trim()}
          </pre>
        </div>
      )}

      <div style={KEY}>parameters ({schema.parameters.length})</div>
      {schema.parameters.length === 0 ? (
        <div style={{ color: "var(--text-faint)", marginTop: 4 }}>(none)</div>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse", marginTop: 4, fontSize: 11 }}>
          <thead>
            <tr style={{ textAlign: "left", color: "var(--text-muted)" }}>
              <th style={{ padding: "2px 4px" }}>name</th>
              <th style={{ padding: "2px 4px" }}>type</th>
              <th style={{ padding: "2px 4px" }}>default</th>
            </tr>
          </thead>
          <tbody>
            {schema.parameters.map((p) => (
              <tr key={p.name} style={{ borderTop: "1px solid var(--border)" }}>
                <td style={{ padding: "2px 4px", fontFamily: "monospace" }}>
                  {p.name}
                  {p.required && <span style={{ color: "var(--danger-text)" }}>*</span>}
                </td>
                <td style={{ padding: "2px 4px", fontFamily: "monospace", color: "var(--text-secondary)" }}>
                  {prettyType(p.type)}
                </td>
                <td style={{ padding: "2px 4px", fontFamily: "monospace", color: "var(--text-muted)" }}>
                  {p.default ?? ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// Backend gives us strings like "<class 'float'>" — strip the wrapping for readability.
function prettyType(t: string): string {
  const m = t.match(/^<class '(.+)'>$/);
  return m ? m[1] : t;
}
