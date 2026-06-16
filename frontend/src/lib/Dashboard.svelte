<script lang="ts">
  import { api, ApiError, type Node, type TrafficMessage, type Network, type Subscription, type Routing } from "./api";
  import TrafficGraph from "./TrafficGraph.svelte";
  import ConnFlow from "./ConnFlow.svelte";
  import Alert from "./Alert.svelte";
  import { confirmDialog } from "./confirm.svelte";
  import { statusStore, subscribeStatus, pollStatusOnce, serverNow } from "./status.svelte";
  import { subWarnings, agoLabel } from "./dashboard";
  import { flagEmoji } from "./flag";
  import { fmtRate, fmtBytes } from "./format";

  const UNTUNNELED_WARN_BPS = 50_000;   // D10: throughput leaking outside the tunnel above this → warn

  const status = $derived(statusStore.value);   // shared poller (no duplicate /api/status)
  let nodes = $state<Node[]>([]);
  let samples = $state<{ ts: number; up: number; down: number }[]>([]);
  let live = $state<TrafficMessage | null>(null);
  let disabled = $state(false);
  let msg = $state("");
  let net = $state<Network | null>(null);       // polled directly (status strip, summaries, ConnFlow, events)
  let routing = $state<Routing | null>(null);    // routing summary card
  let subs = $state<Subscription[]>([]);          // E: expiry / data-cap warnings
  let tick = $state(0);                            // 1s heartbeat → live uptime / freshness labels
  // U3: remember the dismissed failover across reloads
  let failoverDismissed = $state<number | null>(Number(localStorage.getItem("failoverDismissed")) || null);

  let activeName = $derived(nodes.find((n) => n.id === status?.active_node_id)?.name ?? null);
  let activeNode = $derived(nodes.find((n) => n.id === status?.active_node_id) ?? null);
  let liveFrame = $derived(live && !("disabled" in live) && !("error" in live) ? live : null);
  let liveActive = $derived(liveFrame ? liveFrame.active : null);
  let liveTotals = $derived(liveFrame ? liveFrame.totals : null);
  let dataUp = $derived(liveFrame?.lifetime?.up ?? liveTotals?.up ?? null);
  let dataDown = $derived(liveFrame?.lifetime?.down ?? liveTotals?.down ?? null);
  let liveDirect = $derived(liveFrame ? (liveFrame.outbounds.direct ?? { up_bps: 0, down_bps: 0 }) : { up_bps: 0, down_bps: 0 });
  let latest = $derived(samples.length ? samples[samples.length - 1] : { up: 0, down: 0 });
  let clients = $derived(net?.status.dhcp_clients ?? null);
  let sessionUp = $derived(liveFrame?.session?.up ?? null);
  let sessionDown = $derived(liveFrame?.session?.down ?? null);
  let directDown = $derived(liveDirect.down_bps);
  let directUp = $derived(liveDirect.up_bps);
  let xrayError = $derived(status?.xray_state === "error");
  let killArmed = $derived(net?.kill_switch_enabled ?? null);
  // A: surface an auto-failover from the last 24h until the operator dismisses it
  let failover = $derived(
    status?.last_failover_at && status.last_failover_at !== failoverDismissed
      && serverNow() / 1000 - status.last_failover_at < 86400
      ? status.last_failover_at : null);
  let warnings = $derived(subWarnings(subs, serverNow() / 1000));

  // DHCP pool size from the segment range (last octet of start/end on the same /24) — real "n / N leases"
  let poolSize = $derived.by(() => {
    const s = net?.segment.dhcp_start, e = net?.segment.dhcp_end;
    if (!s || !e) return null;
    const a = Number(s.split(".").pop()), b = Number(e.split(".").pop());
    return Number.isFinite(a) && Number.isFinite(b) && b >= a ? b - a + 1 : null;
  });

  // live uptime "Xd HH:MM:SS" (ticks every second via `tick`)
  function fmtUptime(since: number | null | undefined): string {
    if (!since) return "—";
    const s = Math.max(0, Math.floor(serverNow() / 1000 - since));
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    const hms = `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}`;
    return d > 0 ? `${d}d ${hms}` : hms;
  }
  let uptimeStr = $derived((tick, fmtUptime(status?.active_since)));

  function freshness(iso: string | null | undefined): string {
    if (!iso) return "";
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return "";
    const s = Math.max(0, Math.floor((serverNow() - t) / 1000));   // D4: Pi-clock aligned
    if (s < 60) return `${s}s ago`;
    const m = Math.floor(s / 60);
    return m < 60 ? `${m}m ago` : `${Math.floor(m / 60)}h ago`;
  }
  // HH:MM:SS local time from an epoch-seconds event timestamp
  function clockTime(ts: number): string {
    const dt = new Date(ts * 1000);
    return [dt.getHours(), dt.getMinutes(), dt.getSeconds()].map((n) => String(n).padStart(2, "0")).join(":");
  }
  // event kind → [level] token + colour class
  function evLevel(kind: string): "ok" | "warn" | "err" | "info" {
    const k = (kind || "").toLowerCase();
    if (/ok|up|recover|restore|connect|apply|appl/.test(k)) return "ok";
    if (/err|fail|drop|down|leak/.test(k)) return "err";
    if (/warn|degrad|stale|retry|timeout/.test(k)) return "warn";
    return "info";
  }
  // routing action → colour class (BLOCK=err, DIRECT=acc, PROXY=down)
  function actClass(a: string): string {
    const k = (a || "").toLowerCase();
    if (k.includes("block")) return "act-block";
    if (k.includes("direct")) return "act-direct";
    return "act-proxy";
  }
  let ruleSummary = $derived((routing?.rules ?? []).filter((r) => r.enabled).slice(0, 4));

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

  // re-pull recorded history and merge by timestamp (dedup) so the graph fills in after a
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

  // N4: 24h/7d windows ride the durable per-minute series (the live ring only spans ~1h).
  let graphWindow = $state(600);
  let longSamples = $state<{ ts: number; up: number; down: number }[]>([]);
  async function loadLong(sec: number) {
    try {
      const h = await api.getTrafficHistory(sec, 1200);
      longSamples = h.samples.map(([ts, up, down]) => ({ ts, up, down }));
    } catch {}
  }
  function onGraphWindow(sec: number) {
    graphWindow = sec;
    if (sec > 3600) loadLong(sec);
  }
  $effect(() => {
    if (graphWindow <= 3600) return;
    const sec = graphWindow;
    const t = setInterval(() => { if (document.visibilityState === "visible") loadLong(sec); }, 60000);
    return () => clearInterval(t);
  });

  // shared status poller + a nodes refresh, both paused while the tab is hidden
  $effect(() => {
    const stop = subscribeStatus(3000);
    refresh();
    const t = setInterval(() => { if (document.visibilityState === "visible") refresh(); }, 3000);
    return () => { clearInterval(t); stop(); };
  });

  // network poll — drives the status strip (kill-switch/clients), ConnFlow, network summary, event log
  $effect(() => {
    const load = () => api.getNetwork().then((n) => (net = n)).catch(() => {});
    load();
    const t = setInterval(() => { if (document.visibilityState === "visible") load(); }, 4000);
    return () => clearInterval(t);
  });

  // routing summary — rules change rarely, poll slowly
  $effect(() => {
    const load = () => api.getRouting().then((r) => (routing = r)).catch(() => {});
    load();
    const t = setInterval(() => { if (document.visibilityState === "visible") load(); }, 30000);
    return () => clearInterval(t);
  });

  // E: subscription expiry / data-cap warnings — subs change rarely, poll slowly
  $effect(() => {
    const load = () => api.listSubs().then((s) => (subs = s)).catch(() => {});
    load();
    const t = setInterval(() => { if (document.visibilityState === "visible") load(); }, 30000);
    return () => clearInterval(t);
  });

  // 1s heartbeat for live uptime / freshness, paused while hidden
  $effect(() => {
    const t = setInterval(() => { if (document.visibilityState === "visible") tick++; }, 1000);
    return () => clearInterval(t);
  });

  // seed the graph with recorded history so the full window shows immediately on open
  $effect(() => { seedHistory(); });

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

  // upstream-health rows: active node first (real probe), the rest as standby (no fake latencies)
  let healthRows = $derived.by(() => {
    const act = activeNode;
    const rest = nodes.filter((n) => n.id !== act?.id);
    return [...(act ? [act] : []), ...rest].slice(0, 5);
  });
</script>

<!-- failover banner + subscription warnings (kept) -->
{#if failover}
  <div class="banner warn" role="status">
    <span>⚠ Auto-failover {agoLabel(failover, serverNow() / 1000)} — the gateway switched node on its own.</span>
    <button class="banner-x" onclick={() => { failoverDismissed = failover; if (failover) localStorage.setItem("failoverDismissed", String(failover)); }} aria-label="Dismiss">✕</button>
  </div>
{/if}
{#if warnings.length}
  <div class="chips">
    {#each warnings as w (w.name + w.text)}
      <span class="chip {w.level}" title="Subscription {w.name}">{w.name}: {w.text}</span>
    {/each}
  </div>
{/if}

<!-- ===== status strip ===== -->
<div class="strip">
  <div class="cell">
    <div class="cell-k">GATEWAY</div>
    <div class="cell-v">
      <span class="sdot" class:on={status?.running} class:warn={xrayError} class:bad={status?.running === false && !xrayError}></span>
      <span class="big" class:acc={status?.running} class:err={status?.running === false && !xrayError} class:warnc={xrayError}>
        {status?.running ? "UP" : xrayError ? "RECONNECTING" : status?.running === false ? "DOWN" : "—"}
      </span>
    </div>
  </div>
  <div class="cell">
    <div class="cell-k">KILL-SWITCH</div>
    <div class="cell-v">
      <span class="sdot" class:on={killArmed} class:bad={killArmed === false}></span>
      <span class="big" class:err={killArmed === false}>{killArmed == null ? "—" : killArmed ? "ARMED" : "OPEN"}</span>
    </div>
  </div>
  <div class="cell">
    <div class="cell-k">ACTIVE NODE</div>
    <div class="cell-v">
      {#if liveActive?.egress_cc}<span class="flagchip" title={liveActive.egress_cc}>{flagEmoji(liveActive.egress_cc)}</span>{/if}
      <span class="mid">{activeName ?? "—"}</span>
    </div>
  </div>
  <div class="cell">
    <div class="cell-k">UPTIME</div>
    <div class="cell-v"><span class="mid mono" aria-live="off">{status?.running ? uptimeStr : "—"}</span></div>
  </div>
  <div class="cell">
    <div class="cell-k">CLIENTS</div>
    <div class="cell-v">
      <span class="big">{clients ?? "—"}</span>
      {#if clients !== null && poolSize}<span class="cell-sub">/ {poolSize} leases</span>{/if}
    </div>
  </div>
</div>

<!-- ===== main row: live traffic | upstream health ===== -->
<div class="main-row">
  <div class="card traffic">
    <div class="card-top">
      <div class="row-tight">
        <span class="eyebrow">Live Traffic</span>
        <span class="ws-pill" class:off={disabled}>● {disabled ? "off" : "ws"}</span>
      </div>
    </div>
    <div class="readouts">
      <div class="ro">
        <div class="ro-k"><span class="swatch up"></span>DOWNLOAD ↓</div>
        <div class="ro-v up">{disabled ? "—" : fmtRate(latest.down)}</div>
      </div>
      <div class="ro">
        <div class="ro-k"><span class="swatch down"></span>UPLOAD ↑</div>
        <div class="ro-v down">{disabled ? "—" : fmtRate(latest.up)}</div>
      </div>
      <div class="ro session">
        <div class="ro-k">SESSION TOTAL</div>
        <div class="ro-sess mono">
          ↓ {(sessionDown ?? dataDown) !== null ? fmtBytes(sessionDown ?? dataDown!) : "—"}
          · ↑ {(sessionUp ?? dataUp) !== null ? fmtBytes(sessionUp ?? dataUp!) : "—"}
        </div>
      </div>
    </div>
    {#if disabled}
      <p class="msg">Traffic stats disabled — enable in Settings.</p>
    {:else}
      <TrafficGraph series={graphWindow > 3600 ? longSamples : samples} onwindow={onGraphWindow} />
    {/if}
    {#if liveDirect && (liveDirect.down_bps > 0 || liveDirect.up_bps > 0)}
      <div class="direct-note" class:warn={liveDirect.down_bps + liveDirect.up_bps > UNTUNNELED_WARN_BPS}
           title="Traffic leaving the segment WITHOUT going through the tunnel">
        untunneled ↓ {fmtRate(liveDirect.down_bps)} · ↑ {fmtRate(liveDirect.up_bps)}
      </div>
    {/if}
  </div>

  <div class="card health">
    <div class="card-top">
      <span class="eyebrow">Upstream Health</span>
      <span class="pill" class:ok={status?.running}>{status?.running ? "FAILOVER READY" : "OFFLINE"}</span>
    </div>
    <div class="health-list">
      {#each healthRows as n (n.id)}
        {@const isActive = n.id === activeNode?.id}
        {@const degraded = isActive && liveActive?.real_ok === false}
        <div class="hrow" class:active={isActive}>
          <span class="sdot" class:on={isActive && !degraded} class:warn={degraded} class:idle={!isActive}></span>
          <div class="hrow-main">
            <div class="hrow-name">{n.name}</div>
            <div class="hrow-sub">{n.transport || n.network || "—"}{#if isActive && liveActive?.egress_cc} · {liveActive.egress_cc}{/if}</div>
          </div>
          <div class="hrow-right">
            {#if isActive && liveActive?.latency_ms != null && liveActive.real_ok}
              <div class="hrow-lat" class:warnc={degraded}>{liveActive.latency_ms}ms</div>
              <div class="hrow-state acc">active</div>
            {:else if isActive && degraded}
              <div class="hrow-lat warnc">probe failed</div>
              <div class="hrow-state warnc">degraded</div>
            {:else if isActive}
              <div class="hrow-lat dim">—</div>
              <div class="hrow-state acc">active</div>
            {:else}
              <div class="hrow-lat dim">—</div>
              <div class="hrow-state dim">standby</div>
            {/if}
          </div>
        </div>
      {/each}
      {#if !healthRows.length}<p class="msg">No nodes configured.</p>{/if}
    </div>
  </div>
</div>

<!-- ===== connection diagram (kept from the original dashboard, restyled) ===== -->
<ConnFlow
    running={!!status?.running}
    segmentUp={net?.status.segment_up ?? null}
    {clients}
    segmentIp={net?.segment.ip ?? null}
    segmentIface={net?.segment.iface ?? null}
    nodeName={activeName}
    realOk={liveActive?.real_ok ?? null}
    latencyMs={liveActive?.latency_ms ?? null}
    egressIp={liveActive?.egress_ip ?? null}
    egressCc={liveActive?.egress_cc ?? null}
    uplink={net?.status.uplink ?? null}
    uplink6={net?.status.uplink6 ?? null}
    ipv6Enabled={!!net?.ipv6_enabled}
    proxyDown={latest.down}
    proxyUp={latest.up}
    {directDown}
    {directUp}
  />

<!-- ===== bottom row: routing | network | event log ===== -->
<div class="bottom-row">
  <div class="card">
    <div class="card-top">
      <span class="eyebrow">Routing</span>
      <span class="muted-sm">{routing ? `${routing.rules.length} rules` : "—"}</span>
    </div>
    <div class="rules">
      {#each ruleSummary as r (r.id)}
        <div class="rule"><span class="rule-act {actClass(r.action)}">{r.action.toUpperCase()}</span><span class="rule-val">{r.type ? r.type + ":" : ""}{r.value}</span></div>
      {/each}
      {#if routing}
        <div class="rule"><span class="rule-act {actClass(routing.default_action)}">{routing.default_action.toUpperCase()}</span><span class="rule-val">default → {activeName ?? "—"}</span></div>
      {:else if !ruleSummary.length}
        <p class="msg">No rules.</p>
      {/if}
    </div>
  </div>

  <div class="card">
    <div class="card-top">
      <span class="eyebrow">Network</span>
      <span class="acc-sm">nftables · tproxy</span>
    </div>
    {#if net}
      <div class="kv">
        <div><span class="kv-k">segment</span><span class="kv-v">{net.segment.ip || "—"}{net.segment.iface ? ` · ${net.segment.iface}` : ""}</span></div>
        <div><span class="kv-k">dhcp pool</span><span class="kv-v">{net.segment.dhcp_start || "—"} – {net.segment.dhcp_end || "—"}</span></div>
        <div><span class="kv-k">client dns</span><span class="kv-v">{net.segment.client_dns || "—"}</span></div>
        <div><span class="kv-k">ipv6</span><span class="kv-v">{net.ipv6_enabled ? (net.status.ipv6_prefix_source ?? "on") : "off"}</span></div>
      </div>
    {:else}
      <p class="msg">Loading…</p>
    {/if}
  </div>

  <div class="card">
    <div class="card-top">
      <span class="eyebrow">Event Log</span>
      <span class="muted-sm">tail -f</span>
    </div>
    <div class="events">
      {#each (net?.events ?? []).slice(0, 6) as e (e.ts + e.detail)}
        <div class="ev"><span class="ev-t mono">{clockTime(e.ts)}</span><span class="ev-l {evLevel(e.kind)}">[{e.kind}]</span><span class="ev-d">{e.detail}</span></div>
      {/each}
      {#if !(net?.events ?? []).length}<p class="msg">No recent events.</p>{/if}
    </div>
  </div>
</div>

<Alert {msg} />

<style>
  /* ===== status strip — 5 cells joined by 1px hairlines ===== */
  .strip {
    display: grid; grid-template-columns: repeat(5, 1fr); gap: 1px;
    background: var(--bd); border: 1px solid var(--bd); border-radius: 10px; overflow: hidden;
  }
  .cell { background: var(--bg1); padding: 0.8rem 1rem; display: grid; gap: 0.45rem; min-width: 0; }
  .cell-k { font-size: 0.66rem; letter-spacing: 0.14em; color: var(--tx3); }
  .cell-v { display: flex; align-items: center; gap: 0.45rem; min-width: 0; }
  .big { font-size: 1.15rem; font-weight: 600; line-height: 1; }
  .mid { font-size: 0.95rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .cell-sub { font-size: 0.72rem; color: var(--tx3); font-weight: 400; }
  .acc { color: var(--acc); } .err { color: var(--err); } .warnc { color: var(--warn); }
  .flagchip { font-size: 0.85rem; line-height: 1; }
  .sdot { width: 8px; height: 8px; border-radius: 50%; flex: none; background: var(--tx3); }
  .sdot.on { background: var(--acc); box-shadow: 0 0 8px var(--acc); }
  .sdot.bad { background: var(--err); }
  .sdot.warn { background: var(--warn); box-shadow: 0 0 7px var(--warn); animation: v2pulse 1.6s infinite; }
  .sdot.idle { background: var(--acc); box-shadow: none; }

  /* ===== layout rows ===== */
  .main-row { display: grid; grid-template-columns: 2fr 1fr; gap: 0.9rem; }
  .bottom-row { display: grid; grid-template-columns: 1fr 1fr 1.3fr; gap: 0.9rem; }
  .card-top { display: flex; align-items: center; justify-content: space-between; gap: 0.6rem; }
  .row-tight { display: flex; align-items: center; gap: 0.6rem; }
  .muted-sm { font-size: 0.72rem; color: var(--tx2); }
  .acc-sm { font-size: 0.72rem; color: var(--acc); }

  /* pills */
  .ws-pill, .pill { font-size: 0.64rem; color: var(--acc); border: 1px solid var(--acc); border-radius: 20px; padding: 0.05rem 0.5rem; white-space: nowrap; }
  .ws-pill { animation: v2pulse 2.2s infinite; }
  .ws-pill.off { color: var(--tx3); border-color: var(--bd2); animation: none; }
  .pill { color: var(--tx3); border-color: var(--bd2); }
  .pill.ok { color: var(--acc); border-color: var(--acc); }

  /* ===== live traffic ===== */
  .traffic { gap: 0.7rem; }
  .readouts { display: flex; gap: 1.8rem; align-items: flex-end; flex-wrap: wrap; }
  .ro-k { display: flex; align-items: center; gap: 0.4rem; font-size: 0.66rem; color: var(--tx3); letter-spacing: 0.04em; margin-bottom: 0.25rem; }
  .swatch { width: 9px; height: 2px; border-radius: 2px; }
  .swatch.up { background: var(--up); } .swatch.down { background: var(--down); }
  .ro-v { font-size: 1.6rem; font-weight: 600; line-height: 1; font-family: var(--mono); font-variant-numeric: tabular-nums; }
  .ro-v.up { color: var(--up); } .ro-v.down { color: var(--down); }
  .ro.session { margin-left: auto; text-align: right; }
  .ro-sess { font-size: 0.82rem; color: var(--tx2); }
  .direct-note { font-family: var(--mono); font-variant-numeric: tabular-nums; font-size: 0.72rem; color: var(--tx2); }
  .direct-note.warn { color: var(--warn); font-weight: 600; }

  /* ===== upstream health ===== */
  .health { gap: 0.6rem; }
  .health-list { display: flex; flex-direction: column; gap: 0.5rem; }
  .hrow { display: flex; align-items: center; gap: 0.65rem; background: var(--bg2); border: 1px solid var(--bd); border-radius: 8px; padding: 0.55rem 0.7rem; }
  .hrow.active { border-color: var(--acc); box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--acc) 18%, transparent); }
  .hrow-main { flex: 1; min-width: 0; }
  .hrow-name { font-size: 0.8rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .hrow-sub { font-size: 0.66rem; color: var(--tx3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .hrow-right { text-align: right; }
  .hrow-lat { font-size: 0.82rem; font-weight: 600; font-family: var(--mono); color: var(--acc); }
  .hrow-lat.dim { color: var(--tx2); } .hrow-lat.warnc { color: var(--warn); }
  .hrow-state { font-size: 0.62rem; color: var(--acc); }
  .hrow-state.dim { color: var(--tx3); } .hrow-state.warnc { color: var(--warn); }

  /* ===== routing summary ===== */
  .rules { display: flex; flex-direction: column; gap: 0.45rem; }
  .rule { display: flex; align-items: center; gap: 0.6rem; font-size: 0.78rem; }
  .rule-act { width: 3rem; font-weight: 600; flex: none; }
  .act-block { color: var(--err); } .act-direct { color: var(--acc); } .act-proxy { color: var(--down); }
  .rule-val { color: var(--tx2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  /* ===== network summary ===== */
  .kv { display: flex; flex-direction: column; gap: 0.5rem; }
  .kv > div { display: flex; justify-content: space-between; gap: 0.6rem; font-size: 0.78rem; }
  .kv-k { color: var(--tx3); } .kv-v { color: var(--tx2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  /* ===== event log ===== */
  .events { display: flex; flex-direction: column; gap: 0.4rem; }
  .ev { display: flex; gap: 0.5rem; font-size: 0.72rem; line-height: 1.3; }
  .ev-t { color: var(--tx3); flex: none; }
  .ev-l { flex: none; }
  .ev-l.ok { color: var(--acc); } .ev-l.warn { color: var(--warn); } .ev-l.err { color: var(--err); } .ev-l.info { color: var(--down); }
  .ev-d { color: var(--tx2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  /* banners + chips (failover / subscription warnings) */
  .banner { display: flex; align-items: center; gap: 0.6rem; padding: 0.55rem 0.85rem; border-radius: var(--radius);
    border: 1px solid var(--warn); background: color-mix(in srgb, var(--warn) 12%, var(--bg1)); font-size: 0.82rem; }
  .banner span { margin-right: auto; }
  .banner-x { background: none; border: none; color: var(--tx2); cursor: pointer; font: inherit; padding: 0 0.2rem; }
  .banner-x:hover { color: var(--tx); }
  .chips { display: flex; flex-wrap: wrap; gap: 0.45rem; }
  .chip { font-size: 0.72rem; font-weight: 600; padding: 0.2rem 0.55rem; border-radius: 999px; border: 1px solid var(--bd); }
  .chip.warn { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 45%, var(--bd)); background: color-mix(in srgb, var(--warn) 10%, transparent); }
  .chip.bad { color: var(--err); border-color: color-mix(in srgb, var(--err) 45%, var(--bd)); background: color-mix(in srgb, var(--err) 10%, transparent); }

  @media (max-width: 1100px) {
    .main-row { grid-template-columns: 1fr; }
    .bottom-row { grid-template-columns: 1fr; }
  }
  @media (max-width: 680px) {
    .strip { grid-template-columns: repeat(2, 1fr); }
  }
</style>
