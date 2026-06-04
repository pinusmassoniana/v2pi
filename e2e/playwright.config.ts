import { defineConfig, devices } from "@playwright/test";

// Full-stack smoke: Playwright drives a real browser against the actual panel
// (FastAPI serving the built SPA) launched by run-server.sh in a safe dry-run mode.
const PORT = process.env.PI_GW_PORT ?? "8099";
const BASE = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: { baseURL: BASE, trace: "on-first-retry" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "bash run-server.sh",
    url: `${BASE}/api/health`,
    timeout: 120_000,
    reuseExistingServer: !process.env.CI,
    stdout: "pipe",
    stderr: "pipe",
  },
});
