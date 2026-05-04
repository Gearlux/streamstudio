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

// Tooltip copy: each button gets a one-sentence "what it does" + a one-sentence
// "when to use it". Disambiguates the four overlapping verbs (Run/Step/Continue
// + Run-step vs Run-continue) so the user doesn't have to guess.
const TIPS = {
  runStep:
    "Start a fresh run paused after the very first op. Use this to step through a pipeline op-by-op from the beginning.",
  runContinue:
    "Start a fresh run that immediately streams every op without pausing. Use this for a normal end-to-end execution.",
  step: "Advance the currently-paused run by one op. Disabled until you start a run with Run (step).",
  continue: "Resume the currently-paused run and let it stream to the end. Disabled until you start a run.",
  abort: "Stop the currently-running pipeline immediately. Disabled until you start a run.",
} as const;

export default function StepControls({ runId, canStart, onStart, onCommand }: Props) {
  return (
    <div style={{ display: "flex", alignItems: "center" }}>
      <button style={BTN} disabled={!canStart} onClick={() => onStart("step")} title={TIPS.runStep}>
        Run (step)
      </button>
      <button style={BTN} disabled={!canStart} onClick={() => onStart("continue")} title={TIPS.runContinue}>
        Run (continue)
      </button>
      <span style={{ width: 16 }} />
      <button style={BTN} disabled={!runId} onClick={() => onCommand("step")} title={TIPS.step}>
        Step
      </button>
      <button style={BTN} disabled={!runId} onClick={() => onCommand("continue")} title={TIPS.continue}>
        Continue
      </button>
      <button style={BTN} disabled={!runId} onClick={() => onCommand("abort")} title={TIPS.abort}>
        Abort
      </button>
      <span style={{ flex: 1 }} />
      <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
        {runId ? `run_id: ${runId.slice(0, 8)}…` : "no run"}
      </span>
    </div>
  );
}
