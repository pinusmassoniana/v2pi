import { test, expect } from "@playwright/test";

// One full-stack smoke covering the auth surface + SPA routing against the real backend:
//   first-run setup (or login if already provisioned) → dashboard → log out → log in → navigate.
const USER = "admin";
const PASS = "e2e-pass-123456";

test("setup → dashboard → logout → login → navigate", async ({ page }) => {
  await page.goto("/");

  const setupBtn = page.getByRole("button", { name: "Create account" });
  const loginBtn = page.getByRole("button", { name: "Log in" });
  const pageTitle = page.locator("h1.page-title");

  // Wait for the boot probe to resolve into one of the three top-level states.
  await expect(setupBtn.or(loginBtn).or(pageTitle).first()).toBeVisible({ timeout: 20_000 });

  async function fillCreds() {
    await page.getByPlaceholder("username", { exact: true }).fill(USER);
    await page.getByPlaceholder("password", { exact: true }).fill(PASS);
  }

  if (await setupBtn.isVisible().catch(() => false)) {
    await fillCreds();
    await page.getByPlaceholder("confirm password").fill(PASS);
    await setupBtn.click();
  } else if (await loginBtn.isVisible().catch(() => false)) {
    await fillCreds();
    await loginBtn.click();
  }
  await expect(pageTitle).toHaveText("Overview", { timeout: 20_000 });

  // Auth round-trip: log out, then log back in with the same credentials.
  await page.getByRole("button", { name: "Log out" }).click();
  await expect(loginBtn).toBeVisible({ timeout: 10_000 });
  await fillCreds();
  await loginBtn.click();
  await expect(pageTitle).toHaveText("Overview", { timeout: 20_000 });

  // SPA navigation to an authenticated screen.
  await page.getByRole("button", { name: "Nodes", exact: true }).click();
  await expect(pageTitle).toHaveText("Nodes");
});
