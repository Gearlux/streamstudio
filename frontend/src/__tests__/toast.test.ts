/**
 * A toast's **copy** button (the user's ask, 2026-10-05: "to report errors"): a refusal and a warning carry one, an
 * info toast does not, and it copies the WHOLE message — a located message runs past what anyone retypes. The DOM half
 * (`Toasts.tsx`: the button outside the `[data-toast]` box, whose text stays the message alone) is drawn by the page.
 */
import { describe, expect, it, vi } from "vitest";
import { COPYABLE, copyable, copyText } from "../toast";

const LOCATED =
  "add recordstream.ops.contract.ClassNamesScan to stream.ops at [1] refused at /w/files.yaml:12:7: cannot wire " +
  "stream.ops[1].object (object) into stream.ops[1] (Op) at /w/files.yaml:9:9: the types do not meet";

describe("copyable — which toasts carry the copy button", () => {
  it("an error and a warning do, an info toast does not", () => {
    expect(COPYABLE).toEqual(["error", "warning"]);
    expect(copyable("error")).toBe(true);
    expect(copyable("warning")).toBe(true);
    expect(copyable("info")).toBe(false);
  });
});

describe("copyText — the whole message to the clipboard", () => {
  it("writes the message whole, once, and says it did", async () => {
    const writeText = vi.fn(async () => undefined);
    expect(await copyText(LOCATED, { writeText })).toBe(true);
    expect(writeText).toHaveBeenCalledTimes(1);
    expect(writeText).toHaveBeenCalledWith(LOCATED);
  });

  it("a clipboard that refuses (no permission, no focus) is a copy that failed — never thrown", async () => {
    const writeText = vi.fn(async (): Promise<void> => {
      throw new DOMException("Document is not focused.", "NotAllowedError");
    });
    expect(await copyText(LOCATED, { writeText })).toBe(false);
  });

  it("a page without the clipboard API copies nothing", async () => {
    expect(await copyText(LOCATED, undefined)).toBe(false);
  });

  it("a refused clipboard falls back to the page's own copy command, with the whole message", async () => {
    const writeText = vi.fn(async (): Promise<void> => {
      throw new DOMException("Write permission denied.", "NotAllowedError");
    });
    const fallback = vi.fn(() => true);
    expect(await copyText(LOCATED, { writeText }, fallback)).toBe(true);
    expect(fallback).toHaveBeenCalledWith(LOCATED);
  });

  it("no clipboard API falls back too; a fallback that fails is a copy that failed", async () => {
    expect(await copyText(LOCATED, undefined, () => true)).toBe(true);
    expect(await copyText(LOCATED, undefined, () => false)).toBe(false);
  });

  it("a clipboard that copies never runs the fallback", async () => {
    const fallback = vi.fn(() => true);
    expect(await copyText(LOCATED, { writeText: vi.fn(async () => undefined) }, fallback)).toBe(true);
    expect(fallback).not.toHaveBeenCalled();
  });
});
