/**
 * The document model as the routes serve it — a mirror of `streamstudio/document.py`'s JSON (`app._model_dict`)
 * — and the two things the canvas derives from it: React Flow's nodes/edges and the socket-type meet rule.
 *
 * Nothing here is invented on the page: node names, socket names and types, wires and problems are the
 * document's own, so the canvas is a DRAWING of the YAML (docs/rewrite.md §0). The one rule the page needs
 * to apply by itself — may this output be wired into that input? — is `document.py`'s `_meets`, and the
 * half of it the page cannot compute (`issubclass` between registered items, `Stream` into `Source`) is asked
 * from the server once (`GET api/types`) and applied by `compatible`.
 */
import type { Edge, Node } from "@xyflow/react";
import type { Group } from "./groups";

export const NODE_KINDS = [
  "source", "op", "algorithm", "engine", "sink", "runnable", "value", "contract", "host", "alias",
] as const;
export type NodeKind = (typeof NODE_KINDS)[number];

export const WIRE_KINDS = ["record", "entry", "gate", "bind", "ref", "delivered", "list"] as const;
export type WireKind = (typeof WIRE_KINDS)[number];

export const GRAPH_KINDS = ["flow", "graph", "contract"] as const;
export type GraphKind = (typeof GRAPH_KINDS)[number];

/** A socket type: a registered item class name, a graph slot kind, a JSON-schema scalar word, or `*`. */
export type SocketType = string;

/** A JSON value — what a parameter's `value` / `default` and a schema fragment hold. */
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

export interface Socket {
  name: string;
  type: SocketType;
  required: boolean;
  doc: string;
  entry: string;
}

export interface Param {
  name: string;
  type: SocketType; // the JSON-schema word (`number`, `integer`, `string`, `boolean`) or `List[str]` / `object`
  value: Json;
  default: Json;
  doc: string;
  schema: Record<string, Json>;
  spelled: boolean;
  required: boolean;
}

export interface ModelNode {
  name: string;
  class_path: string;
  kind: NodeKind;
  params: Record<string, Param>;
  inputs: Socket[];
  outputs: Socket[];
  loc: string;
  source_file: string;
  note: string;
  alias_of: string;
}

export interface Wire {
  source: string;
  source_socket: string;
  target: string;
  target_socket: string;
  kind: WireKind;
  type: SocketType;
}

export interface Problem {
  message: string;
  loc: string;
}

export interface Model {
  nodes: ModelNode[];
  wires: Wire[];
  problems: Problem[];
  outputs: string[];
}

/** `{node name: [x, y]}` — the sidecar's shape. */
export type Positions = Record<string, [number, number]>;

export interface GraphSummary {
  name: string;
  path: string;
  kind: GraphKind;
}

export interface Graph extends GraphSummary {
  text: string;
  model: Model;
  positions: Positions;
  /** The sidecar's `groups:` block — boxes drawn around nodes, never part of the document (`groups.ts`). */
  groups: Group[];
  history_depth: number;
}

/** `GET api/graphs/<name>/form`: per node, the JSON-schema `properties` of its constructor. */
export type Form = Record<string, Record<string, Record<string, Json>>>;

/** `GET api/types`: per socket type, the types it may be wired INTO (its registered ancestors / declared supertypes). */
export type Supertypes = Record<SocketType, SocketType[]>;

// ---------------------------------------------------------------------------------------------------
// the meet rule
// ---------------------------------------------------------------------------------------------------

export const ANY: SocketType = "*";

/**
 * `document.py`'s `_meets(source, target)`, with the server's table standing in for `issubclass`:
 * identical types meet, the wildcard meets everything, and a type meets each of its listed supertypes.
 */
export function compatible(source: SocketType, target: SocketType, types: Supertypes): boolean {
  if (source === target || source === ANY || target === ANY) return true;
  return (types[source] ?? []).includes(target);
}

// ---------------------------------------------------------------------------------------------------
// handles: `in:<socket>` on the left, `out:<socket>` on the right — the prototype's convention
// ---------------------------------------------------------------------------------------------------

export type HandleKind = "in" | "out";

export const handleId = (kind: HandleKind, socket: string): string => `${kind}:${socket}`;

export function parseHandle(id: string | null | undefined): { kind: HandleKind; socket: string } | undefined {
  if (!id) return undefined;
  const colon = id.indexOf(":");
  if (colon < 0) return undefined;
  const kind = id.slice(0, colon);
  if (kind !== "in" && kind !== "out") return undefined;
  return { kind, socket: id.slice(colon + 1) };
}

/** The type of a node's socket by handle id: an input, an output, or — for `in:` — a constructor parameter (a `bind:` target). */
export function socketType(node: ModelNode, handle: string | null | undefined): SocketType | undefined {
  const parsed = parseHandle(handle);
  if (!parsed) return undefined;
  if (parsed.kind === "out") return node.outputs.find((s) => s.name === parsed.socket)?.type;
  return node.inputs.find((s) => s.name === parsed.socket)?.type ?? node.params[parsed.socket]?.type;
}

// ---------------------------------------------------------------------------------------------------
// React Flow arrays
// ---------------------------------------------------------------------------------------------------

/** A document node on the canvas — every class through the ONE component (`canvas/GenericNode.tsx`). */
export type GenericNode = Node<{ node: ModelNode }, "generic">;

/** A group's box: the sidecar's group and the members it draws (a member the model no longer has is left out). */
export type GroupNode = Node<{ group: Group; members: string[] }, "group">;

export type CanvasNode = GenericNode | GroupNode;

/** What an edge carries: a document wire's kind and type, or `proposal` for a wire drawn but not written. */
export interface EdgeData extends Record<string, unknown> {
  kind: WireKind | "proposal";
  type: SocketType;
}
export type CanvasEdge = Edge<EdgeData>;

/** The document's nodes and wires as React Flow's arrays; the node id IS the document's node name. */
export function modelToFlow(model: Model, positions: Positions): { nodes: GenericNode[]; edges: CanvasEdge[] } {
  const nodes: GenericNode[] = model.nodes.map((node) => {
    const [x, y] = positions[node.name] ?? [0, 0];
    return { id: node.name, type: "generic", position: { x, y }, data: { node } };
  });
  const edges: CanvasEdge[] = model.wires.map((wire, index) => ({
    id: `w${index}`,
    source: wire.source,
    sourceHandle: handleId("out", wire.source_socket),
    target: wire.target,
    targetHandle: handleId("in", wire.target_socket),
    className: `wire wire-${wire.kind}`,
    data: { kind: wire.kind, type: wire.type },
  }));
  return { nodes, edges };
}

// ---------------------------------------------------------------------------------------------------
// colours: fixed for the structural types, hashed (stable) for item types the page has never seen
// ---------------------------------------------------------------------------------------------------

const FIXED_COLORS: Record<string, string> = {
  Record: "#ffd54f",
  Stream: "#4db6ac",
  Source: "#26a69a",
  Op: "#7986cb",
  Sink: "#a1887f",
  "List[str]": "#90a4ae",
  number: "#90a4ae",
  integer: "#90a4ae",
  string: "#b0bec5",
  boolean: "#b0bec5",
  object: "#78909c",
  "*": "#eeeeee",
};

export function socketColor(type: SocketType): string {
  const fixed = FIXED_COLORS[type];
  if (fixed) return fixed;
  let hash = 0;
  for (const char of type) hash = (hash * 31 + char.charCodeAt(0)) | 0;
  return `hsl(${Math.abs(hash) % 360} 70% 62%)`;
}

/** The class short name — `recordstream.ops.numpy.Threshold` → `Threshold`; blank for a synthetic node. */
export const shortClass = (classPath: string): string => classPath.split(".").pop() ?? "";
