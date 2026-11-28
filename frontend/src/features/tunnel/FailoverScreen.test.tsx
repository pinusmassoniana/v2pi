import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { NODES, NODE_HEALTH, NOW_SEC, SUBS, mockApi } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";
import { failoverRows, fmtHealthy, moved } from "./failoverList";

describe("failoverList", () => {
  it("joins the order with nodes, source, health and the healthy run, skipping deleted nodes", () => {
    const rows = failoverRows({ order: [2, 99, 1], ok_since: { "2": NOW_SEC - 900 } }, NODES, NODE_HEALTH, SUBS, 1, NOW_SEC * 1000);
    expect(rows.map((r) => [r.rank, r.name, r.source, r.active, r.healthyFor])).toEqual([
      [1, "de-fra-01", "work", false, 900],
      [2, "nl-ams-03", "work", true, null],
    ]);
    expect(rows[0]!.state).toBe("up · 58 ms");
  });

  it("moves within bounds and formats durations", () => {
    expect(moved([1, 2, 3], 3, -1)).toEqual([1, 3, 2]);
    expect(moved([1, 2, 3], 1, -1)).toEqual([1, 2, 3]);
    expect([fmtHealthy(30), fmtHealthy(720), fmtHealthy(3900)]).toEqual(["< 1 min", "12 min", "1 h 5 min"]);
  });
});

describe("Tunnel › Failover list", () => {
  it("adds, reorders and removes nodes through PUT /failover/order", async () => {
    const api$ = mockApi();
    api$.getFailover.mockResolvedValue({ order: [1, 2], ok_since: {} });
    renderApp("/tunnel/failover");
    const list = await screen.findByRole("list", { name: "Failover order" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);

    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Node to add" }), "3");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(api$.putFailoverOrder).toHaveBeenLastCalledWith([1, 2, 3]));

    await userEvent.click(await screen.findByRole("button", { name: "Move fi-hel-02 up" }));
    await waitFor(() => expect(api$.putFailoverOrder).toHaveBeenLastCalledWith([1, 3, 2]));

    await userEvent.click(screen.getByRole("button", { name: "Remove nl-ams-03" }));
    await waitFor(() => expect(api$.putFailoverOrder).toHaveBeenLastCalledWith([3, 2]));
  });

  it("saves only the changed switching rule", async () => {
    const api$ = mockApi();
    renderApp("/tunnel/failover");
    const rules = await screen.findByRole("region", { name: "Switching rules" });
    const field = within(rules).getByLabelText(/Healthy time before switching back/);
    await userEvent.clear(field);
    await userEvent.type(field, "30");
    await userEvent.click(within(rules).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api$.putSettings).toHaveBeenCalledWith({ failback_minutes: 30 }));
  });

  it("replaces the upstream-health card on the Overview once the list has nodes", async () => {
    const api$ = mockApi();
    api$.getFailover.mockResolvedValue({ order: [2, 1], ok_since: {} });
    renderApp("/");
    const card = await screen.findByRole("region", { name: "Failover list" });
    expect(within(card).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      expect.stringContaining("de-fra-01"), expect.stringContaining("nl-ams-03"),
    ]);
    expect(screen.queryByRole("region", { name: "Upstream health" })).toBeNull();
  });
});
