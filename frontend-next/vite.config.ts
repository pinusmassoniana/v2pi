import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Dev server only. /api — REST and the /api/ws/traffic WebSocket — goes to a running panel.
// Set V2PI_API to preview against another panel, e.g. the gateway VM on real data.
const target = process.env.V2PI_API ?? "http://127.0.0.1:8000";

// Libraries the first screen needs: the auth forms, overlays, the command palette and toasts, with the
// packages they pull in. Only these share the `vendor` chunk; anything a section alone imports (an icon, a
// chart helper) stays in that section's lazily loaded chunk instead of loading at boot.
const BOOT_VENDOR = new RegExp(
  "node_modules[\\\\/](" +
    [
      "radix-ui", "@radix-ui", "@floating-ui", "aria-hidden", "react-remove-scroll", "react-remove-scroll-bar",
      "react-style-singleton", "use-callback-ref", "use-sidecar", "detect-node-es", "get-nonce", "tslib",
      "cmdk", "sonner", "react-hook-form", "@hookform", "zod", "@standard-schema",
      "clsx", "tailwind-merge", "class-variance-authority",
    ].join("|") +
    ")[\\\\/]",
);

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { proxy: { "/api": { target, ws: true, secure: false } } },
  build: {
    rolldownOptions: {
      output: {
        // Name each section's lazy chunk after its section (screens-home, screens-nodes, …).
        chunkFileNames: (chunk) => {
          const section = chunk.facadeModuleId?.match(/features[\\/]([a-z]+)[\\/]screens\.tsx$/)?.[1];
          return section ? `assets/screens-${section}-[hash].js` : "assets/[name]-[hash].js";
        },
        codeSplitting: {
          groups: [
            { name: "react", test: /node_modules[\\/](react|react-dom|scheduler)[\\/]/, priority: 3 },
            { name: "tanstack", test: /node_modules[\\/]@tanstack[\\/]/, priority: 2 },
            { name: "vendor", test: BOOT_VENDOR, priority: 1 },
          ],
        },
      },
    },
  },
});
