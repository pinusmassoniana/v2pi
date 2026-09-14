import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, setOnUnauthorized } from "../api/client";
import { resetClock } from "../api/clock";
import { Button } from "../components/ui/Button";
import { LoginScreen } from "../features/auth/LoginScreen";
import { SetupScreen } from "../features/auth/SetupScreen";

export type Phase =
  | { kind: "booting" }
  | { kind: "offline" }
  | { kind: "setup"; bootstrapRequired: boolean }
  | { kind: "login" }
  | { kind: "authed" };

/** First-run setup, then the session probe. A failed /setup probe means the server is unreachable. */
export async function resolvePhase(): Promise<Phase> {
  let setup: { needs_setup: boolean; bootstrap_required?: boolean };
  try {
    setup = await api.getSetup();
  } catch {
    return { kind: "offline" };
  }
  if (setup.needs_setup) return { kind: "setup", bootstrapRequired: setup.bootstrap_required ?? true };
  try {
    await api.getStatus();
    return { kind: "authed" };
  } catch {
    return { kind: "login" };
  }
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
    void resolvePhase().then((next) => { if (!cancelled) setPhase(next); });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    // A 401 anywhere mid-session (idle timeout, password changed elsewhere) drops back to Login
    // with nothing of the old session left in the cache.
    setOnUnauthorized(() => {
      queryClient.clear();
      resetClock();
      setPhase({ kind: "login" });
    });
    return () => setOnUnauthorized(null);
  }, [queryClient]);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } catch {
      // the session may already be gone; drop local state regardless
    }
    queryClient.clear();
    resetClock();
    setPhase({ kind: "login" });
  }, [queryClient]);

  const retry = () => {
    setPhase({ kind: "booting" });
    void resolvePhase().then(setPhase);
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
