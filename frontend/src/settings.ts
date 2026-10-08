/**
 * The settings tab, the pure half: which fields it lists.
 *
 * A pane graph declares the parameters a person tunes without the canvas in its `app:` block —
 *
 *     app:
 *       inputs:
 *         - [RefuseLargeWindow, max_band_hz]
 *         - [ClassifyBluetooth, deviation_hz__lo]
 *         - [ClassifyBluetooth, deviation_hz__hi]
 *
 * — and the server resolves it against the document the editor holds (`GET …/app`, `api.AppForm`): one field per node
 * and parameter in the block's order (a `__lo` / `__hi` pair is ONE field, edited as two numbers by `ParamWidget`; a
 * class two nodes share gives a field on each), their schemas, and every entry that names nothing as a located problem.
 * The tab shows THOSE fields (the Bluetooth pane: 30, where the whole graph has 247 parameters), one section per node in
 * the order the block first names it, until "all parameters" is ticked; then it lists every parameter of every node of
 * the level the canvas shows (`GET …/form`). A graph WITHOUT an `app:` block names no settings, so the tab lists none
 * and SAYS so (`settingsEmpty`) until the box is ticked — measured 2026-10-02, a host's pane graph with no block listed
 * its 23 parameters on the settings tab, the Stream's plumbing, the window the host fills at run time and the graph's
 * root contract among them, with no word why. Only an `app:` answer that could not be read lists every parameter
 * unasked (and says why). A step's gate (`requires`) is never a field: the server leaves it out of both.
 */
import type { AppForm } from "./api";
import { EMPTY_MODEL, findNode, levelModel } from "./levels";
import type { Form, Json, Model, ModelNode, Param } from "./model";

/** What the tab lists: the `app:` fields, or every parameter. */
export type SettingsMode = "app" | "all";

export interface SettingsField {
  /** The parameter. */
  name: string;
  /** The model's parameter (value, type, doc); `undefined` when the form names one the model does not have. */
  param: Param | undefined;
  /** The parameter's schema: the model's fragment with the form's over it (its `picker` included). */
  schema: Record<string, Json>;
}

export interface SettingsSection {
  node: ModelNode;
  fields: SettingsField[];
}

/**
 * The mode the tab shows: `all` when ticked, or when the graph's `app:` answer could not be read; else `app` — a graph
 * without an `app:` block included, narrowed to the nothing it names.
 */
export const settingsMode = (app: AppForm | null, allTicked: boolean): SettingsMode => (allTicked || app === null ? "all" : "app");

/**
 * What the tab says when it lists no field — `""` when it lists some. `where` names the level (`of this graph`,
 * `inside prep`).
 */
export function settingsEmpty(app: AppForm | null, mode: SettingsMode, count: number, where: string, fixed = false): string {
  if (count > 0) return "";
  if (mode === "all") return `No node ${where} has a parameter to set.`;
  if (app !== null && !app.declared && fixed) return "This graph names no settings — it has no app: block.";
  if (app !== null && !app.declared)
    return "This graph names no settings — it has no app: block. Its parameters are on the canvas; tick all parameters to list every one here.";
  return "No field the app: block names is a parameter of this graph.";
}

const fieldOf = (node: ModelNode, form: Form, name: string): SettingsField => {
  const param: Param | undefined = node.params[name];
  return { name, param, schema: { ...(param?.schema ?? {}), ...(form[node.name]?.[name] ?? {}) } };
};

/**
 * The sections the tab draws. `all`: every node of the level shown with a parameter the form describes, in the model's
 * order. `app`: the `app:` fields, grouped by node in the order the block first names each node, each node's fields in
 * the block's order — a field naming a node the model no longer holds (the answer is a moment older than an edit) is
 * left out until the next answer.
 */
export function settingsSections(model: Model, form: Form, app: AppForm | null, mode: SettingsMode, level = ""): SettingsSection[] {
  if (mode === "all" || app === null) {
    const view = levelModel(model, level) ?? EMPTY_MODEL;
    return view.nodes
      .map((node) => ({ node, fields: Object.keys(form[node.name] ?? {}).map((name) => fieldOf(node, form, name)) }))
      .filter((section) => section.fields.length > 0);
  }
  const sections = new Map<string, SettingsSection>();
  const schemas: Form = { ...app.form };
  for (const [node, params] of Object.entries(form)) schemas[node] = { ...(app.form[node] ?? {}), ...params };
  for (const { node: name, param } of app.fields) {
    const node = findNode(model, name);
    if (node === undefined) continue;
    const section = sections.get(name) ?? { node, fields: [] };
    sections.set(name, section);
    if (!section.fields.some((field) => field.name === param)) section.fields.push(fieldOf(node, schemas, param));
  }
  return [...sections.values()];
}

/** How many fields the sections hold. */
export const fieldCount = (sections: SettingsSection[]): number => sections.reduce((total, section) => total + section.fields.length, 0);

/** The schema key a parameter names the values of OTHER parameters it is read under: `{rule: ["fixed"]}`. */
export const APPLIES_WHEN = "applies_when";

const listed = (allowed: string[]): string =>
  allowed.length < 2 ? allowed.join("") : `${allowed.slice(0, -1).join(", ")} or ${allowed[allowed.length - 1]}`;

/**
 * Why a field is grayed out, or `""` while it is live. A parameter only some values of another one read (a threshold
 * detector's level, read by its `fixed` rule alone) says so in its schema — `applies_when: {rule: ["fixed"]}` — and is
 * grayed while that parameter holds any other value: `rule is cfar — read only while rule is fixed`. The value is the
 * host's when it sends one (`values`, a pane's knob: what the host RUNS), else the node's; compared as text, as a host's
 * field holds it. A declaration that is no map of lists, or names a parameter neither holds, grays nothing — a field
 * is never taken away on a guess. Grayed is never hidden and never refused: the field keeps its value.
 */
export function offReason(
  schema: Record<string, Json> | undefined,
  nodeValues: Record<string, Json | undefined>,
  hostValues: Record<string, Json | undefined> = {},
): string {
  const declared = schema?.[APPLIES_WHEN];
  if (typeof declared !== "object" || declared === null || Array.isArray(declared)) return "";
  for (const [name, allowed] of Object.entries(declared)) {
    if (!Array.isArray(allowed)) continue;
    const value = hostValues[name] !== undefined ? hostValues[name] : nodeValues[name];
    if (value === undefined || value === null) continue;
    const texts = allowed.map((entry) => String(entry));
    if (!texts.includes(String(value))) return `${name} is ${String(value)} — read only while ${name} is ${listed(texts)}`;
  }
  return "";
}
