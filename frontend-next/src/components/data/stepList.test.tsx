import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { StepList } from "./StepList";

const STEPS = [
  { key: "Create VLAN 2", title: "Create VLAN 2", detail: "Add VLAN 2 on the router." },
  { key: "Port-forward", title: <><b>Port-forward</b> WAN :443</>, detail: "on every WAN link" },
];

describe("StepList", () => {
  it("numbered steps in an ordered list, each with its title and detail — no checkboxes", () => {
    render(<StepList steps={STEPS} />);
    const items = within(screen.getByRole("list")).getAllByRole("listitem");
    expect(screen.getByRole("list").tagName).toBe("OL");
    expect(items.map((item) => item.textContent)).toEqual(["1Create VLAN 2Add VLAN 2 on the router.", "2Port-forward WAN :443on every WAN link"]);
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("collapsible: titles only, and a title opens its own detail", async () => {
    render(<StepList steps={STEPS} collapsible />);
    const first = screen.getByRole("button", { name: "Create VLAN 2" });
    expect(first).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("Add VLAN 2 on the router.")).not.toBeVisible();
    await userEvent.click(first);
    expect(first).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Add VLAN 2 on the router.")).toBeVisible();
    expect(screen.getByText("on every WAN link")).not.toBeVisible();
  });
});
