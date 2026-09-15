import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import { confirm, settleConfirm } from "../confirm";
import { ConfirmDialog } from "./ConfirmDialog";
import { Dialog, DialogContent } from "./Dialog";
import { Sheet, SheetContent } from "./Sheet";
import { notifyError, notifyOk } from "./Toaster";

afterEach(() => act(() => settleConfirm(false)));

describe("confirm", () => {
  it("resolves true on its confirm button and false on Cancel", async () => {
    render(<ConfirmDialog />);
    let answer = confirm("Delete node “nl-ams-03”?", { confirmLabel: "Delete" });
    expect(await screen.findByRole("dialog", { name: "Confirm" })).toHaveTextContent("Delete node “nl-ams-03”?");
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await expect(answer).resolves.toBe(true);

    answer = confirm("Disarm the kill-switch?");
    await userEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    await expect(answer).resolves.toBe(false);
  });

  it("Escape answers no", async () => {
    render(<ConfirmDialog />);
    const answer = confirm("Restore this backup?");
    await screen.findByRole("dialog");
    await userEvent.keyboard("{Escape}");
    await expect(answer).resolves.toBe(false);
  });

  it("a newer request settles the one it replaces with false, so nothing awaits forever", async () => {
    render(<ConfirmDialog />);
    const first = confirm("one");
    const second = confirm("two");
    await expect(first).resolves.toBe(false);
    expect(await screen.findByText("two")).toBeInTheDocument();
    act(() => settleConfirm(true));
    await expect(second).resolves.toBe(true);
  });
});

describe("toasts", () => {
  it("error toasts carry the backend's message and stay 20 s", () => {
    const error = vi.spyOn(toast, "error");
    notifyError(new ApiError(409, "That node is active."), "save failed");
    expect(error).toHaveBeenCalledWith("That node is active.", { duration: 20000 });
    notifyError(new Error("boom"), "save failed");
    expect(error).toHaveBeenLastCalledWith("save failed", { duration: 20000 });
  });

  it("success toasts expire after 8 s", () => {
    const success = vi.spyOn(toast, "success");
    notifyOk("saved");
    expect(success).toHaveBeenCalledWith("saved", { duration: 8000 });
  });
});

describe("Dialog and Sheet", () => {
  it("are named after their title", () => {
    render(<Dialog open><DialogContent title="Add server"><p>form</p></DialogContent></Dialog>);
    expect(screen.getByRole("dialog", { name: "Add server" })).toBeInTheDocument();
  });

  it("a dirty dialog asks before closing: Cancel keeps it open, Discard closes it", async () => {
    render(<ConfirmDialog />);
    const onOpenChange = vi.fn();
    const view = render(<Dialog open dirty onOpenChange={onOpenChange}><DialogContent title="Edit node"><p>form</p></DialogContent></Dialog>);
    await userEvent.keyboard("{Escape}");
    await userEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(onOpenChange).not.toHaveBeenCalled();

    await userEvent.keyboard("{Escape}");
    await userEvent.click(await screen.findByRole("button", { name: "Discard" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);

    // clean, it closes at once
    onOpenChange.mockClear();
    view.rerender(<Sheet open onOpenChange={onOpenChange}><SheetContent title="Node 12"><p>detail</p></SheetContent></Sheet>);
    await userEvent.keyboard("{Escape}");
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(screen.queryByRole("dialog", { name: "Confirm" })).not.toBeInTheDocument();
  });

  it("Escape closes a sheet only when focus is in it, or nowhere — not in another overlay above it", () => {
    const onOpenChange = vi.fn();
    render(
      <>
        <button type="button">in another overlay</button>
        <Sheet open onOpenChange={onOpenChange}><SheetContent title="Node 12"><button type="button">inside</button></SheetContent></Sheet>
      </>,
    );
    fireEvent.keyDown(screen.getByText("in another overlay"), { key: "Escape" });
    expect(onOpenChange).not.toHaveBeenCalled();
    fireEvent.keyDown(screen.getByText("inside"), { key: "Escape" });
    expect(onOpenChange).toHaveBeenCalledWith(false);
    onOpenChange.mockClear();
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("the × closes a clean overlay at once and asks first when it holds edits", async () => {
    render(<ConfirmDialog />);
    const onOpenChange = vi.fn();
    const view = render(<Sheet open onOpenChange={onOpenChange}><SheetContent title="Node 12"><p>detail</p></SheetContent></Sheet>);
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
    onOpenChange.mockClear();
    view.rerender(<Dialog open dirty onOpenChange={onOpenChange}><DialogContent title="Edit"><p>form</p></DialogContent></Dialog>);
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    await userEvent.click(await screen.findByRole("button", { name: "Discard" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("a dialog does the same", () => {
    const onOpenChange = vi.fn();
    render(
      <>
        <button type="button">elsewhere</button>
        <Dialog open onOpenChange={onOpenChange}><DialogContent title="Export"><button type="button">copy</button></DialogContent></Dialog>
      </>,
    );
    fireEvent.keyDown(screen.getByText("elsewhere"), { key: "Escape" });
    expect(onOpenChange).not.toHaveBeenCalled();
    fireEvent.keyDown(screen.getByText("copy"), { key: "Escape" });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("a sheet is a dialog too", () => {
    render(<Sheet open><SheetContent title="Node 12"><p>detail</p></SheetContent></Sheet>);
    expect(screen.getByRole("dialog", { name: "Node 12" })).toHaveTextContent("detail");
  });
});
