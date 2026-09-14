import { act, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SECTIONS } from "../app/nav";
import { closePalette } from "../app/shell/palette";
import { mockApi } from "./fixtures";
import { renderApp } from "./renderApp";

// React's version of the freeze fixed in the Svelte panel in v1.17.26: an effect that writes state
// it reads loops until React gives up ("Maximum update depth exceeded") and the screen is dead.
// Mount every screen, fill every field and flip every switch — twice.
const PATHS = [...SECTIONS.flatMap((s) => s.tabs.map((t) => t.to)), "/nodes/1"];

function fillEveryField(root: HTMLElement) {
  for (const el of root.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("input, textarea")) {
    if (el instanceof HTMLInputElement && ["file", "hidden", "submit", "button", "reset"].includes(el.type)) continue;
    if (el instanceof HTMLInputElement && (el.type === "checkbox" || el.type === "radio")) fireEvent.click(el);
    else fireEvent.change(el, { target: { value: el instanceof HTMLInputElement && el.type === "number" ? "7" : "loop-probe" } });
  }
  for (const toggle of root.querySelectorAll<HTMLButtonElement>('button[role="switch"]')) fireEvent.click(toggle);
}

afterEach(() => act(() => closePalette()));

describe.each(PATHS)("%s", (path) => {
  it("does not fall into a render loop when every field is used", async () => {
    mockApi();
    const logged: unknown[][] = [];
    vi.spyOn(console, "error").mockImplementation((...args) => { logged.push(args); });
    renderApp(path);
    await screen.findByRole("heading", { level: 1 });
    for (let pass = 0; pass < 2; pass++) {
      await act(async () => { fillEveryField(document.body); });
    }
    const text = logged.flat().map((item) => (item instanceof Error ? item.message : String(item))).join("\n");
    expect(text).not.toContain("Maximum update depth exceeded");
  });
});
