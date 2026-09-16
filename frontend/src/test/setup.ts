import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach } from "vitest";
import { installViewport } from "./viewport";

// Vitest globals are off, so Testing Library cannot register its own cleanup.
afterEach(() => cleanup());

// Every test starts on a 1440 px desktop viewport; a test that needs a phone calls setViewportWidth(390).
beforeEach(() => installViewport());

// jsdom defines window.scrollTo but logs "Not implemented" on every call; the router
// resets scroll position on each render, so this keeps the test output quiet.
window.scrollTo = () => {};

// cmdk scrolls the active item into view and measures its list; jsdom implements neither.
if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = function scrollIntoView() {};
if (!("ResizeObserver" in globalThis)) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}
