import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { toast } from "sonner";
import { ApiError, api, setOnUnauthorized } from "../api/client";
import { recordServerNow, resetClock } from "../api/clock";
import { keys } from "../api/keys";
import { trafficStore } from "../api/traffic";
import { confirm, settleConfirm } from "../components/confirm";
import { Button } from "../components/ui/Button";
import { notifyError } from "../components/ui/Toaster";
import { LoginScreen } from "../features/auth/LoginScreen";
import { SetupScreen } from "../features/auth/SetupScreen";
import { clearLastRestore } from "../features/system/lastRestore";
import { hasUnsavedEdits } from "./guard";
import { closePalette } from "./shell/palette";

export type Phase =
  | { kind: "booting" }
  | { kind: "offline" }
  | { kind: "setup"; bootstrapRequired: boolean }
  | { kind: "login" }
  | { kind: "authed" };

/**
 * First-run setup, then the session probe. A failed /setup probe means the server is unreachable.
 * The probe's status reply goes into the cache, so the shell does not fetch it again at once.
 */
export async function resolvePhase(queryClient: QueryClient): Promise<Phase> {
  let setup: { needs_setup: boolean; bootstrap_required?: boolean };
  try {
    setup = await api.getSetup();
  } catch {
    return { kind: "offline" };
  }
  if (setup.needs_setup) return { kind: "setup", bootstrapRequired: setup.bootstrap_required ?? true };
  try {
    const status = await api.getStatus();
    recordServerNow(status.server_now);
    queryClient.setQueryData(keys.status, status);
    return { kind: "authed" };
  } catch (error) {
    // Only a lost session is a login. A status read that timed out or failed otherwise is the gateway not answering
    // (a long write holds it, a restart is under way): a bare login form would read as "your session expired".
    return error instanceof ApiError && error.status === 401 ? { kind: "login" } : { kind: "offline" };
  }
}

/**
 * Forget everything the old session left in memory, so none of it reappears after the next login:
 * the query cache, the clock skew, a pending confirmation (answered "no", so a queued risky action
 * never runs), the command palette, the live traffic stream, any toasts, and the last restore's reply — which names
 * a path on the gateway's filesystem and belongs to the session that made it.
 */
export function endSession(queryClient: QueryClient): void {
  queryClient.clear();
  clearLastRestore();
  resetClock();
  settleConfirm(false);
  closePalette();
  trafficStore.reset();
  toast.dismiss();
}

interface AuthValue { logout: () => Promise<void> }

export const AuthContext = createContext<AuthValue | null>(null);

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthGate");
  return value;
}

export function AuthGate({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [phase, setPhase] = useState<Phase>({ kind: "booting" });

  useEffect(() => {
    let cancelled = false;
    void resolvePhase(queryClient).then((next) => { if (!cancelled) setPhase(next); });
    return () => { cancelled = true; };
  }, [queryClient]);

  useEffect(() => {
    // A 401 anywhere mid-session (idle timeout, password changed elsewhere) drops back to Login
    // with nothing of the old session left behind.
    setOnUnauthorized(() => {
      endSession(queryClient);
      setPhase({ kind: "login" });
    });
    return () => setOnUnauthorized(null);
  }, [queryClient]);

  const logout = useCallback(async () => {
    if (hasUnsavedEdits() && !(await confirm("Discard unsaved changes and log out?", { confirmLabel: "Log out" }))) return;
    try {
      await api.logout();
    } catch (error) {
      // Only a session that is already gone (401) is safe to forget here. Any other failure means the gateway may
      // never have heard it: the browser's session cookie stays valid, so showing the login form would be a lie.
      if (!(error instanceof ApiError && error.status === 401)) {
        notifyError(null, "Log out did not reach the gateway — this browser is still logged in. Try again.");
        return;
      }
    }
    endSession(queryClient);
    setPhase({ kind: "login" });
  }, [queryClient]);

  const retry = () => {
    setPhase({ kind: "booting" });
    void resolvePhase(queryClient).then(setPhase);
  };

  switch (phase.kind) {
    case "booting":
      return (
        <div className="grid min-h-dvh place-items-center">
          <span role="status" aria-label="Loading" className="size-8 animate-spin rounded-full border-2 border-line border-t-g2" />
        </div>
      );
    case "offline":
      return (
        <div className="grid min-h-dvh place-items-center p-4">
          <div className="glass flex flex-col items-center gap-3 p-6 text-center">
            <p className="text-sm text-bad">Can't reach the panel server.</p>
            <Button onClick={retry}>Retry</Button>
          </div>
        </div>
      );
    case "setup":
      return <SetupScreen bootstrapRequired={phase.bootstrapRequired} onDone={() => setPhase({ kind: "authed" })} />;
    case "login":
      return <LoginScreen onLogin={() => setPhase({ kind: "authed" })} />;
    case "authed":
      return <AuthContext.Provider value={{ logout }}>{children}</AuthContext.Provider>;
  }
}
