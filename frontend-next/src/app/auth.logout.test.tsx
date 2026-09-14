import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { ConfirmDialog } from "../components/ui/ConfirmDialog";
import { STATUS } from "../test/fixtures";
import { AuthGate, useAuth } from "./auth";

vi.mock("./guard", () => ({ hasUnsavedEdits: () => true }));

function Protected() {
  const { logout } = useAuth();
  return <button onClick={() => void logout()}>Sign me out</button>;
}

describe("log out over unsaved edits", () => {
  it("asks first, and keeps the session when declined", async () => {
    vi.spyOn(api, "getSetup").mockResolvedValue({ needs_setup: false });
    vi.spyOn(api, "getStatus").mockResolvedValue(STATUS);
    const logout = vi.spyOn(api, "logout").mockResolvedValue({ ok: true });
    render(
      <QueryClientProvider client={new QueryClient()}>
        <AuthGate><Protected /><ConfirmDialog /></AuthGate>
      </QueryClientProvider>,
    );

    await userEvent.click(await screen.findByRole("button", { name: "Sign me out" }));
    await userEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(logout).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Sign me out" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Log out" }));
    await waitFor(() => expect(logout).toHaveBeenCalled());
  });
});
