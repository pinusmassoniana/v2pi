<script lang="ts">
  import { api, ApiError, type Network } from "./api";
  import { networkView } from "./network";
  import Modal from "./Modal.svelte";
  import Toggle from "./Toggle.svelte";

  let net = $state<Network | null>(null);
  let msg = $state("");
  let editOpen = $state(false);
  let showClients = $state(false);

  function freshness(iso: string | null): string {
    if (!iso) return "";
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return "";
    const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (s < 60) return `${s}s ago`;
    const m = Math.floor(s / 60);
    return m < 60 ? `${m}m ago` : `${Math.floor(m / 60)}h ago`;
  }

  async function load() {
    try { net = await api.getNetwork(); }
    catch (err) { msg = err instanceof ApiError ? err.message : "load failed"; }
  }
  async function save() {
    if (!net) return;
    const s = net.segment;
    try {
      net = await api.putNetwork({
        segment_iface: s.iface, segment_ip: s.ip,
        dhcp_start: s.dhcp_start, dhcp_end: s.dhcp_end, dhcp_lease: s.dhcp_lease,
        client_dns: s.client_dns, kill_switch_enabled: net.kill_switch_enabled,
      });
      editOpen = false; msg = "saved & applied";
    } catch (err) { msg = err instanceof ApiError ? err.message : "save failed"; }
  }

  // live: poll status; pause while the Edit modal is open so a background reload
  // never clobbers the fields the user is editing (they two-way-bind net.segment.*)
  $effect(() => {
    load();
    const t = setInterval(() => { if (!editOpen) load(); }, 5000);
    return () => clearInterval(t);
  });
</script>

<div class="card">
  <div class="net-head">
    <h3>Network</h3>
    <button class="btn" onclick={() => (editOpen = true)} disabled={!net}>Edit</button>
  </div>
  {#if msg}<p class="msg">{msg}</p>{/if}
  {#if net}
    {@const v = networkView(net)}
    <div class="status">
      <span><span class="dot {v.segment.tone}"></span> Segment: {v.segment.label}</span>
      <span>
        <span class="dot {v.dhcp_clients > 0 ? 'ok' : 'unknown'}"></span> DHCP clients: {v.dhcp_clients}
        {#if net.status.clients?.length}
          <button class="link-btn" onclick={() => (showClients = !showClients)}>{showClients ? "hide" : "show"}</button>
        {/if}
      </span>
      <span>
        <span class="dot {v.tunnel.tone}"></span> Tunnel egress: {v.tunnel.egress} ({v.tunnel.latency})
        {#if net.status.tunnel.checked_at}<span class="fresh">· {freshness(net.status.tunnel.checked_at)}</span>{/if}
      </span>
      {#if net.kill_switch_enabled}<span><span class="dot ok"></span> Kill-switch on</span>{/if}
    </div>
    {#if showClients && net.status.clients?.length}
      <table class="table clients">
        <thead><tr><th>device</th><th>IP</th><th>MAC</th></tr></thead>
        <tbody>
          {#each net.status.clients ?? [] as c (c.mac + c.ip)}
            <tr><td>{c.hostname || "—"}</td><td class="mono">{c.ip}</td><td class="mono">{c.mac}</td></tr>
          {/each}
        </tbody>
      </table>
    {/if}
  {/if}
</div>

{#if editOpen && net}
  <Modal title="Network — client segment" onClose={() => (editOpen = false)}>
    <div class="net-form">
      <label class="field"><span>Segment interface</span><input class="input" bind:value={net.segment.iface} /></label>
      <label class="field"><span>Segment IP (gateway)</span><input class="input" bind:value={net.segment.ip} /></label>
      <label class="field"><span>DHCP start</span><input class="input" bind:value={net.segment.dhcp_start} /></label>
      <label class="field"><span>DHCP end</span><input class="input" bind:value={net.segment.dhcp_end} /></label>
      <label class="field"><span>DHCP lease</span><input class="input" bind:value={net.segment.dhcp_lease} /></label>
      <label class="field"><span>Client DNS</span><input class="input" bind:value={net.segment.client_dns} /></label>
      <div class="check">
        <Toggle checked={net.kill_switch_enabled}
                onchange={(v) => { if (net) net.kill_switch_enabled = v; }} label="kill-switch" />
        <span>Kill-switch — fail closed, drop client→WAN traffic that isn't tunnelled</span>
      </div>
      <div class="hint-block">
        <strong>Router setup</strong>
        <p class="muted hint">The panel never touches your router — set these there, then confirm green.</p>
        <ol class="recs">
          {#each net.recommendations as r (r.title)}<li><strong>{r.title}</strong> — {r.detail}</li>{/each}
        </ol>
      </div>
      <div class="actions">
        <button class="btn btn-primary" onclick={save}>Save &amp; apply</button>
        <button class="btn" onclick={() => (editOpen = false)}>Cancel</button>
      </div>
    </div>
  </Modal>
{/if}

<style>
  .net-head { display: flex; align-items: center; gap: 0.5rem; }
  .net-head h3 { margin: 0; margin-right: auto; font-size: 0.92rem; font-weight: 650; letter-spacing: -0.01em; }
  .status {
    display: flex; flex-wrap: wrap; gap: 0.5rem 1.4rem; margin-top: 0.5rem;
    font-size: 0.84rem; font-variant-numeric: tabular-nums;
  }
  .status > span { display: inline-flex; align-items: center; gap: 0.4rem; }
  .link-btn { background: none; border: none; color: var(--accent); cursor: pointer; font: inherit; font-size: 0.78rem; padding: 0 0 0 0.2rem; text-decoration: underline; text-underline-offset: 2px; }
  .fresh { color: var(--faint); }
  .clients { margin-top: 0.6rem; font-size: 0.8rem; }
  .net-form { display: grid; gap: 0.6rem; }
  .check { display: flex; gap: 0.6rem; align-items: center; }
  .recs { margin: 0.3rem 0 0; padding-left: 1.2rem; display: grid; gap: 0.4rem; }
  .hint { margin: 0.2rem 0; font-size: 0.8rem; }
  .hint-block { border-top: 1px solid var(--border); padding-top: 0.6rem; }
  .actions { display: flex; gap: 0.5rem; }
</style>
