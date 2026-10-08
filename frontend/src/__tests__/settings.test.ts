/**
 * The settings tab's fields (`settings.ts`): the server's `app:` answer (`GET …/app`) narrows them, "all parameters"
 * lists the level shown — the Bluetooth pane's shape, in small.
 */
import { describe, expect, it } from "vitest";
import type { AppForm } from "../api";
import type { Form, Model, ModelNode, Param } from "../model";
import { fieldCount, offReason, settingsEmpty, settingsMode, settingsSections } from "../settings";

const param = (name: string, value: Param["value"], type = "number"): Param => ({
  name,
  type,
  value,
  default: value,
  doc: "",
  schema: { type },
  spelled: false,
  required: false,
});

const node = (name: string, classPath: string, params: Param[]): ModelNode => ({
  name,
  class_path: classPath,
  kind: "op",
  params: Object.fromEntries(params.map((p) => [p.name, p])),
  inputs: [],
  outputs: [],
  loc: "",
  source_file: "",
  note: "",
  alias_of: "",
});

const REFUSE = node("refuse", "waivefront.processing.RefuseLargeWindow", [param("max_band_hz", 2e7), param("max_duration_s", 0.1)]);
const CLASSIFY = node("classify", "waivefront.processing.bluetooth_nodes.ClassifyBluetooth", [
  param("line_db", 5),
  param("deviation_hz", [100000, 750000], "object"),
  param("requires", "ble", "string"), // the gate: on the model, never in a form
]);
const TUNE = node("prep/tune", "waivefront.processing.TuneToBand", [param("width_hz", 2e6)]);

const MODEL: Model = {
  nodes: [REFUSE, node("prep", "recordstream.flow.subgraph.Subgraph", [param("result", "tune", "string")]), CLASSIFY],
  wires: [],
  problems: [],
  outputs: [],
  inside: { prep: { nodes: [TUNE], wires: [], problems: [], outputs: [] } },
};

/** `GET …/form`: every parameter's schema, the gate left out by the server. */
const FORM: Form = {
  refuse: { max_band_hz: { type: "number", minimum: 0 }, max_duration_s: { type: "number" } },
  prep: { result: { type: "string" } },
  classify: {
    line_db: { type: "number", maximum: 200 },
    deviation_hz: { type: "array", prefixItems: [{ type: "number" }, { type: "number" }] },
  },
  "prep/tune": { width_hz: { type: "number", picker: "directory" } },
};

/** `GET …/app`: the block resolved, out of node order, a pair as one field. */
const APP: AppForm = {
  declared: true,
  graph: "editable",
  fields: [
    { node: "classify", param: "line_db", loc: "/w/bte.yaml:12:7" },
    { node: "refuse", param: "max_band_hz", loc: "/w/bte.yaml:13:7" },
    { node: "classify", param: "deviation_hz", loc: "/w/bte.yaml:14:7" },
    { node: "prep/tune", param: "width_hz", loc: "/w/bte.yaml:16:7" },
  ],
  form: {
    classify: { line_db: { type: "number", maximum: 200 }, deviation_hz: FORM.classify.deviation_hz },
    refuse: { max_band_hz: FORM.refuse.max_band_hz },
    "prep/tune": { width_hz: FORM["prep/tune"].width_hz },
  },
  problems: [],
};

const NOTHING: AppForm = { declared: false, graph: "editable", fields: [], form: {}, problems: [] };

const names = (sections: ReturnType<typeof settingsSections>) =>
  sections.map((section) => [section.node.name, section.fields.map((field) => field.name)]);

describe("settingsMode — narrowed to the app: block until 'all parameters' is ticked", () => {
  it("app; all when ticked; all without the answer", () => {
    expect(settingsMode(APP, false)).toBe("app");
    expect(settingsMode(APP, true)).toBe("all");
    expect(settingsMode(null, false)).toBe("all");
  });

  it("a graph WITHOUT an app: block is narrowed too — to nothing: it names no settings, and the tab says so", () => {
    // review 2026-10-02: the fft pane's `settings` listed 23 fields — its Stream's plumbing, the window the host
    // fills at run time, the graph's root contract — with no word why
    expect(settingsMode(NOTHING, false)).toBe("app");
    expect(settingsSections(MODEL, FORM, NOTHING, "app")).toEqual([]);
    expect(settingsMode(NOTHING, true)).toBe("all");
    expect(fieldCount(settingsSections(MODEL, FORM, NOTHING, "all"))).toBe(5);
  });

  it("a block whose every entry names nothing is still narrowed: the tab shows its problems, and offers all", () => {
    const unnamed: AppForm = { declared: true, graph: "editable", fields: [], form: {}, problems: [{ message: "…", loc: "…" }] };
    expect(settingsMode(unnamed, false)).toBe("app");
  });
});

describe("settingsSections — what the tab draws", () => {
  it("app: one section per node in the order the block first names it, the fields in the block's order", () => {
    const sections = settingsSections(MODEL, FORM, APP, "app");
    expect(names(sections)).toEqual([
      ["classify", ["line_db", "deviation_hz"]],
      ["refuse", ["max_band_hz"]],
      ["prep/tune", ["width_hz"]],
    ]);
    expect(fieldCount(sections)).toBe(4);
    // the schema is the model's fragment with the form's over it — the picker the server marked included
    expect(sections[0].fields[0].schema).toEqual({ type: "number", maximum: 200 });
    expect(sections[2].fields[0].schema).toEqual({ type: "number", picker: "directory" });
    expect(sections[0].fields[0].param?.value).toBe(5);
  });

  it("app: a field the answer names on a node the model no longer holds (a rename since) waits for the next answer", () => {
    const stale: AppForm = { ...APP, fields: [{ node: "gone", param: "x", loc: "" }, ...APP.fields.slice(0, 1)] };
    expect(names(settingsSections(MODEL, FORM, stale, "app"))).toEqual([["classify", ["line_db"]]]);
  });

  it("app: the block's schema stands in for a node the full form has not described yet", () => {
    const sections = settingsSections(MODEL, {}, APP, "app");
    expect(sections[0].fields[1].schema).toEqual({ type: "object", ...FORM.classify.deviation_hz });
  });

  it("all: every parameter of every node of the level shown, in the model's order", () => {
    expect(names(settingsSections(MODEL, FORM, APP, "all"))).toEqual([
      ["refuse", ["max_band_hz", "max_duration_s"]],
      ["prep", ["result"]],
      ["classify", ["line_db", "deviation_hz"]],
    ]);
    expect(names(settingsSections(MODEL, FORM, null, "all", "prep"))).toEqual([["prep/tune", ["width_hz"]]]);
  });

  it("all: a node the form does not describe has no section; a field the model lacks carries no param", () => {
    const sections = settingsSections(MODEL, { refuse: { ...FORM.refuse, extra: { type: "number" } } }, NOTHING, "all");
    expect(names(sections)).toEqual([["refuse", ["max_band_hz", "max_duration_s", "extra"]]]);
    expect(sections[0].fields.map((field) => field.param === undefined)).toEqual([false, false, true]);
  });
});

describe("settingsEmpty — what the tab says when it lists no field", () => {
  it("a graph without an app: block names no settings, and says where its parameters are", () => {
    expect(settingsEmpty(NOTHING, "app", 0, "of this graph")).toBe(
      "This graph names no settings — it has no app: block. Its parameters are on the canvas; tick all parameters to list every one here.",
    );
  });

  it("a settings-only page (a host's settings sheet, `?only=settings`) has no tick box, so it points at none", () => {
    expect(settingsEmpty(NOTHING, "app", 0, "of this graph", true)).toBe("This graph names no settings — it has no app: block.");
    expect(settingsEmpty(APP, "app", 0, "of this graph", true)).toBe("No field the app: block names is a parameter of this graph.");
  });

  it("a block naming nothing that is here; a level with no parameter; nothing to say when fields are listed", () => {
    expect(settingsEmpty(APP, "app", 0, "of this graph")).toBe("No field the app: block names is a parameter of this graph.");
    expect(settingsEmpty(NOTHING, "all", 0, "inside prep")).toBe("No node inside prep has a parameter to set.");
    expect(settingsEmpty(APP, "app", 4, "of this graph")).toBe("");
    expect(settingsEmpty(null, "all", 3, "of this graph")).toBe("");
  });
});

describe("offReason — a field the setting it depends on does not ask for is grayed out, never hidden", () => {
  // a threshold detector's shape: the margin read by four rules, the level by one, the FFT size by one spectrogram
  const MARGIN = { type: "number", applies_when: { rule: ["row_median", "noise_floor", "local_floor", "cfar"] } };
  const LEVEL = { type: "number", applies_when: { rule: ["fixed"] } };
  const FFT = { type: "integer", applies_when: { spectrogram: ["pane"] } };
  const NODE = { rule: "row_median", spectrogram: "fast", margin_db: 15, level_dbm_hz: -150 };

  it("PRO: a field the rule in force reads is live; CON: one it never reads says which rule it waits for", () => {
    expect(offReason(MARGIN, NODE)).toBe("");
    expect(offReason(LEVEL, NODE)).toBe("rule is row_median — read only while rule is fixed");
    expect(offReason(FFT, NODE)).toBe("spectrogram is fast — read only while spectrogram is pane");
    expect(offReason(MARGIN, { ...NODE, rule: "otsu" })).toBe(
      "rule is otsu — read only while rule is row_median, noise_floor, local_floor or cfar",
    );
  });

  it("the host's value wins over the node's: the pane runs the rule its knob picked, not the graph's", () => {
    expect(offReason(LEVEL, NODE, { rule: "fixed" })).toBe("");
    expect(offReason(MARGIN, NODE, { rule: "fixed" })).toBe(
      "rule is fixed — read only while rule is row_median, noise_floor, local_floor or cfar",
    );
  });

  it("a value compared as text (a host sends what its field holds); several conditions must all hold", () => {
    expect(offReason({ applies_when: { connectivity: [8] } }, { connectivity: "8" })).toBe("");
    const both = { applies_when: { rule: ["cfar"], spectrogram: ["pane"] } };
    expect(offReason(both, { rule: "cfar", spectrogram: "pane" })).toBe("");
    expect(offReason(both, { rule: "cfar", spectrogram: "fast" })).toBe("spectrogram is fast — read only while spectrogram is pane");
  });

  it("CON: no declaration, a malformed one, or a setting the node does not hold — the field stays live", () => {
    expect(offReason({ type: "number" }, NODE)).toBe("");
    expect(offReason({ applies_when: "fixed" }, NODE)).toBe("");
    expect(offReason({ applies_when: { rule: "fixed" } }, NODE)).toBe("");
    expect(offReason({ applies_when: { mode: ["a"] } }, NODE)).toBe("");
  });
});
