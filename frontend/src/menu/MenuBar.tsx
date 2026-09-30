/**
 * The menu bar, the way a desktop app's works: a click opens a menu (and closes it again), and while one is open,
 * moving over another title opens that one instead; picking an item, a click anywhere else or Escape closes it.
 * Each menu is `[data-menu="<id>"]` (its title button is the click target) and each item carries its
 * `data-command` — the hooks a browser test clicks by.
 *
 * The entries are built by the caller on every render (`menus.barMenus`), so what an open menu greys follows the
 * selection and the graph as they change.
 */
import { useEffect, useRef } from "react";
import type { MenuId } from "../commands";
import type { BarMenu } from "../menus";
import { MenuList } from "./MenuList";

export interface MenuBarProps {
  menus: BarMenu[];
  /** The open menu, held by the page so the keyboard (Escape, a shortcut) can close it. */
  open: MenuId | null;
  onOpen: (id: MenuId | null) => void;
}

export function MenuBar({ menus, open, onOpen }: MenuBarProps) {
  const bar = useRef<HTMLElement>(null);

  useEffect(() => {
    if (open === null) return;
    const outside = (event: MouseEvent) => {
      if (!bar.current?.contains(event.target as Node)) onOpen(null);
    };
    window.addEventListener("mousedown", outside, true);
    return () => window.removeEventListener("mousedown", outside, true);
  }, [open, onOpen]);

  return (
    <nav className="menubar" ref={bar} role="menubar">
      {menus.map((menu) => (
        <div key={menu.id} className={`menubar-menu${open === menu.id ? " open" : ""}`} data-menu={menu.id}>
          <button
            type="button"
            className="menubar-title"
            role="menuitem"
            aria-haspopup="menu"
            aria-expanded={open === menu.id}
            onMouseDown={(event) => {
              event.preventDefault(); // keep the canvas's focus and selection
              onOpen(open === menu.id ? null : menu.id);
            }}
            onClick={(event) => {
              if (event.detail === 0) onOpen(open === menu.id ? null : menu.id); // Enter or Space on a focused title
            }}
            onMouseEnter={() => {
              if (open !== null && open !== menu.id) onOpen(menu.id);
            }}
          >
            {menu.label}
          </button>
          {open === menu.id && <MenuList className="dropdown" entries={menu.entries} close={() => onOpen(null)} />}
        </div>
      ))}
    </nav>
  );
}
