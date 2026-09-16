import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { render } from "@testing-library/react";
import { vi } from "vitest";
import { createQueryClient } from "../api/queryClient";
import { AuthContext } from "../app/auth";
import { createAppRouter } from "../app/router";
import { ConfirmDialog } from "../components/ui/ConfirmDialog";
import { Toaster } from "../components/ui/Toaster";

/** The authenticated app at `path`, on memory history, with the root-level overlays main.tsx mounts. */
export function renderApp(path: string, options: { client?: QueryClient; logout?: () => Promise<void> } = {}) {
  const client = options.client ?? createQueryClient();
  const logout = options.logout ?? vi.fn(async () => {});
  const router = createAppRouter(createMemoryHistory({ initialEntries: [path] }));
  const view = render(
    <QueryClientProvider client={client}>
      <AuthContext.Provider value={{ logout }}>
        <RouterProvider router={router} />
      </AuthContext.Provider>
      <ConfirmDialog />
      <Toaster />
    </QueryClientProvider>,
  );
  return { ...view, client, router, logout };
}
