<script lang="ts">
  // NF1: live connection topology — Devices → Pi gateway → tunnel(node) → Internet. Each hop is
  // coloured by health; the tunnel link carries the live ↓/↑ rate and (NF3) an "untunneled"
  // leak readout drawn around it. Pure presentation — all data comes from the Dashboard.
  import { fmtRate } from "./format";
  import { flagEmoji } from "./flag";
  import { I } from "./icons";

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

  const tone = (v: boolean | null | undefined): Tone => (v == null ? "idle" : v ? "ok" : "bad");
  let leaking = $derived(directDown + directUp > 0);
  let tunTone = $derived<Tone>(!running ? "idle" : realOk === false ? "bad" : realOk ? "ok" : "idle");
  let names = $derived(clientNames.filter(Boolean));
</script>

<div class="card flow-card">
  <span class="eyebrow">Connection</span>
  <div class="flow">
    <div class="hop">
      <span class="hop-ic">{@html I.devices}</span>
      <span class="hop-title"><span class="num">{clients ?? "—"}</span> {clients === 1 ? "device" : "devices"}</span>
      <span class="hop-sub" title={names.join(", ")}>{names.length ? names.slice(0, 2).join(", ") + (names.length > 2 ? ` +${names.length - 2}` : "") : "client segment"}</span>
    </div>

    <div class="link"><div class="line {tone(segmentUp)}"></div><span class="cap">LAN</span></div>

    <div class="hop">
      <span class="hop-ic">{@html I.server}</span>
      <span class="hop-title"><span class="dot {tone(segmentUp)}"></span> Pi gateway</span>
      <span class="hop-sub">{segmentUp == null ? "segment —" : segmentUp ? "segment up" : "segment down"}</span>
    </div>

    <div class="link tunnel" class:leak={leaking}>
      <div class="line {tunTone}"></div>
      <span class="cap {tunTone}">{running ? (realOk === false ? "tunnel ✕" : "tunnel") : "off"}</span>
      <span class="rates">↓ {fmtRate(proxyDown)} · ↑ {fmtRate(proxyUp)}{#if running && realOk && latencyMs != null} · {latencyMs}ms{/if}</span>
      {#if leaking}<span class="leak-note" title="Traffic leaving the segment WITHOUT the tunnel">⚠ untunneled ↓ {fmtRate(directDown)} · ↑ {fmtRate(directUp)}</span>{/if}
    </div>

    <div class="hop">
      <span class="hop-ic flag">{#if egressCc}{flagEmoji(egressCc)}{:else}{@html I.exit}{/if}</span>
      <span class="hop-title"><span class="dot {tone(running ? realOk : null)}"></span> {nodeName ?? "—"}</span>
      <span class="hop-sub mono" title={egressIp ?? ""}>{egressIp ?? "no exit"}</span>
    </div>

    <div class="link"><div class="line {tone(uplink)}"></div><span class="cap">WAN</span></div>

    <div class="hop">
      <span class="hop-ic">{@html I.globe}</span>
      <span class="hop-title"><span class="dot {tone(uplink)}"></span> Internet</span>
      <span class="hop-sub">{uplink == null ? "—" : uplink ? "reachable" : "unreachable"}{#if ipv6Enabled} · v6 {uplink6 == null ? "?" : uplink6 ? "ok" : "✕"}{/if}</span>
    </div>
  </div>
</div>

<style>
  .flow-card { display: grid; gap: 0.6rem; }
  .flow {
    display: flex; align-items: stretch; gap: 0.2rem; overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }
  .hop {
    flex: 1 1 0; min-width: 7.5rem; display: grid; gap: 0.2rem; justify-items: center; text-align: center;
    padding: 0.5rem 0.4rem;
  }
  .hop-ic {
    width: 2rem; height: 2rem; display: inline-grid; place-items: center; border-radius: 50%;
    background: var(--surface-2); border: 1px solid var(--border); color: var(--muted);
    font-size: 1.1rem; line-height: 1;
  }
  .hop-ic :global(svg) { width: 17px; height: 17px; display: block; }
  .hop-title { font-weight: 600; font-size: 0.86rem; display: inline-flex; align-items: center; gap: 0.3rem;
    white-space: nowrap; max-width: 100%; overflow: hidden; text-overflow: ellipsis; }
  .hop-title .num { font-family: var(--mono); font-variant-numeric: tabular-nums; }
  .hop-sub { font-size: 0.7rem; color: var(--muted); white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; max-width: 100%; }
  .hop-sub.mono { font-family: var(--mono); }
  .dot { width: 0.5rem; height: 0.5rem; border-radius: 50%; flex: none; background: var(--muted); }
  .dot.ok { background: var(--success); } .dot.bad { background: var(--danger); } .dot.idle { background: var(--muted); }

  .link { flex: 1 1 0; min-width: 4.5rem; display: grid; gap: 0.18rem; align-content: center;
    justify-items: center; padding: 0 0.2rem; position: relative; }
  .line { width: 100%; height: 2px; border-radius: 2px; background: var(--border); }
  .line.ok { background: var(--success); } .line.bad { background: var(--danger); } .line.idle { background: var(--border); }
  .cap { font-size: 0.62rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--faint); font-weight: 650; }
  .cap.ok { color: var(--success); } .cap.bad { color: var(--danger); }
  .tunnel { min-width: 8rem; }
  .tunnel .line { height: 3px; }
  .rates { font-family: var(--mono); font-variant-numeric: tabular-nums; font-size: 0.66rem; color: var(--muted); white-space: nowrap; }
  .leak-note { font-size: 0.64rem; color: var(--warn); font-weight: 600; white-space: nowrap; }
  .tunnel.leak .line { background: var(--warn); }
  .flag { font-size: 1.15rem; }
</style>
