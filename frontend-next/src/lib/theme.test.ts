import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { THEME_KEY, applyTheme, getStoredTheme, resolveInitialTheme, toggleTheme } from "./theme";

const blocked = () => { throw new DOMException("The operation is insecure.", "SecurityError"); };

beforeEach(() => {
  localStorage.clear();
  delete document.documentElement.dataset.theme;
});
afterEach(() => vi.restoreAllMocks());

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

  it("applyTheme sets the dom attribute, the browser chrome colour, and persists", () => {
    const meta = document.createElement("meta");
    meta.id = "theme-color-meta";
    document.head.append(meta);
    applyTheme("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem(THEME_KEY)).toBe("light");
    expect(meta.getAttribute("content")).toBe("#f4f3fb");
    applyTheme("dark");
    expect(meta.getAttribute("content")).toBe("#07060d");
    meta.remove();
  });

  it("keeps working when reading or writing storage throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(blocked);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(blocked);
    expect(getStoredTheme()).toBeNull();
    expect(() => applyTheme("light")).not.toThrow();
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  it("keeps working when merely touching localStorage throws", () => {
    // jsdom may define localStorage on the instance or on Window.prototype — restore either way.
    const own = Object.getOwnPropertyDescriptor(window, "localStorage");
    Object.defineProperty(window, "localStorage", { configurable: true, get: blocked });
    try {
      expect(getStoredTheme()).toBeNull();
      expect(() => applyTheme("dark")).not.toThrow();
    } finally {
      if (own) Object.defineProperty(window, "localStorage", own);
      else delete (window as { localStorage?: Storage }).localStorage;
    }
  });
});
