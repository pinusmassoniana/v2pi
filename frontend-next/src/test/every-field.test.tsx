import { act, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SECTIONS } from "../app/nav";
import { closePalette } from "../app/shell/palette";
import { settleConfirm } from "../components/confirm";
import { TRAFFIC_FRAME, mockApi, mockGateway } from "./fixtures";
import { renderApp } from "./renderApp";

// React's version of the freeze fixed in the Svelte panel in v1.17.26: an effect that writes state
// it reads loops until React gives up ("Maximum update depth exceeded") and the screen is dead.
// Mount every screen, fill every field and flip every switch — twice.
const PATHS = [...SECTIONS.flatMap((s) => s.tabs.map((t) => t.to)), "/nodes/1"];

// Rebuilt screens are checked with their data on screen, not while they are still loading.
const LOADED: Readonly<Record<string, [region: string, text: string]>> = {
  "/": ["Upstream health", "de-fra-01"],
  "/traffic": ["Probe latency by node", "ch-zrh-02"],
  "/nodes": ["Servers toolbar", "work"],
  "/nodes/subscriptions": ["home", "fetch failed: timeout"],
  "/nodes/1": ["Config", "nl-ams-03.example.org"],
  "/tunnel/routing": ["Defaults", "used by the catch-all row"],
  "/tunnel/anti-dpi": ["Anti-DPI profiles", "fragment-tls"],
  "/tunnel/health": ["Health monitoring", "master switch"],
  "/gateway/network": ["Router checklist", "eth0.2"],
  "/gateway/remote-access": ["Clients", "iphone-anna"],
};

function fillEveryField(root: HTMLElement) {
  for (const el of root.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("input, textarea")) {
    if (el instanceof HTMLInputElement && ["file", "hidden", "submit", "button", "reset"].includes(el.type)) continue;
    if (el instanceof HTMLInputElement && (el.type === "checkbox" || el.type === "radio")) fireEvent.click(el);
    else fireEvent.change(el, { target: { value: el instanceof HTMLInputElement && el.type === "number" ? "7" : "loop-probe" } });
  }
  for (const toggle of root.querySelectorAll<HTMLButtonElement>('button[role="switch"]')) fireEvent.click(toggle);
}

afterEach(() => act(() => {
  closePalette();
  settleConfirm(false);   // a switch that asks first (disarming the kill-switch) leaves its question open
}));

describe.each(PATHS)("%s", (path) => {
  it("does not fall into a render loop when every field is used", async () => {
    // Gateway's screens get their own network read, so flipped switches and questions stay in mocks.
    const api$ = path.startsWith("/gateway") ? mockGateway(mockApi()) : mockApi();
    const logged: unknown[][] = [];
    vi.spyOn(console, "error").mockImplementation((...args) => { logged.push(args); });
    renderApp(path);
    // hidden: a node's detail is a modal sheet on a desktop, which hides the page behind it from queries
    await screen.findByRole("heading", { level: 1, hidden: true });
    const loaded = LOADED[path];
    if (loaded) {
      const [region, text] = loaded;
      await within(await screen.findByRole("region", { name: region })).findByText(text, { exact: false });
      if (api$.connectTraffic.mock.calls.length > 0) api$.emitTraffic(TRAFFIC_FRAME);   // screens that stream live traffic
    }
    for (let pass = 0; pass < 2; pass++) {
      await act(async () => { fillEveryField(document.body); });
    }
    const text = logged.flat().map((item) => (item instanceof Error ? item.message : String(item))).join("\n");
    expect(text).not.toContain("Maximum update depth exceeded");
  });
});
