import { expect, test } from "@playwright/test";
import { ensureLoggedIn } from "./auth-helper";

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

  // The section labels sit on the sidebar's translucent glass, not on the page: composite the aside's background over
  // the body's before measuring, in both themes. The aurora radials and the backdrop blur are left out on purpose —
  // this is the token ground the design defines, the floor the label colour has to clear.
  const ratios = await page.evaluate(() => {
    const label = document.querySelector<HTMLElement>("[data-nav-group]")!;
    const aside = label.closest("aside")!;
    const rgba = (value: string) => {
      const [r, g, b, a = 1] = value.match(/[\d.]+/g)!.map(Number);
      return { r: r!, g: g!, b: b!, a };
    };
    const over = (top: string, ground: string) => {
      const t = rgba(top), u = rgba(ground);
      const mix = (x: number, y: number) => x * t.a + y * (1 - t.a);
      return { r: mix(t.r, u.r), g: mix(t.g, u.g), b: mix(t.b, u.b) };
    };
    const luminance = ({ r, g, b }: { r: number; g: number; b: number }) => {
      const [lr, lg, lb] = [r, g, b].map((channel) => {
        const c = channel / 255;
        return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * lr! + 0.7152 * lg! + 0.0722 * lb!;
    };
    const measure = () => {
      const ground = over(getComputedStyle(aside).backgroundColor, getComputedStyle(document.body).backgroundColor);
      const [lighter, darker] = [luminance(rgba(getComputedStyle(label).color)), luminance(ground)].sort((a, b) => b - a);
      return (lighter! + 0.05) / (darker! + 0.05);
    };
    const root = document.documentElement;
    const theme = root.dataset.theme;
    try {
      root.dataset.theme = "dark";
      const dark = measure();
      root.dataset.theme = "light";
      return { dark, light: measure() };
    } finally {
      if (theme === undefined) delete root.dataset.theme;
      else root.dataset.theme = theme;
    }
  });
  expect(ratios.dark).toBeGreaterThanOrEqual(4.5);
  expect(ratios.light).toBeGreaterThanOrEqual(4.5);
});
