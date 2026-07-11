<script lang="ts">
  import { api, ApiError, type Diagnostics } from "./api";
  import Alert from "./Alert.svelte";
  import { confirmDialog } from "./confirm.svelte";

  let diag = $state<Diagnostics | null>(null);
  let restoreMsg = $state(""); let restoreKind = $state<"ok" | "err">("ok");
  let pw = $state({ current: "", next: "", confirm: "" });
  let pwMsg = $state(""); let pwKind = $state<"ok" | "err">("ok");
  let pwBusy = $state(false); let showPw = $state(false);
  let dangerMsg = $state(""); let dangerKind = $state<"ok" | "err">("ok");

  function errText(err: unknown, fb: string) { return err instanceof ApiError ? err.message : fb; }
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
    let doc: any;
    try { doc = JSON.parse(await file.text()); }
    catch { restoreMsg = "not a valid backup file"; restoreKind = "err"; input.value = ""; return; }
    if (!doc || typeof doc !== "object" || Array.isArray(doc) || !("schema_version" in doc) || !Array.isArray(doc.nodes)) {
      restoreMsg = "not a valid backup file"; restoreKind = "err"; input.value = ""; return;
    }
    try {
      const r = await api.restore(doc);
      restoreMsg = `restored ${r.restored.nodes} nodes, ${r.restored.profiles} profiles — reconnect to apply`;
      restoreKind = "ok";
    } catch (err) { restoreMsg = errText(err, "restore failed"); restoreKind = "err"; }
    finally { input.value = ""; }
  }

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
    if (pwBusy || !pwOk) return;
    pwBusy = true;
    try {
      await api.changePassword(pw.current, pw.next);
      pwMsg = "password changed — other sessions signed out"; pwKind = "ok";
      pw = { current: "", next: "", confirm: "" };
    } catch (err) { pwMsg = errText(err, "change failed"); pwKind = "err"; }
    finally { pwBusy = false; }
  }

  async function resetDefaults() {
    if (!(await confirmDialog("Reset all panel settings (stats, health, failover, session) to their defaults? Nodes, subscriptions and routing are kept."))) return;
    try { await api.resetSettings(); dangerMsg = "settings reset to defaults"; dangerKind = "ok"; }
    catch (err) { dangerMsg = errText(err, "reset failed"); dangerKind = "err"; }
  }

  $effect(() => { api.getDiagnostics().then((d) => (diag = d)).catch(() => {}); });
</script>

<div class="ops-grid">
  <!-- LEFT: backup & restore -->
  <div class="card">
    <div class="card-top"><span class="eyebrow">Backup &amp; Restore</span></div>
    <div class="row">
      <button class="btn btn-primary" type="button" onclick={downloadBackup}>+ Create backup</button>
      <label class="file btn">Restore…<input type="file" accept="application/json,.json" onchange={onRestoreFile} /></label>
    </div>
    <Alert msg={restoreMsg} kind={restoreKind} />
    <p class="muted-sm">A backup bundles nodes, subscriptions, anti-DPI profiles, routing rules and settings into one JSON file. Restore replaces all of them.</p>
  </div>

  <!-- RIGHT column: system · admin · danger -->
  <div class="col">
    <div class="card">
      <div class="card-top"><span class="eyebrow">System</span></div>
      {#if diag}
        <div class="kv">
          <div><span class="kv-k">app version</span><span class="kv-v">{diag.app_version}</span></div>
          <div><span class="kv-k">xray-core</span><span class="kv-v mono">{diag.xray_version}</span></div>
          <div><span class="kv-k">panel uptime</span><span class="kv-v">{fmtUptime(diag.uptime_sec)}</span></div>
          <div><span class="kv-k">database</span><span class="kv-v mono">{fmtBytes(diag.db_bytes)}</span></div>
          <div><span class="kv-k">disk free</span><span class="kv-v">{fmtBytes(diag.disk_free_bytes)} / {fmtBytes(diag.disk_total_bytes)}</span></div>
        </div>
      {:else}<p class="msg">diagnostics unavailable</p>{/if}
    </div>

    <form class="card" onsubmit={changePassword}>
      <div class="card-top"><span class="eyebrow">Admin</span></div>
      <div class="pw">
        <input class="input" type={showPw ? "text" : "password"} bind:value={pw.current} placeholder="current password" autocomplete="current-password" />
        <button type="button" class="pw-toggle" tabindex="-1" onclick={() => (showPw = !showPw)} aria-label={showPw ? "Hide passwords" : "Show passwords"}>{showPw ? "Hide" : "Show"}</button>
      </div>
      <input class="input" type={showPw ? "text" : "password"} bind:value={pw.next} placeholder="new password (min 8)" autocomplete="new-password" />
      <input class="input" type={showPw ? "text" : "password"} bind:value={pw.confirm} placeholder="confirm new password" autocomplete="new-password" />
      {#if pw.next}<p class="muted-sm">strength: {pwStrength}{#if pw.confirm && pw.next !== pw.confirm} · <span class="nomatch">does not match</span>{/if}</p>{/if}
      <div><button class="btn btn-primary" disabled={!pwOk || pwBusy}>Change password</button></div>
      <Alert msg={pwMsg} kind={pwKind} />
    </form>

    <div class="card danger">
      <div class="card-top"><span class="eyebrow err">Danger zone</span></div>
      <div class="danger-row">
        <span class="muted-sm">Reset all panel settings to defaults — keeps nodes, subscriptions &amp; routing.</span>
        <button class="btn btn-danger" type="button" onclick={resetDefaults}>Reset settings</button>
      </div>
      <Alert msg={dangerMsg} kind={dangerKind} />
    </div>
  </div>
</div>

<style>
  .ops-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0.9rem; align-items: start; }
  .col { display: flex; flex-direction: column; gap: 0.9rem; }
  .card-top { display: flex; align-items: center; justify-content: space-between; gap: 0.6rem; }
  .eyebrow.err { color: var(--err); }
  .muted-sm { font-size: 0.74rem; color: var(--tx3); }
  .row { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
  .file { position: relative; overflow: hidden; cursor: pointer; }
  .file input { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
  .nomatch { color: var(--err); }
  .pw { position: relative; display: flex; }
  .pw .input { flex: 1; padding-right: 3.4rem; }
  .pw-toggle { position: absolute; right: 0.4rem; top: 50%; transform: translateY(-50%); background: none; border: 0; padding: 0.2rem 0.4rem; font-size: 0.7rem; color: var(--tx3); cursor: pointer; }
  .pw-toggle:hover { color: var(--tx2); }

  .kv { display: flex; flex-direction: column; gap: 0.5rem; }
  .kv > div { display: flex; justify-content: space-between; gap: 0.6rem; font-size: 0.8rem; }
  .kv-k { color: var(--tx3); } .kv-v { color: var(--tx2); }

  .danger { background: color-mix(in srgb, var(--err) 7%, var(--bg1)); border-color: color-mix(in srgb, var(--err) 40%, var(--bd)); }
  .danger-row { display: flex; align-items: center; justify-content: space-between; gap: 0.8rem; flex-wrap: wrap; }

  @media (max-width: 900px) { .ops-grid { grid-template-columns: 1fr; } }
  @media (max-width: 480px) {
    .danger-row { flex-direction: column; align-items: stretch; }
    .danger-row .btn { width: 100%; }
  }
</style>
