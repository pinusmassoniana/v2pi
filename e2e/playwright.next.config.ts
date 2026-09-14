import { defineConfig, devices } from "@playwright/test";
import path from "node:path";

// The new frontend, full stack: FastAPI (dry-run network backend, throwaway data dir) serving
// frontend-next/dist through PI_GW_STATIC_DIR — no backend change — at a phone and a desktop width.
const PORT = process.env.PI_GW_NEXT_PORT ?? "8098";
const BASE = `http://127.0.0.1:${PORT}`;
const STATIC_DIR = path.resolve(__dirname, "..", "frontend-next", "dist");

export default defineConfig({
  testDir: "./tests-next",
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never", outputFolder: "playwright-report-next" }]] : "list",
  outputDir: "test-results-next",
  use: { baseURL: BASE, trace: "on-first-retry" },
  projects: [
    { name: "phone", use: { ...devices["Pixel 7"], viewport: { width: 390, height: 844 } } },
    { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
  ],
  webServer: {
    command: "bash run-server.sh",
    url: `${BASE}/api/health`,
    timeout: 120_000,
    reuseExistingServer: false,
    stdout: "pipe",
    stderr: "pipe",
    // keep PATH and friends; only add what the new frontend needs
    env: { ...(process.env as Record<string, string>), PI_GW_PORT: PORT, PI_GW_STATIC_DIR: STATIC_DIR },
  },
});
