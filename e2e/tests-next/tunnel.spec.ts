import { expect, test, type Page } from "@playwright/test";
import { ensureLoggedIn } from "../tests/auth-helper";

// The e2e backend is fresh and has no active node: a routing save applies on the next Connect, and nothing here is
// applied live. Every test starts and ends with an empty ruleset, no e2e-* profile and the stock active-check interval —
// the other specs expect a fresh gateway.

const PREFIX = "e2e-";

async function gateway(page: Page) {
  const csrf = ((await (await page.request.get("/api/csrf")).json()) as { csrf: string }).csrf;
  const headers = { "X-CSRF-Token": csrf };
  return {
    get: async <T>(path: string) => (await (await page.request.get(`/api${path}`)).json()) as T,
    put: (path: string, data: unknown) => page.request.put(`/api${path}`, { data, headers }),
    remove: (path: string) => page.request.delete(`/api${path}`, { headers }),
  };
}

async function cleanUp(page: Page) {
  const api = await gateway(page);
  await api.put("/routing", { rules: [], default_action: "proxy", domain_strategy: "IPIfNonMatch" });
  for (const profile of await api.get<{ id: number; name: string }[]>("/profiles")) {
    if (profile.name.startsWith(PREFIX)) await api.remove(`/profiles/${profile.id}`);
  }
  await api.put("/settings", { health_active_interval: 60 });
}

test.beforeEach(async ({ page }) => {
  await ensureLoggedIn(page);
  await cleanUp(page);
});

test.afterEach(async ({ page }) => {
  await cleanUp(page);
});

test("stage a domain rule, validate and save it, test a destination against it, then remove it", async ({ page }, info) => {
  const phone = info.project.name === "phone";
  await page.goto("/#/tunnel/routing");
  await page.reload();   // the ruleset was reset behind the screen's back: load it afresh
  await expect(page.getByText("no rules — all traffic follows the default action")).toBeVisible();

  await page.getByRole("button", { name: "Add rule" }).click();
  if (phone) {
    const sheet = page.getByRole("dialog", { name: "Rule 1 of 1" });
    await sheet.getByLabel("Value", { exact: true }).fill("e2e-test.example");
    await sheet.getByRole("button", { name: "Done" }).click();
    await expect(sheet).toBeHidden();
  } else {
    await page.getByLabel("Rule 1 value").fill("e2e-test.example");
  }
  const staged = page.getByRole("region", { name: "Staged changes" });
  await expect(staged.getByRole("status")).toHaveText("STAGED · 1 change — not yet applied to the live config.");

  await page.getByRole("button", { name: "Validate" }).click();
  // No node is active, so the gateway checks the rules' structure only.
  await expect(page.getByText("✓ ruleset valid")).toBeVisible();

  const tester = page.getByRole("region", { name: "Destination tester" });
  await tester.getByRole("textbox").fill("www.e2e-test.example:443");
  await expect(tester.getByRole("status")).toHaveText('→ proxy (matched domain "e2e-test.example")');
  await tester.getByRole("textbox").fill("10.0.0.7");
  await expect(tester.getByRole("status")).toHaveText("→ direct (private range, always matched first)");

  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("saved — applies on next Connect")).toBeVisible();
  await expect(staged).toBeHidden();

  await page.reload();
  if (phone) {
    const list = page.getByRole("list", { name: "Routing rules" });
    await expect(list).toContainText("e2e-test.example");
    await list.getByRole("button", { name: "Edit rule 1" }).click();
    await page.getByRole("dialog", { name: "Rule 1 of 1" }).getByRole("button", { name: "Delete" }).click();
  } else {
    await expect(page.getByLabel("Rule 1 value")).toHaveValue("e2e-test.example");
    await page.getByRole("button", { name: "Remove rule 1" }).click();
  }
  await expect(staged.getByRole("status")).toHaveText("STAGED · 1 change — not yet applied to the live config.");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(staged).toBeHidden();
  await expect(page.getByText("no rules — all traffic follows the default action")).toBeVisible();
  const api = await gateway(page);
  expect((await api.get<{ rules: unknown[] }>("/routing")).rules).toEqual([]);
});

test("create a profile from a preset, validate and save it, then delete it", async ({ page }, info) => {
  const phone = info.project.name === "phone";
  await page.goto("/#/tunnel/anti-dpi");
  await page.reload();
  if (phone) await page.getByRole("button", { name: "New profile" }).click();
  const editor = page.getByRole("region", { name: "Profile editor" });
  await expect(editor.getByRole("heading", { name: "New profile" })).toBeVisible();
  // A clean editor takes the preset without asking, and its name with it.
  await editor.getByRole("button", { name: phone ? "Stage preset" : "Stage preset…" }).click();
  await page.getByRole("menuitem", { name: /ru-hardened/ }).click();
  await expect(page.getByText('preset "ru-hardened" staged into the editor — Create/Save to apply')).toBeVisible();
  await expect(editor.getByLabel("Name", { exact: true })).toHaveValue("ru-hardened");
  await editor.getByLabel("Name", { exact: true }).fill("e2e-hardened");
  await expect(editor.getByRole("switch", { name: "TLS fragmentation" })).toHaveAttribute("aria-checked", "true");
  await expect(editor.getByLabel("Noise 1 packet")).toHaveValue("50-150");

  await editor.getByRole("button", { name: "Validate" }).click();
  // The gateway's answer: structural checks, then xray -test when a node exists (none here).
  await expect(editor.getByRole("status")).toHaveText(/^(✓ profile valid|✗ .+)$/);
  await editor.getByRole("button", { name: "Create" }).click();
  await expect(page.getByText("saved", { exact: true })).toBeVisible();

  if (phone) {
    const card = page.getByRole("listitem", { name: "e2e-hardened" });
    await expect(card).toContainText("used by — · chrome");
    await card.getByRole("button", { name: "More actions for e2e-hardened" }).click();
    await page.getByRole("menuitem", { name: "Delete…" }).click();
  } else {
    await expect(editor.getByLabel("Name", { exact: true })).toHaveValue("");
    const row = page.locator("tr", { hasText: "e2e-hardened" });
    await expect(row).toContainText("drop");
    await row.getByRole("button", { name: "Delete e2e-hardened" }).click();
  }
  const ask = page.getByRole("dialog", { name: "Confirm" });
  await expect(ask).toContainText('Delete profile "e2e-hardened"?');
  await ask.getByRole("button", { name: "Delete" }).click();
  await expect(page.getByText('deleted "e2e-hardened"')).toBeVisible();
  await expect(page.getByText("e2e-hardened", { exact: true })).toHaveCount(0);
});

test("an active-check interval under its floor is refused; a valid one saves and survives a reload", async ({ page }) => {
  await page.goto("/#/tunnel/health");
  await page.reload();
  await expect(page.getByRole("region", { name: "Current state" })).toContainText("No eligible standby");
  const monitoring = page.getByRole("region", { name: "Health monitoring" });
  const active = monitoring.getByLabel("Active-server check interval");
  await expect(active).toHaveValue("60");
  const save = page.getByRole("button", { name: "Save", exact: true });
  await expect(save).toBeDisabled();

  await active.fill("5");
  await expect(monitoring.getByText("Active-server check interval must be ≥ 10 s")).toBeVisible();
  await expect(save).toBeDisabled();

  await active.fill("30");
  await expect(save).toBeEnabled();
  const saved = page.waitForResponse((response) => response.url().endsWith("/api/settings") && response.request().method() === "PUT");
  await save.click();
  expect((await saved).request().postDataJSON()).toEqual({ health_active_interval: 30 });
  await expect(page.getByText("Health settings saved")).toBeVisible();

  await page.reload();
  await expect(page.getByRole("region", { name: "Health monitoring" }).getByLabel("Active-server check interval")).toHaveValue("30");
});
