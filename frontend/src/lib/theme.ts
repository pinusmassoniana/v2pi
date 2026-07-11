export type Theme = "light" | "dark";
export const THEME_KEY = "v2pi-theme";

// matchMedia is absent under jsdom/SSR — guard with ?. and default to light.
export function systemTheme(): Theme {
  return globalThis.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function getStoredTheme(): Theme | null {
  const v = globalThis.localStorage?.getItem(THEME_KEY);
  return v === "light" || v === "dark" ? v : null;
}

export function resolveInitialTheme(stored: Theme | null, system: Theme): Theme {
  return stored ?? system;
}

// bg0 per theme — kept in sync with the browser-chrome theme-color meta so the address/status bar
// matches the resolved UI theme instead of the OS preference.
const THEME_BG: Record<Theme, string> = { dark: "#0a0d0f", light: "#eceff1" };

export function applyTheme(t: Theme): void {
  document.documentElement.dataset.theme = t;
  globalThis.localStorage?.setItem(THEME_KEY, t);
  document.getElementById("theme-color-meta")?.setAttribute("content", THEME_BG[t]);
}

export function toggleTheme(current: Theme): Theme {
  return current === "dark" ? "light" : "dark";
}
