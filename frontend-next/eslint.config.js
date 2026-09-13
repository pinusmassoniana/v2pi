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
]);
