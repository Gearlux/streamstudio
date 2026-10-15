/**
 * The RULE behind asking for the catalogue (`GET api/palette`) on FIRST NEED (`catalogue.ts`): nothing shown at open
 * needs it, each of the Nodes sidebar, Add Node ▸, the palette and Set source… does, the first need asks once, and
 * only a failed answer lets the next need ask again. The server is a fake fetcher — no network.
 *
 * These tests do NOT render the App. `page` below is a stand-in that calls the rule the way `App.tsx` does (its effect
 * over what is shown, and Add Node ▸ asking when its menu is drawn), so an edit to `App.tsx` itself leaves them green.
 * The App's wiring is pinned by the browser test `tests/test_canvas.py::TestTheCatalogueIsAskedForOnFirstNeed`, which
 * runs against the built page. Measured 2026-10-02 by breaking `App.tsx` and rebuilding:
 *
 *   the fetch at open restored             these tests 391 passed   browser test: assert ['…/api/palette'] == []
 *   the effect told palette/Set source     these tests 391 passed   browser test: [palette] and [set-source] time out
 *   are never shown                                                 waiting for data-palette-ready="true"
 */
import { describe, expect, it } from "vitest";
import { NOTHING_SHOWN, needsCatalogue, onFirstNeed, oncePerClass, type CatalogueShown } from "../catalogue";

interface Server {
  asked: number;
  answer: () => void;
  refuse: () => void;
}

/** A fetcher whose answers the test releases by hand, in order. */
function server(): Server & { fetch: () => Promise<string[]> } {
  const waiting: { resolve: (value: string[]) => void; reject: (exc: Error) => void }[] = [];
  const state = {
    asked: 0,
    fetch: () => {
      state.asked += 1;
      return new Promise<string[]>((resolve, reject) => waiting.push({ resolve, reject }));
    },
    answer: () => waiting.shift()?.resolve(["recordstream.ops.numpy.Threshold"]),
    refuse: () => waiting.shift()?.reject(new Error("the editor could not describe its classes")),
  };
  return state;
}

const settle = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

/** A stand-in for the page (not the App): the rule called as the App's effect calls it, plus Add Node ▸ asking. */
function page(fetch: () => Promise<string[]>) {
  const arrived: string[][] = [];
  const errors: string[] = [];
  const need = onFirstNeed(fetch, (specs) => arrived.push(specs), (exc) => errors.push(String(exc)));
  return {
    arrived,
    errors,
    render: (shown: Partial<CatalogueShown>) => {
      if (needsCatalogue({ ...NOTHING_SHOWN, ...shown })) need();
    },
    addNodeDrawn: () => need(),
  };
}

describe("needsCatalogue — what on the page lists the catalogue", () => {
  it("the page as it opens shows nothing that lists it", () => {
    expect(needsCatalogue(NOTHING_SHOWN)).toBe(false);
  });

  it("each surface that lists it needs it", () => {
    expect(needsCatalogue({ ...NOTHING_SHOWN, sidebar: true })).toBe(true);
    expect(needsCatalogue({ ...NOTHING_SHOWN, palette: true })).toBe(true);
    expect(needsCatalogue({ ...NOTHING_SHOWN, setSource: true })).toBe(true);
  });
});

describe("the rule, through the stand-in page: the first need asks, once", () => {
  it("nothing shown at open asks — renders while the graph loads do not either", async () => {
    const fake = server();
    const shown = page(fake.fetch);
    shown.render({});
    shown.render({});
    await settle();
    expect(fake.asked).toBe(0);
  });

  const firsts: [string, (p: ReturnType<typeof page>) => void][] = [
    ["the Nodes sidebar", (p) => p.render({ sidebar: true })],
    ["Add Node ▸", (p) => p.addNodeDrawn()],
    ["the palette", (p) => p.render({ palette: true })],
    ["Set source…", (p) => p.render({ setSource: true })],
  ];

  for (const [name, open] of firsts) {
    it(`${name} asks once, and nothing asks again after the answer`, async () => {
      const fake = server();
      const shown = page(fake.fetch);
      shown.render({});
      open(shown);
      open(shown); // a re-render while the answer is on its way
      expect(fake.asked).toBe(1);
      fake.answer();
      await settle();
      expect(shown.arrived).toEqual([["recordstream.ops.numpy.Threshold"]]);
      // every other surface, afterwards: the catalogue is there
      shown.render({ sidebar: true, palette: true, setSource: true });
      shown.addNodeDrawn();
      expect(fake.asked).toBe(1);
    });
  }

  it("a refused answer is reported, and the next need asks again", async () => {
    const fake = server();
    const shown = page(fake.fetch);
    shown.render({ sidebar: true });
    fake.refuse();
    await settle();
    expect(shown.errors).toEqual(["Error: the editor could not describe its classes"]);
    expect(shown.arrived).toEqual([]);
    shown.render({ palette: true });
    expect(fake.asked).toBe(2);
    fake.answer();
    await settle();
    expect(shown.arrived).toHaveLength(1);
    shown.render({ setSource: true });
    expect(fake.asked).toBe(2);
  });

  it("onFirstNeed answers whether it asked", () => {
    const fake = server();
    const need = onFirstNeed(fake.fetch, () => undefined, () => undefined);
    expect(need()).toBe(true);
    expect(need()).toBe(false);
  });
});

/**
 * A class's settings (`GET api/palette/<class>`) are asked when the class is SELECTED — Set source…'s pick, the Nodes
 * sidebar's card — once per class for the page's life; a refused answer is forgotten, so the next selection asks again.
 */
describe("oncePerClass — a class's settings, asked on selection", () => {
  function settings() {
    const waiting = new Map<string, { resolve: (value: string) => void; reject: (exc: Error) => void }[]>();
    const asked: string[] = [];
    return {
      asked,
      fetch: (classPath: string) => {
        asked.push(classPath);
        return new Promise<string>((resolve, reject) =>
          waiting.set(classPath, [...(waiting.get(classPath) ?? []), { resolve, reject }]),
        );
      },
      answer: (classPath: string) => waiting.get(classPath)?.shift()?.resolve(`settings of ${classPath}`),
      refuse: (classPath: string) => waiting.get(classPath)?.shift()?.reject(new Error(`no class ${classPath}`)),
    };
  }

  it("selecting a class twice asks once, and both selections get the answer", async () => {
    const fake = settings();
    const settingsOf = oncePerClass(fake.fetch);
    const first = settingsOf("recordstream.sources.files.FilesSource");
    const again = settingsOf("recordstream.sources.files.FilesSource");
    fake.answer("recordstream.sources.files.FilesSource");
    expect(await first).toBe("settings of recordstream.sources.files.FilesSource");
    expect(await again).toBe("settings of recordstream.sources.files.FilesSource");
    expect(await settingsOf("recordstream.sources.files.FilesSource")).toBe(
      "settings of recordstream.sources.files.FilesSource",
    );
    expect(fake.asked).toEqual(["recordstream.sources.files.FilesSource"]);
  });

  it("each class is asked for its own settings", () => {
    const fake = settings();
    const settingsOf = oncePerClass(fake.fetch);
    void settingsOf("recordstream.ops.numpy.Threshold");
    void settingsOf("recordstream.ops.numpy.Scale");
    expect(fake.asked).toEqual(["recordstream.ops.numpy.Threshold", "recordstream.ops.numpy.Scale"]);
  });

  it("a refused answer is forgotten: the next selection asks again", async () => {
    const fake = settings();
    const settingsOf = oncePerClass(fake.fetch);
    const refused = settingsOf("recordstream.ops.numpy.Threshold");
    fake.refuse("recordstream.ops.numpy.Threshold");
    await expect(refused).rejects.toThrow("no class recordstream.ops.numpy.Threshold");
    const asked = settingsOf("recordstream.ops.numpy.Threshold");
    fake.answer("recordstream.ops.numpy.Threshold");
    expect(await asked).toBe("settings of recordstream.ops.numpy.Threshold");
    expect(fake.asked).toEqual(["recordstream.ops.numpy.Threshold", "recordstream.ops.numpy.Threshold"]);
  });
});
