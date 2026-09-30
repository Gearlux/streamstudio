/**
 * The server file dialog's path arithmetic — Open, Save As, Insert steps file… and Move to its own file… browse
 * `GET api/files` on the machine the editor runs on, so every path here is that machine's absolute path.
 */
import { describe, expect, it } from "vitest";
import { baseName, dirName, joinPath, stepsFileName, yamlName } from "../files";

describe("joinPath / dirName / baseName", () => {
  it("joins a folder and a name with one slash", () => {
    expect(joinPath("/data/graphs", "demo.yaml")).toBe("/data/graphs/demo.yaml");
    expect(joinPath("/data/graphs/", "demo.yaml")).toBe("/data/graphs/demo.yaml");
    expect(joinPath("/", "demo.yaml")).toBe("/demo.yaml");
  });

  it("takes a typed absolute path as it is", () => {
    expect(joinPath("/data/graphs", "/elsewhere/x.yaml")).toBe("/elsewhere/x.yaml");
  });

  it("splits a path into its folder and its name", () => {
    expect(dirName("/data/graphs/demo.yaml")).toBe("/data/graphs");
    expect(dirName("/demo.yaml")).toBe("/");
    expect(baseName("/data/graphs/demo.yaml")).toBe("demo.yaml");
    expect(baseName("demo.yaml")).toBe("demo.yaml");
  });
});

describe("yamlName — Save As and New always write a .yaml", () => {
  it("adds the suffix a typed name lacks, and keeps one that has it", () => {
    expect(yamlName("demo")).toBe("demo.yaml");
    expect(yamlName("demo.yaml")).toBe("demo.yaml");
    expect(yamlName("demo.yml")).toBe("demo.yml");
    expect(yamlName("  demo  ")).toBe("demo.yaml");
  });
});

describe("stepsFileName — Move to its own file…'s default", () => {
  it("is the subgraph's own name, the inner part of a qualified one", () => {
    expect(stepsFileName("prep")).toBe("prep.steps.yaml");
    expect(stepsFileName("outer/prep")).toBe("prep.steps.yaml");
    expect(stepsFileName("stream.ops[1]")).toBe("stream.ops_1.steps.yaml");
  });
});
