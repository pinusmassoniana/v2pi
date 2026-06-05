<script lang="ts">
  // NF1: connection topology as a fixed-geometry SVG that matches the app style — subtle surface
  // circles with lucide icons (no emoji except the country flag), small status dots for health,
  // thin lines. Baseline: Devices —Lan— Gateway ——— Internet, the exit Node elevated above centre
  // reached by two tunnel legs (Gateway ↗ Node ↘ Internet). The straight "untunneled" bypass is
  // ALWAYS drawn — faint when unused, amber when traffic leaks around the tunnel (NF3).
  import { fmtRate } from "./format";
  import { flagEmoji } from "./flag";

  type Tone = "ok" | "bad" | "idle";
  let {
    running = false, segmentUp = null, clients = null, clientNames = [],
    nodeName = null, realOk = null, latencyMs = null,
    egressIp = null, egressCc = null,
    uplink = null, uplink6 = null, ipv6Enabled = false,
    proxyDown = 0, proxyUp = 0, directDown = 0, directUp = 0,
  }: {
    running?: boolean; segmentUp?: boolean | null; clients?: number | null; clientNames?: string[];
    nodeName?: string | null; realOk?: boolean | null; latencyMs?: number | null;
    egressIp?: string | null; egressCc?: string | null;
    uplink?: boolean | null; uplink6?: boolean | null; ipv6Enabled?: boolean;
    proxyDown?: number; proxyUp?: number; directDown?: number; directUp?: number;
  } = $props();

  // inner lucide paths (stroked via the parent <g>); no <svg> wrapper so they embed in the diagram
  const P = {
    devices: '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
    server: '<rect x="3" y="4" width="18" height="6" rx="1"/><rect x="3" y="14" width="18" height="6" rx="1"/><path d="M7 7h.01M7 17h.01"/>',
    exit: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5M21 12H9"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a15 15 0 0 1 0 18a15 15 0 0 1 0-18z"/>',
  };
  const tone = (v: boolean | null | undefined): Tone => (v == null ? "idle" : v ? "ok" : "bad");
  const trunc = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
  let leaking = $derived(directDown + directUp > 0);
  let tunCls = $derived(!running ? "off" : realOk === false ? "bad" : realOk ? "ok" : "off");
  let names = $derived(clientNames.filter(Boolean));
  let devSub = $derived(names.length ? trunc(names.slice(0, 2).join(", ") + (names.length > 2 ? ` +${names.length - 2}` : ""), 22) : "client segment");
  let tunnelRate = $derived(
    !running ? "off" : realOk === false ? "no traffic" :
    `↓ ${fmtRate(proxyDown)} · ↑ ${fmtRate(proxyUp)}` + (realOk && latencyMs != null ? ` · ${latencyMs}ms` : ""));
</script>

<div class="card flow-card">
  <span class="eyebrow">Connection</span>
  <div class="diagram-scroll">
  <svg viewBox="0 0 760 208" class="diagram" role="img"
       aria-label="connection path: devices, gateway, tunnel node, internet">
    <!-- lines -->
    <line class="ln {tone(segmentUp)}" x1="86" y1="140" x2="220" y2="140" />
    <text class="lbl" x="153" y="131">Lan</text>

    <line class="ln {leaking ? 'leak' : 'faint'}" x1="280" y1="140" x2="674" y2="140" />
    <text class="lbl {leaking ? 'leak' : 'faint'}" x="477" y="157">untunneled{#if leaking} ↓ {fmtRate(directDown)} · ↑ {fmtRate(directUp)}{/if}</text>

    <line class="tun {tunCls}" x1="272" y1="123" x2="446" y2="68" />
    <line class="tun {tunCls}" x1="494" y1="68" x2="678" y2="123" />
    <text class="lbl" x="470" y="16">Tunnel</text>
    <text class="rate {tunCls === 'bad' ? 'bad' : ''}" x="470" y="31">{tunnelRate}</text>

    <!-- Devices -->
    <circle class="cir" cx="56" cy="140" r="26" />
    <g class="ic" transform="translate(47,131) scale(0.75)">{@html P.devices}</g>
    <circle class="sdot {tone(segmentUp)}" cx="74" cy="122" r="4.5" />
    <text class="ctitle" x="56" y="184">{clients ?? "—"} {clients === 1 ? "device" : "devices"}</text>
    <text class="csub" x="56" y="198">{devSub}</text>

    <!-- Gateway -->
    <circle class="cir" cx="250" cy="140" r="26" />
    <g class="ic" transform="translate(241,131) scale(0.75)">{@html P.server}</g>
    <circle class="sdot {tone(segmentUp)}" cx="268" cy="122" r="4.5" />
    <text class="ctitle" x="250" y="184">Pi gateway</text>
    <text class="csub" x="250" y="198">{segmentUp == null ? "segment —" : segmentUp ? "segment up" : "segment down"}</text>

    <!-- Node (elevated) -->
    <circle class="cir" cx="470" cy="54" r="28" />
    {#if egressCc}<text class="flag" x="470" y="62">{flagEmoji(egressCc)}</text>
    {:else}<g class="ic" transform="translate(461,45) scale(0.75)">{@html P.exit}</g>{/if}
    <circle class="sdot {running ? tone(realOk) : 'idle'}" cx="490" cy="35" r="4.5" />
    <text class="ctitle" x="470" y="100">{trunc(nodeName ?? "—", 24)}</text>
    <text class="csub mono" x="470" y="114">{egressIp ? trunc(egressIp, 22) : "no exit"}</text>

    <!-- Internet -->
    <circle class="cir" cx="704" cy="140" r="26" />
    <g class="ic" transform="translate(695,131) scale(0.75)">{@html P.globe}</g>
    <circle class="sdot {tone(uplink)}" cx="722" cy="122" r="4.5" />
    <text class="ctitle" x="704" y="184">Internet</text>
    <text class="csub" x="704" y="198">{uplink == null ? "—" : uplink ? "reachable" : "down"}{#if ipv6Enabled} · v6 {uplink6 == null ? "?" : uplink6 ? "ok" : "✕"}{/if}</text>
  </svg>
  </div>
</div>

<style>
  .flow-card { display: grid; gap: 0.5rem; }
  /* fill the card width (start at the left edge, not centered) — scroll on phones rather than
     shrinking the text to nothing. */
  .diagram-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .diagram { width: 100%; min-width: 480px; height: auto; display: block; }

  .ln { stroke-width: 1.75; stroke-linecap: round; fill: none; }
  .ln.ok { stroke: var(--success); }
  .ln.bad { stroke: var(--danger); }
  .ln.idle { stroke: var(--border-strong); }
  .ln.faint { stroke: var(--border); }
  .ln.leak { stroke: var(--warn); }
  .tun { stroke-width: 2; stroke-linecap: round; fill: none; }
  .tun.ok { stroke: var(--accent); }
  .tun.bad { stroke: var(--danger); }
  .tun.off { stroke: var(--border); stroke-dasharray: 5 6; }

  .cir { fill: var(--surface-2); stroke: var(--border); stroke-width: 1.5; }
  .ic { fill: none; stroke: var(--muted); stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
  .sdot { stroke: var(--surface); stroke-width: 2.5; }
  .sdot.ok { fill: var(--success); }
  .sdot.bad { fill: var(--danger); }
  .sdot.idle { fill: var(--muted); }
  .flag { font-size: 23px; text-anchor: middle; }

  .ctitle { fill: var(--text); font-size: 12.5px; font-weight: 600; text-anchor: middle; }
  .csub { fill: var(--muted); font-size: 10.5px; text-anchor: middle; }
  .csub.mono { font-family: var(--mono); }
  .lbl { fill: var(--faint); font-size: 10.5px; text-anchor: middle; letter-spacing: 0.03em; }
  .lbl.leak { fill: var(--warn); font-weight: 600; }
  .rate { fill: var(--muted); font-size: 10.5px; text-anchor: middle; font-family: var(--mono); }
  .rate.bad { fill: var(--danger); }
</style>
