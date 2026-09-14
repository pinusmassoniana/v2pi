import { Outlet, createMemoryHistory, createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { act, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { RouterMount } from "./router";

function buildRouter() {
  const beforeLoad = vi.fn();
  const root = createRootRoute({ component: Outlet, beforeLoad });
  const first = createRoute({ getParentRoute: () => root, path: "/first", component: () => <p>first screen</p> });
  const second = createRoute({ getParentRoute: () => root, path: "/second", component: () => <p>second screen</p> });
  const router = createRouter({ routeTree: root.addChildren([first, second]), history: createMemoryHistory({ initialEntries: ["/first"] }) });
  return { router, beforeLoad };
}

describe("RouterMount", () => {
  it("shows the screen the URL moved to while the router was unmounted", async () => {
    const { router } = buildRouter();
    const view = render(<RouterMount router={router} />);
    expect(await screen.findByText("first screen")).toBeInTheDocument();

    view.rerender(<p>logged out</p>);
    act(() => router.history.push("/second"));
    view.rerender(<RouterMount router={router} />);

    expect(await screen.findByText("second screen")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/second");
  });

  it("does not load again when it remounts on the URL it already shows", async () => {
    const { router, beforeLoad } = buildRouter();
    const view = render(<RouterMount router={router} />);
    expect(await screen.findByText("first screen")).toBeInTheDocument();
    const loads = beforeLoad.mock.calls.length;

    view.rerender(<p>logged out</p>);
    view.rerender(<RouterMount router={router} />);
    expect(await screen.findByText("first screen")).toBeInTheDocument();
    await act(() => new Promise((resolve) => setTimeout(resolve, 20)));
    expect(beforeLoad).toHaveBeenCalledTimes(loads);
  });
});
