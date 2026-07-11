<script lang="ts">
  import { api, type Node, type TrafficMessage, type Network } from "./api";
  import TrafficGraph from "./TrafficGraph.svelte";
  import LatencyChart from "./LatencyChart.svelte";
  import { statusStore, subscribeStatus, serverNow } from "./status.svelte";
  import { agoLabel } from "./dashboard";
  import { fmtRate } from "./format";

  const status = $derived(statusStore.value);
  let nodes = $state<Node[]>([]);
  let net = $state<Network | null>(null);
  let samples = $state<{ ts: number; up: number; down: number }[]>([]);
  let live = $state<TrafficMessage | null>(null);
  let disabled = $state(false);
  let graphWindow = $state(600);
  let longSamples = $state<{ ts: number; up: number; down: number }[]>([]);
  let tick = $state(0);

  let liveFrame = $derived(live && !("disabled" in live) && !("error" in live) ? live : null);
  let liveActive = $derived(liveFrame ? liveFrame.active : null);
  let activeNode = $derived(nodes.find((n) => n.id === status?.active_node_id) ?? null);
  // keep showing the short-window series until the long-window fetch resolves (avoids empty-chart flicker)
  let series = $derived(graphWindow > 3600 && longSamples.length ? longSamples : samples);

  // KPIs — all from real data; nothing faked
  let peakDown = $derived(series.reduce((m, s) => (s.down > m ? s.down : m), 0));
  let activeLat = $derived(liveActive?.real_ok ? liveActive.latency_ms : null);
  let latHistory = $derived(liveActive?.lat_history ?? []);
  let avgLat = $derived(latHistory.length ? Math.round(latHistory.reduce((a, b) => a + b, 0) / latHistory.length) : null);
  let failoverEvents = $derived((net?.events ?? []).filter((e) => /failover|switch/i.test(e.kind)));
  let failovers24h = $derived(failoverEvents.filter((e) => serverNow() / 1000 - e.ts < 86400).length);

  function uptimeLabel(): string {
    tick;   // re-evaluate each second
    const since = status?.active_since;
    if (!status?.running || !since) return "—";
    const s = Math.max(0, Math.floor(serverNow() / 1000 - since));
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m`;
  }

  // probe-latency bars: active node has a real number; others have no passive probe → shown as "—"
  let probeRows = $derived.by(() => {
    const act = activeNode;
    const rest = nodes.filter((n) => n.id !== act?.id);
    return [...(act ? [act] : []), ...rest].slice(0, 6).map((n) => ({
      node: n, active: n.id === act?.id,
      ms: n.id === act?.id && liveActive?.real_ok ? liveActive.latency_ms : null,
    }));
  });
  const BAR_MAX = 250;   // ms full-scale for the bars

  async function seedHistory() {
    try {
      const h = await api.getTrafficHistory(3600, 1200);
      const map = new Map<number, { ts: number; up: number; down: number }>();
      for (const [ts, up, down] of h.samples) map.set(ts, { ts, up, down });
      for (const s of samples) map.set(s.ts, s);
      samples = [...map.values()].sort((a, b) => a.ts - b.ts).slice(-4000);
    } catch {}
  }
  async function loadLong(sec: number) {
    try { const h = await api.getTrafficHistory(sec, 1200); longSamples = h.samples.map(([ts, up, down]) => ({ ts, up, down })); } catch {}
  }
  function onGraphWindow(sec: number) { graphWindow = sec; if (sec > 3600) loadLong(sec); }

  $effect(() => subscribeStatus(3000));
  $effect(() => { api.listNodes().then((n) => (nodes = n)).catch(() => {}); });
  $effect(() => {
    const load = () => api.getNetwork().then((n) => (net = n)).catch(() => {});
    load();
    const t = setInterval(() => { if (document.visibilityState === "visible") load(); }, 8000);
    return () => clearInterval(t);
  });
  $effect(() => { seedHistory(); });
  $effect(() => { const t = setInterval(() => { if (document.visibilityState === "visible") tick++; }, 1000); return () => clearInterval(t); });
  $effect(() => {
    if (graphWindow <= 3600) return;
    const sec = graphWindow;
    const t = setInterval(() => { if (document.visibilityState === "visible") loadLong(sec); }, 60000);
    return () => clearInterval(t);
  });
  // live traffic WS (for the live chart + active-node latency) — self-managing, backfills on reconnect
  $effect(() => {
    const conn = api.connectTraffic((m) => {
      if ("disabled" in m) { disabled = true; return; }
      if ("error" in m) return;
      disabled = false; live = m;
      const p = m.outbounds.proxy ?? { up_bps: 0, down_bps: 0 };
      samples.push({ ts: m.ts, up: p.up_bps, down: p.down_bps });
      if (samples.length > 4000) samples.splice(0, samples.length - 4000);
    }, seedHistory);
    return () => conn.close();
  });
</script>

<!-- KPI row -->
<div class="kpis">
  <div class="kpi">
    <div class="kpi-k">PEAK ↓ THROUGHPUT</div>
    <div class="kpi-v up">{peakDown > 0 ? fmtRate(peakDown) : "—"}</div>
  </div>
  <div class="kpi">
    <div class="kpi-k">ACTIVE PROBE LATENCY</div>
    <div class="kpi-v">{activeLat != null ? activeLat : "—"}{#if activeLat != null}<small>ms</small>{/if}{#if avgLat != null}<span class="kpi-extra">avg {avgLat}ms</span>{/if}</div>
  </div>
  <div class="kpi">
    <div class="kpi-k">FAILOVERS · 24H</div>
    <div class="kpi-v" class:warnc={failovers24h > 0}>{failovers24h}</div>
  </div>
  <div class="kpi">
    <div class="kpi-k">UPTIME</div>
    <div class="kpi-v acc">{uptimeLabel()}</div>
  </div>
</div>

<!-- live throughput -->
<div class="card">
  <div class="card-top">
    <span class="eyebrow">Throughput — live</span>
    <span class="legend"><span class="up">↓ download</span> · <span class="down">↑ upload</span></span>
  </div>
  {#if disabled}
    <p class="msg">Traffic stats disabled — enable in Settings.</p>
  {:else}
    <TrafficGraph {series} onwindow={onGraphWindow} />
  {/if}
</div>

<div class="row2">
  <!-- probe latency by node -->
  <div class="card">
    <div class="card-top"><span class="eyebrow">Probe latency by node</span></div>
    <div class="bars">
      {#each probeRows as r (r.node.id)}
        <div class="bar-row">
          <div class="bar-top"><span class="bar-name">{r.node.name}</span><span class="bar-ms" class:dim={r.ms == null} class:acc={r.active && r.ms != null}>{r.ms != null ? r.ms + "ms" : "—"}</span></div>
          <div class="bar-track"><div class="bar-fill" class:active={r.active} style="width:{r.ms != null ? Math.min(100, (r.ms / BAR_MAX) * 100) : 0}%"></div></div>
        </div>
      {/each}
      {#if !probeRows.length}<p class="msg">No nodes configured.</p>{/if}
    </div>
    {#if latHistory.length}
      <div class="spark"><div class="card-top"><span class="eyebrow">Active node · latency history</span></div><LatencyChart values={latHistory} stale={liveActive?.real_ok === false} /></div>
    {/if}
  </div>

  <!-- failover history -->
  <div class="card">
    <div class="card-top"><span class="eyebrow">Failover history</span></div>
    <div class="fh">
      {#each failoverEvents.slice(0, 8) as e, i (e.ts + e.detail + i)}
        <div class="fh-row"><span class="fh-t">{agoLabel(e.ts, serverNow() / 1000)}</span><span class="fh-d">{e.detail}</span></div>
      {/each}
      {#if status?.last_failover_at && !failoverEvents.length}
        <div class="fh-row"><span class="fh-t">{agoLabel(status.last_failover_at, serverNow() / 1000)}</span><span class="fh-d">auto-failover — gateway switched node</span></div>
      {/if}
      {#if !failoverEvents.length && !status?.last_failover_at}<p class="msg">No failovers recorded.</p>{/if}
    </div>
  </div>
</div>

<style>
  .card-top { display: flex; align-items: center; justify-content: space-between; gap: 0.6rem; }
  .legend { font-size: 0.72rem; color: var(--tx2); }
  .legend .up { color: var(--up); } .legend .down { color: var(--down); }

  /* KPI tiles */
  .kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.9rem; }
  .kpi { background: var(--bg1); border: 1px solid var(--bd); border-radius: 10px; padding: 0.8rem 1rem; display: grid; gap: 0.5rem; }
  .kpi-k { font-size: 0.64rem; letter-spacing: 0.13em; color: var(--tx3); }
  .kpi-v { font-size: 1.6rem; font-weight: 600; line-height: 1.15; font-family: var(--mono); font-variant-numeric: tabular-nums;
    display: flex; flex-wrap: wrap; align-items: baseline; }
  .kpi-v small { font-size: 0.7rem; color: var(--tx3); font-weight: 400; margin-left: 0.15rem; }
  .kpi-v.up { color: var(--up); } .kpi-v.acc { color: var(--acc); } .kpi-v.warnc { color: var(--warn); }
  .kpi-extra { font-size: 0.66rem; color: var(--tx3); font-weight: 400; margin-left: 0.5rem; font-family: var(--font); }

  .row2 { display: grid; grid-template-columns: 1.2fr 1fr; gap: 0.9rem; }

  /* probe bars */
  .bars { display: flex; flex-direction: column; gap: 0.75rem; }
  .bar-top { display: flex; justify-content: space-between; gap: 0.6rem; font-size: 0.76rem; margin-bottom: 0.3rem; }
  .bar-name { color: var(--tx2); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bar-ms { font-weight: 600; font-family: var(--mono); flex: none; }
  .bar-ms.acc { color: var(--acc); } .bar-ms.dim { color: var(--tx3); }
  .bar-track { height: 6px; background: var(--bg3); border-radius: 3px; overflow: hidden; }
  .bar-fill { height: 100%; background: var(--tx3); border-radius: 3px; transition: width 0.3s; }
  .bar-fill.active { background: var(--acc); }
  .spark { margin-top: 0.9rem; border-top: 1px solid var(--bd); padding-top: 0.8rem; display: grid; gap: 0.5rem; }

  /* failover history */
  .fh { display: flex; flex-direction: column; gap: 0.6rem; }
  .fh-row { display: flex; gap: 0.7rem; font-size: 0.78rem; }
  .fh-t { color: var(--tx3); white-space: nowrap; flex: none; }
  .fh-d { color: var(--tx2); }

  @media (max-width: 1000px) { .kpis { grid-template-columns: repeat(2, 1fr); } .row2 { grid-template-columns: 1fr; } }
  @media (max-width: 420px) { .kpis { grid-template-columns: 1fr; } }
</style>
