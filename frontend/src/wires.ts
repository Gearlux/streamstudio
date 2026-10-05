/**
 * A wire drawn on the canvas, the pure half: what the page sends for it and what it does with the answer.
 *
 * A wire is SAVED by the server, in the document's own spelling (`Document.add_wire`): a root's output slot as
 * `delivered: {classes: !ref:source}`, any other card's slot as `<slot>: !ref:<card>`, a source's record entry into the
 * root's as `delivered: {input: image}`. The page only names the two ends (`POST …/wires`) and draws the graph the server
 * answers — a card moved out of another card's slot comes back under its new name. A wire the editor cannot save yet
 * is refused by the server in ONE plain line, shown as a warning, and nothing is drawn: the page never draws an edge of
 * its own, so the canvas never shows a wire the document does not hold.
 *
 * Delete on selected wires sends them all in one `POST …/wires/remove` — one edit, one undo. Which edges are selected is
 * React Flow's, read at the gesture (`App.selectedWiresNow`); the DOM half is measured in `tests/test_canvas.py`.
 */
import { errorMessage } from "./api";
import type { Graph } from "./model";
import { parseHandle } from "./model";

/** One wire by its two ends, as the routes name it: an output of one card into an input of another. */
export interface WireEnds {
  source: string;
  source_socket: string;
  target: string;
  target_socket: string;
}

/** What a connection or an edge carries: two cards and the handle on each (`out:<socket>`, `in:<socket>`). */
export interface Connected {
  source: string;
  sourceHandle?: string | null;
  target: string;
  targetHandle?: string | null;
}

/** The ends of a drawn connection (or an edge) — `undefined` when its handles are not an output and an input. */
export function connectionEnds(connection: Connected): WireEnds | undefined {
  const from = parseHandle(connection.sourceHandle);
  const into = parseHandle(connection.targetHandle);
  if (from?.kind !== "out" || into?.kind !== "in") return undefined;
  return { source: connection.source, source_socket: from.socket, target: connection.target, target_socket: into.socket };
}

/** The ends of every SELECTED edge, in the canvas's order. */
export function selectedWires(edges: Array<Connected & { selected?: boolean }>): WireEnds[] {
  return edges.flatMap((edge) => {
    const ends = edge.selected ? connectionEnds(edge) : undefined;
    return ends === undefined ? [] : [ends];
  });
}

/** What drawing a wire needs from the app: the route, the drawing of an answer, the warning of a refusal. */
export interface DrawActions {
  add: (wire: WireEnds) => Promise<Graph>;
  accept: (graph: Graph) => void;
  refuse: (message: string) => void;
}

/** Send a drawn wire; draw the answer, or show the server's line. `true` when the server saved it. */
export async function drawWire(wire: WireEnds | undefined, actions: DrawActions): Promise<boolean> {
  if (wire === undefined) return false;
  try {
    actions.accept(await actions.add(wire));
    return true;
  } catch (exc) {
    actions.refuse(errorMessage(exc));
    return false;
  }
}

/** What removing wires needs from the app. */
export interface RemoveActions {
  remove: (wires: WireEnds[]) => Promise<Graph>;
  accept: (graph: Graph) => void;
  refuse: (message: string) => void;
}

/** Send every selected wire in ONE request; draw the answer, or show the server's line. `true` when they went. */
export async function deleteWires(wires: WireEnds[], actions: RemoveActions): Promise<boolean> {
  if (wires.length === 0) return false;
  try {
    actions.accept(await actions.remove(wires));
    return true;
  } catch (exc) {
    actions.refuse(errorMessage(exc));
    return false;
  }
}
