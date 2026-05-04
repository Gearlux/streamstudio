import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import NodePalette, { PALETTE_DRAG_MIME } from "./NodePalette";
import type { OpSchema } from "../types";

const OPS: OpSchema[] = [
  { path: "pkg:RescaleOp", name: "RescaleOp", doc: "", parameters: [], module: "pkg", category: "DataOp" },
  { path: "pkg:_internal", name: "_internal", doc: "", parameters: [], module: "pkg", category: "DataOp" },
  { path: "pkg:Source", name: "Source", doc: "", parameters: [], module: "pkg", category: "DataSource" },
];

describe("NodePalette", () => {
  it("hides _-prefixed ops by default", () => {
    render(<NodePalette ops={OPS} showInternal={false} onToggleShowInternal={() => {}} />);
    expect(screen.getByText("RescaleOp")).toBeInTheDocument();
    expect(screen.getByText("Source")).toBeInTheDocument();
    expect(screen.queryByText("_internal")).not.toBeInTheDocument();
  });

  it("reveals _-prefixed ops when showInternal is true", () => {
    render(<NodePalette ops={OPS} showInternal={true} onToggleShowInternal={() => {}} />);
    expect(screen.getByText("_internal")).toBeInTheDocument();
  });

  it("invokes onToggleShowInternal when the checkbox is clicked", () => {
    const onToggle = vi.fn();
    render(<NodePalette ops={OPS} showInternal={false} onToggleShowInternal={onToggle} />);
    fireEvent.click(screen.getByLabelText(/Show internal ops/i));
    expect(onToggle).toHaveBeenCalledOnce();
    expect(onToggle).toHaveBeenLastCalledWith(true);
  });

  it("writes a JSON drag payload on dragstart with the palette MIME type", () => {
    render(<NodePalette ops={OPS} showInternal={false} onToggleShowInternal={() => {}} />);
    const item = screen.getByText("RescaleOp").closest("li");
    expect(item).not.toBeNull();

    // jsdom's DataTransfer is a stub; capture what setData receives.
    const recorded: Array<[string, string]> = [];
    const dataTransfer = {
      setData: (k: string, v: string) => recorded.push([k, v]),
      types: [],
      effectAllowed: "" as DataTransfer["effectAllowed"],
    };
    fireEvent.dragStart(item!, { dataTransfer });

    expect(recorded).toHaveLength(1);
    const [mime, raw] = recorded[0];
    expect(mime).toBe(PALETTE_DRAG_MIME);
    expect(JSON.parse(raw)).toEqual({ op_path: "pkg:RescaleOp", category: "DataOp" });
    expect(dataTransfer.effectAllowed).toBe("move");
  });
});
