import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Button } from "./Button";
import { Pill } from "./Pill";
import { EmptyState, ErrorState, Skeleton } from "./States";
import { Toggle } from "./Toggle";

describe("base components", () => {
  it("Button defaults to type=button so it never submits a form by accident", () => {
    render(<Button>Save</Button>);
    expect(screen.getByRole("button", { name: "Save" })).toHaveAttribute("type", "button");
  });

  it("Button can render its child element instead", () => {
    render(<Button asChild><a href="#/nodes">Nodes</a></Button>);
    expect(screen.getByRole("link", { name: "Nodes" })).toHaveAttribute("href", "#/nodes");
  });

  it("Toggle is a named switch that reports the new state", async () => {
    const onChange = vi.fn();
    render(<Toggle label="kill-switch" checked={false} onCheckedChange={onChange} />);
    const sw = screen.getByRole("switch", { name: "kill-switch" });
    expect(sw).toHaveAttribute("aria-checked", "false");
    await userEvent.click(sw);
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it("Pill renders its label with a decorative dot", () => {
    render(<Pill tone="ok" dot>Tunnel online</Pill>);
    const pill = screen.getByText("Tunnel online");
    expect(pill.querySelector("[aria-hidden]")).not.toBeNull();
  });

  it("states: skeleton is hidden from assistive tech, empty is a status, error is an alert with retry", async () => {
    const onRetry = vi.fn();
    const { container } = render(
      <>
        <Skeleton className="h-4" />
        <EmptyState title="No servers here">Add one with “Add server”.</EmptyState>
        <ErrorState message="load failed" onRetry={onRetry} retryLabel="Reload" />
      </>,
    );
    expect(container.querySelector("[aria-hidden]")).not.toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("No servers here");
    expect(screen.getByRole("alert")).toHaveTextContent("load failed");
    await userEvent.click(screen.getByRole("button", { name: "Reload" }));
    expect(onRetry).toHaveBeenCalled();
  });
});
