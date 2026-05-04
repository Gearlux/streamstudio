import { useEffect, useState } from "react";

export type ThemePreference = "auto" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

const DARK_QUERY = "(prefers-color-scheme: dark)";

/** Resolve "auto" against the OS preference; pass-through otherwise. */
export function resolveTheme(pref: ThemePreference): ResolvedTheme {
  if (pref === "light" || pref === "dark") return pref;
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return "light";
  return window.matchMedia(DARK_QUERY).matches ? "dark" : "light";
}

/** Set `<html data-theme="...">` so theme.css's overrides take effect. */
export function applyTheme(resolved: ResolvedTheme): void {
  if (typeof document === "undefined") return;
  document.documentElement.dataset.theme = resolved;
}

/**
 * Apply the user's theme preference and keep it in sync with OS changes when
 * the preference is "auto". Returns the currently-resolved theme so a caller
 * can render a theme-dependent label (e.g. an icon) without re-querying.
 */
export function useTheme(pref: ThemePreference): ResolvedTheme {
  const [resolved, setResolved] = useState<ResolvedTheme>(() => resolveTheme(pref));

  useEffect(() => {
    const next = resolveTheme(pref);
    setResolved(next);
    applyTheme(next);
    if (pref !== "auto") return;
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const mql = window.matchMedia(DARK_QUERY);
    const onChange = () => {
      const r: ResolvedTheme = mql.matches ? "dark" : "light";
      setResolved(r);
      applyTheme(r);
    };
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [pref]);

  return resolved;
}
