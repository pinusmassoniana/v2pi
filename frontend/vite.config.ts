import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

export default defineConfig({
  plugins: [svelte()],
  build: { outDir: "../backend/pi_gw_panel/static", emptyOutDir: true },
  // ws:true — /api/ws/traffic is a WebSocket; without it the live traffic graph is the one thing
  // that never works under `npm run dev` (it works in the built app, which the backend serves itself).
  server: { proxy: { "/api": { target: "http://127.0.0.1:8000", ws: true } } },
});
