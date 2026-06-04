<script lang="ts">
  import { api, ApiError, type Network } from "./api";
  import { networkView } from "./network";
  import { serverNow } from "./status.svelte";
  import { agoLabel } from "./dashboard";
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
    const s = Math.max(0, Math.floor((serverNow() - t) / 1000));   // D4: Pi-clock aligned
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
        segment_iface: s.iface, segment_ip: s.ip, segment_ip6: s.ip6,
        dhcp_start: s.dhcp_start, dhcp_end: s.dhcp_end, dhcp_lease: s.dhcp_lease,
        client_dns: s.client_dns, kill_switch_enabled: net.kill_switch_enabled,
        ipv6_enabled: net.ipv6_enabled,
      });
      editOpen = false;
      msg = "saved · network rules applied";   // A3: don't claim a DHCP apply we don't do
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
    {#if v.wan_blocked}
      <div class="wan-banner" role="status">
        <span class="dot bad"></span> WAN blocked — kill-switch is holding traffic (tunnel down). No leak.
      </div>
    {/if}
    <div class="status">
      <span><span class="dot {v.segment.tone}"></span> Segment: {v.segment.label}</span>
      <span><span class="dot {v.uplink.tone}"></span> Uplink: {v.uplink.label}</span>
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
      {#if net.kill_switch_enabled && !v.wan_blocked}<span><span class="dot ok"></span> Kill-switch on</span>{/if}
      {#if net.ipv6_enabled}<span><span class="dot ok"></span> IPv6: tunneled{#if net.status.ipv6_prefix} <span class="mono fresh">({net.status.ipv6_prefix})</span>{/if}</span>{/if}
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
    {#if net.events?.length}
      <details class="events">
        <summary>Recent connection events ({net.events.length})</summary>
        <ul>
          {#each [...net.events].reverse().slice(0, 12) as e (e.ts + e.kind + e.detail)}
            <li><span class="ev ev-{e.kind}">{e.kind}</span> {e.detail}<span class="fresh"> · {agoLabel(e.ts, serverNow() / 1000)}</span></li>
          {/each}
        </ul>
      </details>
    {/if}
  {/if}
</div>

{#if editOpen && net}
  <Modal title="Network — client segment" onClose={() => (editOpen = false)}>
    <div class="net-form">
      <label class="field"><span>Segment interface</span><input class="input" bind:value={net.segment.iface} /></label>
      <label class="field"><span>Segment IP (gateway)</span><input class="input" bind:value={net.segment.ip} /></label>
      <p class="muted hint dhcp-note">DHCP range / lease / client-DNS are served by the host
        (<code>pi-gw-dhcp.service</code>) — saved here for reference, but applied on the host, not by
        the panel. The segment interface &amp; IP above take effect live.</p>
      <label class="field"><span>DHCP start <small>(host)</small></span><input class="input" bind:value={net.segment.dhcp_start} /></label>
      <label class="field"><span>DHCP end <small>(host)</small></span><input class="input" bind:value={net.segment.dhcp_end} /></label>
      <label class="field"><span>DHCP lease <small>(host)</small></span><input class="input" bind:value={net.segment.dhcp_lease} /></label>
      <label class="field"><span>Client DNS <small>(host)</small></span><input class="input" bind:value={net.segment.client_dns} /></label>
      <div class="check">
        <Toggle checked={net.kill_switch_enabled}
                onchange={(v) => { if (net) net.kill_switch_enabled = v; }} label="kill-switch" />
        <span>Kill-switch — fail closed: drops client→WAN that isn't tunnelled, and <strong>keeps
          blocking even when you stop the tunnel</strong> (incl. IPv6). Clients lose internet rather
          than leak.</span>
      </div>
      <div class="check">
        <Toggle checked={net.ipv6_enabled}
                onchange={(v) => { if (net) net.ipv6_enabled = v; }} label="ipv6" />
        <span>IPv6 (tunnel) — carry segment client IPv6 through the tunnel. Off keeps v6 blocked
          (no leak). Requires a router-delegated /64 + host RA on the segment — see <strong>Router
          setup</strong> below.</span>
      </div>
      {#if net.ipv6_enabled}
        <label class="field"><span>Segment IPv6 /64 <small>(static, or <code>auto</code> for DHCPv6-PD)</small></span>
          <input class="input mono" bind:value={net.segment.ip6} placeholder="2001:db8:0:2::1/64  ·  auto" /></label>
        {#if net.segment.ip6.trim().toLowerCase() === "auto"}
          <p class="muted hint">DHCPv6-PD: a host PD client (odhcp6c / dhcpcd&nbsp;-6) acquires the prefix —
            {#if net.status.ipv6_prefix}delegated <span class="mono">{net.status.ipv6_prefix}</span>.{:else}none observed yet (set up the PD client + RA on the host).{/if}</p>
        {/if}
      {/if}
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
  .wan-banner {
    display: flex; align-items: center; gap: 0.5rem; margin-top: 0.5rem;
    padding: 0.5rem 0.7rem; border-radius: var(--radius); font-size: 0.84rem; font-weight: 500;
    color: var(--danger); border: 1px solid color-mix(in srgb, var(--danger) 40%, var(--border));
    background: color-mix(in srgb, var(--danger) 9%, transparent);
  }
  .status {
    display: flex; flex-wrap: wrap; gap: 0.5rem 1.4rem; margin-top: 0.5rem;
    font-size: 0.84rem; font-variant-numeric: tabular-nums;
  }
  .status > span { display: inline-flex; align-items: center; gap: 0.4rem; }
  .link-btn { background: none; border: none; color: var(--accent); cursor: pointer; font: inherit; font-size: 0.78rem; padding: 0 0 0 0.2rem; text-decoration: underline; text-underline-offset: 2px; }
  .fresh { color: var(--faint); }
  .clients { margin-top: 0.6rem; font-size: 0.8rem; }
  .events { margin-top: 0.7rem; font-size: 0.82rem; }
  .events summary { cursor: pointer; color: var(--muted); }
  .events ul { list-style: none; margin: 0.4rem 0 0; padding: 0; display: grid; gap: 0.25rem; }
  .events li { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.35rem; }
  .ev {
    font-family: var(--mono); font-size: 0.66rem; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.03em; padding: 0.04rem 0.4rem; border-radius: 999px;
    background: var(--surface-2); border: 1px solid var(--border); color: var(--muted);
  }
  .ev-connect { color: var(--success); border-color: color-mix(in srgb, var(--success) 40%, var(--border)); }
  .ev-failover, .ev-xray-restart { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 40%, var(--border)); }
  .ev-disconnect, .ev-xray-stop { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 40%, var(--border)); }
  .net-form { display: grid; gap: 0.6rem; }
  .dhcp-note { margin: 0.1rem 0 -0.1rem; }
  .field span small { color: var(--faint); font-weight: 400; }
  .check { display: flex; gap: 0.6rem; align-items: flex-start; }
  .recs { margin: 0.3rem 0 0; padding-left: 1.2rem; display: grid; gap: 0.4rem; }
  .hint { margin: 0.2rem 0; font-size: 0.8rem; }
  .hint-block { border-top: 1px solid var(--border); padding-top: 0.6rem; }
  .actions { display: flex; gap: 0.5rem; }
</style>
