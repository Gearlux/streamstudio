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
  followPair,
  pairValue,
  parseMetadata,
  pickerFor,
  pluginLookupKey,
  pluginTag,
  pluginValues,
  rangePair,
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
