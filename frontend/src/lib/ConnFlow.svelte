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
    running = false, segmentUp = null, clients = null,
    segmentIp = null, segmentIface = null,
    nodeName = null, realOk = null, latencyMs = null,
    egressIp = null, egressIp6 = null, egressCc = null,
    uplink = null, uplink6 = null, ipv6Enabled = false,
    proxyDown = 0, proxyUp = 0, directDown = 0, directUp = 0,
  }: {
    running?: boolean; segmentUp?: boolean | null; clients?: number | null;
    segmentIp?: string | null; segmentIface?: string | null;
    nodeName?: string | null; realOk?: boolean | null; latencyMs?: number | null;
    egressIp?: string | null; egressIp6?: string | null; egressCc?: string | null;
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
  let gwSub = $derived(
    segmentIp ? segmentIp + (segmentIface ? " · " + segmentIface : "")
    : segmentUp == null ? "segment —" : segmentUp ? "segment up" : "segment down");
  let tunnelRate = $derived(
    !running ? "off" : realOk === false ? "no traffic" :
    `↓ ${fmtRate(proxyDown)} · ↑ ${fmtRate(proxyUp)}` + (realOk && latencyMs != null ? ` · ${latencyMs}ms` : ""));

  // SVG <text> neither wraps nor auto-shrinks. The node name + egress IP sit centred in a fixed slot
  // under the node (between the two tunnel legs ≈208u wide); a long server name / IPv6 address would
  // overflow. Measure the natural width and, only when it exceeds the slot, scale the glyphs to fit
  // (lengthAdjust=spacingAndGlyphs) so the full text stays visible instead of being chopped.
  const NODE_SLOT = 208;
  let nodeTitleEl = $state<SVGTextElement | null>(null);
  let nodeSubEl = $state<SVGTextElement | null>(null);
  let nodeSub6El = $state<SVGTextElement | null>(null);
  function fitText(el: SVGTextElement | null, max: number) {
    if (!el || typeof el.getComputedTextLength !== "function") return;
    el.removeAttribute("textLength");
    el.removeAttribute("lengthAdjust");
    if (el.getComputedTextLength() > max) {
      el.setAttribute("textLength", String(max));
      el.setAttribute("lengthAdjust", "spacingAndGlyphs");
    }
  }
  // re-measure whenever the node name or egress IP changes (effect runs after the DOM text updates)
  $effect(() => { void nodeName; void egressIp; void egressIp6; fitText(nodeTitleEl, NODE_SLOT); fitText(nodeSubEl, NODE_SLOT); fitText(nodeSub6El, NODE_SLOT); });
</script>

<div class="card flow-card">
  <span class="eyebrow">Connection</span>
  <div class="diagram-scroll">
  <svg viewBox="0 0 760 218" class="diagram" role="img"
       aria-label="connection path: devices, gateway, tunnel node, internet">
    <!-- lines -->
    <line class="ln {tone(segmentUp)}" x1="86" y1="150" x2="220" y2="150" />
    <text class="lbl" x="153" y="141">Lan</text>

    <line class="ln {leaking ? 'leak' : 'faint'}" x1="280" y1="150" x2="674" y2="150" />
    <text class="lbl {leaking ? 'leak' : 'faint'}" x="477" y="167">untunneled{#if leaking} ↓ {fmtRate(directDown)} · ↑ {fmtRate(directUp)}{/if}</text>

    <line class="tun {tunCls}" x1="272" y1="133" x2="448" y2="84" />
    <line class="tun {tunCls}" x1="492" y1="84" x2="682" y2="133" />
    <text class="lbl" x="470" y="16">Tunnel</text>
    <text class="rate {tunCls === 'bad' ? 'bad' : ''}" x="470" y="31">{tunnelRate}</text>

    <!-- Devices -->
    <circle class="cir" cx="56" cy="150" r="26" />
    <g class="ic" transform="translate(47,141) scale(0.75)">{@html P.devices}</g>
    <circle class="sdot {tone(segmentUp)}" cx="74" cy="132" r="4.5" />
    <text class="ctitle" x="56" y="194">{clients ?? "—"} {clients === 1 ? "device" : "devices"}</text>

    <!-- Gateway -->
    <circle class="cir" cx="250" cy="150" r="26" />
    <g class="ic" transform="translate(241,141) scale(0.75)">{@html P.server}</g>
    <circle class="sdot {tone(segmentUp)}" cx="268" cy="132" r="4.5" />
    <text class="ctitle" x="250" y="194">Pi gateway</text>
    <text class="csub {segmentIp ? 'mono' : ''}" x="250" y="208">{gwSub}</text>

    <!-- Node (elevated) -->
    <circle class="cir" cx="470" cy="70" r="28" />
    {#if egressCc}<text class="flag" x="470" y="78">{flagEmoji(egressCc)}</text>
    {:else}<g class="ic" transform="translate(461,61) scale(0.75)">{@html P.exit}</g>{/if}
    <circle class="sdot {running ? tone(realOk) : 'idle'}" cx="490" cy="51" r="4.5" />
    <text bind:this={nodeTitleEl} class="ctitle" x="470" y="116">{trunc(nodeName ?? "—", 38)}</text>
    <text bind:this={nodeSubEl} class="csub mono" x="470" y="130">{egressIp ? trunc(egressIp, 42) : "no exit"}</text>
    {#if egressIp6}<text bind:this={nodeSub6El} class="csub6 mono" x="470" y="141">v6 {trunc(egressIp6, 44)}</text>{/if}

    <!-- Internet -->
    <circle class="cir" cx="704" cy="150" r="26" />
    <g class="ic" transform="translate(695,141) scale(0.75)">{@html P.globe}</g>
    <circle class="sdot {tone(uplink)}" cx="722" cy="132" r="4.5" />
    <text class="ctitle" x="704" y="194">Internet</text>
    <text class="csub" x="704" y="208">{uplink == null ? "—" : uplink ? "reachable" : "down"}{#if ipv6Enabled} · v6 {uplink6 == null ? "?" : uplink6 ? "ok" : "✕"}{/if}</text>
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
  .csub6 { fill: var(--faint); font-size: 8px; text-anchor: middle; font-family: var(--mono); }
  .csub.mono { font-family: var(--mono); }
  .lbl { fill: var(--faint); font-size: 10.5px; text-anchor: middle; letter-spacing: 0.03em; }
  .lbl.leak { fill: var(--warn); font-weight: 600; }
  .rate { fill: var(--muted); font-size: 10.5px; text-anchor: middle; font-family: var(--mono); }
  .rate.bad { fill: var(--danger); }
</style>
