import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode, useEffect } from "react";
import { createRoot } from "react-dom/client";
import "./styles/index.css";
import { createQueryClient } from "./api/queryClient";
import { AuthGate } from "./app/auth";
import { router } from "./app/router";
import { ConfirmDialog } from "./components/ui/ConfirmDialog";
import { Toaster } from "./components/ui/Toaster";
import { applyTheme, getStoredTheme, resolveInitialTheme } from "./lib/theme";

// Reconcile what the inline script in index.html set, and persist a default on first run.
applyTheme(resolveInitialTheme(getStoredTheme(), "dark"));

const queryClient = createQueryClient();

// The router is a single long-lived instance, but `<RouterProvider>` only mounts while authed:
// AuthGate swaps it out for Login/Setup/Offline UI, which unsubscribes the router from the
// hash history. If the hash changes while logged out (session expiry, an explicit logout that
// lands elsewhere, a bookmark), the router's own matched location goes stale and does not
// self-correct on remount. Force a fresh reconcile against the real current hash every time
// this remounts, so the screen shown always matches the URL.
function RouterMount() {
  useEffect(() => { void router.load(); }, []);
  return <RouterProvider router={router} />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthGate>
        <RouterMount />
      </AuthGate>
      {/* Outside the gate: log out's own confirmation must not depend on the screen it ends. */}
      <ConfirmDialog />
      <Toaster />
    </QueryClientProvider>
  </StrictMode>,
);
