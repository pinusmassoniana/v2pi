<script lang="ts">
  import { api, ApiError, type Node, type NodeHealth, type Status, type TrafficMessage } from "./api";
  import TrafficGraph from "./TrafficGraph.svelte";

  let status = $state<Status | null>(null);
  let nodes = $state<Node[]>([]);
  let health = $state<Record<number, NodeHealth>>({});
  let samples = $state<{ up: number; down: number }[]>([]);
  let live = $state<TrafficMessage | null>(null);
  let disabled = $state(false);
  let msg = $state("");

  // live active-node health from the latest WS frame (falls back to polled health)
  let liveActive = $derived(live && !("disabled" in live) && !("error" in live) ? live.active : null);

  async function refresh() {
    try {
      status = await api.getStatus();
      const [ns, hs] = await Promise.all([api.listNodes(), api.listNodeHealth()]);
      nodes = ns;
      health = Object.fromEntries(hs.map((h) => [h.node_id, h]));
    } catch (err) { msg = err instanceof ApiError ? err.message : "refresh failed"; }
  }
  async function connect(id: number) {
    msg = "";
    try { await api.apply(id); await refresh(); msg = "applied"; }
    catch (err) { msg = err instanceof ApiError ? err.message : "apply failed"; }
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
    <span class="muted">active node: {status?.active_node_id ?? "—"}</span>
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

<div class="card">
  <table class="table">
    <thead><tr><th>id</th><th>name</th><th>address</th><th>port</th><th>TCP</th><th>real</th><th></th></tr></thead>
    <tbody>
      {#each nodes as n (n.id)}
        {@const h = health[n.id]}
        <tr class:active={n.id === status?.active_node_id}>
          <td>{n.id}</td>
          <td>{n.name}{#if n.stale} <em class="muted">(stale)</em>{/if}</td>
          <td>{n.address}</td><td>{n.port}</td>
          <td>{#if h?.last_tcp_ok}<span class="ok">✓ {h.last_tcp_ms}ms</span>{:else if h && h.last_tcp_ok === false}<span class="bad">✕</span>{:else}—{/if}</td>
          <td>{#if h?.last_real_ok}<span class="ok">✓ {h.last_real_ms}ms</span>{:else if h && h.last_real_ok === false}<span class="bad">✕</span>{:else}—{/if}</td>
          <td><button class="btn" onclick={() => connect(n.id)}>Connect</button></td>
        </tr>
      {/each}
    </tbody>
  </table>
</div>

<style>
  .toolbar .spacer { margin-left: auto; }
  .ok { color: var(--success); }
  .bad { color: var(--danger); }
  tr.active { font-weight: 600; }
  tr.active td:first-child { box-shadow: inset 2px 0 0 var(--accent); }
  em { font-style: normal; }
</style>
