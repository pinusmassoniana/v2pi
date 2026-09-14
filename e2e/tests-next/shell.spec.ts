import { expect, test } from "@playwright/test";
import { ensureLoggedIn } from "../tests/auth-helper";

const TABS = [
  ["Home", "Overview", "/"], ["Home", "Traffic", "/traffic"],
  ["Nodes", "Servers", "/nodes"], ["Nodes", "Subscriptions", "/nodes/subscriptions"],
  ["Tunnel", "Routing", "/tunnel/routing"], ["Tunnel", "Anti-DPI", "/tunnel/anti-dpi"], ["Tunnel", "Health & failover", "/tunnel/health"],
  ["Gateway", "Network", "/gateway/network"], ["Gateway", "Remote access", "/gateway/remote-access"],
  ["System", "Backups", "/system/backups"], ["System", "Access", "/system/access"], ["System", "Logs", "/system/logs"], ["System", "Panel", "/system/panel"],
] as const;

const FIRST_TAB = new Map<string, string>();
for (const [section, tab] of TABS) if (!FIRST_TAB.has(section)) FIRST_TAB.set(section, tab);

const hashOf = (path: string) => new RegExp(`#${path.replace(/[/-]/g, "\\$&")}$`);

test("every tab is reachable through the navigation for this width", async ({ page }, info) => {
  await ensureLoggedIn(page);
  for (const [section, tab, path] of TABS) {
    if (info.project.name === "phone") {
      await page.getByRole("navigation", { name: "Sections" }).getByRole("link", { name: section, exact: true }).click();
      if (FIRST_TAB.get(section) !== tab) {
        await page.getByRole("navigation", { name: `${section} tabs` }).getByRole("link", { name: tab, exact: true }).click();
      }
    } else {
      await page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: tab, exact: true }).click();
    }
    await expect(page.locator("h1.page-title")).toHaveText(tab);
    await expect(page).toHaveURL(hashOf(path));
  }
});

test("deep links, the node route and old bookmarks land on the right screen", async ({ page }) => {
  await ensureLoggedIn(page);
  await page.goto("/#/nodes/7");
  await expect(page.locator("h1.page-title")).toHaveText("Node");
  await page.goto("/#/settings");
  await expect(page).toHaveURL(hashOf("/system/panel"));
  await expect(page.locator("h1.page-title")).toHaveText("Panel");
  await page.goto("/#/no-such-screen");
  await expect(page).toHaveURL(hashOf("/"));
});

test("the command palette reaches a screen by name", async ({ page }) => {
  await ensureLoggedIn(page);
  await page.getByRole("button", { name: "Search and commands" }).click();
  await page.getByPlaceholder("Search nodes, screens and actions…").fill("Remote access");
  await page.keyboard.press("Enter");
  await expect(page.locator("h1.page-title")).toHaveText("Remote access");
});

test("theme toggles, and log out returns to the login screen", async ({ page }, info) => {
  await ensureLoggedIn(page);
  const html = page.locator("html");
  const before = (await html.getAttribute("data-theme")) ?? "dark";
  await page.getByRole("button", { name: "Toggle theme" }).click();
  await expect(html).toHaveAttribute("data-theme", before === "dark" ? "light" : "dark");
  if (info.project.name === "phone") await page.goto("/#/system/panel");
  await page.getByRole("button", { name: "Log out" }).click();
  await expect(page.getByRole("button", { name: "Log in" })).toBeVisible();
});
