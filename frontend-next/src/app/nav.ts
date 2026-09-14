export type AppPath =
  | "/" | "/traffic"
  | "/nodes" | "/nodes/subscriptions"
  | "/tunnel/routing" | "/tunnel/anti-dpi" | "/tunnel/health"
  | "/gateway/network" | "/gateway/remote-access"
  | "/system/backups" | "/system/access" | "/system/logs" | "/system/panel";

export type SectionId = "home" | "nodes" | "tunnel" | "gateway" | "system";

export interface Tab { label: string; to: AppPath }
export interface Section { id: SectionId; label: string; tabs: [Tab, ...Tab[]] }

export const SECTIONS: readonly Section[] = [
  { id: "home", label: "Home", tabs: [{ label: "Overview", to: "/" }, { label: "Traffic", to: "/traffic" }] },
  { id: "nodes", label: "Nodes", tabs: [{ label: "Servers", to: "/nodes" }, { label: "Subscriptions", to: "/nodes/subscriptions" }] },
  {
    id: "tunnel", label: "Tunnel",
    tabs: [{ label: "Routing", to: "/tunnel/routing" }, { label: "Anti-DPI", to: "/tunnel/anti-dpi" }, { label: "Health & failover", to: "/tunnel/health" }],
  },
  { id: "gateway", label: "Gateway", tabs: [{ label: "Network", to: "/gateway/network" }, { label: "Remote access", to: "/gateway/remote-access" }] },
  {
    id: "system", label: "System",
    tabs: [
      { label: "Backups", to: "/system/backups" }, { label: "Access", to: "/system/access" },
      { label: "Logs", to: "/system/logs" }, { label: "Panel", to: "/system/panel" },
    ],
  },
];

/** Bookmarks from the Svelte panel's `#/<view>` URLs. `#/nodes` keeps its meaning, so it is not here. */
export const LEGACY_REDIRECTS: Readonly<Record<string, AppPath>> = {
  "/dashboard": "/",
  "/health": "/traffic",
  "/tuning": "/tunnel/anti-dpi",
  "/routing": "/tunnel/routing",
  "/network": "/gateway/network",
  "/roadwarrior": "/gateway/remote-access",
  "/operations": "/system/backups",
  "/settings": "/system/panel",
};

const NODE_DETAIL = /^\/nodes\/(?!subscriptions$)[^/]+$/;

export function isNodeDetail(pathname: string): boolean {
  return NODE_DETAIL.test(pathname);
}

export function isTabActive(to: AppPath, pathname: string): boolean {
  if (to === "/nodes" && isNodeDetail(pathname)) return true;
  return pathname === to;
}

export function sectionForPath(pathname: string): Section {
  return SECTIONS.find((section) => section.tabs.some((tab) => isTabActive(tab.to, pathname))) ?? SECTIONS[0]!;
}

export function titleForPath(pathname: string): string {
  if (isNodeDetail(pathname)) return "Node";
  for (const section of SECTIONS) {
    for (const tab of section.tabs) if (tab.to === pathname) return tab.label;
  }
  return "Overview";
}
