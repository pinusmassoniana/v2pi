<script lang="ts">
  import { api, ApiError, type Subscription, type Preview, type PreviewNodes, type TuningProfile } from "./api";
  import Modal from "./Modal.svelte";
  import Alert from "./Alert.svelte";
  import { confirmDialog } from "./confirm.svelte";
  import { serverNow } from "./status.svelte";
  import { I } from "./icons";

  let subs = $state<Subscription[]>([]);
  let profiles = $state<TuningProfile[]>([]);
  let msg = $state("");
  let msgKind = $state<"ok" | "err">("ok");
  // add form — interval is set here now (F5); 0 = off.
  let form = $state({ name: "", url: "", interval_min: 0 });
  // default injection mirrors the backend default_injection() (F6)
  let headers = $state<{ k: string; v: string }[]>([
    { k: "x-device-os", v: "{device_os}" },
    { k: "user-agent", v: "v2pi/1.0" },
  ]);
  let queries = $state<{ k: string; v: string }[]>([]);
  let preview = $state<Preview | null>(null);
  let previewNodes = $state<PreviewNodes | null>(null);
  let refreshing = $state<Record<number, boolean>>({});
  let refreshingAll = $state(false);

  // edit modal
  let editId = $state<number | null>(null);
  let editForm = $state({ name: "", url: "", interval_min: 0, enabled: true,
                          default_profile_id: null as number | null });
  let editHeaders = $state<{ k: string; v: string }[]>([]);
  let editQueries = $state<{ k: string; v: string }[]>([]);

  function setMsg(text: string, kind: "ok" | "err" = "ok") { msg = text; msgKind = kind; }
  function errText(err: unknown, fallback: string) {
    return err instanceof ApiError ? err.message : fallback;
  }

  function buildInjection(hs: { k: string; v: string }[], qs: { k: string; v: string }[]) {
    const h: Record<string, string> = {};
    for (const r of hs) if (r.k) h[r.k] = r.v;
    const q: Record<string, string> = {};
    for (const r of qs) if (r.k) q[r.k] = r.v;
    return { headers: h, query: q };
  }
  const rows = (obj: Record<string, any> | undefined) =>
    Object.entries(obj ?? {}).map(([k, v]) => ({ k, v: String(v) }));

  function relTime(iso: string | null): string {
    if (!iso) return "—";
    const t = new Date(iso).getTime();
    if (isNaN(t)) return iso;
    const s = Math.max(0, Math.round((serverNow() - t) / 1000));   // Pi-clock aligned, matching Nodes
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  }
  function fmtBytes(n: number | null): string {
    if (n == null) return "—";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0, v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
  }
  function quotaText(s: Subscription): string {
    const parts: string[] = [];
    if (s.total_bytes != null)
      parts.push(`${fmtBytes((s.up_bytes ?? 0) + (s.down_bytes ?? 0))} / ${fmtBytes(s.total_bytes)}`);
    if (s.expire_at) parts.push(`exp ${new Date(s.expire_at * 1000).toLocaleDateString()}`);
    return parts.join(" · ");
  }

  async function refresh() {
    try {
      const [ss, ps] = await Promise.all([api.listSubs(), api.listProfiles()]);
      subs = ss; profiles = ps;
    } catch (err) { setMsg(errText(err, "load failed"), "err"); }
  }
  async function add(e: Event) {
    e.preventDefault();
    try {
      await api.addSub({ name: form.name, url: form.url,
        interval_sec: Math.max(0, Math.round(form.interval_min)) * 60,
        injection: buildInjection(headers, queries) });
      form.name = ""; form.url = ""; form.interval_min = 0;
      preview = null; previewNodes = null;
      setMsg("subscription added", "ok");
      await refresh();
    } catch (err) { setMsg(errText(err, "add failed"), "err"); }
  }
  async function doPreview() {
    try { preview = await api.previewSub(form.url, buildInjection(headers, queries)); }
    catch (err) { setMsg(errText(err, "preview failed"), "err"); }
  }
  async function doPreviewNodes() {
    previewNodes = null;
    try {
      previewNodes = await api.previewSubNodes(form.url, buildInjection(headers, queries));
      setMsg(`parsed ${previewNodes.count} node(s) (${previewNodes.format})`, "ok");
    } catch (err) { setMsg(errText(err, "dry-run failed"), "err"); }
  }
  async function refreshSub(id: number) {
    refreshing = { ...refreshing, [id]: true };
    try { const r = await api.refreshSub(id); setMsg(r.status ?? r.error ?? "refreshed", r.error ? "err" : "ok"); await refresh(); }
    catch (err) { setMsg(errText(err, "refresh failed"), "err"); }
    finally { refreshing = { ...refreshing, [id]: false }; }
  }
  async function refreshAll() {
    refreshingAll = true;
    try {
      const r = await api.refreshAllSubs();
      const results = Array.isArray(r.results) ? r.results : Object.values(r.results ?? {});
      const failures = results.filter((item) => item.error || item.ok === false);
      const details = failures.slice(0, 3).map((item) => `${item.name ?? `#${item.id ?? "?"}`}: ${item.error ?? item.status ?? "failed"}`).join(" · ");
      setMsg(
        `${r.succeeded}/${r.attempted} refreshed${r.failed ? ` · ${r.failed} failed${details ? ` — ${details}` : ""}` : ""}`,
        r.failed ? "err" : "ok",
      );
      await refresh();
    }
    catch (err) { setMsg(errText(err, "refresh-all failed"), "err"); }
    finally { refreshingAll = false; }
  }
  async function toggleEnabled(s: Subscription) {
    try { await api.updateSub(s.id, { enabled: !s.enabled }); await refresh(); }
    catch (err) { setMsg(errText(err, "toggle failed"), "err"); }
  }
  function startEdit(s: Subscription) {
    editId = s.id;
    editForm = { name: s.name, url: s.url, interval_min: Math.round((s.interval_sec || 0) / 60),
                 enabled: s.enabled, default_profile_id: s.default_profile_id };
    editHeaders = rows(s.injection?.headers);
    editQueries = rows(s.injection?.query);
  }
  async function saveEdit() {
    if (editId === null) return;
    try {
      await api.updateSub(editId, {
        name: editForm.name, url: editForm.url,
        interval_sec: Math.max(0, Math.round(editForm.interval_min)) * 60,
        enabled: editForm.enabled, default_profile_id: editForm.default_profile_id,
        injection: buildInjection(editHeaders, editQueries),
      });
      editId = null; setMsg("saved", "ok"); await refresh();
    } catch (err) { setMsg(errText(err, "save failed"), "err"); }
  }
  async function del(s: Subscription) {
    if (!(await confirmDialog(`Delete subscription “${s.name}”?\nIts ${s.node_count} node(s) are detached to Servers (an active connection is kept).`)))
      return;
    try { await api.deleteSub(s.id); editId = null; setMsg("deleted", "ok"); await refresh(); }
    catch (err) { setMsg(errText(err, "delete failed"), "err"); }
  }

  let previewText = $derived(
    preview ? `${preview.method} ${preview.url}\n` +
      Object.entries(preview.headers).map(([k, v]) => `${k}: ${v}`).join("\n") : "");

  $effect(() => { refresh(); });
</script>

<Alert {msg} kind={msgKind} />

<div class="bar">
  <button class="btn" onclick={refreshAll} disabled={refreshingAll || subs.length === 0}>
    {refreshingAll ? "Refreshing…" : "Refresh all"}
  </button>
</div>

<div class="card">
  <div class="table-wrap"><table class="table subs">
    <thead><tr><th>id</th><th>name</th><th>url</th><th>nodes</th><th>autoupdate</th><th>last</th><th>fetched</th><th></th></tr></thead>
    <tbody>
      {#each subs as s (s.id)}
        <tr class:paused={!s.enabled}>
          <td data-label="id">{s.id}</td>
          <td class="col-name" data-label="name">
            {s.name}
            {#if quotaText(s)}<div class="sub muted">{quotaText(s)}</div>{/if}
          </td>
          <td class="url" data-label="url" title={s.url}>{s.url}</td>
          <td data-label="nodes">{s.node_count}</td>
          <td data-label="autoupdate">{s.interval_sec > 0 ? `every ${Math.round(s.interval_sec / 60)} min` : "off"}</td>
          <td data-label="last" title={s.last_error ?? ""}>
            {s.last_status ?? "—"}{#if s.last_path} <small class="muted">({s.last_path})</small>{/if}
            {#if s.last_error}<span class="warn" title={s.last_error}>⚠</span>{/if}
          </td>
          <td class="muted" data-label="fetched" title={s.last_fetched ?? ""}>{relTime(s.last_fetched)}</td>
          <td data-label="">
            <div class="actions">
            <button class="btn iconbtn" title="Refresh now" aria-label="Refresh subscription" onclick={() => refreshSub(s.id)} disabled={refreshing[s.id]}>{#if refreshing[s.id]}…{:else}{@html I.refresh}{/if}</button>
            <button class="btn iconbtn" title={s.enabled ? "Pause auto-update" : "Resume auto-update"} aria-label={s.enabled ? "Pause" : "Resume"} onclick={() => toggleEnabled(s)}>{@html s.enabled ? I.pause : I.play}</button>
            <button class="btn iconbtn" title="Edit" aria-label="Edit subscription" onclick={() => startEdit(s)}>{@html I.edit}</button>
            <button class="btn iconbtn btn-danger" title="Delete" aria-label="Delete subscription" onclick={() => del(s)}>{@html I.trash}</button>
            </div>
          </td>
        </tr>
      {/each}
      {#if subs.length === 0}
        <tr><td colspan="8" class="muted empty">No subscriptions yet — add one below.</td></tr>
      {/if}
    </tbody>
  </table></div>
</div>

<form onsubmit={add} class="card add">
  <h3>Add subscription</h3>
  <input class="input" bind:value={form.name} placeholder="name" aria-label="subscription name" required />
  <input class="input" bind:value={form.url} placeholder="https://…/sub" aria-label="subscription URL" required />
  <label class="field"><span>Autoupdate (minutes, 0 = off; min 1)</span>
    <input class="input" type="number" min="0" bind:value={form.interval_min} /></label>

  <fieldset>
    <legend>Injected headers</legend>
    {#each headers as row, i (i)}
      <div class="kv">
        <input class="input" bind:value={row.k} placeholder="header" aria-label={`Header ${i + 1} name`} />
        <input class="input" bind:value={row.v} placeholder="value" aria-label={`Header ${i + 1} value`} />
        <button class="btn btn-ghost" type="button" onclick={() => headers.splice(i, 1)} aria-label="remove">×</button>
      </div>
    {/each}
    <div><button class="btn" type="button" onclick={() => headers.push({ k: "", v: "" })}>+ header</button></div>
    <p class="muted privacy">Detailed device fingerprint placeholders are opt-in; the default sends only coarse OS data.</p>
  </fieldset>

  <fieldset>
    <legend>Query params</legend>
    {#each queries as row, i (i)}
      <div class="kv">
        <input class="input" bind:value={row.k} placeholder="key" aria-label={`Query ${i + 1} name`} />
        <input class="input" bind:value={row.v} placeholder="value" aria-label={`Query ${i + 1} value`} />
        <button class="btn btn-ghost" type="button" onclick={() => queries.splice(i, 1)} aria-label="remove">×</button>
      </div>
    {/each}
    <div><button class="btn" type="button" onclick={() => queries.push({ k: "", v: "" })}>+ query</button></div>
  </fieldset>

  <div class="actions">
    <button class="btn" type="button" onclick={doPreview}>Preview request</button>
    <button class="btn" type="button" onclick={doPreviewNodes}>Dry-run parse</button>
    <button class="btn btn-primary">Add subscription</button>
  </div>
</form>

{#if preview}<pre class="preview">{previewText}</pre>{/if}

{#if previewNodes}
  <div class="card pn">
    <h3>Dry-run: {previewNodes.count} node(s) · <span class="muted">{previewNodes.format}</span></h3>
    {#if previewNodes.truncated}<p class="msg">Showing first {previewNodes.returned_count} of {previewNodes.count} nodes.</p>{/if}
    {#if previewNodes.count === 0}
      <p class="muted">No nodes parsed — check the URL, token, or format.</p>
    {:else}
      <div class="table-wrap"><table class="table">
        <thead><tr><th>name</th><th>address</th><th>port</th><th>transport</th><th>security</th></tr></thead>
        <tbody>
          {#each previewNodes.nodes as n, i (n.address + ':' + n.port + '#' + i)}
            <tr><td>{n.name}</td><td>{n.address}</td><td>{n.port}</td><td>{n.transport}</td><td>{n.security}</td></tr>
          {/each}
        </tbody>
      </table></div>
    {/if}
  </div>
{/if}

{#if editId !== null}
  <Modal title="Edit subscription" onClose={() => (editId = null)}>
    <div class="edit">
      <label class="field"><span>Name</span><input class="input" bind:value={editForm.name} /></label>
      <label class="field"><span>URL</span><input class="input" bind:value={editForm.url} /></label>
      <label class="field"><span>Autoupdate (minutes, 0 = off)</span>
        <input class="input" type="number" min="0" bind:value={editForm.interval_min} /></label>
      <label class="check"><input type="checkbox" bind:checked={editForm.enabled} /> <span>Enabled (auto + “refresh all”)</span></label>
      <label class="field"><span>Default tuning profile for new nodes</span>
        <select class="input" value={editForm.default_profile_id === null ? "" : String(editForm.default_profile_id)}
                onchange={(e) => (editForm.default_profile_id = e.currentTarget.value === "" ? null : Number(e.currentTarget.value))}>
          <option value="">(global default)</option>
          {#each profiles as p (p.id)}<option value={String(p.id)}>{p.name}</option>{/each}
        </select></label>

      <fieldset>
        <legend>Injected headers</legend>
        {#each editHeaders as row, i (i)}
          <div class="kv">
            <input class="input" bind:value={row.k} placeholder="header" aria-label={`Edit header ${i + 1} name`} />
            <input class="input" bind:value={row.v} placeholder="value" aria-label={`Edit header ${i + 1} value`} />
            <button class="btn btn-ghost" type="button" onclick={() => editHeaders.splice(i, 1)} aria-label="remove">×</button>
          </div>
        {/each}
        <div><button class="btn" type="button" onclick={() => editHeaders.push({ k: "", v: "" })}>+ header</button></div>
      </fieldset>

      <fieldset>
        <legend>Query params</legend>
        {#each editQueries as row, i (i)}
          <div class="kv">
            <input class="input" bind:value={row.k} placeholder="key" aria-label={`Edit query ${i + 1} name`} />
            <input class="input" bind:value={row.v} placeholder="value" aria-label={`Edit query ${i + 1} value`} />
            <button class="btn btn-ghost" type="button" onclick={() => editQueries.splice(i, 1)} aria-label="remove">×</button>
          </div>
        {/each}
        <div><button class="btn" type="button" onclick={() => editQueries.push({ k: "", v: "" })}>+ query</button></div>
      </fieldset>

      <div class="actions">
        <button class="btn btn-primary" onclick={saveEdit}>Save</button>
        <button class="btn" onclick={() => (editId = null)}>Cancel</button>
      </div>
    </div>
  </Modal>
{/if}

<style>
  .bar { margin-bottom: 0.6rem; }
  .add { max-width: 42rem; }
  .kv { display: flex; gap: 0.4rem; }
  .kv .input { flex: 1; }
  fieldset { border: 1px solid var(--border); border-radius: var(--radius); padding: 0.85rem 0.95rem; display: grid; gap: 0.55rem; background: var(--surface-2); }
  td.url { max-width: 18rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  td .sub { font-size: 0.72rem; margin-top: 0.1rem; }
  tr.paused { opacity: 0.5; }
  .warn { color: var(--danger); cursor: help; margin-left: 0.2rem; }
  /* wrap so the add-form action buttons (Preview / Dry-run parse / Add) don't overflow a narrow card */
  .actions { display: flex; gap: 0.25rem; flex-wrap: wrap; align-items: center; white-space: nowrap; }
  .edit { display: grid; gap: 0.6rem; }
  .field { display: grid; gap: 0.2rem; }
  .check { display: flex; align-items: center; gap: 0.45rem; }
  .empty { text-align: center; padding: 1.2rem; }
  .pn { margin-top: 0.6rem; }
  .preview {
    background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius-sm);
    padding: 0.6rem; white-space: pre-wrap; font-family: var(--mono); font-size: 0.8rem;
  }
  .privacy { margin: 0; font-size: 0.72rem; }

  /* phone card layout — each subscription row becomes a stacked card with labels (mirrors Nodes N-A).
     Scoped to .subs so the dry-run preview table keeps its horizontal-scroll treatment. */
  @media (max-width: 600px) {
    /* drop the desktop 44rem table min-width (app.css) so the card fits the phone instead of overflowing */
    .table-wrap > .subs { min-width: 0; }
    .subs, .subs :global(thead), .subs :global(tbody), .subs :global(tr), .subs :global(td) { display: block; }
    .subs :global(thead) { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
    .subs :global(tbody tr) {
      border: 1px solid var(--border); border-radius: var(--radius); margin-bottom: 0.6rem;
      padding: 0.3rem 0.2rem; background: var(--surface);
    }
    .subs :global(td) {
      display: flex; justify-content: space-between; align-items: center; gap: 1rem;
      border: none; padding: 0.32rem 0.7rem;
    }
    .subs :global(td)::before {
      content: attr(data-label); color: var(--faint); font-size: 0.66rem; font-weight: 650;
      text-transform: uppercase; letter-spacing: 0.05em; flex: none;
    }
    .subs :global(td[data-label=""])::before { content: none; }
    .subs .col-name { display: block; padding-top: 0.5rem; }
    .subs .col-name::before { content: none; }              /* name is the card title, no label */
    .subs .url { max-width: none; white-space: normal; word-break: break-all; }
    .subs .actions { justify-content: flex-start; flex-wrap: wrap; padding-top: 0.5rem; }
    .subs :global(.empty) { display: block; }
  }
</style>
