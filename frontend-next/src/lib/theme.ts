export type Theme = "light" | "dark";
export const THEME_KEY = "v2pi-theme";

// Touching localStorage THROWS (SecurityError) when the browser blocks site data — it does not
// just return null. This runs before the app mounts, so an unguarded call is a blank page.
export function getStoredTheme(): Theme | null {
  try {
    const v = globalThis.localStorage?.getItem(THEME_KEY);
    return v === "light" || v === "dark" ? v : null;
  } catch {
    return null;
  }
}

export function resolveInitialTheme(stored: Theme | null, system: Theme): Theme {
  return stored ?? system;
}

// Ground colour per theme — the browser chrome (theme-color meta) follows the resolved UI theme.
const THEME_BG: Record<Theme, string> = { dark: "#07060d", light: "#f4f3fb" };

export function applyTheme(t: Theme): void {
  document.documentElement.dataset.theme = t;
  try {
    globalThis.localStorage?.setItem(THEME_KEY, t);
  } catch {
    // blocked storage: the theme still applies, it just is not remembered
  }
  document.getElementById("theme-color-meta")?.setAttribute("content", THEME_BG[t]);
}

export function toggleTheme(current: Theme): Theme {
  return current === "dark" ? "light" : "dark";
}
