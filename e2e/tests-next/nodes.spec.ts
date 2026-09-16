import { expect, test, type Page } from "@playwright/test";
import { ensureLoggedIn } from "./auth-helper";

// The e2e backend is fresh and has no live xray, so nothing here connects. Every test starts and ends with no
// e2e-* node or subscription and with the fetch settings at their defaults: the other specs expect an empty gateway.

const PREFIX = "e2e-";
const UUID = "3f1c9a52-7b1e-4c7d-9a0e-5d2c1b8e4f10";

async function gateway(page: Page) {
  const csrf = ((await (await page.request.get("/api/csrf")).json()) as { csrf: string }).csrf;
  const headers = { "X-CSRF-Token": csrf };
  return {
    get: async <T>(path: string) => (await (await page.request.get(`/api${path}`)).json()) as T,
    post: (path: string, data: unknown) => page.request.post(`/api${path}`, { data, headers }),
    put: (path: string, data: unknown) => page.request.put(`/api${path}`, { data, headers }),
    remove: (path: string) => page.request.delete(`/api${path}`, { headers }),
  };
}

async function cleanUp(page: Page) {
  const api = await gateway(page);
  for (const node of await api.get<{ id: number; name: string }[]>("/nodes")) {
    if (node.name.startsWith(PREFIX)) await api.remove(`/nodes/${node.id}`);
  }
  for (const sub of await api.get<{ id: number; name: string }[]>("/subs")) {
    if (sub.name.startsWith(PREFIX)) await api.remove(`/subs/${sub.id}`);
  }
  await api.put("/settings", { tunneled_fetch: true });
}

test.beforeEach(async ({ page }) => {
  await ensureLoggedIn(page);
  await cleanUp(page);
});

test.afterEach(async ({ page }) => {
  await cleanUp(page);
});

test("a fresh gateway: no groups but Servers, no servers, no subscriptions", async ({ page }) => {
  await page.goto("/#/nodes");
  await expect(page.locator("h1.page-title")).toHaveText("Servers");
  await expect(page.getByRole("navigation", { name: "Node groups" }).getByRole("link")).toHaveText(["Servers 0"]);
  await expect(page.getByText("No servers here — add one with Add server.")).toBeVisible();

  await page.goto("/#/nodes/subscriptions");
  await expect(page.locator("h1.page-title")).toHaveText("Subscriptions");
  await expect(page.getByRole("region", { name: "Subscription fetching" })).toBeVisible();
  await expect(page.getByText("No subscriptions yet — add one")).toBeVisible();
  await expect(page.getByRole("button", { name: "Refresh all" })).toBeDisabled();
});

test("add a manual server with Validate, open it, export its link, delete it", async ({ page }, info) => {
  await page.goto("/#/nodes?group=servers");
  await expect(page.getByText("No servers here — add one with Add server.")).toBeVisible();
  if (info.project.name === "phone") {
    await page.getByRole("button", { name: "Add or import servers" }).click();
    await page.getByRole("menuitem", { name: "Add server" }).click();
  } else {
    await page.getByRole("region", { name: "Servers toolbar" }).getByRole("button", { name: "Add server" }).click();
  }
  const form = page.getByRole("dialog", { name: "Add server" });
  await form.getByLabel("Name", { exact: true }).fill("e2e-vps");
  await form.getByLabel("Address", { exact: true }).fill("198.51.100.77");
  await form.getByLabel("UUID", { exact: true }).fill(UUID);
  await form.getByLabel("Public key", { exact: true }).fill("Zm9vX3JlYWxpdHlfcHViX2tleQ");
  await form.getByRole("button", { name: "Validate" }).click();
  // The gateway's answer: "✓ config valid" with an xray binary to test against, "✗ …" without one (as here).
  await expect(form.getByRole("status")).toHaveText(/^(✓ config valid|✗ .+)$/);
  await form.getByRole("button", { name: "Add server" }).click();
  await expect(form).toBeHidden();
  await expect(page.locator('[data-node-name="e2e-vps"]')).toBeVisible();

  await page.getByRole("link", { name: "e2e-vps", exact: true }).click();
  await expect(page).toHaveURL(/#\/nodes\/\d+/);
  const detail = info.project.name === "phone" ? page.locator("main") : page.getByRole("dialog", { name: "e2e-vps" });
  await expect(detail.getByRole("region", { name: "Config" })).toContainText("198.51.100.77");

  await detail.getByRole("button", { name: "Export" }).click();
  const exported = page.getByRole("dialog", { name: "Export e2e-vps" });
  await expect(exported.getByLabel("vless:// link")).toHaveValue(new RegExp(`^vless://${UUID}@198\\.51\\.100\\.77:443\\?type=tcp&security=reality&pbk=`));
  await exported.getByRole("button", { name: "Close" }).click();
  await expect(exported).toBeHidden();

  await detail.getByRole("button", { name: "Delete" }).click();
  const ask = page.getByRole("dialog", { name: "Confirm" });
  await expect(ask).toContainText('Delete server "e2e-vps" (198.51.100.77)?');
  await ask.getByRole("button", { name: "Delete" }).click();
  await expect(page).toHaveURL(/#\/nodes\?group=servers$/);
  await expect(page.getByText("No servers here — add one with Add server.")).toBeVisible();
});

test("a subscription whose URL cannot be fetched shows the refresh error", async ({ page }) => {
  test.setTimeout(120_000);
  await page.goto("/#/nodes/subscriptions");
  await page.getByRole("button", { name: "Add subscription" }).click();
  const form = page.getByRole("dialog", { name: "Add subscription" });
  await expect(form.getByRole("textbox", { name: "Header 1 name" })).toHaveValue("x-device-os");
  await form.getByLabel("Name", { exact: true }).fill("e2e-sub");
  // Shared address space: public enough for the gateway's URL check, and no provider will ever answer there.
  await form.getByLabel("URL", { exact: true }).fill("http://100.64.0.1/e2e-unreachable");
  await form.getByRole("button", { name: "Add subscription" }).click();
  await expect(form).toBeHidden();

  const card = page.getByRole("region", { name: "e2e-sub", exact: true });
  await expect(card).toContainText("never fetched");
  await card.getByRole("button", { name: "Refresh" }).click();
  // The fetch fails at once or after its 20 s deadline, depending on the network; either way the error is stored.
  await expect(async () => {
    await page.reload();
    await expect(page.getByRole("region", { name: "e2e-sub", exact: true }).getByRole("button", { name: "Last error of e2e-sub" })).toBeVisible({ timeout: 3_000 });
  }).toPass({ timeout: 90_000 });
});

test("the subscription fetch settings keep what was switched", async ({ page }) => {
  await page.goto("/#/nodes/subscriptions");
  const tunnel = page.getByRole("switch", { name: "Fetch subscriptions through the tunnel" });
  await expect(tunnel).toHaveAttribute("aria-checked", "true");
  const saved = page.waitForResponse((response) => response.url().endsWith("/api/settings") && response.request().method() === "PUT");
  await tunnel.click();
  expect((await saved).ok()).toBe(true);
  await page.reload();
  await expect(page.getByRole("switch", { name: "Fetch subscriptions through the tunnel" })).toHaveAttribute("aria-checked", "false");
  const restored = page.waitForResponse((response) => response.url().endsWith("/api/settings") && response.request().method() === "PUT");
  await page.getByRole("switch", { name: "Fetch subscriptions through the tunnel" }).click();
  expect((await restored).ok()).toBe(true);
  await page.reload();
  await expect(page.getByRole("switch", { name: "Fetch subscriptions through the tunnel" })).toHaveAttribute("aria-checked", "true");
});

test("the Servers list fits the width: cards on a phone, columns folding on narrower desktops", async ({ page }, info) => {
  const api = await gateway(page);
  expect((await api.post("/nodes", { name: "e2e-cols", address: "198.51.100.78", port: 443, uuid: UUID, public_key: "Zm9vX3JlYWxpdHlfcHViX2tleQ" })).ok()).toBe(true);
  await page.goto("/#/nodes?group=servers");
  await page.reload();   // the node was added behind the screen's back: load the list afresh
  await expect(page.locator('[data-node-name="e2e-cols"]')).toBeVisible();

  if (info.project.name === "phone") {
    await expect(page.getByRole("table")).toHaveCount(0);
    await expect(page.getByRole("list", { name: "Nodes" }).getByRole("button", { name: "Connect e2e-cols" })).toBeVisible();
  } else {
    const table = page.getByRole("table", { name: "Nodes" });
    const header = (name: string) => table.getByRole("columnheader", { name, exact: true });
    await expect(header("Egress")).toBeVisible();
    if (info.project.name === "laptop") {
      for (const name of ["Port", "Transport", "Trend", "Checked"]) await expect(header(name)).toBeHidden();
      await expect(page.locator('tr[data-node-name="e2e-cols"]')).toContainText("· 443 · vision · reality");
    } else {
      for (const name of ["Port", "Transport", "Trend", "Checked"]) await expect(header(name)).toBeVisible();
    }
  }
  const widths = await page.evaluate(() => ({ page: document.documentElement.scrollWidth, viewport: window.innerWidth }));
  expect(widths.page).toBeLessThanOrEqual(widths.viewport);
});
