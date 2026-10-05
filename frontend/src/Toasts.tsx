/**
 * The toasts, drawn — newest last; a click on one dismisses it (`toast.ts` holds them and their timeout).
 *
 * A refusal or a warning carries a small **copy** button (`toast.copyable`, the user's ask of 2026-10-05: to report an
 * error) that copies the WHOLE message (`toast.copyText`) and then says `copied` — or `copy failed`, when the browser
 * refuses the clipboard. The button sits on the box's top-right corner but OUTSIDE the `[data-toast]` element, so that
 * element's text stays the message alone: what a test reads, and what a person selects by hand.
 */
import { useState } from "react";
import { copyable, copyText, copyWithCommand, type Toast } from "./toast";

interface ToastsProps {
  toasts: Toast[];
  dismiss: (id: number) => void;
}

export function Toasts({ toasts, dismiss }: ToastsProps) {
  return (
    <div className="toasts">
      {toasts.map((toast) => (
        <ToastBox key={toast.id} toast={toast} dismiss={dismiss} />
      ))}
    </div>
  );
}

/** What the copy button says: before, after a copy, after a refusal. */
type CopyState = "copy" | "copied" | "copy failed";

function ToastBox({ toast, dismiss }: { toast: Toast; dismiss: (id: number) => void }) {
  const [said, setSaid] = useState<CopyState>("copy");
  const withCopy = copyable(toast.level);
  const copy = () => {
    const clipboard = typeof navigator === "undefined" ? undefined : navigator.clipboard;
    void copyText(toast.text, clipboard, copyWithCommand).then((copied) => setSaid(copied ? "copied" : "copy failed"));
  };
  return (
    <div className={`toast-row${withCopy ? " copyable" : ""}`}>
      <div className={`toast toast-${toast.level}`} data-toast={toast.level} onClick={() => dismiss(toast.id)}>
        {toast.text}
      </div>
      {withCopy && (
        <button type="button" className="toast-copy" data-toast-copy={said} title="copy the whole message" onClick={copy}>
          {said}
        </button>
      )}
    </div>
  );
}
