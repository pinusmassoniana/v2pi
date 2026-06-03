<script lang="ts">
  import { api, ApiError, type Subscription, type Preview } from "./api";

  let subs = $state<Subscription[]>([]);
  let msg = $state("");
  let form = $state({ name: "", url: "", interval_sec: 3600 });
  let headers = $state<{ k: string; v: string }[]>([
    { k: "x-hwid", v: "{machine_id}" },
    { k: "user-agent", v: "v2pi/1.0" },
  ]);
  let queries = $state<{ k: string; v: string }[]>([]);
  let preview = $state<Preview | null>(null);

  function injection() {
    const h: Record<string, string> = {};
    for (const r of headers) if (r.k) h[r.k] = r.v;
    const q: Record<string, string> = {};
    for (const r of queries) if (r.k) q[r.k] = r.v;
    return { headers: h, query: q };
  }
  async function refresh() {
    try { subs = await api.listSubs(); }
    catch (err) { msg = err instanceof ApiError ? err.message : "load failed"; }
  }
  async function add(e: Event) {
    e.preventDefault();
    try {
      await api.addSub({ name: form.name, url: form.url, interval_sec: form.interval_sec, injection: injection() });
      form.name = ""; form.url = ""; preview = null; await refresh();
    } catch (err) { msg = err instanceof ApiError ? err.message : "add failed"; }
  }
  async function doPreview() {
    try { preview = await api.previewSub(form.url, injection()); }
    catch (err) { msg = err instanceof ApiError ? err.message : "preview failed"; }
  }
  async function refreshSub(id: number) {
    msg = "";
    try { const r = await api.refreshSub(id); msg = r.status ?? "refreshed"; await refresh(); }
    catch (err) { msg = err instanceof ApiError ? err.message : "refresh failed"; }
  }
  async function del(id: number) {
    try { await api.deleteSub(id); await refresh(); }
    catch (err) { msg = err instanceof ApiError ? err.message : "delete failed"; }
  }

  let previewText = $derived(
    preview ? `${preview.method} ${preview.url}\n` +
      Object.entries(preview.headers).map(([k, v]) => `${k}: ${v}`).join("\n") : "");

  $effect(() => { refresh(); });
</script>

{#if msg}<p class="msg">{msg}</p>{/if}

<div class="card">
  <table class="table">
    <thead><tr><th>id</th><th>name</th><th>url</th><th>nodes</th><th>last</th><th></th></tr></thead>
    <tbody>
      {#each subs as s (s.id)}
        <tr>
          <td>{s.id}</td><td>{s.name}</td><td class="url">{s.url}</td><td>{s.node_count}</td>
          <td>{s.last_status ?? "—"}{#if s.last_path} <small class="muted">({s.last_path})</small>{/if}</td>
          <td class="actions"><button class="btn" onclick={() => refreshSub(s.id)}>Refresh</button> <button class="btn btn-danger" onclick={() => del(s.id)}>Delete</button></td>
        </tr>
      {/each}
    </tbody>
  </table>
</div>

<form onsubmit={add} class="card add">
  <h3>Add subscription</h3>
  <input class="input" bind:value={form.name} placeholder="name" required />
  <input class="input" bind:value={form.url} placeholder="https://…/sub" required />
  <label class="field"><span>interval (s)</span><input class="input" type="number" bind:value={form.interval_sec} /></label>

  <fieldset>
    <legend>Injected headers</legend>
    {#each headers as row, i (i)}
      <div class="kv">
        <input class="input" bind:value={row.k} placeholder="header" />
        <input class="input" bind:value={row.v} placeholder="value" />
        <button class="btn btn-ghost" type="button" onclick={() => headers.splice(i, 1)} aria-label="remove">×</button>
      </div>
    {/each}
    <div><button class="btn" type="button" onclick={() => headers.push({ k: "", v: "" })}>+ header</button></div>
  </fieldset>

  <fieldset>
    <legend>Query params</legend>
    {#each queries as row, i (i)}
      <div class="kv">
        <input class="input" bind:value={row.k} placeholder="key" />
        <input class="input" bind:value={row.v} placeholder="value" />
        <button class="btn btn-ghost" type="button" onclick={() => queries.splice(i, 1)} aria-label="remove">×</button>
      </div>
    {/each}
    <div><button class="btn" type="button" onclick={() => queries.push({ k: "", v: "" })}>+ query</button></div>
  </fieldset>

  <div class="actions">
    <button class="btn" type="button" onclick={doPreview}>Preview request</button>
    <button class="btn btn-primary">Add subscription</button>
  </div>
</form>

{#if preview}<pre class="preview">{previewText}</pre>{/if}

<style>
  .add { max-width: 42rem; }
  .kv { display: flex; gap: 0.4rem; }
  .kv .input { flex: 1; }
  fieldset { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 0.6rem; display: grid; gap: 0.4rem; }
  td.url { max-width: 18rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .actions { display: flex; gap: 0.5rem; }
  .preview {
    background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius-sm);
    padding: 0.6rem; white-space: pre-wrap; font-family: var(--mono); font-size: 0.8rem;
  }
</style>
