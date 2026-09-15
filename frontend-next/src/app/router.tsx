import {
  RouterProvider, createHashHistory, createRootRoute, createRoute, createRouter, lazyRouteComponent, redirect,
  type AnyRouter, type RouterHistory,
} from "@tanstack/react-router";
import { useLayoutEffect } from "react";
import { nodesSearchSchema } from "../features/nodes/search";
import { LEGACY_REDIRECTS } from "./nav";
import { Shell } from "./shell/Shell";

export const rootRoute = createRootRoute({ component: Shell });

// Each section's screens are one lazily loaded chunk: moving between the tabs of a section never
// waits on the network, and the first load does not carry screens it has not opened yet.
const home = () => import("../features/home/screens");
const nodes = () => import("../features/nodes/screens");
const tunnel = () => import("../features/tunnel/screens");
const gateway = () => import("../features/gateway/screens");
const system = () => import("../features/system/screens");

const homeRoute = createRoute({ getParentRoute: () => rootRoute, path: "/", component: lazyRouteComponent(home, "Overview") });
const trafficRoute = createRoute({ getParentRoute: () => rootRoute, path: "/traffic", component: lazyRouteComponent(home, "Traffic") });
// The Servers list keeps its group, search and sort in the URL (N1–N3); a node's detail keeps the list's search and
// sort, so closing it returns to the list as it was.
const nodesRoute = createRoute({
  getParentRoute: () => rootRoute, path: "/nodes", validateSearch: nodesSearchSchema, component: lazyRouteComponent(nodes, "Servers"),
});
const subscriptionsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/nodes/subscriptions", component: lazyRouteComponent(nodes, "Subscriptions") });
const nodeDetailRoute = createRoute({
  getParentRoute: () => rootRoute, path: "/nodes/$nodeId", validateSearch: nodesSearchSchema, component: lazyRouteComponent(nodes, "NodeDetail"),
});
const routingRoute = createRoute({ getParentRoute: () => rootRoute, path: "/tunnel/routing", component: lazyRouteComponent(tunnel, "Routing") });
const antiDpiRoute = createRoute({ getParentRoute: () => rootRoute, path: "/tunnel/anti-dpi", component: lazyRouteComponent(tunnel, "AntiDpi") });
const healthRoute = createRoute({ getParentRoute: () => rootRoute, path: "/tunnel/health", component: lazyRouteComponent(tunnel, "Health") });
const networkRoute = createRoute({ getParentRoute: () => rootRoute, path: "/gateway/network", component: lazyRouteComponent(gateway, "Network") });
const remoteAccessRoute = createRoute({ getParentRoute: () => rootRoute, path: "/gateway/remote-access", component: lazyRouteComponent(gateway, "RemoteAccess") });
const backupsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/system/backups", component: lazyRouteComponent(system, "Backups") });
const accessRoute = createRoute({ getParentRoute: () => rootRoute, path: "/system/access", component: lazyRouteComponent(system, "Access") });
const logsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/system/logs", component: lazyRouteComponent(system, "Logs") });
const panelRoute = createRoute({ getParentRoute: () => rootRoute, path: "/system/panel", component: lazyRouteComponent(system, "Panel") });

// Anything else is an old `#/<view>` bookmark or an unknown hash: send it to its new home, or Home.
const fallbackRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "$",
  beforeLoad: ({ params }) => {
    throw redirect({ to: LEGACY_REDIRECTS[`/${params._splat ?? ""}`] ?? "/", replace: true });
  },
});

const routeTree = rootRoute.addChildren([
  homeRoute, trafficRoute, nodesRoute, subscriptionsRoute, nodeDetailRoute,
  routingRoute, antiDpiRoute, healthRoute, networkRoute, remoteAccessRoute,
  backupsRoute, accessRoute, logsRoute, panelRoute, fallbackRoute,
]);

export function createAppRouter(history: RouterHistory = createHashHistory()) {
  // Hovering or focusing a link starts loading its section's chunk, so the click rarely waits for it.
  return createRouter({ routeTree, history, defaultPreload: "intent" });
}

export const router = createAppRouter();

// The router is a single long-lived instance, but `<RouterProvider>` only mounts while authed:
// AuthGate swaps it out for Login/Setup/Offline UI, which unsubscribes the router from the
// history. If the URL changes while logged out (session expiry, an explicit logout that lands
// elsewhere, a bookmark), the router's resolved location goes stale and does not self-correct on
// remount. Reconcile before paint, and only when the URL really moved on.
export function RouterMount({ router }: { router: AnyRouter }) {
  useLayoutEffect(() => {
    router.updateLatestLocation();
    const resolved = router.state.resolvedLocation;
    if (resolved && resolved.href !== router.latestLocation.href) router.load().catch(console.error);
  }, [router]);
  return <RouterProvider router={router} />;
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof createAppRouter>;
  }
}
