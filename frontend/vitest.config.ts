import { defineConfig } from "vitest/config";
import { svelte } from "@sveltejs/vite-plugin-svelte";

export default defineConfig({
  plugins: [svelte()],
  // Component tests need Svelte's client entry; the default Node condition resolves the SSR API.
  resolve: { conditions: ["browser"] },
  test: { environment: "jsdom", globals: true },
});
