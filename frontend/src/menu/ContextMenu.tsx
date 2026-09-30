/**
 * A right-click menu: the same list the menu bar draws (`MenuList`), at the pointer, kept inside the window. A press
 * anywhere outside it closes it without being swallowed — so a right-click elsewhere on the canvas closes this one and
 * opens the next — and Escape closes it (the page's key handler). `data-context-menu` names which of the three menus
 * it is (`pane`, `node`, `group`).
 */
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Entry } from "./entries";
import { MenuList } from "./MenuList";

export interface ContextMenuProps {
  /** Where the pointer was, in screen pixels. */
  at: { x: number; y: number };
  kind: string;
  entries: Entry[];
  onClose: () => void;
}

export function ContextMenu({ at, kind, entries, onClose }: ContextMenuProps) {
  const root = useRef<HTMLDivElement>(null);
  const [place, setPlace] = useState({ left: at.x, top: at.y });

  useLayoutEffect(() => {
    const element = root.current;
    if (!element) return;
    const { width, height } = element.getBoundingClientRect();
    setPlace({
      left: Math.max(4, Math.min(at.x, window.innerWidth - width - 4)),
      top: Math.max(4, Math.min(at.y, window.innerHeight - height - 4)),
    });
  }, [at.x, at.y]);

  useEffect(() => {
    const outside = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) onClose();
    };
    window.addEventListener("mousedown", outside, true);
    return () => window.removeEventListener("mousedown", outside, true);
  }, [onClose]);

  return (
    <div ref={root} className="context-menu" style={{ left: place.left, top: place.top }}>
      <MenuList entries={entries} close={onClose} attrs={{ "data-context-menu": kind }} />
    </div>
  );
}
