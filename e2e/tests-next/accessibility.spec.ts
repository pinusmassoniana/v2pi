import { expect, test } from "@playwright/test";
import { ensureLoggedIn } from "../tests/auth-helper";

const PATHS = [
  "/", "/traffic", "/nodes", "/nodes/subscriptions", "/nodes/1",
  "/tunnel/routing", "/tunnel/anti-dpi", "/tunnel/health",
  "/gateway/network", "/gateway/remote-access",
  "/system/backups", "/system/access", "/system/logs", "/system/panel",
];

test("every screen names its controls and keeps dim text readable", async ({ page }) => {
  await ensureLoggedIn(page);

  for (const path of PATHS) {
    await page.goto(`/#${path}`);
    await expect(page.locator("h1.page-title")).toBeVisible();
    const unnamed = await page.locator("a:visible, button:visible, input:visible, select:visible, textarea:visible")
      .evaluateAll((controls) => controls.flatMap((control, index) => {
        const element = control as HTMLElement;
        // The screen being left can still be unmounting when the controls are collected: a detached input has lost
        // its <label>, but it is not on the screen any more either.
        if (!element.isConnected) return [];
        const labelledBy = element.getAttribute("aria-labelledby")
          ?.split(/\s+/)
          .some((id) => document.getElementById(id)?.textContent?.trim());
        const labels = "labels" in element
          ? [...((element as HTMLInputElement).labels ?? [])].map((item) => item.textContent?.trim()).join(" ").trim()
          : "";
        const label = element.getAttribute("aria-label")?.trim()
          || labels
          || labelledBy
          || element.getAttribute("title")?.trim()
          || (element instanceof HTMLButtonElement || element instanceof HTMLAnchorElement ? element.textContent?.trim() : "");
        return label ? [] : [`${index}:${element.tagName.toLowerCase()}:${element.outerHTML.slice(0, 160)}`];
      }));
    expect(unnamed, `${path} has controls without an accessible name`).toEqual([]);
  }

  const ratio = await page.evaluate(() => {
    const fg = getComputedStyle(document.querySelector<HTMLElement>("[data-nav-group]")!).color;
    const bg = getComputedStyle(document.body).backgroundColor;
    const rgb = (value: string) => value.match(/[\d.]+/g)!.slice(0, 3).map(Number);
    const luminance = (value: string) => {
      const [r, g, b] = rgb(value).map((channel) => {
        const c = channel / 255;
        return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
    };
    const [lighter, darker] = [luminance(fg), luminance(bg)].sort((a, b) => b - a);
    return (lighter! + 0.05) / (darker! + 0.05);
  });
  expect(ratio).toBeGreaterThanOrEqual(4.5);
});
