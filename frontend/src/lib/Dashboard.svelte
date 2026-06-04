<script lang="ts">
  import { api, ApiError, type Node, type Status, type TrafficMessage } from "./api";
  import TrafficGraph from "./TrafficGraph.svelte";
  import NetworkCard from "./NetworkCard.svelte";

  let status = $state<Status | null>(null);
  let nodes = $state<Node[]>([]);
  let samples = $state<{ up: number; down: number }[]>([]);
  let live = $state<TrafficMessage | null>(null);
  let disabled = $state(false);
  let msg = $state("");

  let activeName = $derived(nodes.find((n) => n.id === status?.active_node_id)?.name ?? null);
  // live active-node health from the latest WS frame
  let liveActive = $derived(live && !("disabled" in live) && !("error" in live) ? live.active : null);
  let latest = $derived(samples.length ? samples[samples.length - 1] : { up: 0, down: 0 });
  const fmtRate = (bps: number) =>
    bps >= 1e6 ? (bps / 1e6).toFixed(1) + " Mbit/s"
    : bps >= 1e3 ? (bps / 1e3).toFixed(0) + " kbit/s"
    : Math.round(bps) + " bit/s";

  async function refresh() {
    try {
      const [st, ns] = await Promise.all([api.getStatus(), api.listNodes()]);
      status = st;
      nodes = ns;
    } catch (err) { msg = err instanceof ApiError ? err.message : "refresh failed"; }
  }
  async function rollback() {
    try { await api.rollback(); await refresh(); msg = "rolled back"; }
    catch (err) { msg = err instanceof ApiError ? err.message : "rollback failed"; }
  }

  $effect(() => { refresh(); });

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
        samples = [...samples, { up: p.up_bps, down: p.down_bps }].slice(-60);
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
  <span class="hero-spacer"></span>
  <button class="btn" onclick={refresh}>Refresh</button>
  <button class="btn" onclick={rollback}>Rollback</button>
</div>

<div class="metrics">
  <div class="metric">
    <span class="eyebrow">Real latency</span>
    <span class="metric-val mono">{liveActive?.real_ok ? liveActive.latency_ms : "—"}{#if liveActive?.real_ok}<small>ms</small>{/if}</span>
    <span class="metric-sub">{liveActive ? (liveActive.real_ok ? "verified through tunnel" : liveActive.real_ok === false ? "probe failed" : "no probe yet") : "waiting…"}</span>
  </div>
  <div class="metric">
    <span class="eyebrow">Egress IP</span>
    <span class="metric-val mono sm">{liveActive?.egress_ip ?? "—"}</span>
    <span class="metric-sub">{liveActive?.egress_ip ? "tunnel exit" : "unknown"}</span>
  </div>
  <div class="metric">
    <span class="eyebrow">Download</span>
    <span class="metric-val mono rate-down">{fmtRate(latest.down)}</span>
    <span class="metric-sub">live throughput</span>
  </div>
  <div class="metric">
    <span class="eyebrow">Upload</span>
    <span class="metric-val mono rate-up">{fmtRate(latest.up)}</span>
    <span class="metric-sub">live throughput</span>
  </div>
</div>

<div class="card graph-card">
  <div class="graph-head">
    <span class="eyebrow">Live throughput</span>
    <span class="muted gh-sub">last {samples.length}/60s</span>
  </div>
  {#if disabled}
    <p class="msg">Traffic stats disabled — enable in Settings.</p>
  {:else}
    <TrafficGraph {samples} />
  {/if}
</div>

<NetworkCard />

{#if msg}<p class="msg">{msg}</p>{/if}

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
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 1px;
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
  .graph-head { display: flex; align-items: center; justify-content: space-between; }
  .gh-sub { font-size: 0.72rem; }

  @media (max-width: 820px) { .metrics { grid-template-columns: repeat(2, 1fr); } }
  @media (max-width: 460px) { .metrics { grid-template-columns: 1fr; } }
</style>
