import { describe, it, expect, beforeEach } from "vitest";
import { THEME_KEY, getStoredTheme, resolveInitialTheme, toggleTheme, applyTheme } from "./theme";

beforeEach(() => {
  localStorage.clear();
  delete document.documentElement.dataset.theme;
});

describe("theme", () => {
  it("resolveInitialTheme prefers stored over system", () => {
    expect(resolveInitialTheme("dark", "light")).toBe("dark");
    expect(resolveInitialTheme(null, "dark")).toBe("dark");
    expect(resolveInitialTheme(null, "light")).toBe("light");
  });

  it("getStoredTheme reads a valid persisted value, else null", () => {
    expect(getStoredTheme()).toBeNull();
    localStorage.setItem(THEME_KEY, "dark");
    expect(getStoredTheme()).toBe("dark");
    localStorage.setItem(THEME_KEY, "bogus");
    expect(getStoredTheme()).toBeNull();
  });

  it("toggleTheme flips", () => {
    expect(toggleTheme("light")).toBe("dark");
    expect(toggleTheme("dark")).toBe("light");
  });

  it("applyTheme sets the dom attribute and persists", () => {
    applyTheme("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem(THEME_KEY)).toBe("dark");
    applyTheme("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem(THEME_KEY)).toBe("light");
  });
});
