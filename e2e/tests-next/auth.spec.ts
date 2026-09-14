import { expect, test, type Page } from "@playwright/test";
import { PASS, USER, ensureLoggedIn } from "../tests/auth-helper";

async function logOut(page: Page) {
  await page.goto("/#/system/panel");   // Log out is reachable here at every width
  await page.getByRole("button", { name: "Log out" }).first().click();
  await expect(page.getByRole("button", { name: "Log in" })).toBeVisible();
  await page.goto("/#/");                // so the next login lands on Overview
}

test("wrong credentials are rejected with a visible error", async ({ page }) => {
  await ensureLoggedIn(page);
  await logOut(page);
  await page.getByPlaceholder("username", { exact: true }).fill(USER);
  await page.getByPlaceholder("password", { exact: true }).fill("definitely-wrong");
  await page.getByRole("button", { name: "Log in" }).click();
  await expect(page.locator("p.err")).toContainText(/bad credentials|too many attempts/);
});

test("repeated failures hit the rate limit, which then expires", async ({ page }) => {
  await ensureLoggedIn(page);
  await logOut(page);
  const user = page.getByPlaceholder("username", { exact: true });
  const pass = page.getByPlaceholder("password", { exact: true });
  const button = page.getByRole("button", { name: "Log in" });
  const error = page.locator("p.err");
  let limited = false;
  for (let i = 0; i < 7; i++) {   // the bucket may carry failures from the previous test
    await user.fill(USER);
    await pass.fill("wrong-" + i);
    await button.click();
    await expect(error).toBeVisible();
    if ((await error.textContent())?.includes("too many attempts")) { limited = true; break; }
  }
  expect(limited).toBe(true);
  await page.waitForTimeout(2_600);   // run-server.sh sets a 2 s lockout
  await user.fill(USER);
  await pass.fill(PASS);
  await button.click();
  await expect(page.locator("h1.page-title")).toHaveText("Overview", { timeout: 20_000 });
});

test("mutations without a CSRF token are refused", async ({ page }) => {
  await ensureLoggedIn(page);
  const response = await page.request.post("/api/nodes", {
    data: { name: "x", address: "192.0.2.1", port: 443, uuid: "u" },
  });
  expect(response.status()).toBe(403);
});

test("losing the session mid-use drops back to the Login screen", async ({ page, context }) => {
  await ensureLoggedIn(page);
  await context.clearCookies();
  await expect(page.getByRole("button", { name: "Log in" })).toBeVisible({ timeout: 15_000 });
});
