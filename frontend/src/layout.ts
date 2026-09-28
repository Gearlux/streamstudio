/**
 * The client-side "arrange": dagre over the node boxes React Flow has MEASURED, ranked left to right along the
 * wires — a pipeline is a one-direction chain with a few forks, which is exactly dagre's model (no ELK needed).
 *
 * Why on the client when the server has a layout too (`streamstudio/layout.py`, `POST …/layout`): the server
 * lays out on a fixed grid (a column per layer, a row per node) because it cannot know how tall a card is; the
 * page can, so dagre spaces the cards by their real sizes. Both are offered, the client's by default. The result
 * is written like any drag — `PUT …/positions` — so the sidecar is the one place a layout lives.
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

/** A top-left corner per box, ranked along the wires (`LR` by default); a wire to an unknown box is ignored. */
export function arrange(
  boxes: Box[],
  wires: { source: string; target: string }[],
  rankdir: "LR" | "TB" = "LR",
): Record<string, { x: number; y: number }> {
  const graph = new dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}));
  graph.setGraph({ rankdir, nodesep: 40, ranksep: 80 });
  const known = new Set(boxes.map((box) => box.id));
  boxes.forEach((box) => graph.setNode(box.id, { width: box.width, height: box.height }));
  wires.forEach((wire) => {
    if (known.has(wire.source) && known.has(wire.target) && wire.source !== wire.target) graph.setEdge(wire.source, wire.target);
  });
  dagre.layout(graph);
  return Object.fromEntries(
    boxes.map((box) => {
      const placed = graph.node(box.id);
      return [box.id, { x: snap(placed.x - box.width / 2), y: snap(placed.y - box.height / 2) }];
    }),
  );
}
