/**
 * Adding a class, the pure half of WHAT the page sends (`cards.ts`, the user's fix of 2026-10-05): an op (the catalogue's
 * kind `op` or `algorithm`) goes where the insert target says — after a step or a list member, or at the end of a list —
 * and with nowhere to go it is refused in the page's words, nothing sent; any other class is a CARD of its own, which
 * the server writes as a new top-level key above the graph's root (`Document.add_node`), so the page sends it whatever
 * the place, with the end of the wire it was picked from. Measured before, on the files graph a host workspace seeds,
 * Add Node ▸ ClassNamesScan answered `add recordstream.ops.contract.ClassNamesScan to stream.ops at [1] refused at …`
 * (nothing selected) or `a new flow step needs a name — after '<host>', it has none` (the host card selected). The DOM
 * half is measured under playwright in `tests/test_canvas.py::TestACardOfItsOwn`.
 */
import { describe, expect, it } from "vitest";
import { NOWHERE, STEP_KINDS, addPlan, addsAsStep, draggedWire } from "../cards";

const SCAN = "recordstream.ops.contract.ClassNamesScan";
const NAMES = "recordstream.ops.contract.ClassNamesOutput";
const PROBE = "recordstream.ops.debug.PrintRecordOp";

describe("addsAsStep — which catalogue kinds go after a step or into an op list", () => {
  it("an op and an algorithm do (a subgraph too, though it is made, never added)", () => {
    expect(STEP_KINDS).toEqual(["op", "algorithm", "subgraph"]);
    for (const kind of STEP_KINDS) expect(addsAsStep(kind)).toBe(true);
  });

  it("every other kind is a card of its own", () => {
    for (const kind of ["value", "source", "engine", "sink", "runnable", "contract"]) expect(addsAsStep(kind)).toBe(false);
  });

  it("a class the catalogue does not list takes the way every add took before cards", () => {
    expect(addsAsStep(undefined)).toBe(true);
  });
});

describe("draggedWire — the end a dropped wire was dragged out of; the new card is its other end", () => {
  it("out of an INPUT (the root's classes): the new card feeds it", () => {
    expect(draggedWire({ node: "contract", kind: "in", socket: "classes" })).toEqual({ target: "contract", target_socket: "classes" });
  });

  it("out of an OUTPUT: it goes into the new card", () => {
    expect(draggedWire({ node: "file_source", kind: "out", socket: "stream" })).toEqual({ source: "file_source", source_socket: "stream" });
  });
});

describe("addPlan — what the page sends for an add, by the class's kind", () => {
  it("an op after a step or a member: its place", () => {
    expect(addPlan("op", PROBE, "printrecordop", { kind: "after", node: "stream.ops[0]" })).toEqual({
      card: false,
      request: { name: "printrecordop", after: "stream.ops[0]", class_path: PROBE, params: {} },
    });
  });

  it("an op at the end of a list: the list", () => {
    expect(addPlan("algorithm", PROBE, "p", { kind: "end", list: "stream.ops" })).toEqual({
      card: false,
      request: { name: "p", list: "stream.ops", class_path: PROBE, params: {} },
    });
  });

  it("an op with nowhere to go is refused in the page's words — nothing is sent", () => {
    const why = "this graph holds 2 op lists (stream.ops, other.ops) — select a member of one, or the node holding it, to add to it";
    expect(addPlan("op", PROBE, "p", { kind: "none", why })).toEqual({ refused: why });
    expect(addPlan("op", PROBE, "p", { kind: "none" })).toEqual({ refused: NOWHERE });
    expect(NOWHERE).toBe("nothing to add after — a step is inserted after another one");
  });

  it("the user's case: a value is a card — sent with the place the page had, which the server leaves to ops", () => {
    expect(addPlan("value", SCAN, "classnamesscan", { kind: "after", node: "stream.ops[0]" })).toEqual({
      card: true,
      request: { name: "classnamesscan", after: "stream.ops[0]", class_path: SCAN, params: {} },
    });
  });

  it("a value with nowhere to go is a card all the same — no refusal", () => {
    expect(addPlan("value", SCAN, "classnamesscan", { kind: "none", why: "nothing feeds contract" })).toEqual({
      card: true,
      request: { name: "classnamesscan", class_path: SCAN, params: {} },
    });
  });

  it("the wire-drop pick of a value is ONE request carrying the dragged end", () => {
    const wire = draggedWire({ node: "contract", kind: "in", socket: "classes" });
    const plan = addPlan("value", NAMES, "classnamesoutput", { kind: "none", why: "nothing feeds contract" }, wire);
    expect(plan).toEqual({
      card: true,
      request: { name: "classnamesoutput", class_path: NAMES, params: {}, wire: { target: "contract", target_socket: "classes" } },
    });
  });

  it("CON — an op picked from a dropped wire sends no wire: the place after the dragged node IS the wire", () => {
    const wire = draggedWire({ node: "stream.ops[0]", kind: "out", socket: "record" });
    const plan = addPlan("op", PROBE, "p", { kind: "after", node: "stream.ops[0]" }, wire);
    expect(plan).toEqual({ card: false, request: { name: "p", after: "stream.ops[0]", class_path: PROBE, params: {} } });
  });
});
