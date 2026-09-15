import type { Node, NodeHealth } from "../../api/client";
import { CardHeader } from "../../components/data/CardHeader";
import { KeyValueRows, type KeyValueRow } from "../../components/data/KeyValueRows";
import { Sparkline } from "../../components/data/Sparkline";
import { GlassCard } from "../../components/ui/GlassCard";
import { ActiveRealPill, CheckedAgo, Egress, ProbePill } from "./probe";

/** N5 / N7 for one node: TCP, HTTP and real with their age, the failure counter of the active node, egress, trend. */
export function NodeHealthCard({ node, health, active }: { node: Node; health: NodeHealth | undefined; active: boolean }) {
  const history = health?.lat_history ?? [];
  const rows: KeyValueRow[] = [
    { key: "TCP", value: <ProbePill ok={health?.last_tcp_ok} ms={health?.last_tcp_ms} /> },
    { key: "HTTP", value: <ProbePill ok={health?.last_http_ok} ms={health?.last_http_ms} /> },
    { key: "Real", value: active ? <ActiveRealPill nodeId={node.id} health={health} /> : <ProbePill ok={health?.last_real_ok} ms={health?.last_real_ms} /> },
  ];
  if (active) rows.push({ key: "Failures", value: String(health?.fail_count ?? 0), sub: "auto-failover counter" });
  rows.push({ key: "Egress", value: <Egress health={health} className="text-[12px]" /> });
  return (
    <GlassCard aria-label="Health">
      <CardHeader
        title="Health"
        level={3}
        detail={health?.checked_at ? <>checked <CheckedAgo at={health.checked_at} /></> : "not probed"}
      />
      {!health?.checked_at ? <p className="mb-2 text-xs text-t3">Not probed yet — Test runs TCP, HTTP and a real request through this node.</p> : null}
      <KeyValueRows rows={rows} />
      {history.length > 1 ? (
        <div className="mt-2 rounded-xl border border-line bg-glass px-3 py-2">
          <p className="text-[10.5px] font-semibold tracking-wide text-t3">Latency trend · last {history.length} samples</p>
          <Sparkline values={history} className="mt-1" />
        </div>
      ) : null}
    </GlassCard>
  );
}
