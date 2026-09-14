import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { createQueryClient } from "../api/queryClient";
import { ConfirmDialog } from "../components/ui/ConfirmDialog";
import { Toaster } from "../components/ui/Toaster";
import { mockApi } from "../test/fixtures";
import { AuthGate } from "./auth";
import { createAppRouter } from "./router";
import { openPalette, usePalette } from "./shell/palette";

afterEach(() => vi.unstubAllGlobals());

function PaletteProbe() {
  return <span data-testid="palette">{usePalette().open ? "open" : "closed"}</span>;
}

describe("ending a session", () => {
  it("a 401 drops a pending confirmation and the open palette, so neither returns after the next login", async () => {
    mockApi();
    vi.spyOn(api, "getSetup").mockResolvedValue({ needs_setup: false });
    vi.spyOn(api, "login").mockResolvedValue({ ok: true });
    vi.spyOn(api, "ensureCsrf").mockResolvedValue("csrf");
    const rollback = vi.spyOn(api, "rollback").mockResolvedValue({ ok: true });
    const dismiss = vi.spyOn(toast, "dismiss");
    const router = createAppRouter(createMemoryHistory({ initialEntries: ["/"] }));
    render(
      <QueryClientProvider client={createQueryClient()}>
        <AuthGate><RouterProvider router={router} /></AuthGate>
        <ConfirmDialog />
        <Toaster />
        <PaletteProbe />
      </QueryClientProvider>,
    );
    await screen.findByRole("heading", { level: 1, name: "Overview" });

    // Roll back is waiting on its confirmation, and the palette is open again on top of it.
    act(() => openPalette());
    await userEvent.click(await screen.findByText("Roll back to previous node"));
    await screen.findByRole("dialog", { name: "Confirm" });
    act(() => openPalette());
    expect(screen.getByTestId("palette")).toHaveTextContent("open");

    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 401 })));
    await expect(api.getSettings()).rejects.toMatchObject({ status: 401 });
    expect(await screen.findByRole("button", { name: "Log in" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Confirm" })).not.toBeInTheDocument();
    expect(dismiss).toHaveBeenCalled();

    await userEvent.type(screen.getByPlaceholderText("username"), "admin");
    await userEvent.type(screen.getByPlaceholderText("password"), "long-enough");
    await userEvent.click(screen.getByRole("button", { name: "Log in" }));
    await screen.findByRole("heading", { level: 1, name: "Overview" });

    expect(screen.getByTestId("palette")).toHaveTextContent("closed");
    expect(screen.queryByPlaceholderText("Search nodes, screens and actions…")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Confirm" })).not.toBeInTheDocument());
    expect(rollback).not.toHaveBeenCalled();
  });
});
