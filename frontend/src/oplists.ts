/**
 * Op lists, the pure half (DESIGN2 §3 — the user's "yes": adding and removing work on `ops` list members).
 *
 * An OP LIST is a list slot whose members are ops — a `Stream`'s `ops`, a `Pipeline`'s `transforms` (the server's
 * `document._NAMED_SLOTS` entries of socket type `Op` that are lists). The model draws it as its holder node with one
 * `Op` input per item (`ops[0]`, `ops[1]`, …; ONE free `ops[0]` when the list is empty) and one node per member,
 * named by POSITION (`stream.ops[1]`), wired into the holder by a `list` wire. Nothing here is invented: the lists,
 * their members and their item counts are read off those sockets and wires.
 *
 * Where an ADDED node goes (the Nodes sidebar's click or drag, right-click Add Node ▸, the double-click palette, the
 * wire-drop palette) — `insertTarget`:
 *
 *     demo.yaml (a Stream)   stream.ops[1] selected  →  after stream.ops[1]       POST …/steps {after: "stream.ops[1]"}
 *                            nothing selected        →  at the end of stream.ops  POST …/steps {list: "stream.ops"}
 *     a flow                 (unchanged)             →  after the selected step, else after the flow's output
 *
 * Before this, both Stream cases were refused by the server: `'stream.ops[2]' is not a flow step (the steps are: [])`.
 *
 * Members are named by position, so an insert or a remove RENAMES the later ones (`stream.ops[2]` → `stream.ops[3]`).
 * The server re-keys their positions and group members; the page must not diff names to find what was added
 * (`addedMember` says which) nor keep a selection by name across a remove (`shiftsMembers`).
 */
import { levelAnchor } from "./levels";
import type { GraphKind, Model } from "./model";

/** The socket type of an op slot (`document.OP`). */
export const OP_TYPE = "Op";

/** One op list of a level. */
export interface OpList {
  /** The list node a request names: `<holder>.<slot>` — `stream.ops`, `prep.transforms`. */
  name: string;
  /** The node holding the list (`stream`). */
  holder: string;
  /** The slot (`ops`, `transforms`). */
  slot: string;
  /** The members drawn on the level, in list order (`stream.ops[0]`, …) — an item spelled `!ref:x` is no member. */
  members: string[];
  /** How many items the list holds (members and references alike): the index a member appended at the end takes. */
  items: number;
}

/** Where an added node goes. */
export type InsertTarget =
  /** After a flow step, or after a list member (in that member's list). */
  | { kind: "after"; node: string }
  /** At the end of the op list `list` (`stream.ops`). */
  | { kind: "end"; list: string }
  /** Nowhere: the level holds nothing to add after; `why` says so in words when the page knows why. */
  | { kind: "none"; why?: string };

/** `ops[3]` → `{slot: "ops", index: 3}`; a socket that is not a list item → `undefined`. */
function listItem(socket: string): { slot: string; index: number } | undefined {
  const match = /^(.+)\[(\d+)\]$/.exec(socket);
  return match ? { slot: match[1], index: Number(match[2]) } : undefined;
}

/** Every op list on the level, holders in the model's order. */
export function opLists(view: Model): OpList[] {
  const names = new Set(view.nodes.map((node) => node.name));
  const lists: OpList[] = [];
  for (const holder of view.nodes) {
    const slots: string[] = [];
    for (const socket of holder.inputs) {
      const item = listItem(socket.name);
      if (item && socket.type === OP_TYPE && !slots.includes(item.slot)) slots.push(item.slot);
    }
    for (const slot of slots) {
      const name = `${holder.name}.${slot}`;
      const wired = view.wires
        .filter((wire) => wire.target === holder.name && wire.kind === "list" && listItem(wire.target_socket)?.slot === slot)
        .map((wire) => ({ index: listItem(wire.target_socket)?.index ?? 0, source: wire.source }))
        .sort((a, b) => a.index - b.index);
      const members = wired.map(({ source }) => source).filter((source) => source.startsWith(`${name}[`) && names.has(source));
      lists.push({ name, holder: holder.name, slot, members, items: wired.length });
    }
  }
  return lists;
}

/** The op list `node` is a member of — `undefined` for a node that is not one. */
export function memberList(view: Model, node: string): OpList | undefined {
  return opLists(view).find((list) => list.members.includes(node));
}

/**
 * Whether the level is a flow's STEPS — the top of a `flow:` (or of a source graph, a flow with its contract) and
 * the inside of every subgraph — rather than a plain graph whose op lists take the adds (a `Stream` document).
 */
export const levelHasSteps = (level: string, graphKind: GraphKind): boolean => level !== "" || graphKind !== "graph";

/**
 * Where a node added at `level` goes, `selected` being the last selected node (or `undefined`):
 *
 * - on a plain graph (a Stream), a selected member that itself HOLDS one op list (a `Pipeline` in `stream.ops`): the
 *   END of the list it holds (P3 — measured before: the Pipeline selected, a click added after it in `stream.ops`,
 *   although the sidebar said "select … the node holding it, to add to it"; adding after the Pipeline is a wire
 *   dragged out of its record output, `wireTarget`);
 * - a selected LIST MEMBER: after it, in its list — on any level (a flow step's `Pipeline` member too);
 * - a level of flow steps: the flow rule (`levels.levelAnchor`: the selected step, else the flow's output, else the
 *   last step; inside a subgraph the last inner step);
 * - else the level's op list: the one the selected node holds, else the only one — at its END; with several and
 *   nothing saying which, nowhere, in words naming them; with none, the flow rule (the server answers that one).
 */
export function insertTarget(view: Model, level: string, graphKind: GraphKind, selected: string | undefined): InsertTarget {
  const lists = opLists(view);
  const drawn = selected !== undefined && view.nodes.some((node) => node.name === selected) ? selected : undefined;
  const held = lists.filter((list) => list.holder === drawn);
  if (!levelHasSteps(level, graphKind) && held.length === 1) return { kind: "end", list: held[0].name };
  if (drawn !== undefined && lists.some((list) => list.members.includes(drawn))) return { kind: "after", node: drawn };
  const flowRule = (): InsertTarget => {
    const anchor = levelAnchor(view, level, drawn);
    return anchor === undefined ? { kind: "none" } : { kind: "after", node: anchor };
  };
  if (levelHasSteps(level, graphKind) || lists.length === 0) return flowRule();
  if (held.length === 1) return { kind: "end", list: held[0].name };
  if (lists.length === 1) return { kind: "end", list: lists[0].name };
  return {
    kind: "none",
    why:
      `this graph holds ${lists.length} op lists (${lists.map((list) => list.name).join(", ")}) — ` +
      "select a member of one, or the node holding it, to add to it",
  };
}

/**
 * Where the pick of a wire dropped on empty canvas goes, `node` being the node the wire names — the one it was dragged
 * out of, or the one whose record feeds the input it was dragged out of. On a level of flow steps, and for a list
 * member, after `node` as it is (the rule before op lists); on a Stream a node that is no member (its source) names no
 * place in the list, so the pick goes where an add goes (`insertTarget`: the end of the list).
 */
export function wireTarget(view: Model, level: string, graphKind: GraphKind, node: string): InsertTarget {
  if (levelHasSteps(level, graphKind) || memberList(view, node) !== undefined) return { kind: "after", node };
  return insertTarget(view, level, graphKind, node);
}

/**
 * Where the pick of a wire dragged OUT of an op-list INPUT socket (`transforms[k]` of a `Pipeline`, `ops[k]` of a
 * `Stream`) goes (P3): the new member takes that item — after item `k - 1`; the free socket of a list (its end, or the
 * one socket of an empty list) appends. The first item of a list that has one has no place before it (the server
 * inserts after a member or at the end), refused in words. `undefined` for a socket that is no op-list item: the
 * caller's rule applies (a record input: after what feeds it).
 */
export function inputTarget(view: Model, node: string, socket: string): InsertTarget | undefined {
  const item = listItem(socket);
  const list = item === undefined ? undefined : opLists(view).find((each) => each.holder === node && each.slot === item.slot);
  if (item === undefined || list === undefined) return undefined;
  if (item.index >= list.items) return { kind: "end", list: list.name };
  const before = `${list.name}[${item.index - 1}]`;
  if (item.index > 0 && list.members.includes(before)) return { kind: "after", node: before };
  return {
    kind: "none",
    why: `a class is added after a member of ${list.name} or at its end — select ${list.holder} to add at the end`,
  };
}

/** Where it goes, in words: the palette's `added …`, the sidebar's `Click adds …` (`undefined`: nowhere). */
export function insertLabel(target: InsertTarget): string | undefined {
  if (target.kind === "after") return `after ${target.node}`;
  if (target.kind === "end") return `at the end of ${target.list}`;
  return undefined;
}

/** The part of `POST …/steps` that says where: `{after}` for a step or a member, `{list}` to append to a list. */
export function stepFields(target: InsertTarget): { after: string } | { list: string } | undefined {
  if (target.kind === "after") return { after: target.node };
  if (target.kind === "end") return { list: target.list };
  return undefined;
}

/**
 * The name the added MEMBER will have: after `stream.ops[1]` → `stream.ops[2]`; at the end of a 3-item list →
 * `stream.ops[3]`. `undefined` when the add is a flow step (named by its key — read off the answer).
 */
export function addedMember(view: Model, target: InsertTarget): string | undefined {
  if (target.kind === "end") {
    const list = opLists(view).find((candidate) => candidate.name === target.list);
    return list === undefined ? undefined : `${list.name}[${list.items}]`;
  }
  if (target.kind !== "after") return undefined;
  const list = memberList(view, target.node); // a member is named `<list>[<index>]`
  const index = listItem(target.node)?.index;
  return list === undefined || index === undefined ? undefined : `${list.name}[${index + 1}]`;
}

/** The drawn node a CLICKED add is placed next to: the node it goes after; for an append the last member, else the holder. */
export function beside(view: Model, target: InsertTarget): string | undefined {
  if (target.kind === "after") return target.node;
  if (target.kind === "none") return undefined;
  const list = opLists(view).find((candidate) => candidate.name === target.list);
  return list === undefined ? undefined : (list.members[list.members.length - 1] ?? list.holder);
}

/** Whether removing `names` renames other nodes — a member's removal renumbers the members after it. */
export function shiftsMembers(view: Model, names: string[]): boolean {
  const members = new Set(opLists(view).flatMap((list) => list.members));
  return names.some((name) => members.has(name));
}
