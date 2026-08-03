<script lang="ts">
  import { api, ApiError, type Rw } from "./api";
  import { confirmDialog } from "./confirm.svelte";
  import { statusStore } from "./status.svelte";
  import Toggle from "./Toggle.svelte";
  import Alert from "./Alert.svelte";

  let { onDirtyChange }: { onDirtyChange?: (dirty: boolean) => void } = $props();

  let rw = $state<Rw | null>(null);
  let hostRows = $state<{ name: string; ip: string }[]>([]);
  let privKey = $state("");          // write-only: the API never sends the stored key back
  let newClient = $state("");
  let msg = $state("");
  let msgKind = $state<"ok" | "err">("ok");
  let dirty = $state(false);
  let saving = $state(false);
  let busy = $state("");             // client id currently being acted on

  function ok(text: string) { msg = text; msgKind = "ok"; }
  function fail(err: unknown, fallback: string) {
    msg = err instanceof ApiError ? err.message : fallback;
    msgKind = "err";
  }

  function adopt(next: Rw) {
    rw = next;
    hostRows = Object.entries(next.hosts).map(([name, ip]) => ({ name, ip }));
    privKey = "";
  }

  async function load() {
    try { adopt(await api.getRw()); }
    catch (err) { fail(err, "load failed"); }
  }

  async function save() {
    if (!rw || saving) return;
    // Half-filled rows used to be dropped silently on save — the row vanished and the operator
    // had no way to know a host never made it into the config. Refuse instead.
    const halfFilled = hostRows.filter((r) => !!r.name.trim() !== !!r.ip.trim());
    if (halfFilled.length) {
      fail(null, `host "${halfFilled[0].name.trim() || halfFilled[0].ip.trim()}" needs both a name and an IP`);
      return;
    }
    saving = true;
    const hosts: Record<string, string> = {};
    for (const r of hostRows) {
      const name = r.name.trim(), ip = r.ip.trim();
      if (name && ip) hosts[name] = ip;
    }
    try {
      adopt(await api.putRw({
        enabled: rw.enabled, port: rw.port, dest: rw.dest, server_names: rw.server_names,
        short_ids: rw.short_ids, public_key: rw.public_key, endpoint: rw.endpoint,
        private_key: privKey.trim(), hosts, routed_nets: rw.routed_nets_override,
      }));
      dirty = false;
      ok(rw.live ? "saved · inbound rebuilt into the live config"
                 : "saved · no active node, so it applies on the next connect");   // response-truth, not the poller
    } catch (err) { fail(err, "save failed"); }
    finally { saving = false; }
  }

  async function addClient() {
    const email = newClient.trim();
    if (!email || saving) return;
    saving = true;
    try { adopt(await api.addRwClient(email)); newClient = ""; ok(`added ${email}`); }
    catch (err) { fail(err, "add failed"); }
    finally { saving = false; }
  }

  async function removeClient(id: string, email: string) {
    if (!await confirmDialog(`Remove ${email}? Its link and config stop working immediately.`)) return;
    busy = id;
    try { adopt(await api.deleteRwClient(id)); ok(`removed ${email}`); }
    catch (err) { fail(err, "remove failed"); }
    finally { busy = ""; }
  }

  async function toggleClient(id: string, enabled: boolean) {
    busy = id;
    try {
      adopt(await api.setRwClientEnabled(id, enabled));
      ok(enabled ? "client resumed" : "client suspended — its uuid is kept");
    } catch (err) { fail(err, "update failed"); }
    finally { busy = ""; }
  }

  async function genShortId() {
    if (!rw || saving) return;
    try {
      const { short_id } = await api.newRwShortId();
      rw.short_ids = rw.short_ids.trim() ? `${rw.short_ids.trim()},${short_id}` : short_id;
      dirty = true;
      ok("short id added — save to apply");
    } catch (err) { fail(err, "could not generate a short id"); }
  }

  async function copyLink(id: string) {
    busy = id;
    try {
      const { link } = await api.rwClientLink(id);
      await navigator.clipboard.writeText(link);
      ok("vless:// link copied");
    } catch (err) { fail(err, "copy failed"); }
    finally { busy = ""; }
  }

  async function downloadConf(id: string) {
    busy = id;
    try {
      const { filename, config } = await api.rwClientConfig(id);
      const url = URL.createObjectURL(new Blob([config], { type: "text/plain" }));
      const a = document.createElement("a");
      a.href = url; a.download = filename;
      // Attach before clicking and revoke on a later tick: revoking synchronously after click()
      // can cancel a download that has not started yet, and a detached anchor is unreliable.
      // Safari (the target platform here) is the fussiest about both.
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 0);
      ok(`${filename} downloaded — import it in Shadowrocket`);
    } catch (err) { fail(err, "download failed"); }
    finally { busy = ""; }
  }

  $effect(() => { load(); });
  $effect(() => { onDirtyChange?.(dirty); return () => onDirtyChange?.(false); });

  // Enabled but clientless emits NO inbound at all: xray refuses to start on a vless inbound
  // with an empty client list, so the alternative would be a self-inflicted tunnel outage.
  // Say so out loud — otherwise "enabled" reads as "listening" when nothing is.
  let clientless = $derived(!!rw?.enabled && rw.clients.filter((c) => c.enabled).length === 0);
  let noKey = $derived(!!rw && !rw.has_private_key && !privKey.trim());
  // Read liveness from the shared status poller rather than the value frozen into the last
  // /api/rw response — connect a node on another screen and this banner must clear itself.
  // Falls back to the response value until the first status arrives.
  let live = $derived(statusStore.value ? statusStore.value.active_node_id !== null : !!rw?.live);
  // Reality requires the SNI to be what `dest` actually serves. A mismatch is not rejected
  // anywhere — it just fails at handshake time with no useful error.
  let sniMismatch = $derived.by(() => {
    if (!rw) return false;
    const destHost = rw.dest.trim().replace(/:\d+$/, "").toLowerCase();
    const names = rw.server_names.split(",").map((n) => n.trim().toLowerCase()).filter(Boolean);
    return !!destHost && names.length > 0 && !names.includes(destHost);
  });
</script>

{#if rw}
  <div class="rw-grid">
    <div class="col">
      <div class="card">
        <div class="card-top">
          <span class="eyebrow">Remote Access Inbound</span>
          <span class="chip">VLESS · XTLS-Vision · Reality</span>
        </div>

        {#if rw.state_error}
          <div class="warn-row"><span class="sdot bad"></span>
            Stored settings are malformed and were ignored: {rw.state_error}. Save this form to
            overwrite them.</div>
        {/if}
        {#if clientless}
          <div class="warn-row"><span class="sdot bad"></span>
            Enabled with no clients — nothing is listening. xray will not start on an inbound with
            an empty client list, so none is emitted until you add a client.</div>
        {:else if rw.enabled && !live}
          <div class="warn-row"><span class="sdot warn"></span>
            Stored, but not in the running config yet — there is no active node to rebuild it.
            Connect a node and the inbound comes up with it.</div>
        {/if}
        {#if sniMismatch}
          <div class="warn-row"><span class="sdot warn"></span>
            Server name does not match <code>dest</code>. Reality needs the SNI to be what the
            dest host actually serves — a mismatch fails at handshake time with no useful error.</div>
        {/if}

        <div class="opts">
          <div class="opt">
            <Toggle checked={rw.enabled} disabled={saving || noKey}
                    onchange={(val) => { if (rw) { rw.enabled = val; dirty = true; } }} label="rw-enabled" />
            <span>Accept inbound connections from outside. LAN access keeps working when the
              exit node is down — <code>private → direct</code> does not depend on the tunnel.</span>
          </div>
        </div>

        <div class="form">
          <label class="fld"><span>LISTEN PORT</span>
            <input class="mono" type="number" bind:value={rw.port} oninput={() => (dirty = true)} disabled={saving} /></label>
          <label class="fld"><span>EXTERNAL ENDPOINT <small>(DDNS name or WAN IP)</small></span>
            <input class="mono" bind:value={rw.endpoint} oninput={() => (dirty = true)} disabled={saving} placeholder="home.example.org" /></label>
          <label class="fld"><span>REALITY DEST <small>(the site probes get)</small></span>
            <input class="mono" bind:value={rw.dest} oninput={() => (dirty = true)} disabled={saving} placeholder="www.microsoft.com:443" /></label>
          <label class="fld"><span>SERVER NAMES <small>(SNI, must match dest)</small></span>
            <input class="mono" bind:value={rw.server_names} oninput={() => (dirty = true)} disabled={saving} placeholder="www.microsoft.com" /></label>
          <label class="fld"><span>SHORT IDS <small>(csv hex, even length)</small></span>
            <span class="with-btn">
              <input class="mono" bind:value={rw.short_ids} oninput={() => (dirty = true)} disabled={saving} placeholder="ab12cd34" />
              <button class="btn btn-ghost" onclick={genShortId} disabled={saving}>Generate</button>
            </span></label>
          <label class="fld"><span>PUBLIC KEY <small>(goes in client links)</small></span>
            <input class="mono" bind:value={rw.public_key} oninput={() => (dirty = true)} disabled={saving} /></label>
          <label class="fld wide"><span>PRIVATE KEY
              <small>{rw.has_private_key ? "stored — leave blank to keep it" : "required before enabling"}</small></span>
            <input class="mono" type="password" bind:value={privKey} oninput={() => (dirty = true)} disabled={saving}
                   placeholder={rw.has_private_key ? "•••••••• stored" : "paste the private key from `xray x25519`"} /></label>
        </div>

        <p class="note">Generate the pair once on the gateway — <code>docker exec v2pi xray x25519</code> —
          and paste both halves here. The panel never stores or returns the private key to the browser
          after it is set, and it is deliberately left out of backups — after restoring onto a new
          host, paste it again.</p>

        <div class="actions">
          <button class="btn btn-primary" onclick={save} disabled={saving}>{saving ? "Saving…" : "Save"}</button>
        </div>
      </div>

      <div class="card">
        <div class="card-top">
          <span class="eyebrow">LAN hosts by name</span>
          <span class="muted-sm">the collision-free way in</span>
        </div>
        <p class="note">A remote client that routes <code>192.168.1.0/24</code> into the tunnel collides
          with every cafe running the same prefix, and <code>192.168.1.88</code> can silently mean their
          printer. A name cannot collide. The gateway resolves these, so the client only needs one rule
          per suffix. Avoid <code>.local</code> — iOS and macOS answer it over mDNS and it never reaches
          the tunnel.</p>
        <div class="hosts">
          {#each hostRows as row, i}
            <div class="host-row">
              <input class="mono" bind:value={row.name} oninput={() => (dirty = true)} disabled={saving} placeholder="nas.v2pi" />
              <input class="mono" bind:value={row.ip} oninput={() => (dirty = true)} disabled={saving} placeholder="192.168.1.88" />
              <button class="btn btn-ghost" onclick={() => { hostRows.splice(i, 1); dirty = true; }} disabled={saving}>Remove</button>
            </div>
          {/each}
        </div>
        <div class="actions">
          <button class="btn" onclick={() => { hostRows.push({ name: "", ip: "" }); dirty = true; }} disabled={saving}>Add host</button>
        </div>
      </div>
    </div>

    <div class="col">
      <div class="card">
        <div class="card-top"><span class="eyebrow">Clients</span><span class="muted-sm">one per device</span></div>
        <div class="clients">
          {#each rw.clients as c}
            <div class="client" class:suspended={!c.enabled}>
              <div class="client-id">
                <strong>{c.email}{#if !c.enabled}<span class="tag">suspended</span>{/if}</strong>
                <span class="mono muted-sm">{c.id}</span>
              </div>
              <div class="client-acts">
                <button class="btn btn-ghost" onclick={() => toggleClient(c.id, !c.enabled)} disabled={busy === c.id}>{c.enabled ? "Suspend" : "Resume"}</button>
                <button class="btn btn-ghost" onclick={() => downloadConf(c.id)} disabled={busy === c.id}>.conf</button>
                <button class="btn btn-ghost" onclick={() => copyLink(c.id)} disabled={busy === c.id}>Copy link</button>
                <button class="btn btn-ghost danger" onclick={() => removeClient(c.id, c.email)} disabled={busy === c.id}>Remove</button>
              </div>
            </div>
          {:else}
            <p class="note">No clients yet.</p>
          {/each}
        </div>
        <div class="add-row">
          <input bind:value={newClient} disabled={saving} placeholder="iphone"
                 onkeydown={(e) => { if (e.key === "Enter") addClient(); }} />
          <button class="btn btn-primary" onclick={addClient} disabled={saving || !newClient.trim()}>Add</button>
        </div>
        <p class="note"><strong>Suspend</strong> revokes a device now and keeps its uuid — for a
          lost phone, where Remove would mean reissuing everything.
          <strong>.conf</strong> is the full Shadowrocket config — routing rules included,
          so LAN access works without hand-editing anything. Download it while you are on this network;
          there is deliberately no public subscription URL. <strong>Copy link</strong> is the plain
          <code>vless://</code> fallback for any other client.</p>
      </div>

      <div class="card">
        <div class="card-top"><span class="eyebrow">Routed subnets</span></div>
        <p class="note">Pushed into the generated client config. Derived from the live network plan,
          so they follow your addressing instead of a hardcoded guess.</p>
        <div class="nets">
          {#each rw.routed_nets as net}<span class="net mono">{net}</span>{/each}
        </div>
        <label class="fld wide"><span>OVERRIDE <small>(csv, blank = derive)</small></span>
          <input class="mono" bind:value={rw.routed_nets_override} oninput={() => (dirty = true)} disabled={saving}
                 placeholder="192.168.1.0/24,192.168.10.0/24" /></label>
      </div>

      <div class="card">
        <div class="card-top"><span class="eyebrow">Router checklist</span><span class="muted-sm">not automated</span></div>
        <div class="checks">
          <div class="chk"><span class="chk-box">1</span><span class="chk-txt">
            <strong>Port-forward</strong> WAN :{rw.port} → this gateway, <em>on every WAN link</em> —
            a failover that swaps providers must not drop the rule.</span></div>
          <div class="chk"><span class="chk-box">2</span><span class="chk-txt">
            <strong>DDNS in direct mode.</strong> A cloud/proxied DDNS mode forwards HTTP(S) only and
            will not carry Reality's raw TCP.</span></div>
          <div class="chk"><span class="chk-box">3</span><span class="chk-txt">
            <strong>Check :{rw.port} is free on the router itself</strong> — its own web UI or remote-access
            service often sits on 443.</span></div>
        </div>
      </div>
    </div>
  </div>
{:else}
  <p class="note">Loading…</p>
{/if}

<Alert {msg} kind={msgKind} />

<style>
  .rw-grid { display: grid; grid-template-columns: 1.4fr 1fr; gap: 0.9rem; }
  .col { display: flex; flex-direction: column; gap: 0.9rem; }
  .card-top { display: flex; align-items: center; justify-content: space-between; gap: 0.6rem; }
  .muted-sm { font-size: 0.7rem; color: var(--tx3); font-weight: 400; text-transform: none; letter-spacing: 0; }
  .chip { font-size: 0.64rem; color: var(--acc); border: 1px solid var(--acc); border-radius: 4px; padding: 0.05rem 0.45rem; }

  .warn-row { display: flex; align-items: flex-start; gap: 0.5rem; font-size: 0.78rem; color: var(--err);
    border: 1px solid color-mix(in srgb, var(--err) 40%, var(--bd)); background: color-mix(in srgb, var(--err) 9%, transparent);
    border-radius: var(--radius-sm); padding: 0.45rem 0.6rem; }
  .sdot { width: 8px; height: 8px; border-radius: 50%; flex: none; margin-top: 0.3rem; background: var(--tx3); }
  .sdot.bad { background: var(--err); }
  .sdot.warn { background: var(--acc); }

  .form { display: grid; grid-template-columns: 1fr 1fr; gap: 0.7rem; }
  .fld { display: flex; flex-direction: column; gap: 0.3rem; min-width: 0; }
  .fld.wide { grid-column: 1 / -1; }
  .fld > span { font-size: 0.64rem; color: var(--tx3); letter-spacing: 0.08em; }
  .fld span small { text-transform: none; letter-spacing: 0; color: var(--tx3); }
  .fld input { background: var(--bg2); border: 1px solid var(--bd2); border-radius: 6px; padding: 0.45rem 0.6rem;
    color: var(--tx); font: 500 0.82rem var(--font); outline: none; width: 100%; transition: border-color 0.15s; }
  .fld input:focus { border-color: var(--acc); }

  .opts { display: flex; flex-direction: column; gap: 0.6rem; }
  .opt { display: flex; gap: 0.6rem; align-items: flex-start; font-size: 0.76rem; color: var(--tx2); }
  .actions { display: flex; gap: 0.5rem; justify-content: flex-end; }
  .note { font-size: 0.72rem; color: var(--tx3); line-height: 1.55; margin: 0; }
  .note strong { color: var(--tx2); }

  .hosts { display: flex; flex-direction: column; gap: 0.5rem; }
  .host-row { display: grid; grid-template-columns: 1.2fr 1fr auto; gap: 0.5rem; }
  .host-row input { background: var(--bg2); border: 1px solid var(--bd2); border-radius: 6px; padding: 0.4rem 0.55rem;
    color: var(--tx); font: 500 0.8rem var(--font); outline: none; min-width: 0; }
  .host-row input:focus { border-color: var(--acc); }

  .clients { display: flex; flex-direction: column; gap: 0.6rem; }
  .client { display: flex; align-items: center; justify-content: space-between; gap: 0.6rem;
    border-bottom: 1px solid var(--bd); padding-bottom: 0.55rem; }
  .client:last-child { border-bottom: 0; padding-bottom: 0; }
  .client-id { display: flex; flex-direction: column; gap: 0.15rem; min-width: 0; }
  .client-id strong { font-size: 0.84rem; }
  .client-id .mono { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .client.suspended .client-id strong { color: var(--tx3); }
  .client-id .tag { font-size: 0.6rem; color: var(--err); border: 1px solid color-mix(in srgb, var(--err) 45%, var(--bd));
    border-radius: 3px; padding: 0 0.3rem; margin-left: 0.4rem; vertical-align: middle; }
  .with-btn { display: grid; grid-template-columns: 1fr auto; gap: 0.35rem; align-items: center; }
  .client-acts { display: flex; gap: 0.3rem; flex: none; flex-wrap: wrap; justify-content: flex-end; }
  .client-acts .danger { color: var(--err); }

  .add-row { display: grid; grid-template-columns: 1fr auto; gap: 0.5rem; }
  .add-row input { background: var(--bg2); border: 1px solid var(--bd2); border-radius: 6px; padding: 0.45rem 0.6rem;
    color: var(--tx); font: 500 0.82rem var(--font); outline: none; min-width: 0; }
  .add-row input:focus { border-color: var(--acc); }

  .nets { display: flex; flex-wrap: wrap; gap: 0.35rem; }
  .net { font-size: 0.72rem; color: var(--tx2); border: 1px solid var(--bd2); border-radius: 4px; padding: 0.1rem 0.4rem; }

  .checks { display: flex; flex-direction: column; gap: 0.55rem; }
  .chk { display: flex; align-items: flex-start; gap: 0.65rem; font-size: 0.78rem; }
  .chk-box { width: 18px; height: 18px; flex: none; border-radius: 5px; display: grid; place-items: center;
    font-size: 0.7rem; color: var(--acc); background: color-mix(in srgb, var(--acc) 18%, transparent); }
  .chk-txt { color: var(--tx2); }
  .chk-txt strong { color: var(--tx); font-weight: 600; }

  @media (max-width: 1000px) { .rw-grid { grid-template-columns: 1fr; } }
  @media (max-width: 640px) { .form { grid-template-columns: 1fr; } }
</style>
