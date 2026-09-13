import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Dev server only. /api — REST and the /api/ws/traffic WebSocket — goes to a running panel.
// Set V2PI_API to preview against another panel, e.g. the gateway VM on real data.
const target = process.env.V2PI_API ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { proxy: { "/api": { target, ws: true, secure: false } } },
});
