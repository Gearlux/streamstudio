/**
 * Adding a node, the pure half: which classes the palette lists (every one, or — for a wire dropped on empty
 * canvas — the ones that can take or give the dragged type), what the new step is named, and which node it is
 * inserted after. The insert itself is the server's `POST …/steps` (`Document.add_step`): a flow step spelled
 * `from: <anchor>`, the step that read the anchor re-pointed at the new one. That is also how a pick from a
 * dropped wire is WIRED — the entry wires follow from the order and the classes' declarations, as for every
 * other step. On a `Stream` the pick becomes a member of its `ops:` list instead — `oplists.ts` decides where.
 */
import type { NodeSpec } from "./api";
import { compatible, type Graph, type Model, type ModelNode, type SocketType, type Supertypes } from "./model";

/** The most rows the palette draws; the catalogue is hundreds of classes, and typing narrows it. */
export const PALETTE_ROWS = 200;

/**
 * A wire dropped on empty canvas: its type, and the side of the classes to list — `inputs` for a wire dragged
 * OUT of an output (the new node must take the type), `outputs` for one dragged out of an input (it must give it).
 */
export interface PaletteFilter {
  type: SocketType;
  side: "inputs" | "outputs";
}

/** May `spec` be wired to the dropped wire? — the model's meet rule, in the wire's direction. */
export function paletteMatches(spec: NodeSpec, filter: PaletteFilter | undefined, types: Supertypes): boolean {
  if (!filter) return true;
  if (filter.side === "inputs") return spec.inputs.some((socket) => compatible(filter.type, socket.type, types));
  return spec.outputs.some((socket) => compatible(socket.type, filter.type, types));
}

/** The palette's text filter: a case-insensitive substring of the class path or of the class name. */
export function searchMatches(spec: Pick<NodeSpec, "class_path" | "name">, query: string): boolean {
  const needle = query.trim().toLowerCase();
  return needle === "" || spec.class_path.toLowerCase().includes(needle) || spec.name.toLowerCase().includes(needle);
}

/** `base` when it is free, else `base2`, `base3`, … — a new subgraph's or template's suggested name. */
export function uniqueName(base: string, taken: string[]): string {
  const used = new Set(taken);
  if (!used.has(base)) return base;
  let n = 2;
  while (used.has(`${base}${n}`)) n += 1;
  return `${base}${n}`;
}

/** A new step's key: the class short name in lower case, numbered from 2 when the name is taken. */
export function stepName(classPath: string, taken: string[]): string {
  return uniqueName((classPath.split(".").pop() ?? classPath).toLowerCase(), taken);
}

/** The canvas's snap grid. */
const GRID = 10;
/** The room kept around a card a new node would otherwise touch. */
const GAP = 20;

/**
 * Where a node added by a click (the Nodes sidebar) lands: `start` on the grid, stepped DOWN until the card no
 * longer covers any drawn card (with a little room) — so it appears next to the node it follows, never on top of one.
 */
export function freeSpot(
  start: { x: number; y: number },
  size: { width: number; height: number },
  cards: { x: number; y: number; width: number; height: number }[],
): { x: number; y: number } {
  const x = Math.round(start.x / GRID) * GRID;
  let y = Math.round(start.y / GRID) * GRID;
  const covers = (top: number) =>
    cards.some(
      (card) =>
        x < card.x + card.width + GAP &&
        card.x < x + size.width + GAP &&
        top < card.y + card.height + GAP &&
        card.y < top + size.height + GAP,
    );
  for (let tries = 0; tries < 500 && covers(y); tries++) y += GRID;
  return { x, y };
}

/**
 * Where a node added by a click lands: below the card it follows (`anchor`), a little to the right, clear of every
 * card in `cards` — which must be where the cards are AFTER the add (P8): the server lays the later members of a list
 * out again when one is inserted before them, and measured before, a spot found against the old places put the new
 * card on top of the member that had moved down.
 */
export function spotBeside(
  anchor: { x: number; y: number; width: number; height: number },
  cards: { x: number; y: number; width: number; height: number }[],
  size: { width: number; height: number },
): { x: number; y: number } {
  return freeSpot({ x: anchor.x + 60, y: anchor.y + anchor.height + 40 }, size, cards);
}

/**
 * Whether a node can be what an added step goes after: never the host's side (`<host>` — drawn, never written) nor the
 * graph's root (`contract` — what the graph delivers, no step). The user's rule (2026-10-05): measured before, Add Node
 * with the host card selected sent `{after: "<host>"}` and the server answered `a new flow step needs a name`.
 */
export const isAnchor = (node: Pick<ModelNode, "kind">): boolean => node.kind !== "host" && node.kind !== "contract";

/**
 * The node an add goes after, read off a selection: its LAST node the level draws that can be one (`isAnchor`) — so a
 * selection holding only the host card or the root counts as none, and a mixed one acts on its document card.
 */
export function selectedAnchor(view: Pick<Model, "nodes">, selected: readonly string[]): string | undefined {
  for (let index = selected.length - 1; index >= 0; index--) {
    const found = view.nodes.find((node) => node.name === selected[index]);
    if (found !== undefined && isAnchor(found)) return found.name;
  }
  return undefined;
}

/**
 * The node a picked (or pasted) class goes after: the selected node when the model has it and it can be one
 * (`isAnchor`), else the flow's output, else the last node the document spells (a `Stream`'s last op) — never the
 * host's side or the contract root, which are not steps. `undefined` when there is nothing to go after.
 */
export function stepAnchor(graph: Pick<Graph, "model">, selected: string | undefined): string | undefined {
  const names = graph.model.nodes.map((node) => node.name);
  const chosen = graph.model.nodes.find((node) => node.name === selected);
  if (chosen !== undefined && isAnchor(chosen)) return chosen.name;
  const output = graph.model.outputs.find((name) => names.includes(name));
  if (output !== undefined) return output;
  const spelled = graph.model.nodes.filter(isAnchor);
  return spelled.length > 0 ? spelled[spelled.length - 1].name : undefined;
}

/** The node whose record `node` reads — where a step dropped from one of `node`'s INPUTS is inserted after. */
export function recordPredecessor(graph: Pick<Graph, "model">, node: string): string | undefined {
  return graph.model.wires.find((wire) => wire.target === node && wire.kind === "record")?.source;
}
