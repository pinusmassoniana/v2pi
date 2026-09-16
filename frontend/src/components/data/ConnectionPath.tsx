import { cn } from "../../lib/cn";
import { Pill } from "../ui/Pill";
import { ConnectedFor } from "./ConnectedFor";
import { PATH_GEOMETRY, lineBaseline, pillHeight, pillWidth, type PathLayout, type PillBox } from "./pathGeometry";
import { useSvgId } from "./svgId";
import type { NodeSlot, OutboundRates, PathLeg, Tone } from "./types";

export interface ConnectionPathProps {
  /** The drawing's accessible name (pathLabel). */
  label: string;
  clients: number | null;
  poolSize: number | null;
  gatewayIp: string | null;
  gatewayIface: string | null;
  /** The active node's name without its leading flag (splitLeadingFlag): "No node" when none is active. */
  nodeName: string;
  /** Drawn in the node marker, never beside the name: the egress flag, else the name's own; "" for none. */
  nodeFlag: string;
  /** The latency pill and how the tunnel legs draw (nodeHealthSlot). */
  node: NodeSlot;
  /** Epoch seconds the active node has been connected since; null shows "—". */
  connectedSince: number | null;
  egressIp: string | null;
  egressIp6: string | null;
  uplink: boolean | null;
  uplink6: boolean | null;
  /** v6 uplink is shown only when IPv6 goes through the tunnel. */
  ipv6Enabled: boolean;
  /** Live rates of the tunnel and of direct traffic (outboundRates); null without a frame. */
  rates: OutboundRates | null;
  /** The two rate pills' lines (rateLines of each rate). */
  tunnelLines: readonly string[];
  directLines: readonly string[];
  killSwitch: { label: "ARMED" | "OPEN" | "UNKNOWN"; tone: Tone };
  /** The live values come from a frame that stopped updating: dim them and stop the motion. */
  dim?: boolean;
}

type DrawingProps = Pick<ConnectionPathProps, "node" | "nodeFlag" | "rates" | "tunnelLines" | "directLines" | "killSwitch"> & {
  layout: PathLayout;
  dim: boolean;
  className: string;
};

const TONE_TEXT: Record<Tone, string> = { ok: "text-ok", warn: "text-warn", bad: "text-bad", neutral: "text-t2" };
/** A state colour washed into the ground, in either theme: the fill of a tinted marker or badge. */
const tint = (color: string) => `color-mix(in srgb, ${color} 12%, var(--bg))`;
/** Two spaces that SVG text does not collapse, between the direct pill's two directions on one line. */
const JOIN = "\u00a0\u00a0";

// Motion only while something moves; under reduced motion the flow stands still and the alarm ring shows, faded.
const FLOW = "motion-reduce:animate-none";
const ALARM = "origin-center [transform-box:fill-box] animate-[path-alarm_1.8s_ease-out_infinite] motion-reduce:animate-none motion-reduce:opacity-45";

// Stats under each point: a wide card sets a key beside its value, a narrow one on its own line above it. At the
// narrowest card (a phone's: from md the card takes the full row) a value that cannot fit wraps where it may, or truncates with a title.
const COLUMN = "flex min-w-0 flex-col items-center gap-px px-[3px] @max-md:px-px";
const TITLE = "max-w-full truncate text-[12.5px] font-semibold text-t1 @max-md:text-[10.5px]";
const BIG = "flex max-w-full flex-wrap items-baseline justify-center gap-x-[3px] text-[15px] font-bold leading-tight tracking-tight @max-md:text-[13px]";
const BIG_MONO = "max-w-full truncate font-mono text-[13px] font-semibold leading-tight @max-md:text-[11px] @max-3xs:text-[10px]";
const SMALL = "max-w-full truncate text-[11px] leading-snug text-t2 @max-md:text-[10px]";
const SMALL_WRAP = "max-w-full text-[11px] leading-snug text-t2 @max-md:text-[10px]";
const SMALL_MONO = "max-w-full truncate font-mono text-[11px] leading-snug text-t2 @max-md:text-[9.5px]";
const KEY = "text-[10px] leading-tight text-t3";
const INLINE_KEY = "@max-md:block @max-md:text-[10px] @max-md:leading-tight @max-md:text-t3";

type SegmentLook = "glow" | "faded" | "warn" | "bad" | "off";

/** One stretch of the line: a wide soft stroke under it for a glow (never a filter), dashes for a failing or unused leg. */
function Segment({ d, look, gradient, leg }: { d: string; look: SegmentLook; gradient: string; leg?: PathLeg }) {
  const stroke = look === "warn" ? "var(--warn)" : look === "bad" ? "var(--bad)" : look === "off" ? "var(--t3)" : `url(#${gradient})`;
  return (
    <>
      {look === "glow" || look === "warn" ? <path d={d} fill="none" strokeWidth={10} strokeLinecap="round" opacity={0.16} style={{ stroke }} /> : null}
      <path
        data-leg={leg}
        d={d}
        fill="none"
        strokeWidth={3}
        strokeLinecap="round"
        strokeDasharray={look === "bad" ? "8 6" : look === "off" ? "6 6" : undefined}
        opacity={look === "faded" ? 0.55 : undefined}
        style={{ stroke }}
      />
    </>
  );
}

function RateLine({ line }: { line: string }) {
  const parts = /^([↓↑]) (\S+) (\S+)$/.exec(line);
  if (!parts) return <tspan style={{ fill: "var(--t3)" }}>{line}</tspan>;
  return (
    <>
      <tspan style={{ fill: "var(--t3)" }}>{parts[1]} </tspan>
      <tspan style={{ fill: "var(--t1)" }}>{parts[2]}</tspan>
      <tspan style={{ fill: "var(--t3)" }}> {parts[3]}</tspan>
    </>
  );
}

/** A rate pill on the ground colour with a hairline, so its text keeps the token contrast in both themes. */
function RatePill({ kind, pill, lines, oneLine, dim }: { kind: "tunnel" | "direct"; pill: PillBox; lines: readonly string[]; oneLine: boolean; dim: boolean }) {
  const rows = oneLine ? [lines] : lines.map((line) => [line]);
  const width = pillWidth(rows.map((row) => row.join(JOIN)), pill);
  const height = pillHeight(pill, rows.length);
  return (
    <g data-pill={kind} data-dim={dim || undefined} className={cn("transition-opacity duration-200", dim && "opacity-50")}>
      <rect
        x={pill.cx - width / 2}
        y={pill.cy - height / 2}
        width={width}
        height={height}
        rx={pill.rx}
        strokeWidth={1}
        style={{ fill: "var(--bg)", stroke: "var(--line)" }}
      />
      {rows.map((row, index) => (
        <text key={index} x={pill.cx} y={lineBaseline(pill, index, rows.length)} textAnchor="middle" fontSize={pill.size} fontWeight={600} className="font-mono">
          {row.map((line, at) => (
            <tspan key={at}>
              {at > 0 ? JOIN : null}
              <RateLine line={line} />
            </tspan>
          ))}
        </text>
      ))}
    </g>
  );
}

function Dot({ cx, cy, ring, dot, color }: { cx: number; cy: number; ring: number; dot: number; color: string }) {
  return (
    <>
      <circle cx={cx} cy={cy} r={ring} fill="none" strokeOpacity={0.3} style={{ stroke: color }} />
      <circle cx={cx} cy={cy} r={dot} style={{ fill: color }} />
    </>
  );
}

const CLOSED_SHACKLE = "M-2.4,-1V-3a2.4,2.4 0 0 1 4.8,0V-1";
const OPEN_SHACKLE = "M-2.4,-2.8V-4.6a2.4,2.4 0 0 1 4.8,0";

/** The gateway marker is the kill-switch: a closed lock when ARMED, an open one with an alarm ring when OPEN, an outline with ? when UNKNOWN. */
function Lock({ cx, cy, r, state }: { cx: number; cy: number; r: number; state: ConnectionPathProps["killSwitch"]["label"] }) {
  const color = state === "ARMED" ? "var(--ok)" : state === "OPEN" ? "var(--bad)" : "var(--t3)";
  return (
    <g data-kill={state} transform={`translate(${cx},${cy})`}>
      {state === "OPEN" ? <circle data-alarm r={r + 1.5} fill="none" strokeWidth={1.5} className={ALARM} style={{ stroke: color }} /> : null}
      <circle r={r} strokeWidth={1} style={{ fill: state === "UNKNOWN" ? "var(--bg)" : tint(color), stroke: color }} />
      <g transform={`scale(${r / 10.5})`}>
        {state === "UNKNOWN" ? (
          <>
            <rect x={-3.8} y={-1} width={7.6} height={5.6} rx={1.2} fill="none" strokeWidth={1.1} style={{ stroke: color }} />
            <path d={CLOSED_SHACKLE} fill="none" strokeWidth={1.5} style={{ stroke: color }} />
            <text y={4.2} textAnchor="middle" fontSize={5} fontWeight={700} style={{ fill: color }}>?</text>
          </>
        ) : (
          <>
            <rect x={-3.8} y={-1} width={7.6} height={5.6} rx={1.2} style={{ fill: color }} />
            <path d={state === "OPEN" ? OPEN_SHACKLE : CLOSED_SHACKLE} fill="none" strokeWidth={1.5} style={{ stroke: color }} />
          </>
        )}
      </g>
    </g>
  );
}

/** The node: its flag (or a plain dot) in a ring of the leg's state — amber when slow, rose with an alarm ring when the check fails. */
function NodeMarker({ cx, cy, r, flagSize, node, flag }: { cx: number; cy: number; r: number; flagSize: number; node: NodeSlot; flag: string }) {
  const stroke = node.state === "ok" ? "var(--g2)" : node.state === "slow" ? "var(--warn)" : node.state === "bad" ? "var(--bad)" : "var(--t3)";
  const fill = node.state === "slow" || node.state === "bad" ? tint(stroke) : "var(--bg)";
  // No active node: an empty ring. Otherwise the flag, or a plain dot when no flag is known.
  const flagOrDot = flag
    ? <text y={0.35 * flagSize} textAnchor="middle" fontSize={flagSize} style={{ fill: "var(--t1)" }}>{flag}</text>
    : <circle r={r * 0.32} style={{ fill: stroke }} />;
  return (
    <g data-node={node.state} transform={`translate(${cx},${cy})`}>
      {node.state === "bad" ? <circle data-alarm r={r} fill="none" strokeWidth={1.5} className={ALARM} style={{ stroke }} /> : null}
      <circle r={r} strokeWidth={2} style={{ fill, stroke }} />
      {node.state === "none" ? null : flagOrDot}
    </g>
  );
}

function Badge({ cx, cy, r }: { cx: number; cy: number; r: number }) {
  return (
    <g data-badge transform={`translate(${cx},${cy})`}>
      <circle r={r} strokeWidth={1} style={{ fill: tint("var(--bad)"), stroke: "var(--bad)" }} />
      <path transform={`scale(${r / 9.5})`} d="M-3,-3L3,3M3,-3L-3,3" fill="none" strokeWidth={1.9} strokeLinecap="round" style={{ stroke: "var(--bad)" }} />
    </g>
  );
}

function PathDrawing({ layout, className, node, nodeFlag, rates, tunnelLines, directLines, killSwitch, dim }: DrawingProps) {
  const g = PATH_GEOMETRY[layout];
  // Each drawing owns its gradient: a gradient defined inside a display:none SVG does not paint in some browsers.
  const gradient = useSvgId(`path-${layout}`);
  const [devices, gateway, nodeX, internet] = g.xs;
  const y = g.y;
  const direct = rates === null ? "unknown" : rates.direct.down + rates.direct.up > 0 ? "flowing" : "idle";
  const tunnelFlows = !dim && rates !== null && rates.proxy.down + rates.proxy.up > 0;
  const directFlows = !dim && direct === "flowing";
  const healthy = node.leg === "ok";
  let legLook: SegmentLook = "off";
  if (node.state === "bad") legLook = "bad";
  else if (node.state === "slow") legLook = "warn";
  else if (healthy) legLook = "glow";

  return (
    <svg aria-hidden data-layout={layout} viewBox={`0 0 ${g.width} ${g.height}`} className={cn("h-auto w-full overflow-visible", className)}>
      <defs>
        <linearGradient id={gradient} gradientUnits="userSpaceOnUse" x1={devices} y1={0} x2={internet} y2={0}>
          <stop offset="0" style={{ stopColor: "var(--g1)" }} />
          <stop offset="0.5" style={{ stopColor: "var(--g2)" }} />
          <stop offset="1" style={{ stopColor: "var(--g3)" }} />
        </linearGradient>
      </defs>
      <text x={g.directLabel.x} y={g.directLabel.y} textAnchor="middle" fontSize={g.directLabel.size} fontWeight={600} style={{ fill: "var(--t3)" }}>
        direct · by routing rules
      </text>
      <path
        data-direct={direct}
        d={g.direct}
        fill="none"
        strokeLinecap="round"
        strokeWidth={direct === "flowing" ? 2 : 1.6}
        strokeDasharray={direct === "flowing" ? "5 5" : "4 5"}
        opacity={direct === "flowing" ? undefined : 0.5}
        style={{ stroke: "var(--t3)" }}
      />
      {directFlows ? (
        <path
          data-flow="direct"
          d={g.directBack}
          fill="none"
          strokeWidth={2.2}
          strokeLinecap="round"
          strokeDasharray="0 13"
          className={cn("animate-[path-flow_1.3s_linear_infinite]", FLOW)}
          style={{ stroke: "var(--t2)" }}
        />
      ) : null}
      <Segment d={`M${devices},${y} L${gateway},${y}`} look={healthy ? "glow" : "faded"} gradient={gradient} />
      <Segment d={`M${gateway},${y} L${nodeX},${y}`} look={legLook} gradient={gradient} leg={node.leg} />
      <Segment d={`M${nodeX},${y} L${internet},${y}`} look={healthy ? "glow" : "off"} gradient={gradient} />
      {tunnelFlows ? (
        <path
          data-flow="tunnel"
          d={`M${internet},${y} L${devices},${y}`}
          fill="none"
          strokeWidth={2.4}
          strokeLinecap="round"
          strokeDasharray="0 13"
          className={cn("animate-[path-flow_0.9s_linear_infinite]", FLOW)}
          style={{ stroke: "var(--bg)" }}
        />
      ) : null}
      {/* Before the pills: the OPEN alarm ring grows past the lock and passes under their opaque ground, not over their text. */}
      <Lock cx={gateway} cy={y} r={g.marker.lock} state={killSwitch.label} />
      <RatePill kind="direct" pill={g.directPill} lines={directLines} oneLine={g.directPill.lines === 1} dim={dim} />
      {node.state === "bad"
        ? <Badge cx={g.badge.cx} cy={g.badge.cy} r={g.badge.r} />
        : <RatePill kind="tunnel" pill={g.tunnelPill} lines={tunnelLines} oneLine={false} dim={dim} />}
      <Dot cx={devices} cy={y} ring={g.marker.ring} dot={g.marker.dot} color="var(--g1)" />
      <NodeMarker cx={nodeX} cy={y} r={g.marker.node} flagSize={g.marker.flag} node={node} flag={nodeFlag} />
      <Dot cx={internet} cy={y} ring={g.marker.ring} dot={g.marker.dot} color={healthy ? "var(--g3)" : "var(--t3)"} />
    </svg>
  );
}

function Uplink({ label, up }: { label: string; up: boolean | null }) {
  let glyph = <span aria-hidden className="font-extrabold text-t3">?</span>;
  if (up === true) glyph = <span aria-hidden className="font-extrabold text-ok">✓</span>;
  else if (up === false) glyph = <span aria-hidden className="font-extrabold text-bad">✗</span>;
  return (
    <span className="whitespace-nowrap">
      {label} {glyph}<span className="sr-only">{up === true ? "up" : up === false ? "down" : "unknown"}</span>
    </span>
  );
}

/** Devices → gateway → node → internet: each point's own stats under it, live rates on the lines, the kill-switch on the gateway. */
export function ConnectionPath(props: ConnectionPathProps) {
  const {
    label, clients, poolSize, gatewayIp, gatewayIface, nodeName, node, connectedSince, egressIp, egressIp6, uplink, uplink6, ipv6Enabled,
    killSwitch, dim = false,
  } = props;
  const live = cn("transition-opacity duration-200", dim && "opacity-50");

  return (
    <div className="@container">
      {/* One name for both drawings; not a live region, so the label changing every second is not announced. */}
      <div role="img" aria-label={label}>
        <PathDrawing {...props} layout="wide" dim={dim} className="block @max-md:hidden" />
        <PathDrawing {...props} layout="narrow" dim={dim} className="hidden @max-md:block" />
      </div>

      {/* grid-cols-4: column i is centred at (2i + 1) / 8 of the width, exactly under point i in both drawings. */}
      <div data-stats className="mt-1.5 grid grid-cols-4 text-center">
        <div className={COLUMN}>
          <div className={TITLE}>Devices</div>
          <div className={BIG}>
            {clients ?? "—"}<span className="text-[11px] font-semibold tracking-normal text-t3">clients</span>
          </div>
          <div className={SMALL}>pool {poolSize ?? "—"}</div>
        </div>
        <div className={COLUMN}>
          <div className={TITLE}>Gateway</div>
          <div className={BIG_MONO} title={gatewayIp ?? undefined}>{gatewayIp ?? "—"}</div>
          <div className={SMALL_MONO}>{gatewayIface ?? "—"}</div>
          <div className={SMALL}>
            <span className={INLINE_KEY}>kill-switch</span> <b className={cn("text-[10.5px] font-bold tracking-wide @max-3xs:text-[9px] @max-3xs:tracking-normal", TONE_TEXT[killSwitch.tone])}>{killSwitch.label}</b>
          </div>
        </div>
        <div data-live data-dim={dim || undefined} className={COLUMN}>
          <div className={TITLE} title={nodeName}>{nodeName}</div>
          <Pill tone={node.tone} className={cn(
              "max-w-full flex-wrap justify-center gap-x-1 gap-y-0 whitespace-normal rounded-[9px] px-2 py-px text-center",
              "@max-md:gap-x-[3px] @max-md:px-1.5 @max-md:text-[10.5px] @max-3xs:px-1",
              live,
            )}>
            {node.text}
            {node.note ? <small className="text-[9.5px] font-semibold">{node.note}</small> : null}
          </Pill>
          <div className={SMALL}>
            <span className={INLINE_KEY}>connected</span> <ConnectedFor since={connectedSince} />
          </div>
          <div className={cn("flex min-w-0 max-w-full flex-col items-center", live)}>
            <div className={KEY}>egress</div>
            <div className={SMALL_MONO} title={egressIp ?? undefined}>{egressIp ?? "—"}</div>
            {egressIp6 ? <div className={SMALL_MONO} title={egressIp6}>{egressIp6}</div> : null}
          </div>
        </div>
        <div className={COLUMN}>
          <div className={TITLE}>Internet</div>
          <div className={SMALL_WRAP}>
            <span className={INLINE_KEY}>uplink</span> <Uplink label="v4" up={uplink} />
            {ipv6Enabled ? <> <Uplink label="v6" up={uplink6} /></> : null}
          </div>
        </div>
      </div>
    </div>
  );
}
