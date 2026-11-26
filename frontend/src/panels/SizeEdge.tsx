/**
 * The edge of a sidebar that faces the canvas: dragged, it changes that sidebar's width; double-clicked, it puts
 * the width back to where it started. The rule (limits, direction) is `widths.ts`; the app keeps the widths.
 */
import { useRef } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import { draggedWidth } from "../widths";
import type { Sidebar } from "../widths";

type Props = {
  sidebar: Sidebar;
  /** The sidebar's width now, in pixels. */
  width: number;
  /** Called while dragging with the width the pointer asks for (not yet within the limits). */
  onDrag: (wanted: number) => void;
  /** Called when the drag ends: the width is the person's from now on. */
  onDone: () => void;
  /** Called on a double click: back to the initial width. */
  onReset: () => void;
};

const LABELS: Record<Sidebar, string> = { nodes: "Nodes sidebar", side: "side panel" };

export function SizeEdge({ sidebar, width, onDrag, onDone, onReset }: Props): JSX.Element {
  const drag = useRef<{ fromX: number; start: number } | null>(null);

  const begin = (event: ReactPointerEvent<HTMLDivElement>): void => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { fromX: event.clientX, start: width };
  };
  const move = (event: ReactPointerEvent<HTMLDivElement>): void => {
    if (drag.current === null) return;
    onDrag(draggedWidth(sidebar, drag.current.start, drag.current.fromX, event.clientX));
  };
  const end = (): void => {
    if (drag.current === null) return;
    drag.current = null;
    onDone();
  };

  return (
    <div
      className={`size-edge size-edge-${sidebar}`}
      data-size-edge={sidebar}
      role="separator"
      aria-orientation="vertical"
      aria-label={`Width of the ${LABELS[sidebar]}`}
      aria-valuenow={width}
      title="Drag to change the width; double-click to put it back"
      onPointerDown={begin}
      onPointerMove={move}
      onPointerUp={end}
      onPointerCancel={end}
      onDoubleClick={onReset}
    />
  );
}
