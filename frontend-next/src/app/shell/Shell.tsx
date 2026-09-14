import { Outlet, useRouterState } from "@tanstack/react-router";
import { queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { useUnsavedEditsBlocker } from "../guard";
import { sectionForPath, titleForPath } from "../nav";
import { BottomTabBar } from "./BottomTabBar";
import { CommandPalette } from "./CommandPalette";
import { ErrorBoundary } from "./ErrorBoundary";
import { LogoutButton } from "./LogoutButton";
import { OfflineBanner } from "./OfflineBanner";
import { SegmentedTabs } from "./SegmentedTabs";
import { Sidebar } from "./Sidebar";
import { Topbar } from "./Topbar";
import { XrayCard } from "./XrayCard";

export const STATUS_POLL_MS = 3_000;

export function Shell() {
  // The shell is the one polling owner for `status`; everyone else reads the cache.
  const status = usePolledQuery(queries.status(), STATUS_POLL_MS);
  useUnsavedEditsBlocker();
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const section = sectionForPath(pathname);

  return (
    <div className="min-h-dvh md:grid md:grid-cols-[232px_minmax(0,1fr)]">
      <Sidebar pathname={pathname} status={status.data} className="hidden md:flex" />
      <div className="flex min-w-0 flex-col pb-28 md:pb-0">
        <Topbar title={titleForPath(pathname)} status={status.data} stale={status.isError} />
        <OfflineBanner />
        <SegmentedTabs section={section} pathname={pathname} className="md:hidden" />
        <main className="mx-auto flex w-full max-w-[1400px] flex-1 flex-col gap-3 p-4 md:p-5">
          {/* Phone homes for shell controls: xray-core on Home › Overview, log out in System. */}
          {pathname === "/" ? <XrayCard status={status.data} className="md:hidden" /> : null}
          {section.id === "system" ? (
            <div className="flex justify-end md:hidden"><LogoutButton /></div>
          ) : null}
          <ErrorBoundary resetKey={pathname}>
            <Outlet />
          </ErrorBoundary>
        </main>
      </div>
      <BottomTabBar pathname={pathname} className="md:hidden" />
      <CommandPalette />
    </div>
  );
}
