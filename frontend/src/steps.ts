/**
 * Adding a node, the pure half: which classes the palette lists (every one, or — for a wire dropped on empty
 * canvas — the ones that can take or give the dragged type), what the new step is named, and which node it is
 * inserted after. The insert itself is the server's `POST …/steps` (`Document.add_step`): a flow step spelled
 * `from: <anchor>`, the step that read the anchor re-pointed at the new one. That is also how a pick from a
 * dropped wire is WIRED — the entry wires follow from the order and the classes' declarations, as for every
 * other step — and why a document with no step list (a `Stream`'s `ops:`) refuses the pick in the server's words.
 */
import type { NodeSpec } from "./api";
import { compatible, type Graph, type SocketType, type Supertypes } from "./model";

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

/** A new step's key: the class short name in lower case, numbered from 2 when the name is taken. */
export function stepName(classPath: string, taken: string[]): string {
  const base = (classPath.split(".").pop() ?? classPath).toLowerCase();
  const used = new Set(taken);
  if (!used.has(base)) return base;
  let n = 2;
  while (used.has(`${base}${n}`)) n += 1;
  return `${base}${n}`;
}

/**
 * The node a picked (or pasted) class goes after: the selected node when the model has it, else the flow's
 * output, else the last node the document spells (a `Stream`'s last op) — never the host's side or the
 * contract root, which are not steps. `undefined` when there is nothing to go after.
 */
export function stepAnchor(graph: Graph, selected: string | undefined): string | undefined {
  const names = graph.model.nodes.map((node) => node.name);
  if (selected !== undefined && names.includes(selected)) return selected;
  const output = graph.model.outputs.find((name) => names.includes(name));
  if (output !== undefined) return output;
  const spelled = graph.model.nodes.filter((node) => node.kind !== "host" && node.kind !== "contract");
  return spelled.length > 0 ? spelled[spelled.length - 1].name : undefined;
}

/** The node whose record `node` reads — where a step dropped from one of `node`'s INPUTS is inserted after. */
export function recordPredecessor(graph: Graph, node: string): string | undefined {
  return graph.model.wires.find((wire) => wire.target === node && wire.kind === "record")?.source;
}
