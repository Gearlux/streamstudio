/**
 * The sidebars' widths — the pure half: the limits of a drag, which way each edge grows its sidebar, and what the
 * browser remembers. The drag itself is measured under playwright in `tests/test_canvas.py`.
 */
import { describe, expect, it } from "vitest";
import { MIN_CANVAS, MIN_WIDTH, clampWidth, draggedWidth, initialWidth, readWidth, writeWidth } from "../widths";

const storage = (held: Record<string, string> = {}) => ({
  held,
  getItem: (key: string): string | null => held[key] ?? null,
  setItem: (key: string, value: string): void => {
    held[key] = value;
  },
  removeItem: (key: string): void => {
    delete held[key];
  },
});

describe("the width a sidebar starts at", () => {
  it("is 300 for the Nodes sidebar and 380 for the right panel", () => {
    expect(initialWidth("nodes", false)).toBe(300);
    expect(initialWidth("side", false)).toBe(380);
  });

  it("is narrower for the right panel inside a host's panel", () => {
    expect(initialWidth("side", true)).toBe(320);
    expect(initialWidth("nodes", true)).toBe(300);
  });
});

describe("the limits of a width", () => {
  it("keeps a width inside them as it is, in whole pixels", () => {
    expect(clampWidth(512.4, 1600, 300)).toBe(512);
  });

  it("never goes below the sidebar's minimum", () => {
    expect(clampWidth(40, 1600, 300)).toBe(MIN_WIDTH);
  });

  it("leaves the canvas its minimum beside the other sidebar", () => {
    expect(clampWidth(5000, 1600, 300)).toBe(1600 - 300 - MIN_CANVAS);
    expect(clampWidth(5000, 1600, 0)).toBe(1600 - MIN_CANVAS);
  });

  it("keeps the sidebar's minimum on a page too narrow for both limits", () => {
    expect(clampWidth(400, 500, 300)).toBe(MIN_WIDTH);
  });
});

describe("a drag of an edge", () => {
  it("grows the Nodes sidebar to the right", () => {
    expect(draggedWidth("nodes", 300, 300, 420)).toBe(420);
    expect(draggedWidth("nodes", 300, 300, 250)).toBe(250);
  });

  it("grows the right panel to the left", () => {
    expect(draggedWidth("side", 380, 1000, 880)).toBe(500);
    expect(draggedWidth("side", 380, 1000, 1100)).toBe(280);
  });
});

describe("what the browser remembers", () => {
  it("is nothing before a drag", () => {
    expect(readWidth("nodes", storage())).toBeNull();
  });

  it("is each sidebar's own width, in whole pixels", () => {
    const kept = storage();
    writeWidth("nodes", 412.6, kept);
    writeWidth("side", 640, kept);
    expect(readWidth("nodes", kept)).toBe(413);
    expect(readWidth("side", kept)).toBe(640);
  });

  it("is forgotten when the width goes back to its start", () => {
    const kept = storage();
    writeWidth("side", 640, kept);
    writeWidth("side", null, kept);
    expect(readWidth("side", kept)).toBeNull();
    expect(kept.held).toEqual({});
  });

  it.each(["", "wide", "-20", "0", "NaN"])("reads %j as no width", (stored) => {
    expect(readWidth("nodes", storage({ "streamstudio.nodes.width": stored }))).toBeNull();
  });

  it("survives a storage that refuses", () => {
    const refusing = {
      getItem: (): string | null => {
        throw new Error("blocked");
      },
      setItem: (): void => {
        throw new Error("blocked");
      },
      removeItem: (): void => {
        throw new Error("blocked");
      },
    };
    expect(readWidth("nodes", refusing)).toBeNull();
    expect(() => writeWidth("nodes", 400, refusing)).not.toThrow();
  });
});
