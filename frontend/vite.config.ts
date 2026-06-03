import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

export default defineConfig({
  plugins: [svelte()],
  build: { outDir: "../backend/pi_gw_panel/static", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
});
