import { useIsMutating, useMutation, useMutationState, useQueryClient } from "@tanstack/react-query";
import { Activity, Gauge, Radar, Zap } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, errText, type Node, type NodeHealth } from "../../api/client";
import { CONNECTION_WRITE, PING_KEY, TEST_ALL_KEY, invalidate, useApiWrite, useConnectionBusy } from "../../api/invalidation";
import { keys } from "../../api/keys";
import { Button } from "../../components/ui/Button";
import { notifyError, notifyOk } from "../../components/ui/Toaster";
import { nodeLabel } from "../../lib/nodeHealth";
import { Elapsed } from "./Elapsed";
import { bestScope, inScope, mergeHealth, probeScope, type GroupKey } from "./list";

export const NO_CONNECTABLE = "No connectable node in this group";

export interface GroupActionsProps {
  group: GroupKey;
  /** The group's rows after search and sort — all of them, not only the rendered ones. */
  shown: readonly Node[];
  /** Every node, to tell a group with no nodes from a search that shows none of them. */
  nodes: readonly Node[] | undefined;
  /** The gateway is unreachable: no connection write can start. */
  offline: boolean;
}

/**
 * N9: probe every row in turn, one real request at a time. A node that fails does not stop the run; leaving the
 * screen stops it before the next node. The run itself lives in a mutation keyed by TEST_ALL_KEY, so its "still
 * running" state is the query cache's, not this component's — a remount (leaving and returning to /nodes, or a
 * second GroupActions mounted at once) reads the same lock instead of starting an overlapping sweep.
 */
function useTestAll(shown: readonly Node[]) {
  const queryClient = useQueryClient();
  const [remaining, setRemaining] = useState(0);
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  const testAll = useMutation({
    mutationKey: TEST_ALL_KEY,
    mutationFn: async () => {
      const queue = [...shown];
      setRemaining(queue.length);
      let failed = 0;
      let lastError: unknown = null;
      // Each result lands in the list as it arrives; the health list is re-read once, when the run ends or stops,
      // instead of after every probe.
      try {
        for (const node of queue) {
          try {
            const result = await api.probeNode(node.id);
            queryClient.setQueryData<NodeHealth[]>(keys.nodeHealth, (old) => mergeHealth(old, result));
          } catch (error) {
            // one node failing its test is a result too; carry on with the next, but remember it happened
            failed += 1;
            lastError = error;
          }
          if (!mounted.current) return { total: queue.length, failed, lastError };
          setRemaining((count) => count - 1);
        }
        return { total: queue.length, failed, lastError };
      } finally {
        void invalidate(queryClient, "probeNode");
      }
    },
    onSuccess: ({ total, failed, lastError }) => {
      if (failed > 0) notifyError(null, `Test all: ${failed} of ${total} probes failed — ${errText(lastError, "probe failed")}`);
    },
  });
  // `remaining` answers within the same render as a click this instance made; `running` (the shared cache) is
  // what keeps a remounted or second instance's buttons locked while somebody else's sweep is still going.
  const running = useIsMutating({ mutationKey: TEST_ALL_KEY }) > 0;
  const run = useCallback(() => {
    if (queryClient.isMutating({ mutationKey: TEST_ALL_KEY }) > 0) return;   // another instance's sweep is still going
    testAll.mutate();
  }, [queryClient, testAll]);
  return { remaining, testing: running || remaining > 0, run };
}

/** N8–N10: TCP ping and HTTP ping of the group, Test all (real), Connect best. */
export function GroupActions({ group, shown, nodes, offline }: GroupActionsProps) {
  const queryClient = useQueryClient();
  const probeTcp = useApiWrite("probeTcp");
  const probeHttp = useApiWrite("probeHttp");
  const connectBestWrite = useApiWrite("connectBest");
  const connectionBusy = useConnectionBusy();
  const testAll = useTestAll(shown);

  // N8: both pings share one mutation, so neither starts while the other runs (a sweep can take minutes).
  const ping = useMutation({
    mutationKey: PING_KEY,
    mutationFn: (kind: "tcp" | "http") => (kind === "tcp" ? probeTcp(probeScope(group)) : probeHttp(probeScope(group))),
    onSuccess: (rows) => queryClient.setQueryData<NodeHealth[]>(keys.nodeHealth, rows),
    onError: (error, kind) => notifyError(error, `${kind === "tcp" ? "TCP" : "HTTP"} ping failed`),
  });
  const best = useMutation({
    mutationKey: CONNECTION_WRITE,
    mutationFn: () => connectBestWrite(bestScope(group)),
    onSuccess: (result) => notifyOk(`Connected to ${nodeLabel(nodes, result.node_id)}`),
    onError: (error) => (error instanceof ApiError && error.status === 404 ? notifyError(null, NO_CONNECTABLE) : notifyError(error, "connect best failed")),
  });

  // The lock (and which kind, and since when) reads the shared mutation cache, not this `ping` instance's own
  // isPending — that state is this component's, and is lost if it remounts mid-sweep (leaving and returning to
  // /nodes; a second GroupActions under the node sheet). The cache entry survives either.
  const pingBusy = useIsMutating({ mutationKey: PING_KEY }) > 0;
  const runningPing = useMutationState({
    filters: { mutationKey: PING_KEY, status: "pending" },
    select: (mutation) => ({ kind: mutation.state.variables as "tcp" | "http", submittedAt: mutation.state.submittedAt }),
  })[0];
  const pinging = pingBusy ? (runningPing?.kind ?? null) : null;
  // Only read when pinging !== null, where a matching cache entry is guaranteed — the assertion just tells
  // TypeScript what that control-flow link already establishes; it reads no impure clock as a fallback.
  const pingingSince = runningPing?.submittedAt;
  const testing = testAll.testing;
  // Pings and Connect best act on the whole group, whatever the search shows (§13.5): they wait only for a group with
  // no nodes at all. Test all goes through the shown rows, so it needs some.
  const groupEmpty = !nodes?.some((node) => inScope(node, group));

  return (
    <>
      <Button size="sm" aria-label={pinging === "tcp" ? undefined : "TCP ping"} disabled={pinging !== null || groupEmpty} onClick={() => ping.mutate("tcp")}>
        <Radar size={14} aria-hidden className="hidden md:inline" />
        {pinging === "tcp" ? <>Pinging… <Elapsed since={pingingSince!} /></> : <>TCP<span className="hidden md:inline"> ping</span></>}
      </Button>
      <Button size="sm" aria-label={pinging === "http" ? undefined : "HTTP ping"} disabled={pinging !== null || groupEmpty} onClick={() => ping.mutate("http")}>
        <Gauge size={14} aria-hidden className="hidden md:inline" />
        {pinging === "http" ? <>Pinging… <Elapsed since={pingingSince!} /></> : <>HTTP<span className="hidden md:inline"> ping</span></>}
      </Button>
      <Button size="sm" aria-label={testing ? undefined : "Test all (real)"} disabled={testing || shown.length === 0} onClick={() => testAll.run()}>
        <Activity size={14} aria-hidden className="hidden md:inline" />
        {testing ? (testAll.remaining > 0 ? `Testing ${testAll.remaining}…` : "Testing…") : <>Test all<span className="hidden md:inline"> (real)</span></>}
      </Button>
      <Button
        size="sm"
        variant="primary"
        aria-label={best.isPending ? undefined : "Connect best"}
        title={offline ? "Gateway unreachable" : "Connect to the healthiest node in this group"}
        disabled={offline || connectionBusy || groupEmpty}
        onClick={() => best.mutate()}
      >
        <Zap size={14} aria-hidden className="hidden md:inline" />
        {best.isPending ? "Connecting…" : <><span className="md:hidden">Best</span><span className="hidden md:inline">Connect best</span></>}
      </Button>
    </>
  );
}
