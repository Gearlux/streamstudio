/**
 * The palette: a searchable list of every class `GET api/palette` describes, opened by a double-click on empty
 * canvas or by a wire dropped on empty canvas (then narrowed to the classes that can take — or give — the
 * dragged type, by the model's meet rule). The catalogue is hundreds of classes, so at most `PALETTE_ROWS` rows
 * are drawn and the line under the search box says how many there are and to type to narrow; filtering the
 * whole list on every keystroke is cheap, drawing it is not.
 *
 * A pick is handed to the app, which inserts an op where the meta line says (`POST …/steps`): `added after grey`
 * (a flow step, or a list member — `added after stream.ops[1]`), or `added at the end of stream.ops` (an op list with no
 * member selected, `oplists.insertTarget`); any other class is a card of its own at the gesture's point, wired to the
 * dropped wire's end (`cards.ts`). `data-opened-ms` on the root is the time from the gesture to the list being in the
 * DOM, measured in the page — what the tests print.
 */
import { useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import type { DraggedEnd, NodeSpec } from "../api";
import type { Supertypes } from "../model";
import { insertLabel, type InsertTarget } from "../oplists";
import { plainDoc } from "../rst";
import { PALETTE_ROWS, paletteMatches, searchMatches, type PaletteFilter } from "../steps";

export interface PaletteRequest {
  /** Where the gesture happened, in screen pixels — the list opens there. */
  screen: { x: number; y: number };
  /** Where the pick is inserted; `none` when nowhere (the pick is then refused, in the `why` words when given). */
  target: InsertTarget;
  /** A dropped wire's type and side; absent for a double-click. */
  filter?: PaletteFilter;
  /** The end the dropped wire was dragged out of: a CARD picked here is wired to it in the same edit (`cards.ts`). */
  wire?: DraggedEnd;
  /** `performance.now()` at the gesture. */
  openedAt: number;
}

interface PaletteProps {
  request: PaletteRequest;
  specs: NodeSpec[] | null;
  types: Supertypes;
  onPick: (spec: NodeSpec) => void;
  onClose: () => void;
}

const WIDTH = 380;
const HEIGHT = 420;

export function Palette({ request, specs, types, onPick, onClose }: PaletteProps) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const { filter, target } = request;

  const matching = useMemo(
    () => (specs ?? []).filter((spec) => paletteMatches(spec, filter, types) && searchMatches(spec, query)),
    [specs, filter, types, query],
  );
  const shown = matching.slice(0, PALETTE_ROWS);

  // The list is in the DOM when this runs (before paint): the open time is the gesture-to-DOM time.
  useLayoutEffect(() => {
    if (root.current && specs !== null && root.current.dataset.openedMs === undefined)
      root.current.dataset.openedMs = (performance.now() - request.openedAt).toFixed(1);
  }, [specs, request.openedAt]);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    event.stopPropagation(); // the page's shortcuts are not for a query being typed
    if (event.key === "Escape") onClose();
    else if (event.key === "Enter" && shown[active]) onPick(shown[active]);
    else if (event.key === "ArrowDown") setActive((index) => Math.min(index + 1, shown.length - 1));
    else if (event.key === "ArrowUp") setActive((index) => Math.max(index - 1, 0));
    else return;
    event.preventDefault();
  };

  const parts: string[] = [];
  if (specs === null) parts.push("loading the catalogue…");
  else {
    parts.push(`${shown.length} of ${specs.length} classes`);
    if (matching.length > shown.length) parts.push(`${matching.length} match — type to narrow`);
  }
  if (filter) parts.push(`${filter.side === "inputs" ? "an input taking" : "an output giving"} ${filter.type}`);
  const where = insertLabel(target);
  parts.push(where !== undefined ? `added ${where}` : target.kind === "none" && target.why ? target.why : "nothing to add after");

  return (
    <>
      <div className="palette-backdrop" onMouseDown={onClose} />
      <div
        ref={root}
        className="palette"
        data-palette
        style={{
          left: Math.max(8, Math.min(request.screen.x, window.innerWidth - WIDTH - 8)),
          top: Math.max(48, Math.min(request.screen.y, window.innerHeight - HEIGHT - 8)),
        }}
        onKeyDown={onKeyDown}
      >
        <input
          autoFocus
          data-palette-search
          placeholder="search classes…"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setActive(0);
          }}
        />
        <div className="palette-meta" data-palette-meta>
          {parts.join(" · ")}
        </div>
        <ul data-palette-list>
          {shown.map((spec, index) => (
            <li
              key={spec.class_path}
              data-class={spec.class_path}
              className={index === active ? "active" : undefined}
              title={spec.problem || plainDoc(spec.doc)}
              onMouseEnter={() => setActive(index)}
              onClick={() => onPick(spec)}
            >
              <b>{spec.name}</b>
              <span>{spec.class_path}</span>
            </li>
          ))}
        </ul>
      </div>
    </>
  );
}
