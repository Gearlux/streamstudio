import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ConfirmClearDialog from "./ConfirmClearDialog";

describe("ConfirmClearDialog", () => {
  it("renders the message and Cancel/Discard buttons", () => {
    render(<ConfirmClearDialog message="Discard?" onCancel={() => {}} onConfirm={() => {}} />);
    expect(screen.getByText("Discard?")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /cancel/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /discard/i })).toBeInTheDocument();
  });

  it("calls onCancel when Cancel is clicked", () => {
    const onCancel = vi.fn();
    render(<ConfirmClearDialog message="Discard?" onCancel={onCancel} onConfirm={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onCancel).toHaveBeenCalledOnce();
  });

  it("passes dontAskAgain=false to onConfirm when the checkbox is unticked", () => {
    const onConfirm = vi.fn();
    render(<ConfirmClearDialog message="Discard?" onCancel={() => {}} onConfirm={onConfirm} />);
    fireEvent.click(screen.getByRole("button", { name: /discard/i }));
    expect(onConfirm).toHaveBeenCalledOnce();
    expect(onConfirm).toHaveBeenLastCalledWith(false);
  });

  it("passes dontAskAgain=true to onConfirm when the checkbox is ticked", () => {
    const onConfirm = vi.fn();
    render(<ConfirmClearDialog message="Discard?" onCancel={() => {}} onConfirm={onConfirm} />);
    fireEvent.click(screen.getByLabelText(/Don't ask again/i));
    fireEvent.click(screen.getByRole("button", { name: /discard/i }));
    expect(onConfirm).toHaveBeenCalledOnce();
    expect(onConfirm).toHaveBeenLastCalledWith(true);
  });
});
