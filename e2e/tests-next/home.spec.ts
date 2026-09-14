import { expect, test } from "@playwright/test";
import { ensureLoggedIn } from "../tests/auth-helper";

// The e2e backend is fresh: no nodes, no active tunnel, no recorded failovers. These checks are about the
// screens' structure and empty states at each width, never about live numbers.

const OVERVIEW_CARDS = [
  "Status", "↓ Download", "↑ Upload", "↓ Session total", "↑ Session total",
  "Throughput", "Upstream health", "Connection path", "Events", "Routing", "Network",
];

test("Overview shows the status block, KPIs, chart and summaries of a fresh gateway", async ({ page }) => {
  await ensureLoggedIn(page);
  await page.goto("/#/");
  await expect(page.locator("h1.page-title")).toHaveText("Overview");
  for (const name of OVERVIEW_CARDS) await expect(page.getByRole("region", { name, exact: true })).toBeVisible();

  const status = page.getByRole("region", { name: "Status", exact: true });
  await expect(status.getByText("No node", { exact: true })).toBeVisible();
  await expect(status.getByText("Tunnel OFFLINE")).toBeVisible();
  await expect(status.getByRole("button", { name: "Switch node", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /Roll back/ })).toHaveCount(0);
  await expect(status.getByRole("button", { name: "Disconnect" })).toHaveCount(0);

  await expect(page.getByRole("region", { name: "Upstream health" }).getByRole("link", { name: "Nodes › Servers" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Network", exact: true }).getByRole("term"))
    .toHaveText(["Segment", "DHCP pool", "Client DNS", "IPv6 source"]);
  await expect(page.getByRole("region", { name: "Routing", exact: true }).getByText("default:")).toBeVisible();
  await expect(page.getByRole("switch", { name: "xray-core" })).toBeVisible();
});

test("Overview lays the chart beside upstream health on a desktop and above it on a phone", async ({ page }, info) => {
  await ensureLoggedIn(page);
  await page.goto("/#/");
  // Both cards render a skeleton first; StatusBlock/UpstreamHealthCard change height once their data lands.
  // Wait for each card's loaded content — not just the region existing — so a React commit between the two
  // boundingBox() reads below can't shift only one of them.
  await expect(page.getByRole("region", { name: "Status", exact: true }).getByText("No node", { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Upstream health" }).getByRole("link", { name: "Nodes › Servers" })).toBeVisible();

  await expect(async () => {
    const chart = await page.getByRole("region", { name: "Throughput" }).boundingBox();
    const health = await page.getByRole("region", { name: "Upstream health" }).boundingBox();
    expect(chart && health).toBeTruthy();
    if (info.project.name === "desktop") {
      expect(health!.x).toBeGreaterThanOrEqual(chart!.x + chart!.width);
      expect(Math.abs(health!.y - chart!.y)).toBeLessThan(2);
    } else {
      expect(health!.y).toBeGreaterThanOrEqual(chart!.y + chart!.height);
      expect(Math.abs(health!.x - chart!.x)).toBeLessThan(2);
    }
  }).toPass();
});

test("the throughput chart switches to the recorded 24 h window", async ({ page }) => {
  await ensureLoggedIn(page);
  await page.goto("/#/");
  const chart = page.getByRole("region", { name: "Throughput" });
  const history = page.waitForResponse((response) => response.url().includes("/api/traffic/history?window_sec=86400"));
  await chart.getByRole("button", { name: "24h" }).click();
  expect((await history).ok()).toBe(true);
  await expect(chart.getByRole("button", { name: "24h" })).toHaveAttribute("aria-pressed", "true");
  await expect(chart.getByRole("button", { name: "10m" })).toHaveAttribute("aria-pressed", "false");
});

test("Traffic shows its four KPIs, the charts and an empty failover history", async ({ page }, info) => {
  await ensureLoggedIn(page);
  await page.goto("/#/traffic");
  await expect(page.locator("h1.page-title")).toHaveText("Traffic");
  for (const name of ["Peak download · 10m", "Active latency", "Failovers · 24h", "Uptime", "Throughput", "Probe latency by node", "Active node latency"]) {
    await expect(page.getByRole("region", { name, exact: true })).toBeVisible();
  }
  await expect(page.getByRole("region", { name: "Failovers · 24h" })).toContainText("0");
  const history = page.getByRole("region", { name: "Failover history" });
  await expect(history.getByText("No failovers recorded.")).toBeVisible();
  await expect(page.getByRole("region", { name: "Active node latency" }).getByText("no probe history yet")).toBeVisible();

  const kpis = await Promise.all(["Peak download · 10m", "Active latency"].map((name) => page.getByRole("region", { name, exact: true }).boundingBox()));
  if (info.project.name === "desktop") expect(Math.abs(kpis[1]!.y - kpis[0]!.y)).toBeLessThan(2);
  else expect(kpis[1]!.y).toBeGreaterThanOrEqual(kpis[0]!.y + kpis[0]!.height);
});
