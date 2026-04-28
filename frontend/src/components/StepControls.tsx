interface Props {
  runId: string | null;
  canStart: boolean;
  onStart: (mode: "step" | "continue") => void;
  onCommand: (cmd: "step" | "continue" | "abort") => void;
}

const BTN: React.CSSProperties = {
  padding: "6px 14px",
  marginRight: 8,
  fontSize: 13,
};

export default function StepControls({ runId, canStart, onStart, onCommand }: Props) {
  return (
    <div style={{ display: "flex", alignItems: "center" }}>
      <button style={BTN} disabled={!canStart} onClick={() => onStart("step")}>
        Run (step)
      </button>
      <button style={BTN} disabled={!canStart} onClick={() => onStart("continue")}>
        Run (continue)
      </button>
      <span style={{ width: 16 }} />
      <button style={BTN} disabled={!runId} onClick={() => onCommand("step")}>
        Step
      </button>
      <button style={BTN} disabled={!runId} onClick={() => onCommand("continue")}>
        Continue
      </button>
      <button style={BTN} disabled={!runId} onClick={() => onCommand("abort")}>
        Abort
      </button>
      <span style={{ flex: 1 }} />
      <span style={{ fontSize: 11, color: "#666" }}>
        {runId ? `run_id: ${runId.slice(0, 8)}…` : "no run"}
      </span>
    </div>
  );
}
