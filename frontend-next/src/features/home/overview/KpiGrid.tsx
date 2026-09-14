import { useTraffic } from "../../../api/traffic";
import { Chip } from "../../../components/data/Chip";
import { Kpi } from "../../../components/data/Kpi";
import { cn } from "../../../lib/cn";
import { fmtBytes, fmtRate, splitUnit } from "../../../lib/format";
import { recentValues, sessionTotals, type SessionTotals } from "../derive";

const SPARK_WINDOW_MS = 5 * 60_000;
const SOURCE: Record<SessionTotals["source"], string> = {
  session: "since the last connect",
  lifetime: "data used",
  totals: "since xray started",
};
const NONE = { value: "—", unit: "" };

/** O5: live download / upload with a five-minute trend, and the session's totals. "—" while stats are off; dimmed while not live. */
export function KpiGrid({ className }: { className?: string }) {
  const traffic = useTraffic();
  const frame = traffic.disabled ? null : traffic.live;
  const proxy = frame ? (frame.outbounds.proxy ?? { up_bps: 0, down_bps: 0 }) : null;
  const totals = sessionTotals(frame);
  const down = proxy ? splitUnit(fmtRate(proxy.down_bps)) : NONE;
  const up = proxy ? splitUnit(fmtRate(proxy.up_bps)) : NONE;
  const totalDown = totals ? splitUnit(fmtBytes(totals.down)) : NONE;
  const totalUp = totals ? splitUnit(fmtBytes(totals.up)) : NONE;
  const trend = (key: "up" | "down") => (traffic.disabled ? undefined : recentValues(traffic.samples, SPARK_WINDOW_MS, key));
  const stale = !traffic.disabled && !traffic.fresh;
  let live = <Chip tone="ok">live</Chip>;
  if (traffic.disabled) live = <Chip tone="neutral">stats off</Chip>;
  else if (stale) live = <Chip tone="neutral">connecting…</Chip>;

  return (
    <div className={cn("grid grid-cols-2 gap-3", className)}>
      <Kpi label="↓ Download" value={down.value} unit={down.unit} sub="last 5 min" spark={trend("down")} dim={stale} />
      <Kpi label="↑ Upload" value={up.value} unit={up.unit} sub="last 5 min" spark={trend("up")} sparkSeries="up" dim={stale} />
      <Kpi label="↓ Session total" value={totalDown.value} unit={totalDown.unit} sub={totals ? SOURCE[totals.source] : "traffic stats feed"} aside={live} dim={stale} />
      <Kpi label="↑ Session total" value={totalUp.value} unit={totalUp.unit} sub={totals ? SOURCE[totals.source] : "traffic stats feed"} dim={stale} />
    </div>
  );
}
