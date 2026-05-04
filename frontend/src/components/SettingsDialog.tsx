import type { Preferences } from "../types";

interface Props {
  prefs: Preferences;
  onClose: () => void;
  onChange: (partial: Partial<Preferences>) => void;
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
  minWidth: 380,
  fontSize: 13,
  border: "1px solid var(--border)",
  boxShadow: "var(--shadow-elevated)",
};

// Each entry maps a Preferences field to a labelled control. When a new
// field lands on the backend's Preferences model + types.ts, add a row
// here so it shows up in the Settings dialog.
type Row =
  | { key: keyof Preferences; kind: "bool"; label: string; help?: string }
  | { key: keyof Preferences; kind: "string"; label: string; help?: string }
  | {
      key: keyof Preferences;
      kind: "select";
      label: string;
      options: Array<{ value: string; label: string }>;
      help?: string;
    };

const ROWS: Row[] = [
  {
    key: "theme",
    kind: "select",
    label: "Theme",
    options: [
      { value: "auto", label: "Auto (follow OS)" },
      { value: "light", label: "Light" },
      { value: "dark", label: "Dark" },
    ],
    help: "Auto follows your operating system's light/dark setting and updates as it changes.",
  },
  {
    key: "confirm_clear_pipeline",
    kind: "bool",
    label: "Confirm before clearing the canvas (New)",
    help: "Show a dialog before discarding a non-empty pipeline.",
  },
  {
    key: "show_internal_ops",
    kind: "bool",
    label: "Show internal ops in the palette",
    help: "Reveal ops whose names start with an underscore.",
  },
  {
    key: "default_save_filename",
    kind: "string",
    label: "Default save filename",
    help: "Used when Save can't infer a filename from a previous Open.",
  },
];

export default function SettingsDialog({ prefs, onClose, onChange }: Props) {
  return (
    <div style={OVERLAY} onClick={onClose}>
      <div style={PANEL} onClick={(e) => e.stopPropagation()}>
        <div style={{ fontWeight: 600, marginBottom: 14, fontSize: 14 }}>Settings</div>
        {ROWS.map((row) => (
          <div key={row.key} style={{ marginBottom: 14 }}>
            {row.kind === "bool" && (
              <label style={{ display: "block" }}>
                <input
                  type="checkbox"
                  checked={prefs[row.key] as boolean}
                  onChange={(e) => onChange({ [row.key]: e.target.checked } as Partial<Preferences>)}
                  style={{ marginRight: 6 }}
                />
                {row.label}
              </label>
            )}
            {row.kind === "string" && (
              <label style={{ display: "block" }}>
                <div style={{ marginBottom: 4 }}>{row.label}</div>
                <input
                  type="text"
                  value={prefs[row.key] as string}
                  onChange={(e) => onChange({ [row.key]: e.target.value } as Partial<Preferences>)}
                  style={{ width: "100%", padding: 4, fontSize: 13 }}
                />
              </label>
            )}
            {row.kind === "select" && (
              <label style={{ display: "block" }}>
                <div style={{ marginBottom: 4 }}>{row.label}</div>
                <select
                  value={prefs[row.key] as string}
                  onChange={(e) => onChange({ [row.key]: e.target.value } as Partial<Preferences>)}
                  style={{ width: "100%", padding: 4, fontSize: 13 }}
                >
                  {row.options.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {row.help && <div style={{ color: "var(--text-muted)", fontSize: 11, marginTop: 2 }}>{row.help}</div>}
          </div>
        ))}
        <div style={{ display: "flex", justifyContent: "flex-end" }}>
          <button onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
