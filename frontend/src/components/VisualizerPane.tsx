import type { Payload, StepEvent } from "../types";

interface Props {
  event: StepEvent | null;
}

export default function VisualizerPane({ event }: Props) {
  if (!event) {
    return <div style={{ color: "var(--text-faint)", fontSize: 13 }}>Run a pipeline to see per-node previews here.</div>;
  }
  return (
    <div style={{ fontSize: 12 }}>
      <div style={{ fontWeight: 600, marginBottom: 8 }}>
        sample {event.sample_index} · op {event.op_index} · {event.op_name}
        {event.filtered && " · FILTERED"}
        {event.final && " · final"}
      </div>
      {event.payload ? (
        <>
          <Section title="input" payload={event.payload.input} />
          <Section title="target" payload={event.payload.target} />
          <details>
            <summary>metadata</summary>
            <pre style={{ background: "var(--bg-subtle)", color: "var(--text-secondary)", padding: 6, borderRadius: 4 }}>
              {JSON.stringify(event.payload.metadata, null, 2)}
            </pre>
          </details>
        </>
      ) : (
        <div style={{ color: "var(--text-faint)" }}>(no payload)</div>
      )}
    </div>
  );
}

function Section({ title, payload }: { title: string; payload: Payload }) {
  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{ fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase", marginBottom: 4 }}>
        {title}
      </div>
      <PayloadView payload={payload} />
    </div>
  );
}

function PayloadView({ payload }: { payload: Payload }) {
  switch (payload.kind) {
    case "null":
      return <span style={{ color: "var(--text-faint)" }}>null</span>;
    case "scalar":
      return <code>{String(payload.value)}</code>;
    case "image":
      return (
        <img
          src={`data:${payload.mime};base64,${payload.data}`}
          alt={`${payload.width}×${payload.height}`}
          style={{ maxWidth: "100%", border: "1px solid var(--border)" }}
        />
      );
    case "ndarray":
    case "tensor":
      return (
        <div>
          <div>
            shape={JSON.stringify(payload.shape)} dtype={payload.dtype}
          </div>
          {payload.stats && Object.keys(payload.stats).length > 0 && (
            <div style={{ color: "var(--text-muted)" }}>
              min={payload.stats.min?.toFixed(3)} max={payload.stats.max?.toFixed(3)} mean=
              {payload.stats.mean?.toFixed(3)}
            </div>
          )}
          {payload.preview && (
            <img
              src={`data:${payload.preview.mime};base64,${payload.preview.data}`}
              alt="preview"
              style={{ marginTop: 4, maxWidth: "100%", border: "1px solid var(--border)" }}
            />
          )}
        </div>
      );
    case "sequence":
      return (
        <span>
          {payload.type}[{payload.length}]
        </span>
      );
    case "mapping":
      return <span>dict({payload.keys.join(", ")})</span>;
    case "opaque":
      return (
        <code style={{ color: "var(--text-muted)" }}>
          {payload.type}: {payload.repr}
        </code>
      );
  }
}
