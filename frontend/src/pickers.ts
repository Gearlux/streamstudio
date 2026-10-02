/**
 * The parameter pickers, the pure half — the folder and file choosers and the HuggingFace lists the ComfyUI editor gave a
 * node's fields, ported to the ONE widget every surface draws (`canvas/ParamWidget.tsx`: a canvas card, the settings
 * tab, the rerun form, set source…'s form).
 *
 * WHICH field gets which picker is the server's to say: the palette marks the parameter's JSON-schema fragment with
 * `picker` (`GET …/form`, `GET api/palette`), by the ComfyUI editor's rules —
 *
 *     directory     a string named …path / …dir / …root / …folder, or a `pathlib.Path`   the file browser, folder mode
 *     file          a string named …file / …checkpoint / …ckpt (no dir word)            the file browser, any file
 *     hf_dataset    a HuggingFace* class's path / repo_id                               the datasets cached on the
 *                                                                                       editor's machine
 *     hf_feature    its input_feature / target_feature                                  the dataset's columns
 *     hf_split      its split                                                           the dataset's splits
 *     hf_metadata   its metadata_features                                               "add column": `*` (every
 *                                                                                       column but input/target) or
 *                                                                                       a column, appended
 *
 * — and a producer may name a picker of its own (`__taidal_widget_params__`); one the page does not know is a plain
 * field. A picker only FILLS the field: the field stays free text (a Hub id that is not cached, a split slice like
 * `train[:90%]`, a folder typed by hand, `$DATA_ROOT/…`), and a pick is committed like a typed value.
 *
 * The lookups go to the editor's server (`api.ts` `hfDatasets` / `hfFeatures` / `hfSplits` — the machine whose cache a
 * source reads, never the browser's), are asked when the person reaches for the list (never as a canvas draws: a column
 * lookup can open a dataset on the editor's machine), and are remembered for the page's life per question
 * (`cachedLookup`); a lookup that fails leaves the field without its list — a convenience, never a requirement.
 */
import type { Json } from "./model";

/** The pickers the page draws, by the server's names (`schema.picker`). */
export const PICKERS = ["directory", "file", "hf_dataset", "hf_feature", "hf_split", "hf_metadata"] as const;
export type Picker = (typeof PICKERS)[number];

/** The pickers that list what the editor's machine knows about a HuggingFace dataset. */
export type LookupPicker = Exclude<Picker, "directory" | "file">;

/**
 * `metadata_features`' word for "every column but the input and target ones" (`recordstream.sources`'
 * `METADATA_ALL_FEATURES`; `GET api/hf/features` answers it as `all_columns`).
 */
export const ALL_COLUMNS = "*";

/** The picker the server marked the field with, or `undefined` — none, or one the page does not draw. */
export function pickerFor(schema: Record<string, Json>): Picker | undefined {
  const named = schema.picker;
  return typeof named === "string" && (PICKERS as readonly string[]).includes(named) ? (named as Picker) : undefined;
}

/** Does the picker list a HuggingFace lookup (rather than browse the editor's files). */
export const isLookup = (picker: Picker): picker is LookupPicker => picker !== "directory" && picker !== "file";

/** The text of a value a picker's field shows (`null` / unset is empty). */
const asString = (value: Json | undefined): string => (typeof value === "string" ? value : value === null || value === undefined ? "" : String(value));

/** What the dataset lookups need from the node's own fields: the dataset, its subset (`name`) and its split. */
export interface HfDataset {
  path: string;
  name: string;
  split: string;
}

/** The dataset a HuggingFace node names, from its current values (`path`, else `repo_id`; `name`; `split`). */
export function hfDataset(values: Record<string, Json | undefined>): HfDataset {
  return { path: asString(values.path ?? values.repo_id).trim(), name: asString(values.name).trim(), split: asString(values.split).trim() };
}

/** The question a lookup asks the server, as one key — what its list is cached under; `undefined` while it has none to ask. */
export function lookupKey(picker: LookupPicker, dataset: HfDataset): string | undefined {
  if (picker === "hf_dataset") return "datasets";
  if (dataset.path === "") return undefined;
  if (picker === "hf_split") return `splits\u0000${dataset.path}\u0000${dataset.name}`;
  return `features\u0000${dataset.path}\u0000${dataset.name}\u0000${dataset.split}`;
}

const lookups = new Map<string, Promise<unknown>>();

/**
 * `fetch()` asked once per `key` for the page's life: every widget asking the same question shares one answer. A
 * lookup that FAILS is forgotten, so the next widget to ask tries again (the server may have been busy).
 */
export function cachedLookup<T>(key: string, fetch: () => Promise<T>): Promise<T> {
  const known = lookups.get(key) as Promise<T> | undefined;
  if (known !== undefined) return known;
  const asked = fetch().catch((exc: unknown) => {
    lookups.delete(key);
    throw exc;
  });
  lookups.set(key, asked);
  return asked;
}

/** Forget every remembered lookup (a test's isolation). */
export const forgetLookups = (): void => lookups.clear();

/** A `metadata_features` value as the columns it lists: `*` → `["*"]`, `"a, b"` → `["a", "b"]`, `null` → `[]`. */
export function metadataList(value: Json | undefined): string[] {
  if (Array.isArray(value)) return value.filter((item): item is string => typeof item === "string");
  return asString(value)
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

/** The text a `metadata_features` field shows: the columns, comma-separated (`*` alone for all of them). */
export const metadataText = (value: Json | undefined): string => metadataList(value).join(", ");

/**
 * The value a typed `metadata_features` text commits: empty → `null` (no metadata columns), `*` → `"*"` (every column
 * but input/target), else the list of columns.
 */
export function parseMetadata(text: string): Json {
  const columns = metadataList(text);
  if (columns.length === 0) return null;
  return columns.length === 1 && columns[0] === ALL_COLUMNS ? ALL_COLUMNS : columns;
}

/** The value after "add column" picked `column`: `*` replaces the list; a column is appended (once; never after `*`). */
export function withColumn(current: Json | undefined, column: string): Json {
  if (column === ALL_COLUMNS) return ALL_COLUMNS;
  const listed = metadataList(current).filter((item) => item !== ALL_COLUMNS);
  return listed.includes(column) ? listed : [...listed, column];
}

/**
 * The "add column" choices of a `metadata_features` field: `*` (every column but the input and target ones) first —
 * unless it is what the field says already — then every column the node does not read as its input or target, nor
 * lists already.
 */
export function metadataChoices(columns: string[], values: Record<string, Json | undefined>): string[] {
  const listed = metadataList(values.metadata_features);
  const taken = new Set([asString(values.input_feature), asString(values.target_feature), ...listed]);
  return [...(listed.includes(ALL_COLUMNS) ? [] : [ALL_COLUMNS]), ...columns.filter((column) => !taken.has(column))];
}

/**
 * Where a file browser opened from a field starts: the folder the field names (a `directory` field) or the folder of the
 * file it names (a `file` field) — absolute, or `$VAR/…`, which the server expands (a variable its machine does not have
 * is named: `DATA_ROOT is not set on the editor's machine ($DATA_ROOT/captures)`, and the browser shows the editor's folder
 * under it); anything else (empty, relative) is the editor's graph folder.
 */
export function browseStart(picker: "directory" | "file", value: string): string | undefined {
  const typed = value.trim();
  if (!typed.startsWith("/") && !typed.startsWith("$") && !typed.startsWith("~")) return undefined;
  if (picker === "directory") return typed;
  const cut = typed.replace(/\/+$/, "").lastIndexOf("/");
  return cut <= 0 ? undefined : typed.slice(0, cut);
}

/**
 * A `(min, max)` pair's two box texts once the value under them changed from `before` to `truth`: a box still showing
 * what the value WAS takes the new value (an undo, the other surface, a graph switch); a box the person changed keeps
 * what they typed. Leaving the min box writes `[min, old max]`, and its answer arrives while the max box is being typed
 * into — measured before: the answer reset BOTH boxes, so the max box went back to 750000 and the next keys were
 * appended to it (`7` → `7500006` → … `75000061000`, refused, or written).
 */
export function followPair(draft: [string, string], before: [string, string], truth: [string, string]): [string, string] {
  return [draft[0] === before[0] ? truth[0] : draft[0], draft[1] === before[1] ? truth[1] : draft[1]];
}

/** One end of a `(min, max)` pair: its JSON-schema word and bounds. */
export interface PairEnd {
  type: "number" | "integer";
  minimum?: number;
  maximum?: number;
}

const asObject = (value: Json | undefined): Record<string, Json> | undefined =>
  typeof value === "object" && value !== null && !Array.isArray(value) ? value : undefined;

/**
 * A `(min, max)` numeric pair's two ends, when the schema is one (`Tuple[float, float]`: `{type: array, prefixItems:
 * [{type: number, …}, {type: number, …}]}`, also as one arm of an `anyOf` with `null`); `undefined` for anything else.
 * The ComfyUI editor drew such a pair as two widgets, `deviation_hz__lo` / `__hi`; here it is one field of two numbers.
 */
export function rangePair(schema: Record<string, Json>): [PairEnd, PairEnd] | undefined {
  const arms = Array.isArray(schema.anyOf) ? schema.anyOf.map(asObject).filter((arm): arm is Record<string, Json> => arm !== undefined) : [schema];
  for (const arm of arms) {
    if (arm.type !== "array" || !Array.isArray(arm.prefixItems) || arm.prefixItems.length !== 2) continue;
    const ends = arm.prefixItems.map(asObject);
    if (ends.some((end) => end === undefined || (end.type !== "number" && end.type !== "integer"))) continue;
    return ends.map((end) => ({
      type: end!.type as "number" | "integer",
      ...(typeof end!.minimum === "number" ? { minimum: end!.minimum } : {}),
      ...(typeof end!.maximum === "number" ? { maximum: end!.maximum } : {}),
    })) as [PairEnd, PairEnd];
  }
  return undefined;
}

/** A pair's value as two numbers: the value when it is one, else the schema's default when that is one, else none. */
export function pairValue(value: Json | undefined, fallback: Json | undefined): [number, number] | undefined {
  for (const candidate of [value, fallback]) {
    if (Array.isArray(candidate) && candidate.length === 2 && candidate.every((end) => typeof end === "number"))
      return [candidate[0] as number, candidate[1] as number];
  }
  return undefined;
}
