<script lang="ts">
  import { api, ApiError, type Settings, type ApiToken, type ApiTokenCreated, type AuditEntry } from "./api";
  import Toggle from "./Toggle.svelte";
  import Alert from "./Alert.svelte";
  import { confirmDialog } from "./confirm.svelte";

  let { onDirtyChange }: { onDirtyChange?: (dirty: boolean) => void } = $props();

  let s = $state<Settings | null>(null);
  let msg = $state("");
  let msgKind = $state<"ok" | "err">("ok");
  let saving = $state(false);
  let savedSnapshot = $state("");
  let dirty = $derived(!!s && JSON.stringify(s) !== savedSnapshot);

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
    if (s.traffic_sample_ms < 500) return "Sample interval must be ≥ 500 ms";
    if (s.health_interval < 60) return "Probe interval must be ≥ 1 min";
    if (s.health_hysteresis < 1) return "Hysteresis must be ≥ 1";
    if (s.failover_cooldown < 0) return "Cooldown must be ≥ 0";
    if (s.session_timeout_min < 0) return "Session timeout must be ≥ 0";
    if (s.stats_api_port < 1 || s.stats_api_port > 65535) return "xray api port must be 1–65535";
    return "";
  });

  async function load() {
    try { s = await api.getSettings(); savedSnapshot = JSON.stringify(s); }
    catch (err) { setMsg(errText(err, "load failed"), "err"); }
  }
  async function save(e: Event) {
    e.preventDefault();
    if (saving || !s || invalid) { if (invalid) setMsg(invalid, "err"); return; }
    const { routing_default_action, ...patch } = s;   // routing-owned key set on the Routing screen
    saving = true;
    try { s = await api.putSettings(patch); savedSnapshot = JSON.stringify(s); setMsg("saved", "ok"); }
    catch (err) { setMsg(errText(err, "save failed"), "err"); }
    finally { saving = false; }
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
    if (!(await confirmDialog("Update the supplied settings? Unknown fields are rejected; omitted fields stay unchanged."))) { input.value = ""; return; }
    try {
      const doc = JSON.parse(await file.text());
      delete doc.routing_default_action;
      s = await api.putSettings(doc); savedSnapshot = JSON.stringify(s); setMsg("supplied settings updated", "ok");
    } catch (err) { setMsg(errText(err, "import failed"), "err"); }
    finally { input.value = ""; }
  }

  // API tokens
  let tokens = $state<ApiToken[]>([]);
  let tokName = $state(""); let tokScope = $state<"monitor" | "read" | "readwrite">("monitor");
  let newToken = $state<ApiTokenCreated | null>(null);   // shown once, right after creation
  let leaveBlocked = $derived(dirty || !!newToken);
  let tokMsg = $state(""); let tokKind = $state<"ok" | "err">("ok");
  let copied = $state(false);
  let tokBusy = $state(false);
  async function loadTokens() {
    try { tokens = await api.listTokens(); }
    catch (err) { tokMsg = errText(err, "load failed"); tokKind = "err"; }
  }
  async function createTok(e: Event) {
    e.preventDefault();
    if (tokBusy || !tokName.trim()) return;
    if (newToken) { tokMsg = "Finish copying the visible token first."; tokKind = "err"; return; }
    tokBusy = true;
    try {
      newToken = await api.createToken(tokName.trim(), tokScope);
      tokName = ""; copied = false; tokMsg = ""; await loadTokens();
    } catch (err) { tokMsg = errText(err, "create failed"); tokKind = "err"; }
    finally { tokBusy = false; }
  }
  async function revokeTok(t: ApiToken) {
    if (!(await confirmDialog(`Revoke token "${t.name}"? Anything using it stops working immediately.`))) return;
    try { await api.deleteToken(t.id); if (newToken?.id === t.id) newToken = null; await loadTokens(); }
    catch (err) { tokMsg = errText(err, "revoke failed"); tokKind = "err"; }
  }
  async function copyToken() {
    if (!newToken) return;
    try { await navigator.clipboard.writeText(newToken.token); copied = true; } catch { /* clipboard blocked */ }
  }
  function fmtDate(sec: number | null): string { return sec ? new Date(sec * 1000).toLocaleString() : "—"; }
  // audit log (N2)
  let audit = $state<AuditEntry[]>([]);
  let auditOpen = $state(false);
  let auditLoading = $state(false);
  let auditError = $state("");
  async function loadAudit() {
    if (auditLoading) return;
    auditLoading = true; auditError = "";
    try { audit = await api.listAudit(100); }
    catch (err) { auditError = errText(err, "audit load failed"); }
    finally { auditLoading = false; }
  }
  function fmtTs(sec: number): string { return new Date(sec * 1000).toLocaleString(); }

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

  $effect(() => { load(); loadTokens(); });
  $effect(() => { onDirtyChange?.(leaveBlocked); return () => onDirtyChange?.(false); });
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
    <div class="check"><Toggle checked={s.tunneled_fetch} disabled={saving} onchange={(v) => { if (s) s.tunneled_fetch = v; }} label="tunneled fetch" /> <span>Fetch subscriptions through the tunnel <span class="live">applies live</span></span></div>
    <div class="check"><Toggle checked={s.dns_intercept} disabled={saving} onchange={(v) => { if (s) s.dns_intercept = v; }} label="gateway DNS" /> <span>Resolve segment DNS in the gateway over DoH <span class="live">applies live</span></span></div>

    <fieldset>
      <legend>Traffic stats</legend>
      <div class="check"><Toggle checked={s.stats_enabled} disabled={saving} onchange={(v) => { if (s) s.stats_enabled = v; }} label="stats" /> <span>Enabled (live Dashboard graph) <span class="live">applies live</span></span></div>
      <label class="field"><span>Sample interval (ms, ≥500)</span><input class="input" type="number" min="500" bind:value={s.traffic_sample_ms} disabled={saving} /></label>
      <label class="field"><span>xray api port</span><input class="input" type="number" min="1" max="65535" bind:value={s.stats_api_port} /></label>
    </fieldset>

    <fieldset>
      <legend>Health monitoring</legend>
      <div class="check"><Toggle checked={s.health_enabled} disabled={saving} onchange={(v) => { if (s) s.health_enabled = v; }} label="health" /> <span>Enabled</span></div>
      <label class="field"><span>Probe interval (minutes)</span>
        <input class="input" type="number" min="1"
               value={Math.max(1, Math.round((s.health_interval || 1800) / 60))}
               onchange={(e) => { if (s) s.health_interval = Math.max(60, Math.round(Number(e.currentTarget.value)) * 60); }} /></label>
      <label class="field"><span>Probe URL</span><input class="input" type="url" bind:value={s.health_probe_url} /></label>
    </fieldset>

    <fieldset>
      <legend>Auto-failover</legend>
      <div class="check"><Toggle checked={s.failover_enabled} disabled={saving} onchange={(v) => { if (s) s.failover_enabled = v; }} label="failover" /> <span>Enabled</span></div>
      <label class="field"><span>Hysteresis (consecutive real-request fails)</span><input class="input" type="number" min="1" bind:value={s.health_hysteresis} /></label>
      <label class="field"><span>Cooldown (s)</span><input class="input" type="number" min="0" bind:value={s.failover_cooldown} /></label>
    </fieldset>

    <fieldset>
      <legend>Session &amp; backup</legend>
      <label class="field"><span>Idle timeout (minutes, 0 = off)</span><input class="input" type="number" min="0" bind:value={s.session_timeout_min} /></label>
      <div class="check"><Toggle checked={s.auto_backup_enabled} disabled={saving} onchange={(v) => { if (s) s.auto_backup_enabled = v; }} label="auto-backup" /> <span>Daily auto-backup to the Pi (data/backups)</span></div>
    </fieldset>

    {#if invalid}<p class="msg err">{invalid}</p>{/if}
    <div class="actions">
      <button class="btn btn-primary" disabled={!!invalid || saving}>{saving ? "Saving…" : "Save"}</button>
      <button class="btn" type="button" onclick={exportSettings}>Export settings</button>
      <label class="file btn">Import settings…<input type="file" accept="application/json,.json" onchange={onImportSettings} /></label>
    </div>
    <p class="muted hint">Anti-DPI tuning (fingerprint, fragmentation, mux, DoH, QUIC) lives in <strong>Anti-DPI</strong> profiles. Backups, password and system info are on <strong>Operations</strong>.</p>
  </form>

  <div class="card tokens">
    <h3>API tokens</h3>
    <p class="muted hint">Programmatic REST access via <code>Authorization: Bearer &lt;token&gt;</code>.
      <strong>monitor</strong> = non-secret status/health telemetry · <strong>read</strong> = secret-bearing admin GETs · <strong>read/write</strong> = full access.</p>

    {#if newToken}
      <div class="reveal">
        <p><strong>New token “{newToken.name}” · {newToken.scope}</strong> — copy it now, it is shown only once:</p>
        <div class="row">
          <code class="secret mono">{newToken.token}</code>
          <button class="btn" type="button" onclick={copyToken}>{copied ? "Copied ✓" : "Copy"}</button>
          <button class="btn btn-ghost" type="button" onclick={() => (newToken = null)}>Done</button>
        </div>
      </div>
    {/if}

    {#if tokens.length}
      <ul class="tlist">
        {#each tokens as t (t.id)}
          <li>
            <span class="tname">{t.name}</span>
            <span class="badge {t.scope === 'readwrite' ? 'active-b' : ''}">{t.scope === 'readwrite' ? 'read/write' : t.scope}</span>
            <code class="mono prefix">{t.prefix}…</code>
            <span class="muted when">created {fmtDate(t.created_at)} · last used {fmtDate(t.last_used_at)}{#if t.expires_at} · expires {fmtDate(t.expires_at)}{/if}</span>
            <button class="btn btn-danger" type="button" onclick={() => revokeTok(t)}>Revoke</button>
          </li>
        {/each}
      </ul>
    {:else}<p class="muted">No tokens yet.</p>{/if}

    <form onsubmit={createTok} class="row create">
      <input class="input" bind:value={tokName} placeholder="token name (e.g. monitor)" maxlength="64" aria-label="Token name" disabled={tokBusy || !!newToken} />
      <select class="input auto" bind:value={tokScope} aria-label="Token scope" disabled={tokBusy || !!newToken}>
        <option value="monitor">monitor</option><option value="read">read (admin, secret-bearing)</option>
        <option value="readwrite">read/write</option>
      </select>
      <button class="btn btn-primary" disabled={!tokName.trim() || tokBusy || !!newToken}>{tokBusy ? "Creating…" : "Create token"}</button>
    </form>
    <Alert msg={tokMsg} kind={tokKind} />
  </div>

  <div class="card audit">
    <h3>Audit log</h3>
    <p class="muted hint">Mutation attempts made through the panel or API — actor, path and final status.</p>
    <div class="row">
      <button class="btn" type="button" disabled={auditLoading} onclick={() => { auditOpen = true; loadAudit(); }}>
        {auditLoading ? "Loading…" : auditOpen ? "Retry / refresh" : "Show"}</button>
    </div>
    {#if auditOpen}
      {#if auditError}
        <p class="msg err" role="alert">{auditError}</p>
      {:else if auditLoading}
        <p class="muted" role="status">Loading audit log…</p>
      {:else if audit.length}
        <ul class="alist">
          {#each audit as e, i (i)}
            <li>
              <span class="muted when">{fmtTs(e.ts)}</span>
              <span class="badge">{e.actor}</span>
              <code class="mono">{e.method} {e.path}</code>
              <span class="muted">→ {e.status}</span>
            </li>
          {/each}
        </ul>
      {:else}<p class="muted">No recorded changes yet.</p>{/if}
    {/if}
  </div>

  <div class="card">
    <h3>Logs</h3>
    <div class="row">
      <select class="input auto" bind:value={logSource} aria-label="Log source">
        <option value="xray-error">xray error</option>
        <option value="xray-access">xray access</option>
        <option value="app">app</option>
      </select>
      <input class="input num" type="number" min="1" max="1000" bind:value={logCount} title="lines" aria-label="Log line count" />
      <button class="btn" type="button" onclick={loadLogs}>Load</button>
      <input class="input flt" placeholder="filter…" bind:value={logQuery} aria-label="Filter logs" />
      <div class="check"><Toggle checked={logAuto} onchange={(v) => (logAuto = v)} label="auto" /> <span>auto-refresh</span></div>
      {#if logLines.length}<button class="btn" type="button" onclick={downloadLogs}>Download</button>{/if}
    </div>
    {#if logLines.length}<pre class="logs">{shownLogs.join("\n")}</pre>{/if}
  </div>
{/if}

<style>
  .settings { max-width: 34rem; }
  fieldset { border: 1px solid var(--border); border-radius: var(--radius); padding: 0.85rem 0.95rem; display: grid; gap: 0.55rem; background: var(--surface-2); }
  .check { display: flex; gap: 0.55rem; align-items: center; }
  .actions { display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center; }
  .file { position: relative; overflow: hidden; cursor: pointer; }
  .file input { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
  .hint { font-size: 0.8rem; }
  .live { font-size: 0.62rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; color: var(--accent); background: var(--accent-soft); padding: 0.02rem 0.35rem; border-radius: 999px; }
  .row { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
  .row .auto { width: auto; }
  .row .num { width: 5rem; }
  .row .flt { flex: 1; min-width: 8rem; }
  .logs {
    background: var(--surface-2); border: 1px solid var(--border); color: var(--text);
    padding: 0.6rem; border-radius: var(--radius-sm); max-height: 16rem; overflow: auto;
    font-size: 0.78rem; white-space: pre-wrap; font-family: var(--mono);
  }
  .tokens { max-width: 44rem; }
  .tokens .create { margin-top: 0.6rem; }
  .reveal { background: var(--accent-soft); border: 1px solid var(--accent); border-radius: var(--radius-sm); padding: 0.6rem 0.7rem; margin-bottom: 0.6rem; }
  .reveal p { margin: 0 0 0.4rem; font-size: 0.85rem; }
  .secret { background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 0.3rem 0.5rem; word-break: break-all; flex: 1; min-width: 12rem; }
  .tlist { list-style: none; margin: 0.4rem 0 0; padding: 0; display: grid; gap: 0.4rem; }
  .tlist li { display: flex; gap: 0.55rem; align-items: center; flex-wrap: wrap; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 0.45rem 0.6rem; background: var(--surface-2); }
  .tlist .tname { font-weight: 600; }
  .tlist .prefix { color: var(--muted); font-size: 0.78rem; }
  .tlist .when { font-size: 0.75rem; margin-left: auto; }
  .audit { max-width: 44rem; }
  .alist { list-style: none; margin: 0.6rem 0 0; padding: 0; display: grid; gap: 0.3rem; max-height: 18rem; overflow: auto; }
  .alist li { display: flex; gap: 0.55rem; align-items: baseline; flex-wrap: wrap; font-size: 0.82rem; border-bottom: 1px solid var(--border); padding: 0.25rem 0; }
  .alist .when { font-size: 0.72rem; min-width: 9.5rem; }
  .alist code { font-size: 0.78rem; }
</style>
