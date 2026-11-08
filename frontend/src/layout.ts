/**
 * The client-side "arrange": dagre over the node boxes React Flow has MEASURED, ranked left to right along the
 * wires — a pipeline is a one-direction chain with a few forks, which is exactly dagre's model (no ELK needed).
 *
 * Why on the client when the server has a layout too (`streamstudio/layout.py`, `POST …/layout`): the server
 * lays out a column per layer and stacks each column by an ESTIMATE of every card's height (counted from its rows,
 * never measured — it does not see the drawing); the page has the measured sizes, so dagre spaces the cards by
 * their real sizes and crosses fewer wires. Both are offered, the client's by default. The result is written like
 * any drag — `PUT …/positions` — so the sidecar is the one place a layout lives.
 *
 * Three directions, one dagre run each: left to right (`LR`, ranks as columns), top to bottom (`TB`, ranks as rows)
 * and diagonal — dagre's left-to-right columns, each column's top moved below the tallest card of the column
 * before, so a chain runs from the top left to the bottom right. A column keeps its own stacking, and columns never
 * share an x, so the step cannot lay one card over another.
 *
 * Where a card's sockets sit follows the drawing, never a setting: a level whose wires mostly run DOWN (`runsDown`) —
 * what *Arrange top to bottom* leaves — draws each card's inputs on its top edge and its outputs on its bottom edge;
 * any other level draws them on the sides. The positions are the sidecar's, so a reload, an undo and the next arrange
 * all turn the sockets with the cards, and the sidecar gains no block for it.
 */
import dagre from "@dagrejs/dagre";

/** The canvas's snap grid; an arranged corner lands on it, as a dragged one does. */
export const GRID = 10;

export interface Box {
  id: string;
  width: number;
  height: number;
}

const snap = (value: number): number => Math.round(value / GRID) * GRID;

/** How an arrange lays the ranks out: left to right, top left to bottom right, or top to bottom. */
export type Direction = "LR" | "diagonal" | "TB";

/** The gap between a diagonal column's top and the bottom of the tallest card of the column before it. */
export const DIAGONAL_GAP = 2 * GRID;

/** A top-left corner per box, ranked along the wires (`LR` by default); a wire to an unknown box is ignored. */
export function arrange(
  boxes: Box[],
  wires: { source: string; target: string }[],
  direction: Direction = "LR",
): Record<string, { x: number; y: number }> {
  const graph = new dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}));
  graph.setGraph({ rankdir: direction === "TB" ? "TB" : "LR", nodesep: 40, ranksep: 80 });
  const known = new Set(boxes.map((box) => box.id));
  boxes.forEach((box) => graph.setNode(box.id, { width: box.width, height: box.height }));
  wires.forEach((wire) => {
    if (known.has(wire.source) && known.has(wire.target) && wire.source !== wire.target) graph.setEdge(wire.source, wire.target);
  });
  dagre.layout(graph);
  const drop = direction === "diagonal" ? columnDrops(boxes, (id) => graph.node(id)) : () => 0;
  return Object.fromEntries(
    boxes.map((box) => {
      const placed = graph.node(box.id);
      return [box.id, { x: snap(placed.x - box.width / 2), y: snap(placed.y - box.height / 2 + drop(placed.x)) }];
    }),
  );
}

/**
 * How far each left-to-right column moves down in a diagonal arrange, by the column's centre x (dagre centres every
 * box of a rank on one x): its top goes to the top of the column before plus that column's tallest card plus
 * `DIAGONAL_GAP` — the first column's top stays where dagre put it.
 */
function columnDrops(boxes: Box[], placedOf: (id: string) => { x: number; y: number }): (centre: number) => number {
  const columns = new Map<number, { top: number; tallest: number }>();
  boxes.forEach((box) => {
    const placed = placedOf(box.id);
    const centre = Math.round(placed.x);
    const column = columns.get(centre) ?? { top: Infinity, tallest: 0 };
    columns.set(centre, { top: Math.min(column.top, placed.y - box.height / 2), tallest: Math.max(column.tallest, box.height) });
  });
  const drops = new Map<number, number>();
  let top: number | undefined;
  [...columns.keys()]
    .sort((p, q) => p - q)
    .forEach((centre) => {
      const column = columns.get(centre)!;
      top ??= column.top;
      drops.set(centre, top - column.top);
      top += column.tallest + DIAGONAL_GAP;
    });
  return (centre) => drops.get(Math.round(centre)) ?? 0;
}

/** A drawn card's box: its top-left corner and its size. */
export interface Placed {
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * Whether a level is drawn top to bottom: more of its wires run DOWN than across. A wire runs down when its target
 * starts below its source's bottom by more than the two cards' horizontal gap (negative when they overlap), so a chain
 * stacked by *Arrange top to bottom* runs down, a left-to-right row and the diagonal's steps (`DIAGONAL_GAP`, less than
 * a column's gap) run across. A wire to a card not given, or from a card to itself, is not counted; a tie is across.
 */
export function runsDown(boxes: Record<string, Placed>, wires: { source: string; target: string }[]): boolean {
  let down = 0;
  let across = 0;
  for (const wire of wires) {
    const source = boxes[wire.source];
    const target = boxes[wire.target];
    if (source === undefined || target === undefined || wire.source === wire.target) continue;
    const below = target.y - (source.y + source.height);
    const beside = Math.max(target.x - (source.x + source.width), source.x - (target.x + target.width));
    if (below > 0 && below > beside) down += 1;
    else across += 1;
  }
  return down > across;
}
