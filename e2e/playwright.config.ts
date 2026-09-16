import { defineConfig, devices } from "@playwright/test";

// Full stack: FastAPI (dry-run network backend, throwaway data dir — run-server.sh) serving the SPA from its
// packaged default, backend/pi_gw_panel/static, where `npm run build:spa` writes it. A phone and a desktop
// width, plus a 1024 px laptop for the Servers table, whose columns fold by width, the routing rules table,
// the gateway screens and System.
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
  projects: [
    { name: "phone", use: { ...devices["Pixel 7"], viewport: { width: 390, height: 844 } } },
    { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    { name: "laptop", testMatch: /(nodes|tunnel|gateway|system)\.spec\.ts$/, use: { ...devices["Desktop Chrome"], viewport: { width: 1024, height: 768 } } },
  ],
  webServer: {
    command: "bash run-server.sh",
    url: `${BASE}/api/health`,
    timeout: 120_000,
    reuseExistingServer: false,
    stdout: "pipe",
    stderr: "pipe",
    env: { PI_GW_PORT: PORT },   // Playwright passes the rest of the environment through itself
  },
});
