import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles/index.css";
import { createQueryClient } from "./api/queryClient";
import { AuthGate } from "./app/auth";
import { router } from "./app/router";
import { applyTheme, getStoredTheme, resolveInitialTheme } from "./lib/theme";

// Reconcile what the inline script in index.html set, and persist a default on first run.
applyTheme(resolveInitialTheme(getStoredTheme(), "dark"));

const queryClient = createQueryClient();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthGate>
        <RouterProvider router={router} />
      </AuthGate>
    </QueryClientProvider>
  </StrictMode>,
);
