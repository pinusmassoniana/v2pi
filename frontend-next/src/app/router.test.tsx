import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockApi } from "../test/fixtures";
import { renderApp } from "../test/renderApp";
import { LEGACY_REDIRECTS, SECTIONS, isTabActive, sectionForPath, titleForPath } from "./nav";

// The shell needs the query client, auth context and a backend.
function renderAt(path: string) {
  mockApi();
  return renderApp(path).router;
}

const TABS = SECTIONS.flatMap((s) => s.tabs.map((t) => [s.label, t.label, t.to] as const));

describe("navigation model", () => {
  it("has 13 uniquely named tabs in the five approved sections", () => {
    expect(SECTIONS.map((s) => s.label)).toEqual(["Home", "Nodes", "Tunnel", "Gateway", "System"]);
    expect(TABS).toHaveLength(13);
    expect(new Set(TABS.map(([, label]) => label)).size).toBe(13);
  });

  it("treats a node detail as the Servers tab of Nodes, and titles every tab", () => {
    expect(isTabActive("/nodes", "/nodes/12")).toBe(true);
    expect(isTabActive("/nodes", "/nodes/subscriptions")).toBe(false);
    expect(sectionForPath("/nodes/12").id).toBe("nodes");
    expect(sectionForPath("/traffic").id).toBe("home");
    expect(titleForPath("/nodes/12")).toBe("Node");
    expect(titleForPath("/tunnel/health")).toBe("Health & failover");
  });
});

// Rebuilt screens show a card of their own instead of the placeholder.
const BUILT: Readonly<Record<string, string>> = { "/": "Status" };

describe("router", () => {
  it.each(TABS)("%s › %s renders at %s", async (section, tab, path) => {
    renderAt(path);
    const card = BUILT[path];
    if (card) expect(await screen.findByRole("region", { name: card })).toBeInTheDocument();
    else expect(await screen.findByText(`${section} › ${tab}`)).toBeInTheDocument();
  });

  it("renders a node detail, while the static subscriptions route wins over the node id", async () => {
    renderAt("/nodes/42");
    expect(await screen.findByText("Nodes › Node 42")).toBeInTheDocument();
  });

  it.each(Object.entries(LEGACY_REDIRECTS))("old bookmark %s lands on %s", async (from, to) => {
    const router = renderAt(from);
    await waitFor(() => expect(router.state.location.pathname).toBe(to));
  });

  it("sends an unknown screen to Home", async () => {
    const router = renderAt("/no-such-screen");
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
  });
});
