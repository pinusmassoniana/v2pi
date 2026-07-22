import { test, expect, type Page } from "@playwright/test";
import { ensureLoggedIn, PASS, USER } from "./auth-helper";

// Negative-path auth coverage (audit I5): bad credentials, the per-IP login rate limit
// (run-server.sh sets PI_GW_LOGIN_LOCKOUT_SEC=2 so the lockout clears between tests),
// CSRF enforcement, and the mid-session 401 → Login drop (F1).
// Runs before smoke.spec.ts (alphabetical), so it provisions the account itself if needed.
async function logoutIfIn(page: Page) {
  const out = page.getByRole("button", { name: "Log out" });
  if (await out.isVisible().catch(() => false)) await out.click();
}

test("wrong credentials are rejected with a visible error", async ({ page }) => {
  await ensureLoggedIn(page);   // provisions the account on a fresh server
  await logoutIfIn(page);
  await page.getByPlaceholder("username", { exact: true }).fill(USER);
  await page.getByPlaceholder("password", { exact: true }).fill("definitely-wrong");
  await page.getByRole("button", { name: "Log in" }).click();
  await expect(page.locator("p.err")).toContainText(/bad credentials|too many attempts/);
});

test("repeated failures hit the rate limit, which then expires", async ({ page }) => {
  await ensureLoggedIn(page);
  await logoutIfIn(page);
  const user = page.getByPlaceholder("username", { exact: true });
  const pass = page.getByPlaceholder("password", { exact: true });
  const btn = page.getByRole("button", { name: "Log in" });
  const err = page.locator("p.err");
  let limited = false;
  for (let i = 0; i < 7; i++) {       // bucket may carry fails from the previous test
    await user.fill(USER);
    await pass.fill("wrong-" + i);
    await btn.click();
    await expect(err).toBeVisible();
    if ((await err.textContent())?.includes("too many attempts")) { limited = true; break; }
  }
  expect(limited).toBe(true);
  // the e2e server runs with a 2s lockout — after it expires, a correct login works
  await page.waitForTimeout(2_600);
  await user.fill(USER);
  await pass.fill(PASS);
  await btn.click();
  await expect(page.locator("h1.page-title")).toHaveText("Overview", { timeout: 20_000 });
});

test("mutations without a CSRF token are refused", async ({ page }) => {
  await ensureLoggedIn(page);
  // same cookie jar as the page, but no X-CSRF-Token header → 403 from require_csrf
  const res = await page.request.post("/api/nodes", {
    data: { name: "x", address: "192.0.2.1", port: 443, uuid: "u" },
  });
  expect(res.status()).toBe(403);
});

test("losing the session mid-use drops back to the Login screen", async ({ page, context }) => {
  await ensureLoggedIn(page);
  await context.clearCookies();
  // next API poll/click gets a 401 → the F1 handler swaps the shell to Login
  await page.getByRole("button", { name: "Nodes", exact: true }).click();
  await expect(page.getByRole("button", { name: "Log in" })).toBeVisible({ timeout: 15_000 });
});
