/**
 * Docstring markup in the Nodes sidebar, its hover card, the palette and Add Node ▸: reStructuredText literals
 * (``` ``Mask`` ```), interpreted text (`` `Mask` ``) and Sphinx roles (`` :class:`DirectorySink` ``) become CODE spans
 * holding the name a reader should see — never the markup — and `plainDoc` is the same text for a tooltip.
 */
import { describe, expect, it } from "vitest";
import { docSpans, plainDoc, shownTarget } from "../rst";

const code = (text: string) => ({ code: true, text });
const text = (value: string) => ({ code: false, text: value });

describe("docSpans", () => {
  it("draws a double-backtick literal as code", () => {
    expect(docSpans("Keep the ``Mask`` entry.")).toEqual([text("Keep the "), code("Mask"), text(" entry.")]);
  });

  it("draws single-backtick interpreted text as code", () => {
    expect(docSpans("the `image` entry")).toEqual([text("the "), code("image"), text(" entry")]);
  });

  it("drops a Sphinx role and keeps its target as code — the measured DirectorySource line", () => {
    const doc = "Read records written by :class:`DirectorySink` (one ``fields.json`` + one file per entry).";
    expect(docSpans(doc)).toEqual([
      text("Read records written by "),
      code("DirectorySink"),
      text(" (one "),
      code("fields.json"),
      text(" + one file per entry)."),
    ]);
    expect(docSpans(doc).some((span) => span.text.includes(":class:"))).toBe(false);
  });

  it("drops every role spelling: :func:, :meth:, :data:, :mod:, :attr:, a domain prefix", () => {
    for (const role of [":func:", ":meth:", ":data:", ":mod:", ":attr:", ":exc:", ":obj:", ":py:class:"])
      expect(docSpans(`see ${role}\`thing\` here`)).toEqual([text("see "), code("thing"), text(" here")]);
  });

  it("shows a ~target by its last segment and a titled target by its title", () => {
    expect(docSpans(":meth:`~recordstream.core.stream.Stream.run`")).toEqual([code("run")]);
    expect(docSpans(":class:`the sink <recordstream.storage.DirectorySink>`")).toEqual([code("the sink")]);
  });

  it("keeps a hyperlink reference as text", () => {
    expect(docSpans("read `the guide`_ first")).toEqual([text("read the guide first")]);
  });

  it("keeps a lone backtick and plain text as they are", () => {
    expect(docSpans("a ` alone")).toEqual([text("a ` alone")]);
    expect(docSpans("no markup at all")).toEqual([text("no markup at all")]);
    expect(docSpans("")).toEqual([]);
  });

  it("keeps a single backtick inside a double-backtick literal", () => {
    expect(docSpans("``a`b``")).toEqual([code("a`b")]);
  });
});

describe("plainDoc", () => {
  it("is the description without markup, for a tooltip", () => {
    expect(plainDoc("Read records written by :class:`DirectorySink` (one ``fields.json``).")).toBe(
      "Read records written by DirectorySink (one fields.json).",
    );
  });
});

describe("shownTarget", () => {
  it("strips the ! of a suppressed link and follows ~", () => {
    expect(shownTarget("!Stream")).toBe("Stream");
    expect(shownTarget("~a.b.C")).toBe("C");
    expect(shownTarget("plain")).toBe("plain");
  });
});
