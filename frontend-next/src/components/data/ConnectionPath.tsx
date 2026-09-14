import { cn } from "../../lib/cn";
import { fmtRate } from "../../lib/format";
import { Chip } from "./Chip";
import { useSvgId } from "./svgId";
import type { PathLeg, Tone } from "./types";

export interface ConnectionPathProps {
  clients: number | null;
  poolSize: number | null;
  gatewayIp: string | null;
  gatewayIface: string | null;
  nodeName: string | null;
  nodeFlag: string;
  /** Fresh, passing latency of the tunnel; null otherwise. */
  latencyMs: number | null;
  egressIp: string | null;
  egressIp6: string | null;
  uplink: boolean | null;
  uplink6: boolean | null;
  /** v6 uplink is shown only when IPv6 goes through the tunnel. */
  ipv6Enabled: boolean;
  leg: PathLeg;
  /** Direct (untunneled) throughput, bits per second. Any amount turns the bypass line amber. */
  bypassBps: number;
  killSwitch: { label: string; tone: Tone };
  /** The live values (latency, egress, tunnel leg, bypass) come from a frame that stopped updating: dim them. */
  dim?: boolean;
}

const LEG_LABEL: Record<PathLeg, string> = { ok: "OK", bad: "DOWN", off: "OFF" };
const LEG_TONE: Record<PathLeg, Tone> = { ok: "ok", bad: "bad", off: "neutral" };

const TUNNEL_LEG = "M190,66 C264.7,66 281.3,30 356,30 S468,66 516,66";
const BYPASS = "M190,75 C196,116 212,116 234,116 L472,116 C494,116 510,116 516,75";

function Uplink({ label, up }: { label: string; up: boolean | null }) {
  return (
    <span>
      {label}{" "}
      <span className={up === true ? "text-ok" : up === false ? "text-bad" : "text-t3"}>{up === true ? "✓" : up === false ? "✗" : "?"}</span>
    </span>
  );
}

/** Devices → gateway → node → internet, with the tunnel leg's health and the bypass around it. */
export function ConnectionPath(props: ConnectionPathProps) {
  const { clients, poolSize, gatewayIp, gatewayIface, nodeName, nodeFlag, latencyMs, egressIp, egressIp6, uplink, uplink6, ipv6Enabled, leg, bypassBps, killSwitch, dim = false } = props;
  const liveClass = cn("transition-opacity duration-200", dim && "opacity-50");
  const id = useSvgId("path");
  const leaking = bypassBps > 0;
  const nodeLabel = nodeName ? `${nodeFlag ? `${nodeFlag} ` : ""}${nodeName}` : "No node";
  const legStroke = leg === "ok" ? `url(#${id}-brand)` : leg === "bad" ? "var(--bad)" : "var(--t3)";

  return (
    <div>
      <svg
        role="img"
        aria-label={`Connection path: devices, gateway, ${nodeName ?? "no node"}, internet. Tunnel leg ${LEG_LABEL[leg]}; bypass ${leaking ? fmtRate(bypassBps) : "idle"}.`}
        viewBox="0 0 560 150"
        className="block h-auto w-full overflow-visible"
      >
        <defs>
          <linearGradient id={`${id}-brand`} gradientUnits="userSpaceOnUse" x1="40" y1="0" x2="516" y2="0">
            <stop offset="0" style={{ stopColor: "var(--g1)" }} />
            <stop offset="0.5" style={{ stopColor: "var(--g2)" }} />
            <stop offset="1" style={{ stopColor: "var(--g3)" }} />
          </linearGradient>
          <filter id={`${id}-glow`} x="-20%" y="-50%" width="140%" height="200%">
            <feGaussianBlur stdDeviation="3" result="blur" />
            <feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge>
          </filter>
        </defs>
        <path d="M40,66 L190,66" fill="none" strokeWidth={3} style={{ stroke: `url(#${id}-brand)` }} filter={`url(#${id}-glow)`} />
        <path
          data-leg={leg}
          d={TUNNEL_LEG}
          fill="none"
          strokeWidth={3}
          strokeDasharray={leg === "off" ? "6 6" : undefined}
          filter={leg === "ok" ? `url(#${id}-glow)` : undefined}
          style={{ stroke: legStroke }}
        />
        <path
          data-bypass={leaking ? "leaking" : "idle"}
          d={BYPASS}
          fill="none"
          strokeWidth={2}
          strokeDasharray="5 5"
          style={{ stroke: leaking ? "var(--warn)" : "var(--line)" }}
        />
        <circle cx="40" cy="66" r="8" style={{ fill: "var(--g1)" }} />
        <circle cx="190" cy="66" r="9" style={{ fill: "var(--g2)" }} />
        <circle cx="356" cy="30" r="10" style={{ fill: leg === "bad" ? "var(--bad)" : leg === "off" ? "var(--t3)" : "var(--g2)" }} />
        <circle cx="516" cy="66" r="8" style={{ fill: "var(--g3)" }} />
        <g fontSize="12" fontWeight="650" textAnchor="middle" style={{ fill: "var(--t1)" }}>
          <text x="40" y="50">Devices</text>
          <text x="190" y="50">Gateway</text>
          <text x="356" y="13">{nodeLabel}</text>
          <text x="516" y="50">Internet</text>
        </g>
        {leaking ? (
          <text x="353" y="136" fontSize="11" fontWeight="600" textAnchor="middle" style={{ fill: "var(--warn)" }}>
            bypass · direct {fmtRate(bypassBps)}
          </text>
        ) : null}
      </svg>

      <div className="mt-1 grid grid-cols-2 gap-x-2.5 gap-y-1.5 text-[11px] leading-snug text-t2 md:grid-cols-[20.5fr_28.5fr_29fr_22fr] md:text-center">
        <div><b className="block text-[11.5px] font-semibold text-t1">Devices · {clients ?? "—"} clients</b>pool {poolSize ?? "—"}</div>
        <div>
          <b className="block text-[11.5px] font-semibold text-t1">Gateway</b>
          <span className="font-mono">{gatewayIp ?? "—"}{gatewayIface ? ` · ${gatewayIface}` : ""}</span>
        </div>
        <div data-live data-dim={dim || undefined} className={cn("min-w-0", liveClass)}>
          <b className="block text-[11.5px] font-semibold text-t1">Node{latencyMs !== null ? ` · ${latencyMs} ms` : ""} · egress</b>
          <span className="block truncate font-mono">{egressIp ?? "—"}</span>
          {egressIp6 ? <span className="block truncate font-mono">{egressIp6}</span> : null}
        </div>
        <div>
          <b className="block text-[11.5px] font-semibold text-t1">Internet · uplink</b>
          <Uplink label="v4" up={uplink} />
          {ipv6Enabled ? <>{" · "}<Uplink label="v6" up={uplink6} /></> : null}
        </div>
      </div>

      <div className="mt-2.5 flex flex-wrap gap-1.5">
        <Chip tone={LEG_TONE[leg]} data-dim={dim || undefined} className={liveClass}>tunnel leg <b>{LEG_LABEL[leg]}</b></Chip>
        <Chip tone={leaking ? "warn" : "neutral"} data-dim={dim || undefined} className={liveClass}>bypass <b>{leaking ? fmtRate(bypassBps) : "idle"}</b></Chip>
        <Chip tone={killSwitch.tone} className={cn(killSwitch.tone === "neutral" && "text-t2")}>kill-switch <b>{killSwitch.label}</b></Chip>
      </div>
    </div>
  );
}
