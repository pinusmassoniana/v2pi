<script lang="ts">
  import { api, ApiError, type Network } from "./api";
  import { networkView } from "./network";
  import { serverNow } from "./status.svelte";
  import Toggle from "./Toggle.svelte";
  import Alert from "./Alert.svelte";

  let net = $state<Network | null>(null);
  let msg = $state("");
  let dirty = $state(false);   // pause polling while the operator is editing
  let saving = $state(false);

  async function load() {
    try { net = await api.getNetwork(); }
    catch (err) { msg = err instanceof ApiError ? err.message : "load failed"; }
  }
  async function save() {
    if (!net) return;
    saving = true;
    const s = net.segment;
    try {
      net = await api.putNetwork({
        segment_iface: s.iface, segment_ip: s.ip, segment_ip6: s.ip6,
        dhcp_start: s.dhcp_start, dhcp_end: s.dhcp_end, dhcp_lease: s.dhcp_lease,
        client_dns: s.client_dns, client_dns6: s.client_dns6,
        kill_switch_enabled: net.kill_switch_enabled, lan_access_enabled: net.lan_access_enabled,
        ipv6_enabled: net.ipv6_enabled,
      });
      dirty = false;
      msg = "saved · network + DHCP applied to host";
    } catch (err) { msg = err instanceof ApiError ? err.message : "save failed"; }
    finally { saving = false; }
  }

  // live poll; pauses while dirty (don't clobber edited fields) and while the tab is hidden
  $effect(() => {
    load();
    const t = setInterval(() => { if (!dirty && document.visibilityState === "visible") load(); }, 5000);
    return () => clearInterval(t);
  });

  function leaseAge(expiry: number): string {
    // dnsmasq lease expiry is in the future; show remaining time as a coarse label
    const s = Math.max(0, Math.floor(expiry - serverNow() / 1000));
    if (s < 3600) return `${Math.floor(s / 60)}m left`;
    if (s < 86400) return `${Math.floor(s / 3600)}h left`;
    return `${Math.floor(s / 86400)}d left`;
  }

  // Stable numeric ordering so the lease list doesn't reshuffle on every 5s poll.
  // Split on '.' (v4) or ':' (v6) and compare segment-by-segment as numbers.
  function ipKey(ip: string): number[] {
    const v6 = ip.includes(":");
    return ip.split(v6 ? ":" : ".").map((p) => parseInt(p, v6 ? 16 : 10) || 0);
  }
  function ipCompare(a: { ip: string }, b: { ip: string }): number {
    const ka = ipKey(a.ip), kb = ipKey(b.ip);
    for (let i = 0; i < Math.max(ka.length, kb.length); i++) {
      const d = (ka[i] ?? 0) - (kb[i] ?? 0);
      if (d) return d;
    }
    return 0;
  }
</script>

{#if net}
  {@const v = networkView(net)}
  <div class="net-grid">
    <!-- LEFT: segment form + router checklist -->
    <div class="col">
      <div class="card">
        <div class="card-top">
          <span class="eyebrow">Gateway Segment</span>
          <span class="chip">nftables tproxy + policy routing</span>
        </div>
        {#if v.wan_blocked}
          <div class="warn-row"><span class="sdot bad"></span> WAN blocked — kill-switch holding traffic (tunnel down). No leak.</div>
        {/if}
        {#if v.foreign_ra}
          <div class="warn-row"><span class="sdot bad"></span> Another router is advertising IPv6 on the client VLAN — clients will leak. Disable RA for this VLAN on your router.</div>
        {/if}
        <div class="form">
          <label class="fld"><span>SEGMENT INTERFACE</span><input bind:value={net.segment.iface} oninput={() => (dirty = true)} disabled={saving} /></label>
          <label class="fld"><span>GATEWAY IP / CIDR</span><input class="mono" bind:value={net.segment.ip} oninput={() => (dirty = true)} disabled={saving} /></label>
          <label class="fld"><span>DHCP RANGE START</span><input class="mono" bind:value={net.segment.dhcp_start} oninput={() => (dirty = true)} disabled={saving} /></label>
          <label class="fld"><span>DHCP RANGE END</span><input class="mono" bind:value={net.segment.dhcp_end} oninput={() => (dirty = true)} disabled={saving} /></label>
          <label class="fld"><span>CLIENT DNS</span><input class="mono" bind:value={net.segment.client_dns} oninput={() => (dirty = true)} disabled={saving} /></label>
          <label class="fld"><span>DHCP LEASE</span><input class="mono" bind:value={net.segment.dhcp_lease} oninput={() => (dirty = true)} disabled={saving} /></label>
        </div>
        <div class="opts">
          <div class="opt">
            <Toggle checked={net.lan_access_enabled} onchange={(val) => { if (net) { net.lan_access_enabled = val; dirty = true; } }} label="lan-access" />
            <span>LAN access — let segment clients reach the home LAN directly (internet still tunnel-only).</span>
          </div>
          <div class="opt">
            <Toggle checked={net.ipv6_enabled} onchange={(val) => { if (net) { net.ipv6_enabled = val; dirty = true; } }} label="ipv6" />
            <span>IPv6 (tunnel) — carry segment client IPv6 through the tunnel. Off keeps v6 blocked.</span>
          </div>
          {#if net.ipv6_enabled}
            <label class="fld wide"><span>SEGMENT IPv6 /64 <small>(static, <code>auto</code> for DHCPv6-PD, blank = ULA)</small></span>
              <input class="mono" bind:value={net.segment.ip6} oninput={() => (dirty = true)} disabled={saving} placeholder="2001:db8:0:2::/64 · auto · blank" /></label>
            <label class="fld wide"><span>CLIENT DNS (v6)</span>
              <input class="mono" bind:value={net.segment.client_dns6} oninput={() => (dirty = true)} disabled={saving} placeholder="2606:4700:4700::1111" /></label>
          {/if}
        </div>
        <div class="actions">
          <button class="btn btn-primary" onclick={save} disabled={saving}>{saving ? "Applying…" : "Apply to host"}</button>
        </div>
      </div>

      <div class="card">
        <div class="card-top"><span class="eyebrow">Router checklist</span><span class="muted-sm">the one box v2pi never touches</span></div>
        <div class="checks">
          {#each net.recommendations as r (r.title)}
            <div class="chk"><span class="chk-box">✓</span><span class="chk-txt"><strong>{r.title}</strong> — {r.detail}</span></div>
          {/each}
          {#if !net.recommendations.length}<p class="msg">No router actions outstanding.</p>{/if}
        </div>
      </div>
    </div>

    <!-- RIGHT: kill-switch + leases -->
    <div class="col">
      <div class="card">
        <div class="card-top"><span class="eyebrow">Fail-closed kill-switch</span></div>
        <div class="kill">
          <Toggle checked={net.kill_switch_enabled} onchange={(val) => { if (net) { net.kill_switch_enabled = val; dirty = true; } }} label="kill-switch" />
          <div class="kill-state">
            <div class="kill-lbl" class:armed={net.kill_switch_enabled} class:open={!net.kill_switch_enabled}>{net.kill_switch_enabled ? "ARMED" : "OPEN"}</div>
            <div class="kill-sub">{net.kill_switch_enabled ? "fail-closed · traffic dropped if no healthy upstream" : "⚠ clients may leak around the tunnel"}</div>
          </div>
        </div>
        <p class="kill-note">When armed, any client traffic that can't reach a healthy upstream is dropped at nftables — no plaintext leak around the tunnel. {#if dirty}<strong>Apply to host to take effect.</strong>{/if}</p>
        {#if dirty}<div class="actions"><button class="btn btn-primary" onclick={save} disabled={saving}>{saving ? "Applying…" : "Apply to host"}</button></div>{/if}
      </div>

      <div class="card">
        <div class="card-top"><span class="eyebrow">DHCP leases</span><span class="muted-sm">{net.status.dhcp_clients} active</span></div>
        <div class="leases">
          {#each [...(net.status.clients ?? [])].sort(ipCompare) as c (c.mac + c.ip)}
            <div class="lease"><span class="lease-ip mono" title={c.ip}>{c.ip}</span><span class="lease-host">{c.hostname || "—"} · {leaseAge(c.expiry)}</span></div>
          {/each}
          {#if !(net.status.clients ?? []).length}<p class="msg">No active leases.</p>{/if}
        </div>
      </div>
    </div>
  </div>
{:else}
  <p class="msg">Loading…</p>
{/if}

<Alert {msg} />

<style>
  .net-grid { display: grid; grid-template-columns: 1.4fr 1fr; gap: 0.9rem; }
  .col { display: flex; flex-direction: column; gap: 0.9rem; }
  .card-top { display: flex; align-items: center; justify-content: space-between; gap: 0.6rem; }
  .muted-sm { font-size: 0.7rem; color: var(--tx3); font-weight: 400; text-transform: none; letter-spacing: 0; }
  .chip { font-size: 0.64rem; color: var(--acc); border: 1px solid var(--acc); border-radius: 4px; padding: 0.05rem 0.45rem; }

  .warn-row { display: flex; align-items: center; gap: 0.5rem; font-size: 0.78rem; color: var(--err);
    border: 1px solid color-mix(in srgb, var(--err) 40%, var(--bd)); background: color-mix(in srgb, var(--err) 9%, transparent);
    border-radius: var(--radius-sm); padding: 0.45rem 0.6rem; }
  .sdot { width: 8px; height: 8px; border-radius: 50%; flex: none; background: var(--tx3); }
  .sdot.bad { background: var(--err); }

  .form { display: grid; grid-template-columns: 1fr 1fr; gap: 0.7rem; }
  .fld { display: flex; flex-direction: column; gap: 0.3rem; min-width: 0; }
  .fld.wide { grid-column: 1 / -1; }
  .fld > span { font-size: 0.64rem; color: var(--tx3); letter-spacing: 0.08em; }
  .fld span small { text-transform: none; letter-spacing: 0; color: var(--tx3); }
  .fld input { background: var(--bg2); border: 1px solid var(--bd); border-radius: 6px; padding: 0.45rem 0.6rem;
    color: var(--tx); font: 500 0.82rem var(--font); outline: none; width: 100%; transition: border-color 0.15s; }
  .fld input:focus { border-color: var(--acc); }

  .opts { display: flex; flex-direction: column; gap: 0.6rem; }
  .opt { display: flex; gap: 0.6rem; align-items: flex-start; font-size: 0.76rem; color: var(--tx2); }
  .actions { display: flex; gap: 0.5rem; justify-content: flex-end; }

  .checks { display: flex; flex-direction: column; gap: 0.55rem; }
  .chk { display: flex; align-items: flex-start; gap: 0.65rem; font-size: 0.78rem; }
  .chk-box { width: 18px; height: 18px; flex: none; border-radius: 5px; display: grid; place-items: center;
    font-size: 0.7rem; color: var(--acc); background: color-mix(in srgb, var(--acc) 18%, transparent); }
  .chk-txt { color: var(--tx2); }
  .chk-txt strong { color: var(--tx); font-weight: 600; }

  .kill { display: flex; align-items: center; gap: 0.9rem; }
  .kill-lbl { font-size: 1.15rem; font-weight: 600; }
  .kill-lbl.armed { color: var(--acc); } .kill-lbl.open { color: var(--err); }
  .kill-sub { font-size: 0.68rem; color: var(--tx3); }
  .kill-note { font-size: 0.72rem; color: var(--tx3); line-height: 1.5; border-top: 1px solid var(--bd); padding-top: 0.7rem; margin: 0.2rem 0 0; }

  .leases { display: flex; flex-direction: column; gap: 0.5rem; }
  .lease { display: flex; justify-content: space-between; gap: 0.6rem; font-size: 0.78rem; }
  .lease-ip { color: var(--tx2); min-width: 0; flex: 1 1 auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .lease-host { color: var(--tx3); min-width: 0; flex: 1 1 auto; text-align: right; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  @media (max-width: 1000px) { .net-grid { grid-template-columns: 1fr; } }
  @media (max-width: 640px) { .form { grid-template-columns: 1fr; } }
</style>
