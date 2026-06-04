<script lang="ts">
  import { api, ApiError, type Node, type Status, type TrafficMessage } from "./api";
  import TrafficGraph from "./TrafficGraph.svelte";

  let status = $state<Status | null>(null);
  let nodes = $state<Node[]>([]);
  let samples = $state<{ up: number; down: number }[]>([]);
  let live = $state<TrafficMessage | null>(null);
  let disabled = $state(false);
  let msg = $state("");

  let activeName = $derived(nodes.find((n) => n.id === status?.active_node_id)?.name ?? null);
  // live active-node health from the latest WS frame
  let liveActive = $derived(live && !("disabled" in live) && !("error" in live) ? live.active : null);

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

<div class="card">
  <div class="toolbar">
    <span class="badge">
      <span class="dot" class:ok={status?.running} class:bad={status?.running === false}></span>
      {status?.running ? "connected" : "disconnected"}
    </span>
    <span class="muted">active node: {activeName ?? "—"}</span>
    {#if liveActive}
      <span class="badge">
        <span class="dot" class:ok={liveActive.real_ok} class:bad={liveActive.real_ok === false}></span>
        {liveActive.real_ok ? `real ${liveActive.latency_ms}ms` : liveActive.real_ok === false ? "real failed" : "real —"}
      </span>
      {#if liveActive.egress_ip}<span class="muted">egress {liveActive.egress_ip}</span>{/if}
    {/if}
    <span class="spacer"></span>
    <button class="btn" onclick={refresh}>Refresh</button>
    <button class="btn" onclick={rollback}>Rollback</button>
  </div>
</div>

<div class="card">
  {#if disabled}
    <p class="msg">traffic stats disabled — enable in Settings</p>
  {:else}
    <TrafficGraph {samples} />
  {/if}
</div>

{#if msg}<p class="msg">{msg}</p>{/if}

<style>
  .toolbar .spacer { margin-left: auto; }
  .ok { color: var(--success); }
  .bad { color: var(--danger); }
</style>
