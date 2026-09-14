import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Vitest globals are off, so Testing Library cannot register its own cleanup.
afterEach(() => cleanup());

// jsdom defines window.scrollTo but logs "Not implemented" on every call; the router
// resets scroll position on each render, so this keeps the test output quiet.
window.scrollTo = () => {};
