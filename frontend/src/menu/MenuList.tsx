/**
 * One menu's list — the ONE renderer the menu bar, the right-click menus and every submenu share, so they all look
 * and behave alike: a row per item with its tick, its label and its shortcut right-aligned; a greyed row for an item
 * that cannot act (a real `disabled` button); ▸ on a submenu row, which opens on hover — with a short delay when
 * another submenu is already open, so a diagonal move towards the open flyout does not close it — or on click.
 *
 * A flyout is drawn `position: fixed` from its row's rectangle rather than inside the list, so a long list that
 * scrolls (Add Node's classes) never clips it; after it is measured it flips to the left of its row when the window
 * has no room on the right, and moves up when it would run off the bottom.
 */
import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import type { Entry, ItemEntry, SubmenuEntry } from "./entries";

/** How long a hover waits before it swaps one open submenu for another (ms). */
const SWITCH_DELAY = 140;

export interface MenuListProps {
  entries: Entry[];
  /** Closes the whole menu (after an item is picked). */
  close: () => void;
  className?: string;
  style?: CSSProperties;
  attrs?: Record<string, string>;
  /** Drawn above the rows (Add Node's search box). */
  header?: ReactNode;
}

export function MenuList({ entries, close, className, style, attrs, header }: MenuListProps) {
  return (
    <div className={`menu${className ? ` ${className}` : ""}`} role="menu" style={style} {...attrs} onContextMenu={(event) => event.preventDefault()}>
      {header}
      <MenuRows entries={entries} close={close} />
    </div>
  );
}

/** The rows of a menu, and the one submenu open among them. */
export function MenuRows({ entries, close }: { entries: Entry[]; close: () => void }) {
  const [open, setOpen] = useState<string | null>(null);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const later = (next: string | null) => {
    window.clearTimeout(timer.current);
    if (open === null) setOpen(next);
    else timer.current = window.setTimeout(() => setOpen(next), SWITCH_DELAY);
  };
  const hold = () => window.clearTimeout(timer.current);

  return (
    <div className="menu-rows">
      {entries.map((entry) => {
        if (entry.type === "separator") return <div key={entry.key} className="menu-separator" role="separator" />;
        if (entry.type === "header")
          return (
            <div key={entry.key} className="menu-header">
              {entry.label}
            </div>
          );
        if (entry.type === "submenu")
          return (
            <SubmenuRow
              key={entry.key}
              entry={entry}
              open={open === entry.key}
              onEnter={() => later(entry.key)}
              onClick={() => {
                window.clearTimeout(timer.current);
                setOpen(entry.key);
              }}
              onHold={hold}
              close={close}
            />
          );
        return <ItemRow key={entry.key} entry={entry} onEnter={() => later(null)} close={close} />;
      })}
    </div>
  );
}

function ItemRow({ entry, onEnter, close }: { entry: ItemEntry; onEnter: () => void; close: () => void }) {
  return (
    <button
      type="button"
      className="menu-item"
      role="menuitem"
      disabled={entry.disabled}
      title={entry.hint}
      aria-checked={entry.checked}
      onMouseEnter={onEnter}
      onClick={() => {
        close();
        entry.onSelect();
      }}
      {...entry.attrs}
    >
      <span className="menu-check">{entry.checked ? "✓" : ""}</span>
      {entry.swatch && <span className="menu-swatch" style={{ background: entry.swatch }} />}
      <span className="menu-label">{entry.label}</span>
      <span className="menu-shortcut">{entry.shortcut ?? ""}</span>
    </button>
  );
}

interface SubmenuRowProps {
  entry: SubmenuEntry;
  open: boolean;
  onEnter: () => void;
  onClick: () => void;
  onHold: () => void;
  close: () => void;
}

function SubmenuRow({ entry, open, onEnter, onClick, onHold, close }: SubmenuRowProps) {
  const row = useRef<HTMLButtonElement>(null);
  return (
    <>
      <button
        ref={row}
        type="button"
        className={`menu-item has-sub${open ? " open" : ""}`}
        role="menuitem"
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={entry.disabled}
        title={entry.hint}
        onMouseEnter={onEnter}
        onClick={onClick}
        {...entry.attrs}
      >
        <span className="menu-check" />
        <span className="menu-label">{entry.label}</span>
        <span className="menu-arrow">▸</span>
      </button>
      {open && !entry.disabled && row.current && (
        <Flyout anchor={row.current} onHold={onHold}>
          {entry.panel ? entry.panel(close) : <MenuRows entries={entry.items ?? []} close={close} />}
        </Flyout>
      )}
    </>
  );
}

/** A submenu's box, beside its row, kept inside the window. */
function Flyout({ anchor, onHold, children }: { anchor: HTMLElement; onHold: () => void; children: ReactNode }) {
  const box = useRef<HTMLDivElement>(null);
  const [place, setPlace] = useState<CSSProperties>(() => {
    const rect = anchor.getBoundingClientRect();
    return { left: rect.right - 2, top: rect.top - 5, visibility: "hidden" };
  });
  useLayoutEffect(() => {
    const element = box.current;
    if (!element) return;
    const row = anchor.getBoundingClientRect();
    const own = element.getBoundingClientRect();
    const left = row.right - 2 + own.width > window.innerWidth - 4 ? Math.max(4, row.left - own.width + 2) : row.right - 2;
    const top = Math.max(4, Math.min(row.top - 5, window.innerHeight - own.height - 4));
    setPlace({ left, top });
  }, [anchor]);
  return (
    <div ref={box} className="menu flyout" role="menu" style={{ position: "fixed", ...place }} onMouseEnter={onHold}>
      {children}
    </div>
  );
}
