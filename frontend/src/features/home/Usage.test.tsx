import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TRAFFIC_USAGE, mockApi } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";
import { CAP_NOTE, NO_CAP, capFraction, capText, capTone, ordinal, usageDays, usageRows } from "./usage";

async function openTraffic() {
  const api$ = mockApi();
  const view = renderApp("/traffic");
  await screen.findByRole("region", { name: "Usage" });
  await screen.findByText("Last month");
  return { api$, ...view };
}

const card = () => screen.getByRole("region", { name: "Usage" });

describe("Home › Traffic — usage (A7)", () => {
  it("shows the four figures, the cap and a bar per day", async () => {
    await openTraffic();
    const usage = card();
    expect(usage).toHaveTextContent("Today");
    expect(usage).toHaveTextContent("2.1 GB");
    expect(usage).toHaveTextContent("11.8 GB");        // last 7 days
    expect(usage).toHaveTextContent("18.4 GB of 100 GB · resets on the 1st");
    expect(within(usage).getByRole("meter", { name: "Monthly cap" })).toHaveAttribute("aria-valuenow", "18");
    expect(within(usage).getAllByRole("listitem")).toHaveLength(TRAFFIC_USAGE.days.length);
    expect(usage).toHaveTextContent("90 days kept");
    expect(usage).toHaveTextContent("a day ends at its midnight, not yours");
  });

  it("without a cap it says so and draws no meter", async () => {
    const api$ = mockApi();
    api$.getTrafficUsage.mockResolvedValue({ ...TRAFFIC_USAGE, cap_bytes: 0 });
    renderApp("/traffic");
    await screen.findByRole("region", { name: "Usage" });

    expect(await screen.findByText(`18.4 GB this month · ${NO_CAP}`)).toBeInTheDocument();
    expect(within(card()).queryByRole("meter", { name: "Monthly cap" })).toBeNull();
    expect(card()).not.toHaveTextContent(CAP_NOTE);
  });

  it("past the cap the month is called out", async () => {
    const api$ = mockApi();
    api$.getTrafficUsage.mockResolvedValue({ ...TRAFFIC_USAGE, month: 104_000_000_000 });
    renderApp("/traffic");
    await screen.findByRole("region", { name: "Usage" });

    expect(await within(card()).findByText("100% of cap")).toBeInTheDocument();
    expect(within(card()).getByRole("meter", { name: "Monthly cap" })).toHaveAttribute("aria-valuenow", "100");
  });
});

describe("usage helpers", () => {
  it("colours the month against the cap, and only when there is one", () => {
    expect(capTone(10, 0)).toBeNull();
    expect(capTone(10, 100)).toBe("ok");
    expect(capTone(80, 100)).toBe("warn");
    expect(capTone(100, 100)).toBe("bad");
    expect(capFraction(150, 100)).toBe(1);            // the bar never overflows
    expect(capFraction(10, 0)).toBeNull();
  });

  it("words the cap line and the ordinals", () => {
    expect(capText({ month: 1_000_000_000, cap_bytes: 0, cap_reset_day: 1 })).toBe(`1 GB this month · ${NO_CAP}`);
    expect(capText({ month: 1_000_000_000, cap_bytes: 50_000_000_000, cap_reset_day: 22 }))
      .toBe("1 GB of 50 GB · resets on the 22nd");
    expect([1, 2, 3, 4, 11, 12, 13, 21].map(ordinal)).toEqual(
      ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st"]);
  });

  it("lists the four figures in reading order", () => {
    expect(usageRows(TRAFFIC_USAGE).map((row) => row.label)).toEqual(["Today", "Last 7 days", "This month", "Last month"]);
  });

  it("labels each day on the GATEWAY's clock, not the browser's", () => {
    // Day 20000 is 2024-10-04 UTC. A browser in any zone must read the same label, because the
    // gateway already cut its days on its own offset.
    const days = usageDays({ ...TRAFFIC_USAGE, tz_offset_sec: 10 * 3_600, days: [{ day: 20_000, up_bytes: 1, down_bytes: 2 }] });
    expect(days).toEqual([{ day: 20_000, label: new Date(20_000 * 86_400_000).toLocaleDateString([], { month: "short", day: "numeric", timeZone: "UTC" }), bytes: 3 }]);
    expect(usageDays(TRAFFIC_USAGE, 5)).toHaveLength(5);      // newest five
  });
});
