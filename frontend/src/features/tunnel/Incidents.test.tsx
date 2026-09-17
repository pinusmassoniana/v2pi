import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { EVENTS, mockApi, mockTunnel } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";
import { downtimeText, durationText, uptimeShare } from "./IncidentsCard";

async function openHealth() {
  const api$ = mockTunnel(mockApi());
  const view = renderApp("/tunnel/health");
  await screen.findByRole("region", { name: "Incidents" });
  await screen.findByRole("list", { name: "Drops" });
  return { api$, ...view };
}

const card = () => screen.getByRole("region", { name: "Incidents" });

describe("Tunnel › Health — incidents (A9)", () => {
  it("adds the drops up and names the one that is still open", async () => {
    await openHealth();
    const incidents = card();
    expect(incidents).toHaveTextContent("2 in this window");
    expect(incidents).toHaveTextContent("14 m 00 s");        // 840 s of downtime
    expect(incidents).toHaveTextContent("99.9%");
    expect(within(incidents).getByText("down now")).toBeInTheDocument();

    const rows = within(incidents).getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("10 m 00 s · still down");
    expect(rows[0]).toHaveTextContent("no alive node to fail over to");
    expect(rows[1]).toHaveTextContent("4 m 00 s");
  });

  it("the window is switchable and each choice is one read", async () => {
    const { api$ } = await openHealth();
    expect(api$.listEvents).toHaveBeenCalledWith(604_800, "");

    await userEvent.selectOptions(within(card()).getByLabelText("Incident window"), "86400");

    await waitFor(() => expect(api$.listEvents).toHaveBeenCalledWith(86_400, ""));
  });

  it("a quiet window says so rather than showing an empty list", async () => {
    const api$ = mockTunnel(mockApi());
    api$.listEvents.mockResolvedValue({ ...EVENTS, incidents: [], downtime_sec: 0 });
    renderApp("/tunnel/health");
    await screen.findByRole("region", { name: "Incidents" });

    expect(await screen.findByText("No drops in this window.")).toBeInTheDocument();
    expect(card()).toHaveTextContent("no downtime");
    expect(card()).toHaveTextContent("100.0%");
  });
});

describe("incident helpers", () => {
  it("words a duration short enough for a row and exact enough to compare", () => {
    expect(durationText(0)).toBe("0 s");
    expect(durationText(45)).toBe("45 s");
    expect(durationText(252)).toBe("4 m 12 s");
    expect(durationText(3_960)).toBe("1 h 06 m");
  });

  it("says 'no downtime' rather than zero, and computes the share that is up", () => {
    expect(downtimeText(0)).toBe("no downtime");
    expect(downtimeText(90)).toBe("1 m 30 s");
    expect(uptimeShare(0, 86_400)).toBe("100.0%");
    expect(uptimeShare(864, 86_400)).toBe("99.0%");
    expect(uptimeShare(86_400, 86_400)).toBe("0.0%");
    expect(uptimeShare(10, 0)).toBe("—");
  });
});
