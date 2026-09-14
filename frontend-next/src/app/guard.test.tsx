import { Outlet, RouterProvider, createMemoryHistory, createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { settleConfirm } from "../components/confirm";
import { ConfirmDialog } from "../components/ui/ConfirmDialog";
import { closeGuarded, hasUnsavedEdits, useUnsavedGuard } from "./guard";

afterEach(() => act(() => settleConfirm(false)));

function Editor() {
  const [dirty, setDirty] = useState(false);
  useUnsavedGuard(dirty);
  return (
    <label>
      <input type="checkbox" checked={dirty} onChange={(event) => setDirty(event.target.checked)} /> edited
    </label>
  );
}

function TestLayout() {
  return (<><Outlet /><ConfirmDialog /></>);
}

function OtherScreen() {
  return <p>other screen</p>;
}

function buildRouter() {
  const root = createRootRoute({ component: TestLayout });
  const editor = createRoute({ getParentRoute: () => root, path: "/edit", component: Editor });
  const other = createRoute({ getParentRoute: () => root, path: "/other", component: OtherScreen });
  return createRouter({ routeTree: root.addChildren([editor, other]), history: createMemoryHistory({ initialEntries: ["/edit"] }) });
}

describe("useUnsavedGuard", () => {
  it("lets navigation through while nothing is edited", async () => {
    const router = buildRouter();
    render(<RouterProvider router={router} />);
    await screen.findByLabelText("edited");
    act(() => router.history.push("/other"));
    expect(await screen.findByText("other screen")).toBeInTheDocument();
  });

  it("asks before leaving an edited screen: Cancel stays, Discard leaves", async () => {
    const router = buildRouter();
    render(<RouterProvider router={router} />);
    await userEvent.click(await screen.findByLabelText("edited"));
    expect(hasUnsavedEdits()).toBe(true);

    act(() => router.history.push("/other"));
    await userEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/edit"));

    act(() => router.history.push("/other"));
    await userEvent.click(await screen.findByRole("button", { name: "Discard" }));
    expect(await screen.findByText("other screen")).toBeInTheDocument();
    expect(hasUnsavedEdits()).toBe(false);
  });
});

describe("closeGuarded", () => {
  it("closes at once when clean, and asks when dirty", async () => {
    render(<ConfirmDialog />);
    const close = vi.fn();
    await closeGuarded(false, close);
    expect(close).toHaveBeenCalledTimes(1);

    const declined = closeGuarded(true, close);
    await userEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    await declined;
    expect(close).toHaveBeenCalledTimes(1);

    const accepted = closeGuarded(true, close);
    await userEvent.click(await screen.findByRole("button", { name: "Discard" }));
    await accepted;
    expect(close).toHaveBeenCalledTimes(2);
  });
});
