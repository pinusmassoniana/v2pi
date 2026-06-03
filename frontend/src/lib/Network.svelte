<script lang="ts">
  import { api, ApiError, type Network } from "./api";
  import { networkView } from "./network";

  let net = $state<Network | null>(null);
  let msg = $state("");

  async function load() {
    try { net = await api.getNetwork(); }
    catch (err) { msg = err instanceof ApiError ? err.message : "load failed"; }
  }
  async function save(e: Event) {
    e.preventDefault();
    if (!net) return;
    const s = net.segment;
    try {
      net = await api.putNetwork({
        segment_iface: s.iface, segment_ip: s.ip,
        dhcp_start: s.dhcp_start, dhcp_end: s.dhcp_end, dhcp_lease: s.dhcp_lease,
        client_dns: s.client_dns, kill_switch_enabled: net.kill_switch_enabled,
      });
      msg = "saved & applied";
    } catch (err) { msg = err instanceof ApiError ? err.message : "save failed"; }
  }

  $effect(() => { load(); });
</script>

{#if msg}<p class="msg">{msg}</p>{/if}

{#if net}
  {@const v = networkView(net)}
  <form onsubmit={save} class="card net">
    <h3>Pi network (client segment)</h3>
    <label class="field"><span>Segment interface</span><input class="input" bind:value={net.segment.iface} /></label>
    <label class="field"><span>Segment IP (gateway)</span><input class="input" bind:value={net.segment.ip} /></label>
    <label class="field"><span>DHCP start</span><input class="input" bind:value={net.segment.dhcp_start} /></label>
    <label class="field"><span>DHCP end</span><input class="input" bind:value={net.segment.dhcp_end} /></label>
    <label class="field"><span>DHCP lease</span><input class="input" bind:value={net.segment.dhcp_lease} /></label>
    <label class="field"><span>Client DNS</span><input class="input" bind:value={net.segment.client_dns} /></label>
    <label class="check"><input type="checkbox" bind:checked={net.kill_switch_enabled} />
      Kill-switch — fail closed, drop client→WAN traffic that isn't tunnelled</label>
    <div><button class="btn btn-primary">Save &amp; apply</button></div>
  </form>

  <div class="card">
    <h3>Live status</h3>
    <div class="status">
      <span><span class="dot {v.segment.tone}"></span> Segment: {v.segment.label}</span>
      <span><span class="dot {v.dhcp_clients > 0 ? 'ok' : 'unknown'}"></span> DHCP clients: {v.dhcp_clients}</span>
      <span><span class="dot {v.tunnel.tone}"></span> Tunnel egress: {v.tunnel.egress} ({v.tunnel.latency})</span>
    </div>
    <div><button class="btn" type="button" onclick={load}>Refresh</button></div>
  </div>

  <div class="card">
    <h3>Router setup checklist</h3>
    <p class="muted hint">The panel never touches your router — set these there, then confirm green above.</p>
    <ol class="recs">
      {#each net.recommendations as r (r.title)}
        <li><strong>{r.title}</strong> — {r.detail}</li>
      {/each}
    </ol>
  </div>
{/if}

<style>
  .net { max-width: 34rem; }
  .status { display: grid; gap: 0.4rem; }
  .recs { margin: 0; padding-left: 1.2rem; display: grid; gap: 0.4rem; }
  .hint { margin: 0; font-size: 0.8rem; }
</style>
