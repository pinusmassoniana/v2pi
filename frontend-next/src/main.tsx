import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles/index.css";
import { createQueryClient } from "./api/queryClient";
import { AuthGate } from "./app/auth";
import { RouterMount, router } from "./app/router";
import { ConfirmDialog } from "./components/ui/ConfirmDialog";
import { Toaster } from "./components/ui/Toaster";
import { applyTheme, getStoredTheme, resolveInitialTheme } from "./lib/theme";

// Reconcile what the inline script in index.html set, and persist a default on first run.
applyTheme(resolveInitialTheme(getStoredTheme(), "dark"));

const queryClient = createQueryClient();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthGate>
        <RouterMount router={router} />
      </AuthGate>
      {/* Outside the gate: log out's own confirmation must not depend on the screen it ends. */}
      <ConfirmDialog />
      <Toaster />
    </QueryClientProvider>
  </StrictMode>,
);
