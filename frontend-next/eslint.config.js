import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import { defineConfig } from "eslint/config";

export default defineConfig([
  { ignores: ["dist/**", "node_modules/**"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  reactHooks.configs.flat.recommended,
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: { globals: globals.browser },
    // `try { socket.close() } catch {}` is intentional in this codebase: best-effort teardown.
    rules: { "no-empty": ["error", { allowEmptyCatch: true }] },
  },
  { files: ["*.config.{js,ts}"], languageOptions: { globals: globals.node } },
  {
    // Ported verbatim from the Svelte client; untyped responses are narrowed at call sites.
    files: ["src/api/client.ts", "src/api/client.test.ts"],
    rules: {
      "@typescript-eslint/no-explicit-any": "off",
      "@typescript-eslint/no-unused-vars": ["error", { caughtErrors: "none" }],
      // A fake WebSocket constructor in client.test.ts captures `this` for the test to reach the
      // instance the client code created internally — ported verbatim from the Svelte client.
      "@typescript-eslint/no-this-alias": "off",
    },
  },
  {
    files: ["src/**/*.{ts,tsx}"],
    ignores: ["src/api/live.ts", "src/**/*.test.{ts,tsx}"],
    rules: {
      "no-restricted-syntax": ["error", {
        selector: "Property[key.name='refetchInterval']",
        message: "Poll through usePolledQuery (src/api/live.ts): one polling owner per query key.",
      }],
    },
  },
]);
