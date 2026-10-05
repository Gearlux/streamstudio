/**
 * "Set source…", the pure half (DESIGN5 step 1, item 5): putting a source into a Stream's `source` slot — or replacing
 * the one there — from the page.
 *
 *     stream (Stream)                               Set the source of stream
 *       ○ source  Source   [set…]        →            [search sources…]  HuggingFaceSource · FilesSource · …
 *       ○ ops[0]  Op                                  then its parameter form (with the folder and HF pickers)
 *                                                     [Set source]  → POST …/slot {node: "stream", slot: "source",
 *                                                                                  class_path, params}
 *
 * A wire drawn into the slot cannot say WHICH object to build (`docs/rewrite.md` §5 — it stays a dashed proposal), so the
 * gesture is a class and its values, written by the server as ONE marker (`source: !class:… {…}`) on the slot's own line;
 * the answer is the graph, with the new node drawn and wired. Three ways in: the slot's `set…` button on the card, the
 * node's right-click menu (Set source…), and a wire dragged out of the slot's handle and dropped on empty canvas.
 *
 * The classes offered are the ones the model's meet rule lets into the slot — every class with an output whose type
 * meets the slot's (`Source`: the sources, and the engines, since a `Stream` IS a source) — the palette's own filter
 * (`steps.paletteMatches`), less the Subgraph class. The form is the class's constructor schema, asked when it is picked
 * (`GET api/palette/<class>`, `api.settingsOf`);
 * only the values the person changed are sent, so every other parameter stays the constructor's default, unspelled.
 */
import type { NodeSpec, NodeSpecWithSettings } from "./api";
import { SUBGRAPH_CLASS } from "./sidebar";
import { paletteMatches, searchMatches } from "./steps";
import type { Json, Model, ModelNode, Param, Socket, SocketType, Supertypes } from "./model";

/** The socket type of a source slot (`document.SOURCE`). */
export const SOURCE_TYPE: SocketType = "Source";

/** A list item socket (`sources[0]`, `ops[1]`): a member of a list, not a slot one marker fills. */
const LIST_ITEM = /\[\d+\]$/;

/** A slot of a node a marker is set into, and what fills it now. */
export interface Slot {
  node: string;
  slot: string;
  type: SocketType;
  /** The node wired into it now (`stream.source`, or the step a `!ref:` names), else `undefined`. */
  filled: string | undefined;
}

/** Is the input `socket` a source slot — one source fills it (`Stream.source`; a `sources[0]` list item is not one). */
export const isSourceSlot = (socket: Pick<Socket, "name" | "type">): boolean => socket.type === SOURCE_TYPE && !LIST_ITEM.test(socket.name);

/** The node's source slots, in the node's order, each with what fills it now. */
export function sourceSlots(model: Model, node: ModelNode): Slot[] {
  return node.inputs
    .filter(isSourceSlot)
    .map((socket) => ({
      node: node.name,
      slot: socket.name,
      type: socket.type,
      filled: model.wires.find((wire) => wire.target === node.name && wire.target_socket === socket.name)?.source,
    }));
}

/** The classes a slot of `type` takes, sources first, then by name — `query` narrowing them as the palette's search does. */
export function slotCandidates(specs: NodeSpec[], type: SocketType, types: Supertypes, query = ""): NodeSpec[] {
  const rank = (spec: NodeSpec) => (spec.kind === "source" ? 0 : 1);
  return specs
    .filter((spec) => spec.class_path !== SUBGRAPH_CLASS && paletteMatches(spec, { type, side: "outputs" }, types) && searchMatches(spec, query))
    .sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name) || a.class_path.localeCompare(b.class_path));
}

const asSchema = (value: Json | undefined): Record<string, Json> | undefined =>
  typeof value === "object" && value !== null && !Array.isArray(value) ? value : undefined;

/**
 * The one word a parameter's socket shows, from its schema fragment — the model's own rule (`document._param_type`): the
 * first type it accepts, `null` skipped; a `$ref` is an `object`; an array is `List[str]` when its items are strings, else
 * an `object`; no type at all is `*`.
 */
export function schemaWord(schema: Record<string, Json>): SocketType {
  const found: SocketType[] = [];
  const visit = (fragment: Record<string, Json>) => {
    if (Array.isArray(fragment.anyOf)) {
      for (const arm of fragment.anyOf) {
        const member = asSchema(arm);
        if (member) visit(member);
      }
      return;
    }
    if (typeof fragment.$ref === "string") return void found.push("object");
    const kind = fragment.type;
    if (kind === undefined) found.push("*");
    else if (kind === "array") found.push(asSchema(fragment.items)?.type === "string" ? "List[str]" : "object");
    else if (kind !== "null") found.push(String(kind));
  };
  visit(schema);
  return found[0] ?? "*";
}

/**
 * The class's constructor parameters as the form's fields, before anything is set: each at its default (`null` when the
 * constructor requires it), its schema the class's, nothing spelled.
 */
export function draftParams(spec: Pick<NodeSpecWithSettings, "params_schema">): Param[] {
  return Object.entries(spec.params_schema).map(([name, value]) => {
    const schema = asSchema(value) ?? {};
    const has = "default" in schema;
    const fallback: Json = has ? (schema.default as Json) : null;
    return {
      name,
      type: schemaWord(schema),
      value: fallback,
      default: fallback,
      doc: typeof schema.description === "string" ? schema.description : "",
      schema,
      spelled: false,
      required: !has,
    };
  });
}

/** The form's fields with the values typed so far: what each widget shows, and what the pickers read. */
export function draftValues(params: Param[], typed: Record<string, Json>): Record<string, Json> {
  return Object.fromEntries(params.map((param) => [param.name, param.name in typed ? typed[param.name] : param.value]));
}

/**
 * What Set sends as `params`: the values the person changed — a typed value equal to the default is NOT sent, so the
 * document spells only what differs from the constructor's own.
 */
export function slotParams(params: Param[], typed: Record<string, Json>): Record<string, Json> {
  const defaults = new Map(params.map((param) => [param.name, JSON.stringify(param.default)]));
  return Object.fromEntries(Object.entries(typed).filter(([name, value]) => defaults.get(name) !== JSON.stringify(value)));
}

/** The required parameters not given yet — Set waits for them (the server would refuse the marker, in its words). */
export function missingRequired(params: Param[], typed: Record<string, Json>): string[] {
  return params.filter((param) => param.required && (typed[param.name] === undefined || typed[param.name] === null || typed[param.name] === "")).map((param) => param.name);
}

/** The dialog's title: `Set the source of stream`; with `replacing`, what it replaces. */
export function slotTitle(slot: Pick<Slot, "node" | "slot">): string {
  return `Set the ${slot.slot} of ${slot.node}`;
}

/**
 * What the form's head says the Set will do: fill the empty slot, replace the object written IN it (`stream.source`), or
 * — when the slot holds a REFERENCE to a node of its own (`source: !ref:files`) — take the reference's place, the node
 * staying in the document, unwired. Measured before: "replaces source (FilesSource)", and after Set the old `source`
 * was still there, drawn unwired.
 */
export function slotEffect(slot: Pick<Slot, "node" | "slot" | "filled">, model: Model, className: string): string {
  if (slot.filled === undefined) return `${className} goes into the empty ${slot.slot} slot.`;
  const filler = model.nodes.find((node) => node.name === slot.filled);
  const what = filler?.class_path ? `${slot.filled} (${filler.class_path.split(".").pop()})` : slot.filled;
  if (slot.filled === `${slot.node}.${slot.slot}`) return `${className} replaces ${what}.`;
  return `${className} goes into ${slot.node}'s ${slot.slot} slot in place of its reference to ${slot.filled}; ${what} stays in the document, unwired.`;
}
