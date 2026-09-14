import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { Command } from "cmdk";
import { useEffect } from "react";
import { api } from "../../api/client";
import { invalidate, type MutationName } from "../../api/invalidation";
import { queries } from "../../api/keys";
import { confirm } from "../../components/confirm";
import { notifyError, notifyOk } from "../../components/ui/Toaster";
import { SECTIONS, type AppPath } from "../nav";
import { closePalette, openPalette, usePalette } from "./palette";

const ITEM = "flex cursor-pointer select-none items-center gap-3 rounded-lg px-3 py-2 text-sm text-t1 data-[selected=true]:bg-glass-2";
const GROUP =
  "px-1 pb-2 [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:text-[10px] " +
  "[&_[cmdk-group-heading]]:font-semibold [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-[.1em] [&_[cmdk-group-heading]]:text-t3";

export function CommandPalette() {
  const { open, mode } = usePalette();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  // Fetched when opened; this component never polls.
  const nodes = useQuery({ ...queries.nodes(), enabled: open });

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

  async function run(name: MutationName, action: () => Promise<string>, failure: string) {
    closePalette();
    try {
      const message = await action();
      await invalidate(queryClient, name);
      notifyOk(message);
    } catch (error) {
      notifyError(error, failure);
    }
  }

  async function rollBack() {
    closePalette();
    if (!(await confirm("Roll back the live config to the previously applied node?", { confirmLabel: "Roll back" }))) return;
    await run("rollback", async () => ((await api.rollback()).ok ? "Rolled back to the previous node" : "Nothing to roll back"), "roll back failed");
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
              onSelect={() => (mode === "nodes"
                ? void run("apply", async () => { await api.apply(n.id); return `Connected to ${n.name}`; }, "connect failed")
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
                onSelect={() => void run("connectBest", async () => `Connected to node ${(await api.connectBest(null)).node_id}`, "connect best failed")}
                className={ITEM}
              >
                Connect best
              </Command.Item>
              <Command.Item
                value="action refresh all subscriptions"
                onSelect={() => void run("refreshAllSubs", async () => {
                  const result = await api.refreshAllSubs();
                  return `${result.succeeded}/${result.attempted} subscriptions refreshed`;
                }, "refresh failed")}
                className={ITEM}
              >
                Refresh all subscriptions
              </Command.Item>
              <Command.Item value="action roll back previous node" onSelect={() => void rollBack()} className={ITEM}>
                Roll back to previous node
              </Command.Item>
              <Command.Item
                value="action tcp ping all nodes"
                onSelect={() => void run("probeTcp", async () => { await api.probeTcp(); return "TCP ping finished"; }, "TCP ping failed")}
                className={ITEM}
              >
                TCP ping all nodes
              </Command.Item>
              <Command.Item
                value="action http ping all nodes"
                onSelect={() => void run("probeHttp", async () => { await api.probeHttp(); return "HTTP ping finished"; }, "HTTP ping failed")}
                className={ITEM}
              >
                HTTP ping all nodes
              </Command.Item>
            </Command.Group>
          </>
        ) : null}
      </Command.List>
    </Command.Dialog>
  );
}
