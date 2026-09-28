/**
 * Toasts: the page's one channel for a refusal. A refused edit shows the server's LOCATED message verbatim
 * (`file:line:col: …`) and a drawn wire the document cannot express says which spelling it would need.
 * Each toast carries `data-toast="<level>"` so a test reads the level, not a class.
 */
import { useCallback, useState } from "react";

export const TOAST_LEVELS = ["error", "warning", "info"] as const;
export type ToastLevel = (typeof TOAST_LEVELS)[number];

export interface Toast {
  id: number;
  level: ToastLevel;
  text: string;
}

/** How long a toast stays, in milliseconds — long enough to read a located message and act on it. */
export const TOAST_MS = 10_000;

let sequence = 0;

export function useToasts(): { toasts: Toast[]; push: (level: ToastLevel, text: string) => void; dismiss: (id: number) => void } {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const dismiss = useCallback((id: number) => setToasts((all) => all.filter((toast) => toast.id !== id)), []);
  const push = useCallback(
    (level: ToastLevel, text: string) => {
      const id = ++sequence;
      setToasts((all) => [...all, { id, level, text }]);
      window.setTimeout(() => dismiss(id), TOAST_MS);
    },
    [dismiss],
  );
  return { toasts, push, dismiss };
}
