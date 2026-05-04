import { useState } from "react";

interface Props {
  message: string;
  onCancel: () => void;
  onConfirm: (dontAskAgain: boolean) => void;
}

const OVERLAY: React.CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "var(--overlay)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 1000,
};

const PANEL: React.CSSProperties = {
  background: "var(--bg-elevated)",
  color: "var(--text-primary)",
  padding: 20,
  borderRadius: 8,
  minWidth: 320,
  maxWidth: 480,
  fontSize: 13,
  border: "1px solid var(--border)",
  boxShadow: "var(--shadow-elevated)",
};

export default function ConfirmClearDialog({ message, onCancel, onConfirm }: Props) {
  const [dontAskAgain, setDontAskAgain] = useState(false);
  return (
    <div style={OVERLAY} onClick={onCancel}>
      <div style={PANEL} onClick={(e) => e.stopPropagation()}>
        <div style={{ marginBottom: 14 }}>{message}</div>
        <label style={{ display: "block", marginBottom: 14, color: "var(--text-secondary)" }}>
          <input
            type="checkbox"
            checked={dontAskAgain}
            onChange={(e) => setDontAskAgain(e.target.checked)}
            style={{ marginRight: 6 }}
          />
          Don't ask again
        </label>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button onClick={onCancel}>Cancel</button>
          <button
            onClick={() => onConfirm(dontAskAgain)}
            style={{ background: "var(--danger)", color: "#fff", borderColor: "var(--danger)" }}
          >
            Discard
          </button>
        </div>
      </div>
    </div>
  );
}
