<script lang="ts">
  import { api, ApiError, type Node, type TrafficMessage, type Network, type Subscription } from "./api";
  import TrafficGraph from "./TrafficGraph.svelte";
  import NetworkCard from "./NetworkCard.svelte";
  import Alert from "./Alert.svelte";
  import { confirmDialog } from "./confirm.svelte";
  import { statusStore, subscribeStatus, pollStatusOnce, serverNow } from "./status.svelte";
  import { subWarnings, sparkPath, agoLabel } from "./dashboard";

  const UNTUNNELED_WARN_BPS = 50_000;   // D10: throughput leaking outside the tunnel above this → warn

  const status = $derived(statusStore.value);   // shared poller (no duplicate /api/status)
  let nodes = $state<Node[]>([]);
  let samples = $state<{ ts: number; up: number; down: number }[]>([]);
  let live = $state<TrafficMessage | null>(null);
  let disabled = $state(false);
  let msg = $state("");
  let net = $state<Network | null>(null);       // C: connected-clients count
  let subs = $state<Subscription[]>([]);          // E: expiry / data-cap warnings
  let failoverDismissed = $state<number | null>(null);

  let activeName = $derived(nodes.find((n) => n.id === status?.active_node_id)?.name ?? null);
  let liveFrame = $derived(live && !("disabled" in live) && !("error" in live) ? live : null);
  let liveActive = $derived(liveFrame ? liveFrame.active : null);
  let liveTotals = $derived(liveFrame ? liveFrame.totals : null);
  // durable data-used (survives xray restart, F); fall back to the process-lifetime totals
  let dataUp = $derived(liveFrame?.lifetime?.up ?? liveTotals?.up ?? null);
  let dataDown = $derived(liveFrame?.lifetime?.down ?? liveTotals?.down ?? null);
  // never null — a null here would crash the {#if} below (Svelte drops the parens around
  // the `|| ` so `liveDirect && a || liveDirect.up_bps` reads up_bps on null). Default to zeros.
  let liveDirect = $derived(liveFrame ? (liveFrame.outbounds.direct ?? { up_bps: 0, down_bps: 0 }) : { up_bps: 0, down_bps: 0 });
  let latest = $derived(samples.length ? samples[samples.length - 1] : { up: 0, down: 0 });
  let clients = $derived(net?.status.dhcp_clients ?? null);
  let spark = $derived(liveActive?.lat_history?.length ? sparkPath(liveActive.lat_history, 56, 16) : "");
  // A: surface an auto-failover from the last 24h until the operator dismisses it
  let failover = $derived(
    status?.last_failover_at && status.last_failover_at !== failoverDismissed
      && serverNow() / 1000 - status.last_failover_at < 86400
      ? status.last_failover_at : null);
  let warnings = $derived(subWarnings(subs, serverNow() / 1000));

  const fmtRate = (bps: number) =>
    bps >= 1e6 ? (bps / 1e6).toFixed(1) + " Mbit/s"
    : bps >= 1e3 ? (bps / 1e3).toFixed(0) + " kbit/s"
    : Math.round(bps) + " bit/s";
  const fmtBytes = (b: number) =>
    b >= 1e9 ? (b / 1e9).toFixed(2) + " GB"
    : b >= 1e6 ? (b / 1e6).toFixed(1) + " MB"
    : b >= 1e3 ? (b / 1e3).toFixed(0) + " KB"
    : Math.round(b) + " B";
  function freshness(iso: string | null | undefined): string {
    if (!iso) return "";
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return "";
    const s = Math.max(0, Math.floor((serverNow() - t) / 1000));   // D4: Pi-clock aligned
    if (s < 60) return `checked ${s}s ago`;
    const m = Math.floor(s / 60);
    return m < 60 ? `checked ${m}m ago` : `checked ${Math.floor(m / 60)}h ago`;
  }
  function uptimeLabel(since: number | null | undefined): string {
    if (!since) return "—";
    const s = Math.max(0, Math.floor(serverNow() / 1000 - since));   // D4
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m` : `${s}s`;
  }

  async function refresh() {
    try { nodes = await api.listNodes(); }   // status comes from the shared store now
    catch (err) { msg = err instanceof ApiError ? err.message : "refresh failed"; }
  }
  async function rollback() {
    if (!(await confirmDialog("Roll back the live config to the previously applied node?"))) return;
    try {
      const r = await api.rollback();                    // D1: honour ok — don't claim success on a no-op
      msg = r.ok ? "rolled back" : "nothing to roll back";
      if (r.ok) { await pollStatusOnce(); await refresh(); }
    } catch (err) { msg = err instanceof ApiError ? err.message : "rollback failed"; }
  }

  // re-pull the recorded history and merge by timestamp (dedup), so the graph fills in after a
  // reconnect / long gap instead of only showing post-reconnect data (D5). Also the initial seed.
  async function seedHistory() {
    try {
      const h = await api.getTrafficHistory(3600, 1200);
      const map = new Map<number, { ts: number; up: number; down: number }>();
      for (const [ts, up, down] of h.samples) map.set(ts, { ts, up, down });
      for (const s of samples) map.set(s.ts, s);          // live samples win on a tie
      samples = [...map.values()].sort((a, b) => a.ts - b.ts).slice(-4000);
    } catch {}
  }

  // shared status poller + a nodes refresh, both paused while the tab is hidden
  $effect(() => {
    const stop = subscribeStatus(3000);
    refresh();
    const t = setInterval(() => { if (document.visibilityState === "visible") refresh(); }, 3000);
    return () => { clearInterval(t); stop(); };
  });

  // C: connected-clients count — its own light poll (5s, visibility-gated)
  $effect(() => {
    const load = () => api.getNetwork().then((n) => (net = n)).catch(() => {});
    load();
    const t = setInterval(() => { if (document.visibilityState === "visible") load(); }, 5000);
    return () => clearInterval(t);
  });

  // E: subscription expiry / data-cap warnings — subs change rarely, poll slowly
  $effect(() => {
    const load = () => api.listSubs().then((s) => (subs = s)).catch(() => {});
    load();
    const t = setInterval(() => { if (document.visibilityState === "visible") load(); }, 30000);
    return () => clearInterval(t);
  });

  // seed the graph with recorded history so the full window shows immediately on open
  $effect(() => { seedHistory(); });

  // active-node liveness (P4): re-probe the active node every 60s while the tab is visible, so
  // real latency / egress stay fresh. real_only → skip the two direct probes (D6).
  $effect(() => {
    const t = setInterval(() => {
      const id = status?.active_node_id;
      if (id && status?.running && document.visibilityState === "visible")
        api.probeNode(id, true).catch(() => {});
    }, 60000);
    return () => clearInterval(t);
  });

  // live traffic WebSocket with reconnect/backoff; closes on unmount
  $effect(() => {
    let ws: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let stop = false;
    function open() {
      ws = api.openTraffic((m) => {
        if ("disabled" in m) { disabled = true; return; }
        if ("error" in m) { return; }            // transient stats error → leave a gap
        disabled = false;
        live = m;
        const p = m.outbounds.proxy ?? { up_bps: 0, down_bps: 0 };
        samples.push({ ts: m.ts, up: p.up_bps, down: p.down_bps });   // mutate (no per-second 4000-copy)
        if (samples.length > 4000) samples.splice(0, samples.length - 4000);
      });
      ws.onclose = () => {
        if (!stop) { seedHistory(); timer = setTimeout(open, 2000); }   // D5: backfill the gap
      };
      ws.onerror = () => { try { ws?.close(); } catch {} };
    }
    open();
    return () => { stop = true; if (timer) clearTimeout(timer); try { ws?.close(); } catch {} };
  });
</script>

<div class="hero" class:up={status?.running} class:down={status?.running === false}>
  <span class="status-dot" class:live={status?.running} class:bad={status?.running === false}></span>
  <div class="hero-state">
    <span class="eyebrow">Gateway</span>
    <span class="hero-headline" aria-live="polite">{status?.running ? "Connected" : status?.running === false ? "Disconnected" : "—"}</span>
  </div>
  <div class="hero-node">
    <span class="eyebrow">Active node</span>
    <span class="hero-nodename">{activeName ?? "—"}</span>
  </div>
  {#if status?.running && status?.active_since}
    <div class="hero-node">
      <span class="eyebrow">Uptime</span>
      <span class="hero-nodename mono">{uptimeLabel(status.active_since)}</span>
    </div>
  {/if}
  {#if clients !== null}
    <div class="hero-node">
      <span class="eyebrow">Clients</span>
      <span class="hero-nodename mono">{clients}</span>
    </div>
  {/if}
  <span class="hero-spacer"></span>
  <button class="btn" onclick={rollback} title="Revert the live config to the previously applied node">Rollback</button>
</div>

{#if failover}
  <div class="banner warn" role="status">
    <span>⚠ Auto-failover {agoLabel(failover, serverNow() / 1000)} — the gateway switched node on its own.</span>
    <button class="banner-x" onclick={() => (failoverDismissed = failover)} aria-label="Dismiss">✕</button>
  </div>
{/if}

{#if warnings.length}
  <div class="chips">
    {#each warnings as w (w.name + w.text)}
      <span class="chip {w.level}" title="Subscription {w.name}">{w.name}: {w.text}</span>
    {/each}
  </div>
{/if}

<div class="metrics">
  <div class="metric">
    <span class="eyebrow">Real latency</span>
    <span class="metric-row">
      <span class="metric-val mono">{liveActive?.real_ok ? liveActive.latency_ms : "—"}{#if liveActive?.real_ok}<small>ms</small>{/if}</span>
      {#if spark}<svg class="spark" width="56" height="16" viewBox="0 0 56 16" aria-hidden="true"><path d={spark} /></svg>{/if}
    </span>
    <span class="metric-sub">{liveActive ? (liveActive.checked_at ? freshness(liveActive.checked_at) : (liveActive.real_ok === false ? "probe failed" : "no probe yet")) : "waiting…"}</span>
  </div>
  <div class="metric">
    <span class="eyebrow">Egress IP</span>
    <span class="metric-val mono sm">{liveActive?.egress_ip ?? "—"}</span>
    <span class="metric-sub">{liveActive?.egress_ip ? (freshness(liveActive.checked_at) || "tunnel exit") : "unknown"}</span>
  </div>
  <div class="metric">
    <span class="eyebrow">Download</span>
    <span class="metric-val mono rate-down">{disabled ? "—" : fmtRate(latest.down)}</span>
    <span class="metric-sub">{disabled ? "stats off" : "live"}</span>
  </div>
  <div class="metric">
    <span class="eyebrow">Upload</span>
    <span class="metric-val mono rate-up">{disabled ? "—" : fmtRate(latest.up)}</span>
    <span class="metric-sub">{disabled ? "stats off" : "live"}</span>
  </div>
  <div class="metric">
    <span class="eyebrow">Data down</span>
    <span class="metric-val mono">{dataDown !== null ? fmtBytes(dataDown) : "—"}</span>
    <span class="metric-sub">total used</span>
  </div>
  <div class="metric">
    <span class="eyebrow">Data up</span>
    <span class="metric-val mono">{dataUp !== null ? fmtBytes(dataUp) : "—"}</span>
    <span class="metric-sub">total used</span>
  </div>
</div>

<div class="card graph-card">
  <div class="graph-top">
    <span class="eyebrow">Throughput</span>
    {#if liveDirect && (liveDirect.down_bps > 0 || liveDirect.up_bps > 0)}
      <span class="direct-note" class:warn={liveDirect.down_bps + liveDirect.up_bps > UNTUNNELED_WARN_BPS}
            title="Traffic leaving the segment WITHOUT going through the tunnel">
        untunneled ↓ {fmtRate(liveDirect.down_bps)} · ↑ {fmtRate(liveDirect.up_bps)}
      </span>
    {/if}
  </div>
  {#if disabled}
    <p class="msg">Traffic stats disabled — enable in Settings.</p>
  {:else}
    <TrafficGraph series={samples} />
  {/if}
</div>

<NetworkCard />

<Alert {msg} />

<style>
  /* status hero */
  .hero {
    position: relative; overflow: hidden;
    display: flex; align-items: center; gap: 1.2rem; flex-wrap: wrap;
    background: var(--surface);
    border: 1px solid var(--border); border-radius: var(--radius-lg);
    box-shadow: var(--shadow-sm);
    padding: 1.1rem 1.3rem 1.1rem 1.5rem;
  }
  .hero::before {
    content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 4px;
    background: var(--border); transition: background 0.2s;
  }
  .hero.up::before { background: var(--success); }
  .hero.down::before { background: var(--danger); }
  .status-dot { width: 0.85rem; height: 0.85rem; border-radius: 50%; flex: none; background: var(--muted); }
  .status-dot.live { background: var(--success); animation: pulse-ring 2.4s ease-out infinite; }
  .status-dot.bad { background: var(--danger); }
  .hero-state, .hero-node { display: grid; gap: 0.12rem; }
  .hero-headline { font-size: 1.3rem; font-weight: 720; letter-spacing: -0.02em; line-height: 1.1; }
  .hero-nodename { font-weight: 600; font-size: 0.95rem; }
  .hero-spacer { margin-left: auto; }

  /* banners + chips (failover A / subscription warnings E) */
  .banner {
    display: flex; align-items: center; gap: 0.6rem;
    padding: 0.65rem 0.9rem; border-radius: var(--radius);
    border: 1px solid var(--warn); background: color-mix(in srgb, var(--warn) 12%, var(--surface));
    font-size: 0.86rem;
  }
  .banner span { margin-right: auto; }
  .banner-x { background: none; border: none; color: var(--muted); cursor: pointer; font: inherit; font-size: 0.9rem; padding: 0 0.2rem; }
  .banner-x:hover { color: var(--text); }
  .chips { display: flex; flex-wrap: wrap; gap: 0.45rem; }
  .chip {
    font-size: 0.74rem; font-weight: 600; padding: 0.22rem 0.6rem; border-radius: 999px;
    border: 1px solid var(--border);
  }
  .chip.warn { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 45%, var(--border)); background: color-mix(in srgb, var(--warn) 10%, transparent); }
  .chip.bad { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 45%, var(--border)); background: color-mix(in srgb, var(--danger) 10%, transparent); }

  /* metric tiles — single surface split by 1px gap-dividers (no card overuse) */
  .metrics {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 1px;
    background: var(--border);
    border: 1px solid var(--border); border-radius: var(--radius-lg);
    box-shadow: var(--shadow-sm); overflow: hidden;
  }
  .metric { background: var(--surface); padding: 0.85rem 1.1rem; display: grid; gap: 0.25rem; min-width: 0; }
  .metric-row { display: flex; align-items: center; gap: 0.6rem; min-width: 0; }
  .metric-val {
    font-size: 1.4rem; font-weight: 680; letter-spacing: -0.02em; line-height: 1.1;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .metric-val.sm { font-size: 1.02rem; }
  .metric-val small { font-size: 0.7rem; font-weight: 500; color: var(--muted); margin-left: 0.15rem; }
  .metric-val.rate-down { color: var(--accent); }
  .metric-val.rate-up { color: var(--success); }
  .metric-sub { font-size: 0.72rem; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .spark { flex: none; overflow: visible; }
  .spark path { fill: none; stroke: var(--accent); stroke-width: 1.5; stroke-linejoin: round; stroke-linecap: round; }

  /* traffic graph card */
  .graph-card { gap: 0.7rem; }
  .graph-top { display: flex; align-items: center; justify-content: space-between; gap: 0.6rem; flex-wrap: wrap; }
  .direct-note { font-family: var(--mono); font-variant-numeric: tabular-nums; font-size: 0.72rem; color: var(--muted); }
  .direct-note.warn { color: var(--warn); font-weight: 600; }

  @media (max-width: 820px) { .metrics { grid-template-columns: repeat(2, 1fr); } }
  @media (max-width: 460px) { .metrics { grid-template-columns: 1fr; } }
</style>
