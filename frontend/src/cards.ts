/**
 * Adding a class, the pure half of WHAT the page sends — the user's fix of 2026-10-05.
 *
 * An OP (the catalogue's kind `op` or `algorithm`; a subgraph is made, never added) goes where the insert target says
 * (`oplists.insertTarget`): after a flow step or a list member, or at the end of an op list — and with nowhere to go it
 * is refused in the page's words, nothing sent. Any other class — a value like `ClassNamesScan`, a source, an engine,
 * a sink — is a CARD of its own: the server writes it as a new top-level key named after its class, above the graph's
 * root (`Document.add_node`), so the page sends it whatever the place, and draws the card where the person asked (the
 * right-click, the drop, the wire's release; a click in the Nodes sidebar leaves it to the editor's layout). Measured
 * before, on the files graph a host workspace seeds, Add Node ▸ ClassNamesScan was refused either way:
 *
 *     nothing selected        add recordstream.ops.contract.ClassNamesScan to stream.ops at [1] refused at …:12:7: …
 *     the host card selected  a new flow step needs a name — after '<host>', it has none
 *
 * Both kinds go through `POST …/steps`, which hands a class that is not an op to `add_node`: the SERVER decides by the
 * kind the DOCUMENT derives, never by the catalogue's word — the catalogue lists 35 classes as values that the
 * document derives as ops (`FilterOp`, `WrappedOp`, …), and those keep going after the place sent with them.
 *
 * A wire dragged out of a socket and dropped on empty canvas, then a class picked: an op goes after the node the wire
 * names (its place IS the wire); a card is sent with the end the wire was dragged out of (`draggedWire`), and the server
 * saves the wire to it in the same edit — one undo — or adds nothing and answers why.
 */
import type { DraggedEnd, NewStep } from "./api";
import type { HandleKind } from "./model";
import { stepFields, type InsertTarget } from "./oplists";

/** The catalogue kinds added AFTER a step or INTO an op list (`document._STEP_KINDS`); any other kind is a card. */
export const STEP_KINDS: readonly string[] = ["op", "algorithm", "subgraph"];

/**
 * Whether a class of catalogue kind `kind` is added after a step or into a list. A class the catalogue does not list
 * (`undefined`: a drop the page holds no entry for) takes that way, as every add did before cards.
 */
export const addsAsStep = (kind: string | undefined): boolean => kind === undefined || STEP_KINDS.includes(kind);

/** The end a dropped wire was dragged out of: out of an INPUT the new card feeds it, out of an OUTPUT it goes into the card. */
export function draggedWire(from: { node: string; kind: HandleKind; socket: string }): DraggedEnd {
  return from.kind === "in" ? { target: from.node, target_socket: from.socket } : { source: from.node, source_socket: from.socket };
}

/** The page's words when an op has nowhere to go and the target does not say why. */
export const NOWHERE = "nothing to add after — a step is inserted after another one";

/** The add to send, and whether it makes a CARD (placed by the name the server answers) — or the page's refusal. */
export type AddPlan = { request: NewStep; card: boolean } | { refused: string };

/**
 * What the page sends for an add of `classPath` (catalogue kind `kind`, step name `name` on the level drawn): an op
 * with its place, or refused when it has none; a card with whatever place the page had (the server places only ops by
 * it) and the dropped `wire`.
 */
export function addPlan(kind: string | undefined, classPath: string, name: string, target: InsertTarget, wire?: DraggedEnd): AddPlan {
  const place = stepFields(target);
  const base = { name, class_path: classPath, params: {} };
  if (addsAsStep(kind)) {
    if (place === undefined) return { refused: (target.kind === "none" ? target.why : undefined) ?? NOWHERE };
    return { card: false, request: { ...base, ...place } };
  }
  return { card: true, request: { ...base, ...(place ?? {}), ...(wire === undefined ? {} : { wire }) } };
}
