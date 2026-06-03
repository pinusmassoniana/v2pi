<script lang="ts">
  import { api, ApiError, type TuningProfile, type Node } from "./api";

  // editor doubles as "add" (id=null) and "edit" (id set) — one form, no duplication.
  const blank = () => ({
    id: null as number | null, name: "", fingerprint: "chrome",
    frag_enabled: false, frag_packets: "tlshello", frag_length: "100-200", frag_interval: "10-20",
    mux_enabled: false, doh_enabled: true, doh_url: "", quic: "allow",
  });

  let profiles = $state<TuningProfile[]>([]);
  let nodes = $state<Node[]>([]);
  let msg = $state("");
  let editor = $state(blank());

  async function refresh() {
    try { [profiles, nodes] = await Promise.all([api.listProfiles(), api.listNodes()]); }
    catch (err) { msg = err instanceof ApiError ? err.message : "load failed"; }
  }
  function edit(p: TuningProfile) {
    editor = {
      id: p.id, name: p.name, fingerprint: p.fingerprint, frag_enabled: p.frag_enabled,
      frag_packets: p.frag_packets, frag_length: p.frag_length, frag_interval: p.frag_interval,
      mux_enabled: p.mux_enabled, doh_enabled: p.doh_enabled, doh_url: p.doh_url, quic: p.quic,
    };
  }
  async function save(e: Event) {
    e.preventDefault();
    const { id, ...body } = editor;
    try {
      if (id === null) await api.addProfile(body);
      else await api.updateProfile(id, body);
      editor = blank();
      await refresh();
    } catch (err) { msg = err instanceof ApiError ? err.message : "save failed"; }
  }
  async function del(id: number) {
    try { await api.deleteProfile(id); if (editor.id === id) editor = blank(); await refresh(); }
    catch (err) { msg = err instanceof ApiError ? err.message : "delete failed"; }
  }
  async function makeDefault(id: number) {
    try { await api.setDefaultProfile(id); await refresh(); }
    catch (err) { msg = err instanceof ApiError ? err.message : "set-default failed"; }
  }
  async function assign(n: Node, value: string) {
    try { await api.updateNode(n.id, { tuning_profile_id: value === "" ? null : Number(value) }); await refresh(); }
    catch (err) { msg = err instanceof ApiError ? err.message : "assign failed"; }
  }

  $effect(() => { refresh(); });
</script>

{#if msg}<p class="msg">{msg}</p>{/if}

<div class="card">
  <h3>Tuning profiles</h3>
  <table class="table">
    <thead><tr><th>name</th><th>fingerprint</th><th>frag</th><th>mux</th><th>DoH</th><th>QUIC</th><th></th></tr></thead>
    <tbody>
      {#each profiles as p (p.id)}
        <tr>
          <td>{p.name}{#if p.is_default} <span class="badge">default</span>{/if}</td>
          <td>{p.fingerprint}</td>
          <td>{p.frag_enabled ? "on" : "off"}</td>
          <td>{p.mux_enabled ? "on" : "off"}</td>
          <td>{p.doh_enabled ? "on" : "off"}</td>
          <td>{p.quic}</td>
          <td class="row-actions">
            <button class="btn" onclick={() => edit(p)}>Edit</button>
            {#if !p.is_default}
              <button class="btn" onclick={() => makeDefault(p.id)}>Make default</button>
              <button class="btn btn-danger" onclick={() => del(p.id)}>Delete</button>
            {/if}
          </td>
        </tr>
      {/each}
    </tbody>
  </table>
</div>

<form onsubmit={save} class="card editor">
  <h3>{editor.id === null ? "New profile" : `Edit profile #${editor.id}`}</h3>
  <label class="field"><span>name</span><input class="input" bind:value={editor.name} placeholder="name" required /></label>
  <label class="field"><span>fingerprint</span>
    <select class="input" bind:value={editor.fingerprint}>
      <option value="chrome">chrome</option>
      <option value="firefox">firefox</option>
      <option value="safari">safari</option>
      <option value="randomized">randomized</option>
      <option value="">empty</option>
    </select>
  </label>
  <label class="check"><input type="checkbox" bind:checked={editor.frag_enabled} /> TLS fragmentation</label>
  <label class="field"><span>packets</span><input class="input" bind:value={editor.frag_packets} /></label>
  <label class="field"><span>length</span><input class="input" bind:value={editor.frag_length} /></label>
  <label class="field"><span>interval</span><input class="input" bind:value={editor.frag_interval} /></label>
  <label class="check"><input type="checkbox" bind:checked={editor.mux_enabled} /> Mux</label>
  <label class="check"><input type="checkbox" bind:checked={editor.doh_enabled} /> DoH</label>
  <label class="field"><span>DoH URL</span><input class="input" bind:value={editor.doh_url} placeholder="(default resolver)" /></label>
  <label class="field"><span>QUIC</span>
    <select class="input" bind:value={editor.quic}>
      <option value="allow">allow</option>
      <option value="drop">drop (block)</option>
      <option value="proxy">proxy</option>
    </select>
  </label>
  <div class="actions">
    <button class="btn btn-primary">{editor.id === null ? "Create" : "Save"}</button>
    {#if editor.id !== null}<button class="btn" type="button" onclick={() => (editor = blank())}>New</button>{/if}
  </div>
</form>

<div class="card">
  <h3>Per-node profile</h3>
  <table class="table">
    <thead><tr><th>node</th><th>address</th><th>profile</th></tr></thead>
    <tbody>
      {#each nodes as n (n.id)}
        <tr>
          <td>{n.name}</td>
          <td>{n.address}</td>
          <td>
            <select class="input" value={n.tuning_profile_id === null ? "" : String(n.tuning_profile_id)}
                    onchange={(e) => assign(n, e.currentTarget.value)}>
              <option value="">(inherit default)</option>
              {#each profiles as p (p.id)}
                <option value={String(p.id)}>{p.name}</option>
              {/each}
            </select>
          </td>
        </tr>
      {/each}
    </tbody>
  </table>
</div>

<style>
  .editor { max-width: 30rem; }
  .row-actions { display: flex; gap: 0.35rem; flex-wrap: wrap; }
  .check { display: flex; gap: 0.5rem; align-items: center; }
  .actions { display: flex; gap: 0.5rem; }
</style>
