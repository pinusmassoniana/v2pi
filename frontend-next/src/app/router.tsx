import {
  Outlet, createHashHistory, createRootRoute, createRoute, createRouter, redirect, type RouterHistory,
} from "@tanstack/react-router";
import { LEGACY_REDIRECTS } from "./nav";
import { NodeDetailPlaceholder, TabPlaceholder } from "./pages/Placeholder";

function RootLayout() {
  return <Outlet />;
}

export const rootRoute = createRootRoute({ component: RootLayout });

const homeRoute = createRoute({ getParentRoute: () => rootRoute, path: "/", component: () => <TabPlaceholder path="/" /> });
const trafficRoute = createRoute({ getParentRoute: () => rootRoute, path: "/traffic", component: () => <TabPlaceholder path="/traffic" /> });
const nodesRoute = createRoute({ getParentRoute: () => rootRoute, path: "/nodes", component: () => <TabPlaceholder path="/nodes" /> });
const subscriptionsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/nodes/subscriptions", component: () => <TabPlaceholder path="/nodes/subscriptions" /> });
const nodeDetailRoute = createRoute({ getParentRoute: () => rootRoute, path: "/nodes/$nodeId", component: NodeDetailPlaceholder });
const routingRoute = createRoute({ getParentRoute: () => rootRoute, path: "/tunnel/routing", component: () => <TabPlaceholder path="/tunnel/routing" /> });
const antiDpiRoute = createRoute({ getParentRoute: () => rootRoute, path: "/tunnel/anti-dpi", component: () => <TabPlaceholder path="/tunnel/anti-dpi" /> });
const healthRoute = createRoute({ getParentRoute: () => rootRoute, path: "/tunnel/health", component: () => <TabPlaceholder path="/tunnel/health" /> });
const networkRoute = createRoute({ getParentRoute: () => rootRoute, path: "/gateway/network", component: () => <TabPlaceholder path="/gateway/network" /> });
const remoteAccessRoute = createRoute({ getParentRoute: () => rootRoute, path: "/gateway/remote-access", component: () => <TabPlaceholder path="/gateway/remote-access" /> });
const backupsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/system/backups", component: () => <TabPlaceholder path="/system/backups" /> });
const accessRoute = createRoute({ getParentRoute: () => rootRoute, path: "/system/access", component: () => <TabPlaceholder path="/system/access" /> });
const logsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/system/logs", component: () => <TabPlaceholder path="/system/logs" /> });
const panelRoute = createRoute({ getParentRoute: () => rootRoute, path: "/system/panel", component: () => <TabPlaceholder path="/system/panel" /> });

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
  return createRouter({ routeTree, history });
}

export const router = createAppRouter();

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof createAppRouter>;
  }
}
