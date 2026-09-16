import { Outlet, RouterProvider, createMemoryHistory, createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { settleConfirm } from "../components/confirm";
import { ConfirmDialog } from "../components/ui/ConfirmDialog";
import { closeGuarded, hasUnsavedEdits, useUnsavedEditsBlocker, useUnsavedGuard } from "./guard";

afterEach(() => act(() => settleConfirm(false)));

function Editor({ label = "edited" }: { label?: string }) {
  const [dirty, setDirty] = useState(false);
  useUnsavedGuard(dirty);
  return (
    <label>
      <input type="checkbox" checked={dirty} onChange={(event) => setDirty(event.target.checked)} /> {label}
    </label>
  );
}

// A screen with a sheet open over it, both holding edits.
function TwoEditors() {
  return (<><Editor label="screen edited" /><Editor label="sheet edited" /></>);
}

// Stands in for the shell, which owns the one blocker.
function TestLayout() {
  useUnsavedEditsBlocker();
  return (<><Outlet /><ConfirmDialog /></>);
}

function OtherScreen() {
  return <p>other screen</p>;
}

function buildRouter(initialPath = "/edit") {
  const root = createRootRoute({ component: TestLayout });
  const editor = createRoute({ getParentRoute: () => root, path: "/edit", component: Editor });
  const other = createRoute({ getParentRoute: () => root, path: "/other", component: OtherScreen });
  const both = createRoute({ getParentRoute: () => root, path: "/both", component: TwoEditors });
  return createRouter({
    routeTree: root.addChildren([editor, other, both]),
    history: createMemoryHistory({ initialEntries: [initialPath] }),
  });
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

  it("asks once per navigation however many guards hold edits", async () => {
    const router = buildRouter("/both");
    render(<RouterProvider router={router} />);
    await userEvent.click(await screen.findByLabelText("screen edited"));
    await userEvent.click(screen.getByLabelText("sheet edited"));

    act(() => router.history.push("/other"));
    await userEvent.click(await screen.findByRole("button", { name: "Discard" }));
    expect(await screen.findByText("other screen")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Confirm" })).not.toBeInTheDocument();
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
