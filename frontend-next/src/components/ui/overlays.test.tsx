import { act, render, screen } from "@testing-library/react";
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

  it("a sheet is a dialog too", () => {
    render(<Sheet open><SheetContent title="Node 12"><p>detail</p></SheetContent></Sheet>);
    expect(screen.getByRole("dialog", { name: "Node 12" })).toHaveTextContent("detail");
  });
});
