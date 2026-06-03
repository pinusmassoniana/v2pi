<script lang="ts">
  import { api, ApiError, type Settings } from "./api";

  let s = $state<Settings | null>(null);
  let msg = $state("");

  let pw = $state({ current: "", next: "" });
  let pwMsg = $state("");
  let restoreMsg = $state("");
  let logSource = $state("xray-error");
  let logLines = $state<string[]>([]);

  async function load() {
    try { s = await api.getSettings(); }
    catch (err) { msg = err instanceof ApiError ? err.message : "load failed"; }
  }
  async function save(e: Event) {
    e.preventDefault();
    if (!s) return;
    // routing_default_action is owned by the Routing screen — don't clobber it from here.
    const { routing_default_action, ...patch } = s;
    try { s = await api.putSettings(patch); msg = "saved"; }
    catch (err) { msg = err instanceof ApiError ? err.message : "save failed"; }
  }

  async function downloadBackup() {
    try {
      const doc = await api.getBackup();
      const blob = new Blob([JSON.stringify(doc, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "v2pi-backup.json";
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (err) { restoreMsg = err instanceof ApiError ? err.message : "backup failed"; }
  }
  async function onRestoreFile(e: Event) {
    const input = e.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    if (!confirm("Restore replaces all nodes, subscriptions, profiles, routing and settings. Continue?")) {
      input.value = ""; return;
    }
    try {
      const doc = JSON.parse(await file.text());
      const r = await api.restore(doc);
      restoreMsg = `restored ${r.restored.nodes} nodes, ${r.restored.profiles} profiles`;
    } catch (err) { restoreMsg = err instanceof ApiError ? err.message : "restore failed"; }
    finally { input.value = ""; }
  }

  async function changePassword(e: Event) {
    e.preventDefault();
    try {
      await api.changePassword(pw.current, pw.next);
      pwMsg = "password changed"; pw = { current: "", next: "" };
    } catch (err) { pwMsg = err instanceof ApiError ? err.message : "change failed"; }
  }

  async function loadLogs() {
    try { logLines = (await api.getLogs(logSource, 200)).lines; }
    catch (err) { logLines = [err instanceof ApiError ? err.message : "load failed"]; }
  }

  $effect(() => { load(); });
</script>

{#if msg}<p class="msg">{msg}</p>{/if}

{#if s}
  <form onsubmit={save} class="card settings">
    <h3>General</h3>
    <label class="check"><input type="checkbox" bind:checked={s.tunneled_fetch} /> Fetch subscriptions through the tunnel</label>

    <fieldset>
      <legend>Traffic stats</legend>
      <label class="check"><input type="checkbox" bind:checked={s.stats_enabled} /> Enabled (live Dashboard graph)</label>
      <label class="field"><span>Sample interval (ms)</span><input class="input" type="number" min="250" bind:value={s.traffic_sample_ms} /></label>
      <label class="field"><span>xray api port</span><input class="input" type="number" bind:value={s.stats_api_port} /></label>
    </fieldset>

    <fieldset>
      <legend>Health monitoring</legend>
      <label class="check"><input type="checkbox" bind:checked={s.health_enabled} /> Enabled</label>
      <label class="field"><span>Interval (s)</span><input class="input" type="number" min="5" bind:value={s.health_interval} /></label>
      <label class="field"><span>Probe URL</span><input class="input" bind:value={s.health_probe_url} /></label>
    </fieldset>

    <fieldset>
      <legend>Auto-failover</legend>
      <label class="check"><input type="checkbox" bind:checked={s.failover_enabled} /> Enabled</label>
      <label class="field"><span>Hysteresis (consecutive real-request fails)</span><input class="input" type="number" min="1" bind:value={s.health_hysteresis} /></label>
      <label class="field"><span>Cooldown (s)</span><input class="input" type="number" min="0" bind:value={s.failover_cooldown} /></label>
    </fieldset>

    <div><button class="btn btn-primary">Save</button></div>
    <p class="muted hint">Anti-DPI tuning (fingerprint, fragmentation, mux, DoH, QUIC) lives in <strong>Tuning</strong> profiles.</p>
  </form>

  <div class="card">
    <h3>Backup / restore</h3>
    <div class="row">
      <button class="btn" type="button" onclick={downloadBackup}>Download backup (JSON)</button>
      <label class="file btn">Restore…<input type="file" accept="application/json,.json" onchange={onRestoreFile} /></label>
    </div>
    {#if restoreMsg}<p class="msg">{restoreMsg}</p>{/if}
  </div>

  <form onsubmit={changePassword} class="card pw">
    <h3>Change password</h3>
    <input class="input" type="password" bind:value={pw.current} placeholder="current password" autocomplete="current-password" />
    <input class="input" type="password" bind:value={pw.next} placeholder="new password" autocomplete="new-password" />
    <div><button class="btn btn-primary" disabled={!pw.current || !pw.next}>Change password</button></div>
    {#if pwMsg}<p class="msg">{pwMsg}</p>{/if}
  </form>

  <div class="card">
    <h3>Logs</h3>
    <div class="row">
      <select class="input auto" bind:value={logSource}>
        <option value="xray-error">xray error</option>
        <option value="xray-access">xray access</option>
        <option value="app">app</option>
      </select>
      <button class="btn" type="button" onclick={loadLogs}>Load</button>
    </div>
    {#if logLines.length}<pre class="logs">{logLines.join("\n")}</pre>{/if}
  </div>
{/if}

<style>
  .settings, .pw { max-width: 34rem; }
  .pw { gap: 0.5rem; }
  fieldset { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 0.6rem; display: grid; gap: 0.4rem; }
  .check { display: flex; gap: 0.5rem; align-items: center; }
  .file { position: relative; overflow: hidden; cursor: pointer; }
  .file input { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
  .hint { font-size: 0.8rem; }
  .logs {
    background: var(--surface-2); border: 1px solid var(--border); color: var(--text);
    padding: 0.6rem; border-radius: var(--radius-sm); max-height: 16rem; overflow: auto;
    font-size: 0.78rem; white-space: pre-wrap; font-family: var(--mono);
  }
</style>
