/**
 * The shell every dialog shares: a dimmed backdrop, a titled box, Escape and the backdrop cancel, and the page's
 * shortcuts are kept out (a key typed in a dialog is the dialog's). `data-dialog` names which dialog is open.
 *
 * The dialog keeps the keyboard (P5): Tab and Shift+Tab cycle through its own controls (`focus.nextFocus`) — measured
 * before, the fourth Tab of the recovery question reached the menu bar behind it, and New ▸ Flow graph… made a graph
 * while the question was still asked.
 */
import { useRef, type KeyboardEvent, type ReactNode } from "react";
import { FOCUSABLE, nextFocus } from "../focus";

export interface ModalProps {
  title: string;
  kind: string;
  onCancel: () => void;
  children: ReactNode;
  footer: ReactNode;
  wide?: boolean;
}

export function Modal({ title, kind, onCancel, children, footer, wide }: ModalProps) {
  const box = useRef<HTMLDivElement>(null);
  const keepFocus = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Tab" || box.current === null) return;
    const controls = Array.from(box.current.querySelectorAll<HTMLElement>(FOCUSABLE));
    const next = nextFocus(controls.length, controls.indexOf(document.activeElement as HTMLElement), event.shiftKey);
    event.preventDefault();
    controls[next]?.focus();
  };
  return (
    <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onCancel()}>
      <div
        ref={box}
        className={`modal${wide ? " wide" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        data-dialog={kind}
        onKeyDown={(event) => {
          event.stopPropagation();
          if (event.key === "Escape") onCancel();
          keepFocus(event);
        }}
      >
        <header className="modal-title">{title}</header>
        <div className="modal-body">{children}</div>
        <footer className="modal-footer">{footer}</footer>
      </div>
    </div>
  );
}
