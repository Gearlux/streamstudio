/**
 * Explicit Save, the page's half: which graphs hold changes not yet written (the server's `dirty`, per graph), how
 * the title and the graph dropdown mark them with ●, and whether leaving the page must ask first.
 */
import { describe, expect, it } from "vitest";
import { DIRTY_MARK, anyDirty, fileTitle, markDirty, pageTitle, pickerLabel } from "../dirty";
import type { GraphSummary } from "../model";

const entry = (name: string, dirty = false): GraphSummary => ({ name, path: `/g/${name}.yaml`, kind: "flow", dirty });

describe("markDirty", () => {
  it("sets one entry's flag and leaves the others", () => {
    const list = [entry("a"), entry("b")];
    expect(markDirty(list, "b", true)).toEqual([entry("a"), entry("b", true)]);
    expect(list[1].dirty).toBe(false); // not edited in place
  });

  it("answers the SAME list when nothing changes (no redraw), and for a name it does not hold", () => {
    const list = [entry("a", true)];
    expect(markDirty(list, "a", true)).toBe(list);
    expect(markDirty(list, "zzz", true)).toBe(list);
  });
});

describe("anyDirty — what arms the beforeunload prompt", () => {
  it("is true when one listed graph is dirty — every graph shown is listed (New… writes its file at once)", () => {
    expect(anyDirty([entry("a"), entry("b")])).toBe(false);
    expect(anyDirty([entry("a"), entry("b", true)])).toBe(true);
    expect(anyDirty([])).toBe(false);
  });
});

describe("the marks", () => {
  it("the page title keeps its name and gains ● while the shown graph is dirty", () => {
    expect(pageTitle(false)).toBe("streamstudio editor");
    expect(pageTitle(true)).toBe(`${DIRTY_MARK} streamstudio editor`);
  });

  it("the dropdown names the graph and its kind, ● first when dirty", () => {
    expect(pickerLabel(entry("wires"))).toBe("wires · flow");
    expect(pickerLabel(entry("wires", true))).toBe("● wires · flow");
  });

  it("the bar's title is the file name", () => {
    expect(fileTitle({ path: "/g/wires.yaml", dirty: false })).toBe("wires.yaml");
    expect(fileTitle({ path: "/g/wires.yaml", dirty: true })).toBe("● wires.yaml");
  });
});
