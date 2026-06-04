<script lang="ts">
  import { api, ApiError, type Settings, type Diagnostics } from "./api";
  import Toggle from "./Toggle.svelte";
  import Alert from "./Alert.svelte";
  import { confirmDialog } from "./confirm.svelte";

  let s = $state<Settings | null>(null);
  let msg = $state("");
  let msgKind = $state<"ok" | "err">("ok");
  let diag = $state<Diagnostics | null>(null);

  let pw = $state({ current: "", next: "", confirm: "" });
  let pwMsg = $state(""); let pwKind = $state<"ok" | "err">("ok");
  let restoreMsg = $state(""); let restoreKind = $state<"ok" | "err">("ok");
  let logSource = $state("xray-error");
  let logLines = $state<string[]>([]);
  let logQuery = $state("");
  let logCount = $state(200);
  let logAuto = $state(false);

  function setMsg(t: string, k: "ok" | "err" = "ok") { msg = t; msgKind = k; }
  function errText(err: unknown, fb: string) { return err instanceof ApiError ? err.message : fb; }

  // client-side validation mirrors the server floors (SC2/SF2)
  const invalid = $derived.by(() => {
    if (!s) return "";
    if (s.traffic_sample_ms < 250) return "Sample interval must be ≥ 250 ms";
    if (s.health_interval < 60) return "Probe interval must be ≥ 1 min";
    if (s.health_hysteresis < 1) return "Hysteresis must be ≥ 1";
    if (s.failover_cooldown < 0) return "Cooldown must be ≥ 0";
    if (s.session_timeout_min < 0) return "Session timeout must be ≥ 0";
    if (s.stats_api_port < 1 || s.stats_api_port > 65535) return "xray api port must be 1–65535";
    return "";
  });

  async function load() {
    try { s = await api.getSettings(); }
    catch (err) { setMsg(errText(err, "load failed"), "err"); }
    try { diag = await api.getDiagnostics(); } catch { /* non-fatal */ }
  }
  async function save(e: Event) {
    e.preventDefault();
    if (!s || invalid) { if (invalid) setMsg(invalid, "err"); return; }
    const { routing_default_action, ...patch } = s;   // routing-owned key set on the Routing screen
    try { s = await api.putSettings(patch); setMsg("saved", "ok"); }
    catch (err) { setMsg(errText(err, "save failed"), "err"); }
  }
  async function resetDefaults() {
    if (!(await confirmDialog("Reset all settings on this screen to their defaults?"))) return;
    try { s = await api.resetSettings(); setMsg("reset to defaults", "ok"); }
    catch (err) { setMsg(errText(err, "reset failed"), "err"); }
  }

  function fmtBytes(n: number): string {
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0, v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
  }
  function fmtUptime(sec: number): string {
    const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  }

  async function downloadBackup() {
    try {
      const doc = await api.getBackup();
      const date = new Date().toISOString().slice(0, 10);
      const blob = new Blob([JSON.stringify(doc, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob); a.download = `v2pi-backup-${date}.json`; a.click();
      URL.revokeObjectURL(a.href);
    } catch (err) { restoreMsg = errText(err, "backup failed"); restoreKind = "err"; }
  }
  async function onRestoreFile(e: Event) {
    const input = e.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    if (!(await confirmDialog("Restore replaces all nodes, subscriptions, profiles, routing and settings. Continue?"))) {
      input.value = ""; return;
    }
    try {
      const r = await api.restore(JSON.parse(await file.text()));
      restoreMsg = `restored ${r.restored.nodes} nodes, ${r.restored.profiles} profiles — reconnect to apply`;
      restoreKind = "ok"; await load();
    } catch (err) { restoreMsg = errText(err, "restore failed"); restoreKind = "err"; }
    finally { input.value = ""; }
  }
  function exportSettings() {
    if (!s) return;
    const { routing_default_action, ...rest } = s;
    const blob = new Blob([JSON.stringify(rest, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = "v2pi-settings.json"; a.click();
    URL.revokeObjectURL(a.href);
  }
  async function onImportSettings(e: Event) {
    const input = e.target as HTMLInputElement;
    const file = input.files?.[0]; if (!file) { return; }
    try {
      const doc = JSON.parse(await file.text());
      delete doc.routing_default_action;
      s = await api.putSettings(doc); setMsg("settings imported", "ok");
    } catch (err) { setMsg(errText(err, "import failed"), "err"); }
    finally { input.value = ""; }
  }

  // password (SF4/SN5)
  const pwStrength = $derived.by(() => {
    const p = pw.next;
    if (!p) return "";
    if (p.length < 8) return "too short (min 8)";
    let score = 0;
    if (/[a-z]/.test(p) && /[A-Z]/.test(p)) score++;
    if (/\d/.test(p)) score++;
    if (/[^a-zA-Z0-9]/.test(p)) score++;
    if (p.length >= 12) score++;
    return ["weak", "ok", "good", "strong"][Math.min(score, 3)];
  });
  const pwOk = $derived(pw.current && pw.next.length >= 8 && pw.next === pw.confirm);
  async function changePassword(e: Event) {
    e.preventDefault();
    try {
      await api.changePassword(pw.current, pw.next);
      pwMsg = "password changed — other sessions signed out"; pwKind = "ok";
      pw = { current: "", next: "", confirm: "" };
    } catch (err) { pwMsg = errText(err, "change failed"); pwKind = "err"; }
  }

  async function loadLogs() {
    try { logLines = (await api.getLogs(logSource, Math.max(1, Math.min(logCount, 1000)))).lines; }
    catch (err) { logLines = [errText(err, "load failed")]; }
  }
  const shownLogs = $derived(logQuery.trim()
    ? logLines.filter((l) => l.toLowerCase().includes(logQuery.trim().toLowerCase())) : logLines);
  function downloadLogs() {
    const blob = new Blob([logLines.join("\n")], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `${logSource}.log`; a.click();
    URL.revokeObjectURL(a.href);
  }

  $effect(() => { load(); });
  $effect(() => {                       // SN4 auto-refresh
    if (!logAuto) return;
    const id = setInterval(loadLogs, 5000);
    return () => clearInterval(id);
  });
</script>

<Alert {msg} kind={msgKind} />

{#if s}
  <form onsubmit={save} class="card settings">
    <h3>General</h3>
    <div class="check"><Toggle checked={s.tunneled_fetch} onchange={(v) => { if (s) s.tunneled_fetch = v; }} label="tunneled fetch" /> <span>Fetch subscriptions through the tunnel <span class="live">applies live</span></span></div>
    <div class="check"><Toggle checked={s.dns_intercept} onchange={(v) => { if (s) s.dns_intercept = v; }} label="gateway DNS" /> <span>Resolve segment DNS in the gateway over DoH <span class="live">applies live</span></span></div>

    <fieldset>
      <legend>Traffic stats</legend>
      <div class="check"><Toggle checked={s.stats_enabled} onchange={(v) => { if (s) s.stats_enabled = v; }} label="stats" /> <span>Enabled (live Dashboard graph) <span class="live">applies live</span></span></div>
      <label class="field"><span>Sample interval (ms, ≥250)</span><input class="input" type="number" min="250" bind:value={s.traffic_sample_ms} /></label>
      <label class="field"><span>xray api port</span><input class="input" type="number" min="1" max="65535" bind:value={s.stats_api_port} /></label>
    </fieldset>

    <fieldset>
      <legend>Health monitoring</legend>
      <div class="check"><Toggle checked={s.health_enabled} onchange={(v) => { if (s) s.health_enabled = v; }} label="health" /> <span>Enabled</span></div>
      <label class="field"><span>Probe interval (minutes)</span>
        <input class="input" type="number" min="1"
               value={Math.max(1, Math.round((s.health_interval || 1800) / 60))}
               onchange={(e) => { if (s) s.health_interval = Math.max(60, Math.round(Number(e.currentTarget.value)) * 60); }} /></label>
      <label class="field"><span>Probe URL</span><input class="input" bind:value={s.health_probe_url} /></label>
    </fieldset>

    <fieldset>
      <legend>Auto-failover</legend>
      <div class="check"><Toggle checked={s.failover_enabled} onchange={(v) => { if (s) s.failover_enabled = v; }} label="failover" /> <span>Enabled</span></div>
      <label class="field"><span>Hysteresis (consecutive real-request fails)</span><input class="input" type="number" min="1" bind:value={s.health_hysteresis} /></label>
      <label class="field"><span>Cooldown (s)</span><input class="input" type="number" min="0" bind:value={s.failover_cooldown} /></label>
    </fieldset>

    <fieldset>
      <legend>Session &amp; backup</legend>
      <label class="field"><span>Idle timeout (minutes, 0 = off)</span><input class="input" type="number" min="0" bind:value={s.session_timeout_min} /></label>
      <div class="check"><Toggle checked={s.auto_backup_enabled} onchange={(v) => { if (s) s.auto_backup_enabled = v; }} label="auto-backup" /> <span>Daily auto-backup to the Pi (data/backups)</span></div>
    </fieldset>

    {#if invalid}<p class="msg err">{invalid}</p>{/if}
    <div class="actions">
      <button class="btn btn-primary" disabled={!!invalid}>Save</button>
      <button class="btn" type="button" onclick={resetDefaults}>Reset to defaults</button>
      <button class="btn" type="button" onclick={exportSettings}>Export settings</button>
      <label class="file btn">Import settings…<input type="file" accept="application/json,.json" onchange={onImportSettings} /></label>
    </div>
    <p class="muted hint">Anti-DPI tuning (fingerprint, fragmentation, mux, DoH, QUIC) lives in <strong>Tuning</strong> profiles.</p>
  </form>

  <div class="card">
    <h3>Backup / restore</h3>
    <div class="row">
      <button class="btn" type="button" onclick={downloadBackup}>Download backup (JSON)</button>
      <label class="file btn">Restore…<input type="file" accept="application/json,.json" onchange={onRestoreFile} /></label>
    </div>
    <Alert msg={restoreMsg} kind={restoreKind} />
  </div>

  <form onsubmit={changePassword} class="card pw">
    <h3>Change password</h3>
    <input class="input" type="password" bind:value={pw.current} placeholder="current password" autocomplete="current-password" />
    <input class="input" type="password" bind:value={pw.next} placeholder="new password (min 8)" autocomplete="new-password" />
    <input class="input" type="password" bind:value={pw.confirm} placeholder="confirm new password" autocomplete="new-password" />
    {#if pw.next}<p class="muted hint">strength: {pwStrength}{#if pw.confirm && pw.next !== pw.confirm} · <span class="nomatch">does not match</span>{/if}</p>{/if}
    <div><button class="btn btn-primary" disabled={!pwOk}>Change password</button></div>
    <Alert msg={pwMsg} kind={pwKind} />
  </form>

  <div class="card">
    <h3>Logs</h3>
    <div class="row">
      <select class="input auto" bind:value={logSource}>
        <option value="xray-error">xray error</option>
        <option value="xray-access">xray access</option>
        <option value="app">app</option>
      </select>
      <input class="input num" type="number" min="1" max="1000" bind:value={logCount} title="lines" />
      <button class="btn" type="button" onclick={loadLogs}>Load</button>
      <input class="input" placeholder="filter…" bind:value={logQuery} />
      <label class="check"><Toggle checked={logAuto} onchange={(v) => (logAuto = v)} label="auto" /> <span>auto-refresh</span></label>
      {#if logLines.length}<button class="btn" type="button" onclick={downloadLogs}>Download</button>{/if}
    </div>
    {#if logLines.length}<pre class="logs">{shownLogs.join("\n")}</pre>{/if}
  </div>

  <div class="card diag">
    <h3>About / diagnostics</h3>
    {#if diag}
      <dl>
        <dt>app version</dt><dd>{diag.app_version}</dd>
        <dt>xray</dt><dd class="mono">{diag.xray_version}</dd>
        <dt>panel uptime</dt><dd>{fmtUptime(diag.uptime_sec)}</dd>
        <dt>database</dt><dd class="mono">{diag.db_path} · {fmtBytes(diag.db_bytes)}</dd>
        <dt>disk free</dt><dd>{fmtBytes(diag.disk_free_bytes)} / {fmtBytes(diag.disk_total_bytes)}</dd>
      </dl>
    {:else}<p class="muted">diagnostics unavailable</p>{/if}
  </div>
{/if}

<style>
  .settings, .pw { max-width: 34rem; }
  .pw { gap: 0.5rem; }
  fieldset { border: 1px solid var(--border); border-radius: var(--radius); padding: 0.85rem 0.95rem; display: grid; gap: 0.55rem; background: var(--surface-2); }
  .check { display: flex; gap: 0.55rem; align-items: center; }
  .actions { display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center; }
  .file { position: relative; overflow: hidden; cursor: pointer; }
  .file input { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
  .hint { font-size: 0.8rem; }
  .live { font-size: 0.62rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; color: var(--accent); background: var(--accent-soft); padding: 0.02rem 0.35rem; border-radius: 999px; }
  .nomatch { color: var(--danger); }
  .row { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
  .row .auto { width: auto; }
  .row .num { width: 5rem; }
  .diag dl { display: grid; grid-template-columns: auto 1fr; gap: 0.3rem 1rem; margin: 0; }
  .diag dt { color: var(--muted); }
  .diag dd { margin: 0; }
  .logs {
    background: var(--surface-2); border: 1px solid var(--border); color: var(--text);
    padding: 0.6rem; border-radius: var(--radius-sm); max-height: 16rem; overflow: auto;
    font-size: 0.78rem; white-space: pre-wrap; font-family: var(--mono);
  }
</style>
