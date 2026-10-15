/**
 * The subgraph LEVEL the canvas draws, the pure half (DESIGN §0.2, §2.6, §3 F2).
 *
 * A `recordstream.flow.subgraph.Subgraph` step is drawn as ONE node of kind `subgraph`; its inside is a model of its
 * own, `model.inside[<node name>]` (recursive), derived by the same builder as the top. The canvas draws ONE level at
 * a time: `""` is the document's top, `prep` the inside of the subgraph node `prep`, `prep/inner` the inside of the
 * subgraph `inner` inside it. Inner nodes are QUALIFIED — `prep/grey`, a list member's `stream.ops[1]/grey` — plus the
 * two boundary nodes `prep/<in>` and `prep/<out>` (kind `boundary`), so a level's node names are unique across the
 * whole document and every route takes them as they are: every edit the page sends names the node qualified.
 *
 * What lives here: which model a level shows, the level the canvas falls back to when its subgraph went away (an
 * undo, a rename), the breadcrumb, the qualified name a rename or a new step takes, where a step added at a level goes,
 * and the re-keying a subgraph rename implies for every name inside it. Nothing here draws.
 */
import type { Model, ModelNode, Positions } from "./model";
import { recordPredecessor, stepAnchor } from "./steps";

/** Between a subgraph node's name and the name of a step inside it (`prep/grey`). */
export const SEPARATOR = "/";

/** A level with nothing to draw — what the canvas shows for a level it cannot find. */
export const EMPTY_MODEL: Model = { nodes: [], wires: [], problems: [], outputs: [] };

/** A step's own name: `prep/grey` → `grey`; a top-level name is itself. */
export const shortName = (name: string): string => name.slice(name.lastIndexOf(SEPARATOR) + 1);

/** The level a node sits on: `prep/grey` → `prep`; a top-level name → `""`. */
export const levelOfNode = (name: string): string => {
  const cut = name.lastIndexOf(SEPARATOR);
  return cut < 0 ? "" : name.slice(0, cut);
};

/** A step name on a level: `qualify("prep", "grey")` → `prep/grey`; the top level leaves it bare. */
export const qualify = (level: string, name: string): string => (level === "" ? name : `${level}${SEPARATOR}${name}`);

/** The subgraph nodes a level is inside, from the top down: `prep/inner` → `["prep", "prep/inner"]`. */
export function levelChain(level: string): string[] {
  if (level === "") return [];
  const parts = level.split(SEPARATOR);
  return parts.map((_, index) => parts.slice(0, index + 1).join(SEPARATOR));
}

/** One level up: `prep/inner` → `prep` → `""` (the top stays the top). */
export const parentLevel = (level: string): string => levelOfNode(level);

/**
 * The model the canvas draws at `level`: the document's own at the top, else the inside of that subgraph node —
 * looked up by its qualified name (`inside["prep/inner"]`) or, at a nested level, by the step's own name
 * (`inside["inner"]`), whichever the server keyed it by. `undefined` when the document holds no such subgraph (any
 * more), or when the server sent no insides at all.
 */
export function levelModel(model: Model, level: string): Model | undefined {
  let current: Model | undefined = model;
  for (const name of levelChain(level)) {
    current = current.inside?.[name] ?? current.inside?.[shortName(name)];
    if (current === undefined) return undefined;
  }
  return current;
}

/** `level` when the document holds it, else the deepest level above it that it still holds (at worst the top). */
export function existingLevel(model: Model, level: string): string {
  let candidate = level;
  while (candidate !== "" && levelModel(model, candidate) === undefined) candidate = parentLevel(candidate);
  return candidate;
}

/** One crumb of `demo › prep › inner`: what it says and the level a click on it shows. */
export interface Crumb {
  label: string;
  level: string;
}

/** The breadcrumb above the canvas: the graph, then one crumb per subgraph entered, each by the step's own name. */
export function breadcrumb(graphName: string, level: string): Crumb[] {
  return [{ label: graphName, level: "" }, ...levelChain(level).map((name) => ({ label: shortName(name), level: name }))];
}

/**
 * The name a rename sends: the typed name kept on the node's level (`grey` renamed `mono` inside prep →
 * `prep/mono`). A typed name holding a slash is sent as typed — the server refuses a prefix change in its own words.
 */
export const renamedTo = (node: string, typed: string): string =>
  typed.includes(SEPARATOR) ? typed : qualify(levelOfNode(node), typed);

/**
 * `name` after the node `old` was renamed `next`: the node itself and every name INSIDE it (`prep/grey` when `prep`
 * becomes `pre` → `pre/grey`); a name that merely starts with the same letters (`prep2/grey`) is another node.
 */
export function rekeyName(name: string, old: string, next: string): string {
  if (name === old) return next;
  if (name.startsWith(`${old}${SEPARATOR}`)) return `${next}${name.slice(old.length)}`;
  return name;
}

/**
 * How an undo or a redo renamed op list MEMBERS (`POST …/undo` answers them as `shifts`): `list[i]` with `i >= first`
 * became `list[i + by]`, and a negative `by` took the members `first + by … first - 1` away. A member is named by its
 * POSITION, so this is a rename of every later member at once.
 */
export interface NameShift {
  list: string;
  first: number;
  by: number;
}

const escapeRegExp = (text: string): string => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/**
 * `name` after `shift`: the member (and every name under it — `stream.ops[1]/grey`, `stream.ops[1].source`) moved to
 * its new position, `undefined` when the shift took that member away, anything else as it is.
 */
export function shiftName(name: string, shift: NameShift): string | undefined {
  const found = new RegExp(`^${escapeRegExp(shift.list)}\\[(\\d+)\\](?=$|[./\\[])`).exec(name);
  if (found === null) return name;
  const index = Number(found[1]);
  if (index >= shift.first) return `${shift.list}[${index + shift.by}]${name.slice(found[0].length)}`;
  return shift.first + shift.by <= index ? undefined : name;
}

/**
 * The level to draw after `shifts` (P7): the SAME subgraph under its new name — measured before, Ctrl+Z inside
 * `stream.ops[0]` put a member back before it and the canvas kept the name, showing the OTHER subgraph under the same
 * breadcrumb — or, when its member went away, the level holding the list.
 */
export function followShifts(level: string, shifts: NameShift[]): string {
  let current = level;
  for (const shift of shifts) {
    const moved = shiftName(current, shift);
    if (moved === undefined) return levelOfNode(shift.list);
    current = moved;
  }
  return current;
}

/** A map the page holds by node name (a card's measured size), re-keyed through `shift`; a name gone is dropped. */
export function shiftKeys<T>(held: Record<string, T>, shift: NameShift): Record<string, T> {
  const out: Record<string, T> = {};
  for (const [name, value] of Object.entries(held)) {
    const moved = shiftName(name, shift);
    if (moved !== undefined) out[moved] = value;
  }
  return out;
}

/** Every node of every level, the top's first, each inside after its level's own nodes. */
export function allNodeNames(model: Model): string[] {
  const names = model.nodes.map((node) => node.name);
  for (const inside of Object.values(model.inside ?? {})) names.push(...allNodeNames(inside));
  return names;
}

/** The node named `name` on any level. */
export function findNode(model: Model, name: string): ModelNode | undefined {
  const own = model.nodes.find((node) => node.name === name);
  if (own !== undefined) return own;
  for (const inside of Object.values(model.inside ?? {})) {
    const found = findNode(inside, name);
    if (found !== undefined) return found;
  }
  return undefined;
}

/** A subgraph's `<in>` / `<out>`: where the record enters and leaves it — drawn, never a step an edit acts on. */
export const isBoundary = (node: ModelNode): boolean => node.kind === "boundary";

/** The host card (`<host>`): what a host hands the graph — drawn, never written, so no edit renames or removes it. */
export const isHostCard = (node: Pick<ModelNode, "kind">): boolean => node.kind === "host";

/**
 * `names` less the host card, in their own order — what Delete removes of a selection: with the host card in it, Delete
 * acts on the document cards and leaves the host card where it is (the user's rule, 2026-10-05).
 */
export function withoutHost(view: Pick<Model, "nodes">, names: readonly string[]): string[] {
  const hosts = new Set(view.nodes.filter(isHostCard).map((node) => node.name));
  return names.filter((name) => !hosts.has(name));
}

/** The level's nodes a gesture acts on, in the model's order, boundaries left out — only those in `names`, if given. */
export function stepNames(view: Model, names?: Iterable<string>): string[] {
  const wanted = names === undefined ? undefined : new Set(names);
  return view.nodes.filter((node) => !isBoundary(node) && (wanted === undefined || wanted.has(node.name))).map((node) => node.name);
}

/**
 * The node a step added at `level` goes after — a class from the sidebar or the palette, a paste, a template, a steps
 * file. At the top it is the page's usual rule (`stepAnchor`: the selection, else the flow's output, else the last
 * step); inside a subgraph it is the selected inner node, else the last inner step — never `<out>`, never a node of
 * another level. `undefined` when the level holds no step.
 */
export function levelAnchor(view: Model, level: string, selected: string | undefined): string | undefined {
  if (level === "") return stepAnchor({ model: view }, selected);
  const steps = stepNames(view);
  if (selected !== undefined && steps.includes(selected)) return selected;
  return steps[steps.length - 1];
}

/**
 * The node whose record `node` reads, on its level — where a step dropped from one of `node`'s inputs is inserted
 * after. Inside a subgraph the head step reads `<in>`, which is not a step to insert after: `undefined`.
 */
export function levelPredecessor(view: Model, node: string): string | undefined {
  const source = recordPredecessor({ model: view }, node);
  const found = source === undefined ? undefined : view.nodes.find((candidate) => candidate.name === source);
  return found !== undefined && isBoundary(found) ? undefined : source;
}

/** The steps inside the subgraph node `node`, by their own names — what its card lists. */
export function innerSteps(model: Model, node: string): string[] {
  const inside = levelModel(model, node);
  return inside === undefined ? [] : stepNames(inside).map(shortName);
}

/** The canvas's snap grid, and the step between two cards placed side by side (a card's min-width and a gap). */
const GRID = 10;
const STEP = 280;
/** The row a level with no placed node starts on (the top's first layout row). */
const FIRST_ROW = 40;

/**
 * `positions` with a place for every node of the level: the sidecar's where it has one; a node it does not place
 * goes to the right of the placed ones, in the model's order, one card apart on the grid. The SAME map comes back
 * when every node is placed, so a redraw is not triggered for nothing. (The server lays out each inside on its own;
 * this only covers a node it has not placed yet.)
 */
export function levelPositions(view: Model, positions: Positions): Positions {
  const missing = view.nodes.filter((node) => positions[node.name] === undefined);
  if (missing.length === 0) return positions;
  const placed = view.nodes.map((node) => positions[node.name]).filter((xy): xy is [number, number] => xy !== undefined);
  const x0 = placed.length === 0 ? 0 : Math.ceil((Math.max(...placed.map(([x]) => x)) + STEP) / GRID) * GRID;
  const y = placed.length === 0 ? FIRST_ROW : Math.round(Math.min(...placed.map(([, top]) => top)) / GRID) * GRID;
  const filled: Positions = { ...positions };
  missing.forEach((node, index) => {
    filled[node.name] = [x0 + index * STEP, y];
  });
  return filled;
}

/** The query parameter a link names the level with: `?graph=blobs&level=prep` opens the inside of `prep`. */
export const LEVEL_PARAM = "level";

/** The level a link asks for (`?level=prep`); none is the top, `""`. */
export const levelFromSearch = (search: string): string => new URLSearchParams(search).get(LEVEL_PARAM) ?? "";

/**
 * `href` naming the graph and the level the canvas shows — kept in step with the canvas like `?graph=`, so a reload or
 * a shared link opens the same level. The top drops the parameter.
 */
export function levelHref(href: string, graph: string, level: string): string {
  const url = new URL(href);
  url.searchParams.set("graph", graph);
  if (level === "") url.searchParams.delete(LEVEL_PARAM);
  else url.searchParams.set(LEVEL_PARAM, level);
  return url.toString();
}
