/**
 * The Nodes sidebar's pure half: the catalogue `GET api/palette` serves (every `@configurable` class the server can
 * describe) grouped by PACKAGE — the first segment of the class path, the thing a person knows a class by — then by
 * KIND, in the order a pipeline is read (sources, ops, … values, contracts), each non-empty section only. The search
 * narrows by every typed word, the chips say what an entry reads and writes, and the parameter rows are what its
 * hover card lists. `panels/NodesSidebar.tsx` draws it; `menu/AddNodeMenu.tsx` reuses the kind order and the search.
 */
import type { NodeSpec, NodeSpecWithSettings } from "./api";
import type { Json } from "./model";

/** The kinds the palette serves (`streamstudio/palette.py::NodeKind`, less the document-only ones), in reading order. */
export const KIND_ORDER = ["source", "op", "algorithm", "engine", "sink", "runnable", "value", "contract"] as const;

const KIND_LABELS: Record<string, string> = {
  source: "Sources",
  op: "Ops",
  algorithm: "Algorithms",
  engine: "Engines",
  sink: "Sinks",
  runnable: "Runnables",
  value: "Values",
  contract: "Contracts",
};

/** A kind's section heading; a kind the page does not know is shown capitalised. */
export const kindLabel = (kind: string): string => KIND_LABELS[kind] ?? kind.charAt(0).toUpperCase() + kind.slice(1);

/**
 * The class a document spells a subgraph step with (`streamstudio/document.py::SUBGRAPH_CLASS`). It is NOT a class to
 * add: an empty one is refused (`Subgraph: steps is empty`), and a subgraph is MADE from steps — Graph ▸ Convert
 * selection to subgraph, Insert template ▸, Insert steps file… — so no list the page offers holds it.
 */
export const SUBGRAPH_CLASS = "recordstream.flow.subgraph.Subgraph";

/** The classes the Nodes sidebar, the palette and Add Node ▸ offer: the catalogue less `SUBGRAPH_CLASS`. */
export function addableSpecs(specs: NodeSpec[]): NodeSpec[] {
  return specs.some((spec) => spec.class_path === SUBGRAPH_CLASS) ? specs.filter((spec) => spec.class_path !== SUBGRAPH_CLASS) : specs;
}

/** The package a class comes from: the first segment of its path. */
export const packageOf = (classPath: string): string => classPath.split(".")[0];

export interface KindSection {
  kind: string;
  label: string;
  specs: NodeSpec[];
}

export interface PackageSection {
  name: string;
  count: number;
  kinds: KindSection[];
}

const kindRank = (kind: string): number => {
  const index = (KIND_ORDER as readonly string[]).indexOf(kind);
  return index < 0 ? KIND_ORDER.length : index;
};

/** Packages alphabetically; inside each, the kinds in `KIND_ORDER` (an unknown kind after, alphabetically); entries by name. */
export function groupSpecs(specs: NodeSpec[]): PackageSection[] {
  const byPackage = new Map<string, Map<string, NodeSpec[]>>();
  for (const spec of specs) {
    const kinds = byPackage.get(packageOf(spec.class_path)) ?? new Map<string, NodeSpec[]>();
    byPackage.set(packageOf(spec.class_path), kinds);
    kinds.set(spec.kind, [...(kinds.get(spec.kind) ?? []), spec]);
  }
  return [...byPackage.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([name, kinds]) => {
      const sections = [...kinds.entries()]
        .sort(([a], [b]) => kindRank(a) - kindRank(b) || a.localeCompare(b))
        .map(([kind, entries]) => ({
          kind,
          label: kindLabel(kind),
          specs: [...entries].sort((a, b) => a.name.localeCompare(b.name) || a.class_path.localeCompare(b.class_path)),
        }));
      return { name, count: sections.reduce((total, section) => total + section.specs.length, 0), kinds: sections };
    });
}

/** Every whitespace-separated word of `query` appears in the name, the class path or the description (any case). */
export function sidebarMatches(spec: Pick<NodeSpec, "name" | "class_path" | "doc">, query: string): boolean {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return true;
  const haystack = `${spec.name}\n${spec.class_path}\n${spec.doc}`.toLowerCase();
  return words.every((word) => haystack.includes(word));
}

export interface Chip {
  name: string;
  type: string;
}

const isRecord = (socket: Chip): boolean => socket.name === "record" && socket.type === "Record";
const isPlumbing = (socket: Chip): boolean =>
  isRecord(socket) || (socket.name === "op" && socket.type === "Op") || (socket.name === "object" && socket.type === "object");

/**
 * What an entry reads and writes, as chips: its declared inputs and outputs, less the plumbing every op has (the
 * record it passes on, itself as an op, a value's built object) — unless the record is ALL it declares, so a plain
 * chain op still shows that it takes and gives a record.
 */
export function chips(spec: Pick<NodeSpec, "inputs" | "outputs">): { reads: Chip[]; writes: Chip[] } {
  const strip = (sockets: Chip[], plumbing: (socket: Chip) => boolean): Chip[] => {
    const kept = sockets.filter((socket) => !plumbing(socket)).map(({ name, type }) => ({ name, type }));
    if (kept.length > 0) return kept;
    const record = sockets.find(isRecord);
    return record ? [{ name: record.name, type: record.type }] : [];
  };
  return { reads: strip(spec.inputs, isRecord), writes: strip(spec.outputs, isPlumbing) };
}

export interface ParamRow {
  name: string;
  /** The JSON-schema type in a word: `number`, `number?` (nullable), `a | b` (an enum), `list[string]`, a `$ref` name. */
  type: string;
  /** The default as JSON, blank when the constructor requires the value. */
  default: string;
  doc: string;
  required: boolean;
}

type Schema = Record<string, Json>;

const asSchema = (value: Json | undefined): Schema | undefined =>
  typeof value === "object" && value !== null && !Array.isArray(value) ? value : undefined;

function typeWord(schema: Schema): string {
  if (Array.isArray(schema.enum)) return schema.enum.map((option) => (typeof option === "string" ? option : JSON.stringify(option))).join(" | ");
  if (typeof schema.$ref === "string") return schema.$ref.split("/").pop() ?? schema.$ref;
  if (Array.isArray(schema.anyOf)) {
    const arms = schema.anyOf.map(asSchema).filter((arm): arm is Schema => arm !== undefined);
    const real = arms.filter((arm) => arm.type !== "null");
    const word = real.map(typeWord).join(" | ") || "null";
    return real.length < arms.length ? `${word}?` : word;
  }
  if (schema.type === "array") {
    const items = asSchema(schema.items);
    return `list[${items ? typeWord(items) : "any"}]`;
  }
  return typeof schema.type === "string" ? schema.type : "any";
}

/** The constructor's parameters from `params_schema` (pydantic's `properties`), in their declared order. */
export function paramRows(spec: Pick<NodeSpecWithSettings, "params_schema">): ParamRow[] {
  return Object.entries(spec.params_schema).map(([name, value]) => {
    const schema = asSchema(value) ?? {};
    const has = "default" in schema;
    return {
      name,
      type: typeWord(schema),
      default: has ? JSON.stringify(schema.default) : "",
      doc: typeof schema.description === "string" ? schema.description : "",
      required: !has,
    };
  });
}
