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

/**
 * The toasts that carry a **copy** button (`Toasts.tsx`): a refusal and a warning — the message a person reports (the
 * user's ask, 2026-10-05). A located message runs past what anyone retypes.
 */
export const COPYABLE: readonly ToastLevel[] = ["error", "warning"];

export const copyable = (level: ToastLevel): boolean => COPYABLE.includes(level);

/** What `copyText` writes through: the one method of the async Clipboard API it uses. */
export interface ClipboardLike {
  writeText: (text: string) => Promise<void>;
}

/**
 * Copy `text` — the WHOLE message, never a cut of it — through `clipboard` (`navigator.clipboard`; absent outside a
 * secure page), else through `fallback` (`copyWithCommand`: the page's own copy command). `true` when it was copied;
 * when both refuse (no permission, no focus) it is `false`, never thrown, and the button says so. The fallback exists
 * because a browser may deny the Clipboard API outright — measured 2026-10-05 in an embedded browser: `NotAllowedError:
 * … Write permission denied.` on the page and in the editor's frame alike.
 */
export async function copyText(
  text: string,
  clipboard: ClipboardLike | undefined,
  fallback?: (text: string) => boolean,
): Promise<boolean> {
  if (clipboard !== undefined) {
    try {
      await clipboard.writeText(text);
      return true;
    } catch {
      // refused — the page's own copy command may still be allowed
    }
  }
  return fallback !== undefined ? fallback(text) : false;
}

/** The page's own copy command over a hidden text box — `true` when the browser says it copied, never thrown. */
export function copyWithCommand(text: string): boolean {
  if (typeof document === "undefined") return false;
  const box = document.createElement("textarea");
  box.value = text;
  box.setAttribute("readonly", "");
  box.style.position = "fixed";
  box.style.opacity = "0";
  document.body.appendChild(box);
  box.select();
  try {
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    box.remove();
  }
}

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
