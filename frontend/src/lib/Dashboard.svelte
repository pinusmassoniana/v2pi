<script lang="ts">
  import { api, ApiError, type Node, type TrafficMessage } from "./api";
  import TrafficGraph from "./TrafficGraph.svelte";
  import NetworkCard from "./NetworkCard.svelte";
  import Alert from "./Alert.svelte";
  import { confirmDialog } from "./confirm.svelte";
  import { statusStore, subscribeStatus } from "./status.svelte";

  const status = $derived(statusStore.value);   // shared poller (no duplicate /api/status)
  let nodes = $state<Node[]>([]);
  let samples = $state<{ ts: number; up: number; down: number }[]>([]);
  let live = $state<TrafficMessage | null>(null);
  let disabled = $state(false);
  let msg = $state("");

  let activeName = $derived(nodes.find((n) => n.id === status?.active_node_id)?.name ?? null);
  let liveFrame = $derived(live && !("disabled" in live) && !("error" in live) ? live : null);
  let liveActive = $derived(liveFrame ? liveFrame.active : null);
  let liveTotals = $derived(liveFrame ? liveFrame.totals : null);
  // never null — a null here would crash the {#if} below (Svelte drops the parens around
  // the `|| ` so `liveDirect && a || liveDirect.up_bps` reads up_bps on null). Default to zeros.
  let liveDirect = $derived(liveFrame ? (liveFrame.outbounds.direct ?? { up_bps: 0, down_bps: 0 }) : { up_bps: 0, down_bps: 0 });
  let latest = $derived(samples.length ? samples[samples.length - 1] : { up: 0, down: 0 });

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
    const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (s < 60) return `checked ${s}s ago`;
    const m = Math.floor(s / 60);
    return m < 60 ? `checked ${m}m ago` : `checked ${Math.floor(m / 60)}h ago`;
  }
  function uptimeLabel(since: number | null | undefined): string {
    if (!since) return "—";
    const s = Math.max(0, Math.floor(Date.now() / 1000 - since));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m` : `${s}s`;
  }

  async function refresh() {
    try { nodes = await api.listNodes(); }   // status comes from the shared store now
    catch (err) { msg = err instanceof ApiError ? err.message : "refresh failed"; }
  }
  async function rollback() {
    if (!(await confirmDialog("Roll back the live config to the previously applied node?"))) return;
    try { await api.rollback(); await refresh(); msg = "rolled back"; }
    catch (err) { msg = err instanceof ApiError ? err.message : "rollback failed"; }
  }

  // shared status poller + a nodes refresh, both paused while the tab is hidden
  $effect(() => {
    const stop = subscribeStatus(3000);
    refresh();
    const t = setInterval(() => { if (document.visibilityState === "visible") refresh(); }, 3000);
    return () => { clearInterval(t); stop(); };
  });

  // seed the graph with recorded history so the full window shows immediately on open
  $effect(() => {
    api.getTrafficHistory(3600, 1200)
      .then((h) => {
        const seed = h.samples.map(([ts, up, down]) => ({ ts, up, down }));
        if (seed.length) samples = [...seed, ...samples].slice(-4000);
      })
      .catch(() => {});
  });

  // active-node liveness (P4): re-probe the active node every 60s while the tab is
  // visible, so real latency / egress stay fresh instead of waiting on the 30-min sweep.
  // Reads status only inside the callback so the interval isn't reset on every poll.
  $effect(() => {
    const t = setInterval(() => {
      const id = status?.active_node_id;
      if (id && status?.running && document.visibilityState === "visible")
        api.probeNode(id).catch(() => {});
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
      ws.onclose = () => { if (!stop) timer = setTimeout(open, 2000); };
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
    <span class="hero-headline">{status?.running ? "Connected" : status?.running === false ? "Disconnected" : "—"}</span>
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
  <span class="hero-spacer"></span>
  <button class="btn" onclick={rollback} title="Revert the live config to the previously applied node">Rollback</button>
</div>

<div class="metrics">
  <div class="metric">
    <span class="eyebrow">Real latency</span>
    <span class="metric-val mono">{liveActive?.real_ok ? liveActive.latency_ms : "—"}{#if liveActive?.real_ok}<small>ms</small>{/if}</span>
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
    <span class="metric-val mono">{liveTotals ? fmtBytes(liveTotals.down) : "—"}</span>
    <span class="metric-sub">this session</span>
  </div>
  <div class="metric">
    <span class="eyebrow">Data up</span>
    <span class="metric-val mono">{liveTotals ? fmtBytes(liveTotals.up) : "—"}</span>
    <span class="metric-sub">this session</span>
  </div>
</div>

<div class="card graph-card">
  <div class="graph-top">
    <span class="eyebrow">Throughput</span>
    {#if liveDirect && (liveDirect.down_bps > 0 || liveDirect.up_bps > 0)}
      <span class="direct-note" class:warn={liveDirect.down_bps + liveDirect.up_bps > 50000}
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

  /* metric tiles — single surface split by 1px gap-dividers (no card overuse) */
  .metrics {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 1px;
    background: var(--border);
    border: 1px solid var(--border); border-radius: var(--radius-lg);
    box-shadow: var(--shadow-sm); overflow: hidden;
  }
  .metric { background: var(--surface); padding: 0.85rem 1.1rem; display: grid; gap: 0.25rem; min-width: 0; }
  .metric-val {
    font-size: 1.4rem; font-weight: 680; letter-spacing: -0.02em; line-height: 1.1;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .metric-val.sm { font-size: 1.02rem; }
  .metric-val small { font-size: 0.7rem; font-weight: 500; color: var(--muted); margin-left: 0.15rem; }
  .metric-val.rate-down { color: var(--accent); }
  .metric-val.rate-up { color: var(--success); }
  .metric-sub { font-size: 0.72rem; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  /* traffic graph card */
  .graph-card { gap: 0.7rem; }
  .graph-top { display: flex; align-items: center; justify-content: space-between; gap: 0.6rem; flex-wrap: wrap; }
  .direct-note { font-family: var(--mono); font-variant-numeric: tabular-nums; font-size: 0.72rem; color: var(--muted); }
  .direct-note.warn { color: var(--warn); font-weight: 600; }

  @media (max-width: 820px) { .metrics { grid-template-columns: repeat(2, 1fr); } }
  @media (max-width: 460px) { .metrics { grid-template-columns: 1fr; } }
</style>
