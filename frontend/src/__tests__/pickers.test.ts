/**
 * The pickers (`pickers.ts`), ported from the ComfyUI editor: the picker the server marks a field with, where the file
 * browser opens, what an HF list is asked about, how "add column" builds `metadata_features`, and a `(min, max)` pair read
 * off its schema. The lookups are a fake fetcher — no network.
 */
import { afterEach, describe, expect, it } from "vitest";
import {
  ALL_COLUMNS,
  ASK_AGAIN,
  browseStart,
  cachedLookup,
  forgetLookup,
  forgetLookups,
  hfDataset,
  isLookup,
  lookupKey,
  metadataChoices,
  metadataList,
  metadataText,
  parseTextList,
  textListEditable,
  textListText,
  followPair,
  mapEditable,
  mapRows,
  numberList,
  numberListEditable,
  numberListText,
  numberOrText,
  numberOrTextValue,
  pairValue,
  parseMapRows,
  parseMetadata,
  parseNumberList,
  pickerFor,
  pluginLookupKey,
  pluginTag,
  pluginValues,
  rangePair,
  scalarMap,
  tupleEnds,
  withColumn,
} from "../pickers";

afterEach(() => forgetLookups());

describe("pickerFor — the picker the server marked the field with (GET …/form, GET api/palette)", () => {
  it("each picker the page draws, by the server's name", () => {
    expect(pickerFor({ type: "string", picker: "directory" })).toBe("directory");
    expect(pickerFor({ type: "string", picker: "file" })).toBe("file");
    expect(pickerFor({ type: "string", picker: "hf_dataset" })).toBe("hf_dataset");
    expect(pickerFor({ type: "string", picker: "hf_feature" })).toBe("hf_feature");
    expect(pickerFor({ type: "string", picker: "hf_split" })).toBe("hf_split");
    expect(pickerFor({ anyOf: [{ type: "array" }, { type: "null" }], picker: "hf_metadata" })).toBe("hf_metadata");
  });

  it("CON: no mark, or a producer's own picker the page does not know, is a plain field", () => {
    expect(pickerFor({ type: "string" })).toBeUndefined();
    expect(pickerFor({ type: "string", picker: "PLATFORM_DATASET_ID" })).toBeUndefined();
    expect(pickerFor({ type: "string", picker: 3 })).toBeUndefined();
  });

  it("a producer's own tag a plugin lists (`lookup: true`) is the plugin list, its tag kept", () => {
    const schema = { type: "string", picker: "SHOP_DATASET", lookup: true };
    expect(pickerFor(schema)).toBe("plugin");
    expect(pluginTag(schema)).toBe("SHOP_DATASET");
    expect(isLookup("plugin")).toBe(true);
  });

  it("CON: `lookup` beside a picker the page draws itself changes nothing; no tag without a plugin list", () => {
    expect(pickerFor({ type: "string", picker: "directory", lookup: true })).toBe("directory");
    expect(pluginTag({ type: "string", picker: "directory", lookup: true })).toBe("");
    expect(pluginTag({ type: "string", picker: "PLATFORM_DATASET_ID" })).toBe("");
  });

  it("the lookups are the HuggingFace ones; the two browsers are not", () => {
    expect(isLookup("hf_dataset")).toBe(true);
    expect(isLookup("hf_metadata")).toBe(true);
    expect(isLookup("directory")).toBe(false);
    expect(isLookup("file")).toBe(false);
  });
});

describe("browseStart — where the file browser opens for a field", () => {
  it("a folder field: the folder it names, absolute or $VAR (the server expands it) or ~", () => {
    expect(browseStart("directory", "/data/gert/models")).toBe("/data/gert/models");
    expect(browseStart("directory", " $DATA_ROOT/runs ")).toBe("$DATA_ROOT/runs");
    expect(browseStart("directory", "~/graphs")).toBe("~/graphs");
  });

  it("a file field: the folder of the file it names", () => {
    expect(browseStart("file", "/data/models/best.ckpt")).toBe("/data/models");
    expect(browseStart("file", "$MODEL_ROOT/best.ckpt")).toBe("$MODEL_ROOT");
    expect(browseStart("file", "/best.ckpt")).toBeUndefined();
  });

  it("CON: empty or relative opens the editor's graph folder", () => {
    expect(browseStart("directory", "")).toBeUndefined();
    expect(browseStart("directory", "runs/out")).toBeUndefined();
    expect(browseStart("file", "best.ckpt")).toBeUndefined();
  });
});

describe("hfDataset and lookupKey — what an HF list is asked about", () => {
  it("the dataset from path (else repo_id), its subset and split, trimmed", () => {
    expect(hfDataset({ path: " ylecun/mnist ", name: null, split: "train" })).toEqual({ path: "ylecun/mnist", name: "", split: "train" });
    expect(hfDataset({ repo_id: "kitofrank/RFUAV" })).toEqual({ path: "kitofrank/RFUAV", name: "", split: "" });
  });

  it("one key per question; a list about a dataset asks nothing until one is named", () => {
    const mnist = { path: "ylecun/mnist", name: "", split: "train" };
    expect(lookupKey("hf_dataset", { path: "", name: "", split: "" })).toBe("datasets");
    expect(lookupKey("hf_split", mnist)).toBe("splits\u0000ylecun/mnist\u0000");
    expect(lookupKey("hf_feature", mnist)).toBe("features\u0000ylecun/mnist\u0000\u0000train");
    expect(lookupKey("hf_metadata", mnist)).toBe(lookupKey("hf_feature", mnist));
    expect(lookupKey("hf_feature", { path: "", name: "", split: "train" })).toBeUndefined();
  });
});

describe("cachedLookup — one answer per question for the page's life", () => {
  it("a second ask of the same key shares the first answer; another key asks again", async () => {
    let asked = 0;
    const fetch = async () => {
      asked += 1;
      return ["ylecun/mnist"];
    };
    expect(await cachedLookup("datasets", fetch)).toEqual(["ylecun/mnist"]);
    expect(await cachedLookup("datasets", fetch)).toEqual(["ylecun/mnist"]);
    expect(asked).toBe(1);
    await cachedLookup("splits\u0000ylecun/mnist\u0000", fetch);
    expect(asked).toBe(2);
  });

  it("a failed lookup is forgotten: the next ask tries again", async () => {
    let asked = 0;
    const failing = async (): Promise<string[]> => {
      asked += 1;
      throw new Error("the editor is busy");
    };
    await expect(cachedLookup("datasets", failing)).rejects.toThrow("the editor is busy");
    expect(await cachedLookup("datasets", async () => ["a"])).toEqual(["a"]);
    expect(asked).toBe(1);
  });
});

describe("forgetLookup — a list asked again on the person's word (\"↻ ask again\")", () => {
  it("the forgotten question is asked again; every other one keeps its answer", async () => {
    let models = ["$RUN_DIR/a/package"];
    let asked = 0;
    const listModels = async () => {
      asked += 1;
      return [...models];
    };
    const key = pluginLookupKey("TRAINED_MODEL", { model: "" });
    expect(await cachedLookup(key, listModels)).toEqual(["$RUN_DIR/a/package"]);
    models = ["$RUN_DIR/a/package", "$RUN_DIR/b/package"]; // a package made in between
    expect(await cachedLookup(key, listModels)).toEqual(["$RUN_DIR/a/package"]); // still the page's answer
    await cachedLookup("datasets", async () => ["ylecun/mnist"]);

    forgetLookup(key);

    expect(await cachedLookup(key, listModels)).toEqual(["$RUN_DIR/a/package", "$RUN_DIR/b/package"]);
    expect(asked).toBe(2);
    expect(await cachedLookup("datasets", async () => ["other"])).toEqual(["ylecun/mnist"]);
  });

  it("the entry that asks again is no name a list can hold", () => {
    expect(ASK_AGAIN.startsWith("\u0000")).toBe(true);
  });
});

describe("metadata_features — the text, the list, and 'add column'", () => {
  it("the value as columns and as text", () => {
    expect(metadataList(["a", "b"])).toEqual(["a", "b"]);
    expect(metadataList(" a, b ,, c")).toEqual(["a", "b", "c"]);
    expect(metadataList(ALL_COLUMNS)).toEqual(["*"]);
    expect(metadataList(null)).toEqual([]);
    expect(metadataText(["a", "b"])).toBe("a, b");
    expect(metadataText("*")).toBe("*");
  });

  it("a typed text commits null (none), '*' (every column but input/target) or a list", () => {
    expect(parseMetadata("")).toBeNull();
    expect(parseMetadata(" * ")).toBe("*");
    expect(parseMetadata("snr, label")).toEqual(["snr", "label"]);
  });

  it("'add column' appends once; '*' replaces the list; a column after '*' starts a list", () => {
    expect(withColumn(["snr"], "label")).toEqual(["snr", "label"]);
    expect(withColumn(["snr"], "snr")).toEqual(["snr"]);
    expect(withColumn(null, "snr")).toEqual(["snr"]);
    expect(withColumn(["snr"], "*")).toBe("*");
    expect(withColumn("*", "snr")).toEqual(["snr"]);
  });

  it("the choices: '*' first, then the columns the node does not read as input or target, nor lists already", () => {
    const columns = ["image", "label", "snr", "drone"];
    expect(metadataChoices(columns, { input_feature: "image", target_feature: "label", metadata_features: ["snr"] })).toEqual(["*", "drone"]);
    expect(metadataChoices(columns, { input_feature: "image", target_feature: "label", metadata_features: "*" })).toEqual(["snr", "drone"]);
    expect(metadataChoices([], {})).toEqual(["*"]);
  });
});

describe("rangePair — a (min, max) pair read off its schema", () => {
  const DEVIATION = {
    type: "array",
    minItems: 2,
    maxItems: 2,
    prefixItems: [
      { type: "number", minimum: 0, maximum: 5e6 },
      { type: "number", minimum: 0, maximum: 5e6 },
    ],
    default: [100000, 750000],
  };

  it("two numeric ends with their bounds — also as the arm of an anyOf with null", () => {
    const ends = [
      { type: "number", minimum: 0, maximum: 5e6 },
      { type: "number", minimum: 0, maximum: 5e6 },
    ];
    expect(rangePair(DEVIATION)).toEqual(ends);
    expect(rangePair({ anyOf: [{ type: "array", prefixItems: [{ type: "integer" }, { type: "integer" }] }, { type: "null" }] })).toEqual([
      { type: "integer" },
      { type: "integer" },
    ]);
  });

  it("CON: a list of strings, a three-tuple, a pair of strings, a plain number are no pair", () => {
    expect(rangePair({ type: "array", items: { type: "string" } })).toBeUndefined();
    expect(rangePair({ type: "array", prefixItems: [{ type: "number" }, { type: "number" }, { type: "number" }] })).toBeUndefined();
    expect(rangePair({ type: "array", prefixItems: [{ type: "string" }, { type: "string" }] })).toBeUndefined();
    expect(rangePair({ type: "number" })).toBeUndefined();
  });

  it("the value shown: the spelled pair, else the schema's default, else none", () => {
    expect(pairValue([1, 2], [3, 4])).toEqual([1, 2]);
    expect(pairValue(null, [3, 4])).toEqual([3, 4]);
    expect(pairValue("x", "y")).toBeUndefined();
  });
});

describe("followPair — a (min, max) pair's two boxes when the value under them changes", () => {
  it("an answer arriving while the max box is typed into keeps what was typed there (measured before: 7 → 7500006)", () => {
    // min typed and left: [111000, 750000] is committed; the person is typing 7… into max when the answer arrives
    expect(followPair(["111000", "7"], ["100000", "750000"], ["111000", "750000"])).toEqual(["111000", "7"]);
  });
  it("CON: a box nobody changed takes the new value — an undo, the other surface, a host's switch", () => {
    expect(followPair(["111000", "761000"], ["111000", "761000"], ["100000", "750000"])).toEqual(["100000", "750000"]);
    expect(followPair(["111000", "750000"], ["100000", "750000"], ["111000", "750000"])).toEqual(["111000", "750000"]);
  });
});

describe("a plugin list's question — the tag and the node's values", () => {
  it("text, numbers and switches are sent as text; a container is not", () => {
    expect(pluginValues({ dataset: "bte", count: 3, strict: false, tags: ["a"], gone: undefined, none: null })).toEqual({
      dataset: "bte",
      count: "3",
      strict: "false",
    });
  });

  it("a changed field is another question; the order of the fields is not", () => {
    const one = pluginLookupKey("SHOP_VERSION", { project: "store", dataset: "bte" });
    expect(pluginLookupKey("SHOP_VERSION", { dataset: "bte", project: "store" })).toBe(one);
    expect(pluginLookupKey("SHOP_VERSION", { project: "store", dataset: "lte" })).not.toBe(one);
    expect(pluginLookupKey("SHOP_DATASET", { project: "store", dataset: "bte" })).not.toBe(one);
  });
});

describe("a field that takes a number or text", () => {
  it("is one when the schema's arms are a number and a string, the number arm's word answered", () => {
    expect(numberOrText({ anyOf: [{ type: "number" }, { type: "string" }], default: "0" })).toBe("number");
    expect(numberOrText({ anyOf: [{ type: "integer" }, { type: "string" }] })).toBe("integer");
    expect(numberOrText({ anyOf: [{ type: "number" }, { type: "null" }] })).toBeUndefined();
    expect(numberOrText({ anyOf: [{ type: "string" }, { type: "null" }] })).toBeUndefined();
    expect(numberOrText({ type: "number" })).toBeUndefined();
  });

  it("sends a plain number as a number and anything else as the text typed", () => {
    expect(numberOrTextValue("200 kSa/s", "number")).toBe("200 kSa/s");
    expect(numberOrTextValue("30kHz", "integer")).toBe("30kHz");
    expect(numberOrTextValue("250000", "number")).toBe(250000);
    expect(numberOrTextValue(" 1e6 ", "number")).toBe(1e6);
    expect(numberOrTextValue("4096", "integer")).toBe(4096);
    expect(numberOrTextValue("2.5", "integer")).toBe("2.5");
  });
});

describe("a list of text — typed as names separated by commas", () => {
  it("the list as text and the text as a list", () => {
    expect(textListText(["cat", "dog"])).toBe("cat, dog");
    expect(textListText([])).toBe("");
    expect(textListText(null)).toBe("");
    expect(parseTextList("cat, dog")).toEqual(["cat", "dog"]);
  });

  it("spaces around a name go, and an empty name between commas is no name", () => {
    expect(parseTextList("  cat ,dog ,, bird  ")).toEqual(["cat", "dog", "bird"]);
    expect(parseTextList("two words, one")).toEqual(["two words", "one"]);
  });

  it("an empty box is an empty list", () => {
    expect(parseTextList("")).toEqual([]);
    expect(parseTextList("  ,  ")).toEqual([]);
  });

  it("a list the text can spell is editable: unset, empty, or plain names", () => {
    expect(textListEditable(null)).toBe(true);
    expect(textListEditable(undefined)).toBe(true);
    expect(textListEditable([])).toBe(true);
    expect(textListEditable(["cat", "dog"])).toBe(true);
    expect(textListEditable(["0", "1"])).toBe(true);
  });

  it("CON: a list the text cannot spell back stays read-only", () => {
    expect(textListEditable(["a, b", "c"])).toBe(false); // a name holding a comma would come back as two
    expect(textListEditable([" padded"])).toBe(false); // the spaces would be trimmed away
    expect(textListEditable([""])).toBe(false); // an empty name would vanish
    expect(textListEditable([1, 2])).toBe(false); // numbers would come back as text
    expect(textListEditable([["a"], ["b"]])).toBe(false);
    expect(textListEditable("cat")).toBe(false); // a value that is no list (a reference, a typed word)
    expect(textListEditable({ a: 1 })).toBe(false);
  });

  it("what is editable round-trips", () => {
    for (const names of [[], ["cat"], ["cat", "dog"], ["two words", "x"]]) {
      expect(textListEditable(names)).toBe(true);
      expect(parseTextList(textListText(names))).toEqual(names);
    }
  });
});

describe("a list of numbers — typed as numbers separated by commas", () => {
  it("is read off its schema: a list of numbers or of whole numbers, also beside null", () => {
    expect(numberList({ type: "array", items: { type: "number" } })).toBe("number");
    expect(numberList({ anyOf: [{ type: "array", items: { type: "integer" } }, { type: "null" }] })).toBe("integer");
  });

  it("is nothing else: names, a fixed count, a list of lists, a number beside the list", () => {
    expect(numberList({ type: "array", items: { type: "string" } })).toBeUndefined();
    expect(numberList({ type: "array", prefixItems: [{ type: "number" }, { type: "number" }] })).toBeUndefined();
    expect(numberList({ type: "array", items: { type: "array", items: { type: "number" } } })).toBeUndefined();
    expect(numberList({ anyOf: [{ type: "integer" }, { type: "array", items: { type: "integer" } }] })).toBeUndefined();
    expect(numberList({ type: "number" })).toBeUndefined();
  });

  it("the list as text and the text as a list", () => {
    expect(numberListText([1200, 2400.5, -3])).toBe("1200, 2400.5, -3");
    expect(numberListText(null)).toBe("");
    expect(parseNumberList("1200, 2400.5 ,-3", "number")).toEqual([1200, 2400.5, -3]);
    expect(parseNumberList(" 1e3 ", "number")).toEqual([1000]);
    expect(parseNumberList("", "number")).toEqual([]);
  });

  it("text that is not all numbers is no list, and a fraction is no whole number", () => {
    expect(parseNumberList("1, two", "number")).toBeUndefined();
    expect(parseNumberList("1,,2", "number")).toBeUndefined();
    expect(parseNumberList("1, 2.5", "integer")).toBeUndefined();
    expect(parseNumberList("1, 2", "integer")).toEqual([1, 2]);
  });

  it("a list the text can spell is editable: unset, empty, or plain numbers", () => {
    expect(numberListEditable(null)).toBe(true);
    expect(numberListEditable([])).toBe(true);
    expect(numberListEditable([1, 2.5])).toBe(true);
    expect(numberListEditable([1, "2"])).toBe(false);
    expect(numberListEditable([[1, 2]])).toBe(false);
    expect(numberListEditable("1, 2")).toBe(false);
  });
});

describe("tupleEnds — a fixed count of numbers read off its schema", () => {
  const SHAPE = { type: "array", prefixItems: [{ type: "integer" }, { type: "integer" }, { type: "integer" }] };

  it("three whole numbers are three ends, also beside null; a pair is two", () => {
    const ends = [{ type: "integer" }, { type: "integer" }, { type: "integer" }];
    expect(tupleEnds(SHAPE)).toEqual(ends);
    expect(tupleEnds({ anyOf: [SHAPE, { type: "null" }] })).toEqual(ends);
    expect(tupleEnds({ type: "array", prefixItems: [{ type: "number", minimum: 0 }, { type: "number" }] })).toEqual([
      { type: "number", minimum: 0 },
      { type: "number" },
    ]);
  });

  it("is nothing else: a member that is no number, an open list, a number", () => {
    expect(tupleEnds({ type: "array", prefixItems: [{ type: "integer" }, { type: "string" }] })).toBeUndefined();
    expect(tupleEnds({ type: "array", items: { type: "integer" } })).toBeUndefined();
    expect(tupleEnds({ type: "integer" })).toBeUndefined();
  });

  it("its value is that many numbers: the value, else the default, else none", () => {
    expect(pairValue([3, 224, 224], null, 3)).toEqual([3, 224, 224]);
    expect(pairValue(null, [1, 2, 3], 3)).toEqual([1, 2, 3]);
    expect(pairValue([3, 224], null, 3)).toBeUndefined();
  });

  it("three boxes follow the value like two do", () => {
    expect(followPair(["3", "9", "224"], ["3", "224", "224"], ["4", "224", "224"])).toEqual(["4", "9", "224"]);
  });
});

describe("names with a value each — one row per name", () => {
  it("is read off its schema: a map to numbers, whole numbers or text, also beside null", () => {
    expect(scalarMap({ type: "object", additionalProperties: { type: "integer" } })).toBe("integer");
    expect(scalarMap({ type: "object", additionalProperties: { type: "number" } })).toBe("number");
    expect(scalarMap({ anyOf: [{ type: "object", additionalProperties: { type: "string" } }, { type: "null" }] })).toBe("string");
  });

  it("is nothing else: an open map, a map of lists, a model with named fields", () => {
    expect(scalarMap({ type: "object", additionalProperties: true })).toBeUndefined();
    expect(scalarMap({ type: "object" })).toBeUndefined();
    expect(scalarMap({ type: "object", additionalProperties: { type: "array", items: { type: "number" } } })).toBeUndefined();
    expect(scalarMap({ type: "object", properties: { a: { type: "integer" } }, additionalProperties: { type: "integer" } })).toBeUndefined();
    expect(scalarMap({ type: "string" })).toBeUndefined();
  });

  it("the map as rows and the rows as a map, in the order written", () => {
    expect(mapRows({ dog: 1, cat: 0 })).toEqual([["dog", "1"], ["cat", "0"]]);
    expect(mapRows(null)).toEqual([]);
    expect(parseMapRows([["dog", "1"], [" cat ", " 0 "]], "integer")).toEqual({ dog: 1, cat: 0 });
    expect(parseMapRows([["a", "x y"]], "string")).toEqual({ a: "x y" });
    expect(parseMapRows([], "integer")).toEqual({});
  });

  it("a row left empty is no row; a half-typed or doubled one is no map yet", () => {
    expect(parseMapRows([["cat", "0"], ["", ""]], "integer")).toEqual({ cat: 0 });
    expect(parseMapRows([["cat", ""]], "integer")).toBeUndefined();
    expect(parseMapRows([["", "1"]], "integer")).toBeUndefined();
    expect(parseMapRows([["cat", "0"], ["cat", "1"]], "integer")).toBeUndefined();
    expect(parseMapRows([["cat", "0.5"]], "integer")).toBeUndefined();
    expect(parseMapRows([["cat", "0.5"]], "number")).toEqual({ cat: 0.5 });
    expect(parseMapRows([["cat", "zero"]], "number")).toBeUndefined();
  });

  it("a map the rows can spell is editable: unset, empty, or values of its one kind", () => {
    expect(mapEditable(null, "integer")).toBe(true);
    expect(mapEditable({}, "integer")).toBe(true);
    expect(mapEditable({ cat: 0 }, "integer")).toBe(true);
    expect(mapEditable({ cat: "0" }, "integer")).toBe(false);
    expect(mapEditable({ cat: "x" }, "string")).toBe(true);
    expect(mapEditable({ cat: { a: 1 } }, "string")).toBe(false);
    expect(mapEditable([1], "integer")).toBe(false);
  });
});
