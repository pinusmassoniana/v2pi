import { expect, type Page } from "@playwright/test";

export const USER = "admin";
export const PASS = "e2e-pass-123456";

export async function fillCredentials(page: Page) {
  await page.getByPlaceholder("username", { exact: true }).fill(USER);
  await page.getByPlaceholder("password", { exact: true }).fill(PASS);
}

export async function fillBootstrapProofIfRequired(page: Page) {
  const response = await page.request.get("/api/setup");
  expect(response.ok()).toBe(true);
  const setup = await response.json() as { needs_setup: boolean; bootstrap_required?: boolean };
  if (!setup.bootstrap_required) return;

  const proof = process.env.PI_GW_E2E_BOOTSTRAP_TOKEN;
  expect(
    proof,
    "The E2E server requires first-run proof; expose its test-only value as PI_GW_E2E_BOOTSTRAP_TOKEN",
  ).toBeTruthy();
  await page.getByLabel("One-time bootstrap token").fill(proof!);
}

export async function ensureLoggedIn(page: Page) {
  await page.goto("/");
  const setupBtn = page.getByRole("button", { name: "Create account" });
  const loginBtn = page.getByRole("button", { name: "Log in" });
  const pageTitle = page.locator("h1.page-title");
  await expect(setupBtn.or(loginBtn).or(pageTitle).first()).toBeVisible({ timeout: 20_000 });
  if (await pageTitle.isVisible().catch(() => false)) return;

  await fillCredentials(page);
  if (await setupBtn.isVisible().catch(() => false)) {
    await fillBootstrapProofIfRequired(page);
    await page.getByPlaceholder("confirm password").fill(PASS);
    await setupBtn.click();
  } else {
    await loginBtn.click();
  }
  await expect(pageTitle).toBeVisible({ timeout: 20_000 });
}
