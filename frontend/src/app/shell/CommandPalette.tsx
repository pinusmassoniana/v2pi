import { useIsMutating, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { Command } from "cmdk";
import { useEffect } from "react";
import type { Status } from "../../api/client";
import {
  CONNECTION_BUSY, CONNECTION_WRITE, PING_KEY, SUBS_REFRESH_KEY, isConnectionBusy, useApiWrite, useConnectionBusy,
} from "../../api/invalidation";
import { keys, queries } from "../../api/keys";
import { confirm } from "../../components/confirm";
import { notifyError, notifyOk } from "../../components/ui/Toaster";
import { ROLLBACK_TARGET_CHANGED, rollbackStillValid } from "../../features/home/derive";
import { updateOutcome } from "../../features/system/updates";
import { SECTIONS, type AppPath } from "../nav";
import { closePalette, openPalette, usePalette } from "./palette";

const ITEM =
  "flex cursor-pointer select-none items-center gap-3 rounded-lg px-3 py-2 text-sm text-t1 data-[selected=true]:bg-glass-2 " +
  "data-[disabled=true]:cursor-not-allowed data-[disabled=true]:opacity-50";
/** Said when ⌘K is asked for a ping or a subscription refresh while one already runs (here or on a screen). */
const PING_BUSY = "A ping is still running — try again when it finishes";
const REFRESH_BUSY = "A subscription refresh is still running — try again when it finishes";

const GROUP =
  "px-1 pb-2 [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:text-[10px] " +
  "[&_[cmdk-group-heading]]:font-semibold [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-[.1em] [&_[cmdk-group-heading]]:text-t3";

export function CommandPalette() {
  const { open, mode } = usePalette();
  const navigate = useNavigate();
  // Fetched when opened; this component never polls.
  const nodes = useQuery({ ...queries.nodes(), enabled: open });
  const queryClient = useQueryClient();
  const rollbackAvailable = useQuery(queries.status()).data?.rollback_available === true;   // the shell polls it
  const apply = useApiWrite("apply");
  const connectBest = useApiWrite("connectBest");
  const refreshAllSubs = useApiWrite("refreshAllSubs");
  const rollback = useApiWrite("rollback");
  const probeTcp = useApiWrite("probeTcp");
  const probeHttp = useApiWrite("probeHttp");
  const checkUpdates = useApiWrite("checkUpdates");
  // Connect, connect best and roll back run as connection writes: offered only while no other one is running.
  const connectionBusy = useConnectionBusy();
  const connection = useMutation({ mutationKey: CONNECTION_WRITE, mutationFn: (action: () => Promise<string>) => action() });
  // The same keys as Servers' pings and the Subscriptions screen's refreshes, so neither overlaps the other's.
  const ping = useMutation({ mutationKey: PING_KEY, mutationFn: (kind: "tcp" | "http") => (kind === "tcp" ? probeTcp() : probeHttp()) });
  const refresh = useMutation({ mutationKey: SUBS_REFRESH_KEY, mutationFn: () => refreshAllSubs() });
  const pingBusy = useIsMutating({ mutationKey: PING_KEY }) > 0;
  const refreshBusy = useIsMutating({ mutationKey: SUBS_REFRESH_KEY }) > 0;

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() === "k" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        openPalette("all");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const go = (to: AppPath) => {
    closePalette();
    void navigate({ to });
  };

  const openNode = (id: number) => {
    closePalette();
    void navigate({ to: "/nodes/$nodeId", params: { nodeId: String(id) } });
  };

  async function run(action: () => Promise<string>, failure: string, { connectionWrite = false } = {}) {
    closePalette();
    try {
      notifyOk(await (connectionWrite ? connection.mutateAsync(action) : action()));
    } catch (error) {
      notifyError(error, failure);
    }
  }

  function pingAll(kind: "tcp" | "http") {
    const label = kind === "tcp" ? "TCP" : "HTTP";
    if (queryClient.isMutating({ mutationKey: PING_KEY }) > 0) {
      closePalette();
      notifyError(null, PING_BUSY);
      return;
    }
    void run(async () => { await ping.mutateAsync(kind); return `${label} ping finished`; }, `${label} ping failed`);
  }

  function refreshAll() {
    if (queryClient.isMutating({ mutationKey: SUBS_REFRESH_KEY }) > 0) {
      closePalette();
      notifyError(null, REFRESH_BUSY);
      return;
    }
    void run(async () => {
      const result = await refresh.mutateAsync();
      return `${result.succeeded}/${result.attempted} subscriptions refreshed`;
    }, "refresh failed");
  }

  async function rollBack() {
    const targetId = queryClient.getQueryData<Status>(keys.status)?.prev_active_node_id ?? null;
    closePalette();
    if (!(await confirm("Roll back the live config to the previously applied node?", { confirmLabel: "Roll back" }))) return;
    if (isConnectionBusy(queryClient)) {
      notifyError(null, CONNECTION_BUSY);
      return;
    }
    if (!rollbackStillValid(queryClient.getQueryData<Status>(keys.status), targetId)) {
      notifyError(null, ROLLBACK_TARGET_CHANGED);
      return;
    }
    await run(async () => ((await rollback()).ok ? "Rolled back to the previous node" : "Nothing to roll back"), "roll back failed", { connectionWrite: true });
  }

  return (
    <Command.Dialog
      open={open}
      onOpenChange={(next) => (next ? openPalette(mode) : closePalette())}
      label="Command menu"
      overlayClassName="fixed inset-0 z-50 bg-[rgba(4,3,10,.6)] backdrop-blur-sm"
      contentClassName="glass fixed left-1/2 top-[12dvh] z-50 w-[min(36rem,calc(100vw-2rem))] -translate-x-1/2 overflow-hidden p-0"
    >
      <Command.Input
        placeholder={mode === "nodes" ? "Switch to node…" : "Search nodes, screens and actions…"}
        className="h-12 w-full border-b border-line bg-transparent px-4 text-sm text-t1 outline-none placeholder:text-t3"
      />
      <Command.List className="max-h-[60dvh] overflow-y-auto p-2">
        <Command.Empty className="p-4 text-sm text-t3">No matches.</Command.Empty>
        <Command.Group heading={mode === "nodes" ? "Connect to" : "Nodes"} className={GROUP}>
          {nodes.isPending ? <Command.Loading>Loading nodes…</Command.Loading> : null}
          {nodes.data?.map((n) => (
            <Command.Item
              key={n.id}
              value={`node ${n.id} ${n.name} ${n.address} ${n.note}`}
              disabled={mode === "nodes" && connectionBusy}
              onSelect={() => (mode === "nodes"
                ? void run(async () => { await apply(n.id); return `Connected to ${n.name}`; }, "connect failed", { connectionWrite: true })
                : openNode(n.id))}
              className={ITEM}
            >
              <span className="truncate">{n.name}</span>
              <span className="ml-auto font-mono text-[11px] text-t3">{n.address}</span>
            </Command.Item>
          ))}
        </Command.Group>
        {mode === "all" ? (
          <>
            <Command.Group heading="Go to" className={GROUP}>
              {SECTIONS.flatMap((section) => section.tabs.map((tab) => (
                <Command.Item key={tab.to} value={`go ${section.label} ${tab.label}`} onSelect={() => go(tab.to)} className={ITEM}>
                  {tab.label}
                  <span className="ml-auto text-[11px] text-t3">{section.label}</span>
                </Command.Item>
              )))}
            </Command.Group>
            <Command.Group heading="Actions" className={GROUP}>
              <Command.Item
                value="action connect best healthiest"
                disabled={connectionBusy}
                onSelect={() => void run(async () => `Connected to node ${(await connectBest(null)).node_id}`, "connect best failed", { connectionWrite: true })}
                className={ITEM}
              >
                Connect best
              </Command.Item>
              <Command.Item value="action refresh all subscriptions" disabled={refreshBusy} onSelect={refreshAll} className={ITEM}>
                Refresh all subscriptions
              </Command.Item>
              {rollbackAvailable ? (
                <Command.Item value="action roll back previous node" disabled={connectionBusy} onSelect={() => void rollBack()} className={ITEM}>
                  Roll back to previous node
                </Command.Item>
              ) : null}
              <Command.Item value="action tcp ping all nodes" disabled={pingBusy} onSelect={() => pingAll("tcp")} className={ITEM}>
                TCP ping all nodes
              </Command.Item>
              <Command.Item value="action http ping all nodes" disabled={pingBusy} onSelect={() => pingAll("http")} className={ITEM}>
                HTTP ping all nodes
              </Command.Item>
              {/* B2: the only other way to run it is System › Panel, which is four keystrokes further away. */}
              <Command.Item
                value="action check for updates new release version"
                onSelect={() => void run(async () => updateOutcome(await checkUpdates()), "the release check failed")}
                className={ITEM}
              >
                Check for updates
              </Command.Item>
            </Command.Group>
          </>
        ) : null}
      </Command.List>
    </Command.Dialog>
  );
}
