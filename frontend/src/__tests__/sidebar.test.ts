/**
 * The Nodes sidebar, the pure half: the catalogue (`GET api/palette`) grouped by package — the first segment of the
 * class path — then by kind, the search that narrows it, the chips an entry shows for what it reads and writes, and
 * the parameter rows its hover card lists.
 */
import { describe, expect, it } from "vitest";
import type { NodeSpecWithSettings } from "../api";
import { KIND_ORDER, SUBGRAPH_CLASS, addableSpecs, chips, groupSpecs, kindLabel, packageOf, paramRows, sidebarMatches } from "../sidebar";

// with its settings — what a selected class answers (`GET api/palette/<class>`); a list entry is the same less those
const spec = (class_path: string, kind: string, extra: Partial<NodeSpecWithSettings> = {}): NodeSpecWithSettings => ({
  class_path,
  name: class_path.split(".").pop() ?? class_path,
  category: "",
  group: "",
  kind,
  params_schema: {},
  params_defs: {},
  inputs: [],
  outputs: [],
  consumes: {},
  produces: {},
  consumes_types: [],
  produces_types: [],
  flags: [],
  reports: [],
  doc: "",
  tuple_form: false,
  problem: "",
  ...extra,
});

const record = { name: "record", type: "Record", required: true, doc: "the record flowing through the chain" };
const recordOut = { name: "record", type: "Record", required: false, doc: "the record after this node" };
const opOut = { name: "op", type: "Op", required: false, doc: "this node as an op" };

describe("groupSpecs", () => {
  const specs = [
    spec("signals.ops.Spectrogram", "op"),
    spec("recordstream.ops.numpy.Threshold", "op"),
    spec("recordstream.sources.HuggingFaceSource", "source"),
    spec("recordstream.ops.numpy.Scale", "op"),
    spec("signals.processing.NoiseFloor", "algorithm"),
    spec("keras.optimizers.Adam", "value"),
  ];

  it("groups by package, alphabetically, each with its count", () => {
    const grouped = groupSpecs(specs);
    expect(grouped.map((section) => [section.name, section.count])).toEqual([
      ["keras", 1],
      ["recordstream", 3],
      ["signals", 2],
    ]);
  });

  it("orders the kinds of a package as sources, ops, algorithms, … and leaves out empty ones", () => {
    const recordstream = groupSpecs(specs).find((section) => section.name === "recordstream")!;
    expect(recordstream.kinds.map((section) => section.kind)).toEqual(["source", "op"]);
    expect(recordstream.kinds.map((section) => section.label)).toEqual(["Sources", "Ops"]);
    const signals = groupSpecs(specs).find((section) => section.name === "signals")!;
    expect(signals.kinds.map((section) => section.kind)).toEqual(["op", "algorithm"]);
  });

  it("sorts the entries of a kind by name", () => {
    const ops = groupSpecs(specs).find((section) => section.name === "recordstream")!.kinds[1];
    expect(ops.specs.map((entry) => entry.name)).toEqual(["Scale", "Threshold"]);
  });

  it("puts a kind the page does not know after the known ones", () => {
    const grouped = groupSpecs([spec("p.A", "gizmo"), spec("p.B", "op")]);
    expect(grouped[0].kinds.map((section) => section.kind)).toEqual(["op", "gizmo"]);
    expect(kindLabel("gizmo")).toBe("Gizmo");
  });

  it("knows every kind the palette serves", () => {
    expect(KIND_ORDER).toEqual(["source", "op", "algorithm", "engine", "sink", "runnable", "value", "contract"]);
  });

  it("packageOf is the first segment of the class path", () => {
    expect(packageOf("recordstream.ops.numpy.Threshold")).toBe("recordstream");
    expect(packageOf("Loose")).toBe("Loose");
  });
});

describe("groupSpecs — folders from the class's @configurable group", () => {
  const specs = [
    spec("signals.fourier.window.WindowOp", "op", { group: "fourier/numpy" }),
    spec("signals.fourier.fourier.FourierOp", "op", { group: "fourier/numpy" }),
    spec("signals.fourier_torch.fourier.TorchFourierOp", "op", { group: "fourier/torch" }),
    spec("signals.processing.measure.MeasureOp", "op", { group: "measure" }),
    spec("signals.detection.LocateOp", "op"),
    spec("signals.zigbee.zcl.OnOff", "value", { group: "zigbee/zcl" }),
    spec("keras.losses.Huber", "value"),
    spec("keras.losses.Dice", "value"),
  ];
  const signals = () => groupSpecs(specs).find((section) => section.name === "signals")!;

  it("nests a class under its kind, then one folder per segment of its group", () => {
    const ops = signals().kinds.find((kind) => kind.kind === "op")!;
    const fourier = ops.folders.find((folder) => folder.name === "fourier")!;
    const numpy = fourier.folders.find((folder) => folder.name === "numpy")!;
    expect(numpy.specs.map((entry) => entry.name)).toEqual(["FourierOp", "WindowOp"]);
    expect(fourier.folders.map((folder) => folder.name)).toEqual(["numpy", "torch"]);
    expect(fourier.specs).toEqual([]);
  });

  it("keys every folder by its path from the package — the name the sidebar remembers it under", () => {
    const ops = signals().kinds.find((kind) => kind.kind === "op")!;
    expect(ops.key).toBe("signals/op");
    const fourier = ops.folders.find((folder) => folder.name === "fourier")!;
    expect(fourier.key).toBe("signals/op/fourier");
    expect(fourier.folders.map((folder) => folder.key)).toEqual(["signals/op/fourier/numpy", "signals/op/fourier/torch"]);
  });

  it("lists the folders alphabetically, then the classes with no group of their own", () => {
    const ops = signals().kinds.find((kind) => kind.kind === "op")!;
    expect(ops.folders.map((folder) => folder.name)).toEqual(["fourier", "measure"]);
    expect(ops.specs.map((entry) => entry.name)).toEqual(["LocateOp"]);
  });

  it("counts every class beneath a folder, at every level", () => {
    const section = signals();
    expect(section.count).toBe(6);
    const ops = section.kinds.find((kind) => kind.kind === "op")!;
    expect(ops.count).toBe(5);
    expect(ops.folders.map((folder) => [folder.name, folder.count])).toEqual([
      ["fourier", 3],
      ["measure", 1],
    ]);
    const values = section.kinds.find((kind) => kind.kind === "value")!;
    expect(values.folders[0].folders[0]).toMatchObject({ name: "zcl", count: 1, key: "signals/value/zigbee/zcl" });
  });

  it("leaves a package that declares no group exactly as flat as before: its classes straight under the kind", () => {
    const keras = groupSpecs(specs).find((section) => section.name === "keras")!;
    expect(keras.kinds).toHaveLength(1);
    expect(keras.kinds[0].folders).toEqual([]);
    expect(keras.kinds[0].specs.map((entry) => entry.name)).toEqual(["Dice", "Huber"]);
    expect(keras.kinds[0].count).toBe(2);
  });

  it("ignores empty segments of a group path", () => {
    const grouped = groupSpecs([spec("p.A", "op", { group: "/fourier//numpy/" })]);
    const fourier = grouped[0].kinds[0].folders[0];
    expect(fourier.name).toBe("fourier");
    expect(fourier.folders.map((folder) => folder.key)).toEqual(["p/op/fourier/numpy"]);
  });

  it("built from a search's matches, holds only the folders on the way to a match, counted by matches", () => {
    const matching = specs.filter((entry) => sidebarMatches(entry, "window"));
    const ops = groupSpecs(matching)[0].kinds[0];
    expect(ops.count).toBe(1);
    expect(ops.folders.map((folder) => [folder.name, folder.count])).toEqual([["fourier", 1]]);
    expect(ops.folders[0].folders.map((folder) => [folder.name, folder.count])).toEqual([["numpy", 1]]);
    expect(ops.specs).toEqual([]);
  });
});

describe("sidebarMatches", () => {
  const threshold = spec("recordstream.ops.numpy.Threshold", "op", { doc: "Binarize an array against a level." });

  it("matches every word, anywhere in the name, the class path or the description, ignoring case", () => {
    expect(sidebarMatches(threshold, "")).toBe(true);
    expect(sidebarMatches(threshold, "thresh")).toBe(true);
    expect(sidebarMatches(threshold, "numpy")).toBe(true);
    expect(sidebarMatches(threshold, "BINARIZE")).toBe(true);
    expect(sidebarMatches(threshold, "numpy level")).toBe(true);
  });

  it("refuses when one word is missing", () => {
    expect(sidebarMatches(threshold, "numpy spectrogram")).toBe(false);
  });
});

describe("chips — what an entry reads and writes", () => {
  it("leaves out the record that every op passes on, and the op and object outputs", () => {
    const noise = spec("signals.processing.NoiseFloor", "algorithm", {
      inputs: [record, { name: "spectrogram", type: "Spectrogram", required: true, doc: "The dB spectrogram." }],
      outputs: [recordOut, opOut, { name: "noise_floor_db", type: "number", required: false, doc: "" }],
    });
    expect(chips(noise)).toEqual({
      reads: [{ name: "spectrogram", type: "Spectrogram" }],
      writes: [{ name: "noise_floor_db", type: "number" }],
    });
  });

  it("keeps the record when it is all an op declares, so a chain op still shows what it takes", () => {
    const threshold = spec("recordstream.ops.numpy.Threshold", "op", { inputs: [record], outputs: [recordOut, opOut] });
    expect(chips(threshold)).toEqual({ reads: [{ name: "record", type: "Record" }], writes: [{ name: "record", type: "Record" }] });
  });

  it("shows nothing for a value with only its built object", () => {
    const adam = spec("keras.optimizers.Adam", "value", {
      outputs: [{ name: "object", type: "object", required: false, doc: "the built object itself" }],
    });
    expect(chips(adam)).toEqual({ reads: [], writes: [] });
  });
});

describe("paramRows — the hover card's parameter list", () => {
  it("reads the type, the default and the description from the constructor schema", () => {
    const threshold = spec("recordstream.ops.numpy.Threshold", "op", {
      params_schema: {
        low_level: { anyOf: [{ type: "number" }, { type: "null" }], default: null, description: "The low level." },
        mode: { enum: ["a", "b"], type: "string", default: "a" },
        keys: { type: "array", items: { type: "string" } },
        model: { $ref: "#/$defs/Backbone" },
      },
    });
    expect(paramRows(threshold)).toEqual([
      { name: "low_level", type: "number?", default: "null", doc: "The low level.", required: false },
      { name: "mode", type: "a | b", default: '"a"', doc: "", required: false },
      { name: "keys", type: "list[string]", default: "", doc: "", required: true },
      { name: "model", type: "Backbone", default: "", doc: "", required: true },
    ]);
  });
});

describe("addableSpecs — what the sidebar, the palette and Add Node ▸ offer", () => {
  it("leaves out the Subgraph class: an empty one is refused, and there are three ways to make one", () => {
    const specs = [spec("recordstream.ops.numpy.Threshold", "op"), spec(SUBGRAPH_CLASS, "op"), spec("recordstream.core.stream.Stream", "engine")];
    expect(addableSpecs(specs).map((each) => each.class_path)).toEqual([
      "recordstream.ops.numpy.Threshold",
      "recordstream.core.stream.Stream",
    ]);
  });

  it("names the class the document spells a subgraph step with", () => {
    expect(SUBGRAPH_CLASS).toBe("recordstream.flow.subgraph.Subgraph");
  });

  it("keeps the same array when there is nothing to leave out", () => {
    const specs = [spec("recordstream.ops.numpy.Threshold", "op")];
    expect(addableSpecs(specs)).toBe(specs);
  });
});
