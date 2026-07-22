import { expect, test } from "@playwright/test";
import { ensureLoggedIn } from "./auth-helper";

const screens = [
  "Overview", "Health & Traffic", "Nodes", "Anti-DPI", "Routing",
  "Network", "Operations", "Settings",
] as const;

test("primary screens expose named controls and readable dim text", async ({ page }) => {
  await ensureLoggedIn(page);

  for (const screen of screens) {
    if (screen !== "Overview") {
      await page.getByRole("button", { name: screen, exact: true }).click();
    }
    await expect(page.locator("h1.page-title")).toHaveText(screen);

    const unnamed = await page.locator("button:visible, input:visible, select:visible, textarea:visible")
      .evaluateAll((controls) => controls.flatMap((control, index) => {
        const element = control as HTMLButtonElement | HTMLInputElement |
          HTMLSelectElement | HTMLTextAreaElement;
        const labelledBy = element.getAttribute("aria-labelledby")
          ?.split(/\s+/)
          .some((id) => document.getElementById(id)?.textContent?.trim());
        const label = element.getAttribute("aria-label")?.trim()
          || ("labels" in element
            ? [...(element.labels ?? [])].map((item) => item.textContent?.trim()).join(" ").trim()
            : "")
          || labelledBy
          || element.getAttribute("title")?.trim()
          || (element instanceof HTMLButtonElement ? element.textContent?.trim() : "");
        return label ? [] : [`${index}:${element.tagName.toLowerCase()}:${element.outerHTML.slice(0, 160)}`];
      }));
    expect(unnamed, `${screen} contains controls without an accessible name`).toEqual([]);
  }

  const contrast = await page.evaluate(() => {
    const fg = getComputedStyle(document.querySelector<HTMLElement>(".nav-group")!).color;
    const bg = getComputedStyle(document.querySelector<HTMLElement>(".sidebar")!).backgroundColor;
    const rgb = (value: string) => value.match(/[\d.]+/g)!.slice(0, 3).map(Number);
    const luminance = (value: string) => {
      const channels = rgb(value).map((channel) => {
        const normalized = channel / 255;
        return normalized <= 0.04045
          ? normalized / 12.92
          : ((normalized + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
    };
    const [lighter, darker] = [luminance(fg), luminance(bg)].sort((a, b) => b - a);
    return (lighter + 0.05) / (darker + 0.05);
  });
  expect(contrast).toBeGreaterThanOrEqual(4.5);
});
