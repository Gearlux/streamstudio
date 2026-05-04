import { useRef } from "react";
import type { Preferences } from "../types";
import type { ResolvedTheme } from "../theme";

interface Props {
  hasGraph: boolean;
  themePref: Preferences["theme"];
  resolvedTheme: ResolvedTheme;
  onCycleTheme: () => void;
  onNew: () => void;
  onOpen: (file: File) => void;
  onSave: () => void;
  onLoadDemo: () => void;
  onOpenSettings: () => void;
}

const BAR: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  padding: "6px 12px",
  borderBottom: "1px solid var(--border)",
  background: "var(--bg-elevated)",
  color: "var(--text-primary)",
  fontSize: 12,
};

const TITLE: React.CSSProperties = {
  fontWeight: 700,
  marginRight: 16,
  fontSize: 13,
};

const SPACER: React.CSSProperties = { flex: 1 };

const THEME_GLYPH: Record<Preferences["theme"], string> = {
  auto: "◐",
  light: "☀",
  dark: "☾",
};

const THEME_TIP: Record<Preferences["theme"], string> = {
  auto: "Theme: Auto (follows OS). Click to switch to Light.",
  light: "Theme: Light. Click to switch to Dark.",
  dark: "Theme: Dark. Click to switch to Auto.",
};

export default function AppBar({
  hasGraph,
  themePref,
  resolvedTheme,
  onCycleTheme,
  onNew,
  onOpen,
  onSave,
  onLoadDemo,
  onOpenSettings,
}: Props) {
  const fileRef = useRef<HTMLInputElement | null>(null);

  return (
    <div style={BAR}>
      <div style={TITLE}>FluxStudio</div>
      <button onClick={onNew} title="Discard the current graph and start a new empty pipeline.">
        New
      </button>
      <button onClick={() => fileRef.current?.click()} title="Open a YAML pipeline from your computer.">
        Open…
      </button>
      <button
        onClick={onSave}
        disabled={!hasGraph}
        title="Save the current graph as a YAML file (downloaded by your browser)."
      >
        Save…
      </button>
      <button onClick={onLoadDemo} title="Load the bundled tiny_pipeline.yaml demo into the canvas.">
        Load demo
      </button>
      <input
        ref={fileRef}
        type="file"
        accept=".yaml,.yml"
        style={{ display: "none" }}
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) onOpen(file);
          e.target.value = ""; // allow re-selecting the same file
        }}
      />
      <div style={SPACER} />
      <button
        onClick={onCycleTheme}
        title={`${THEME_TIP[themePref]} (currently ${resolvedTheme})`}
        aria-label={`Theme: ${themePref}`}
        style={{ minWidth: 32 }}
      >
        {THEME_GLYPH[themePref]}
      </button>
      <button onClick={onOpenSettings} title="Open settings.">
        ⚙ Settings
      </button>
    </div>
  );
}
