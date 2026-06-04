<script lang="ts">
  import { api, ApiError, type Node, type NodeHealth, type TuningProfile, type Status } from "./api";

  let nodes = $state<Node[]>([]);
  let health = $state<Record<number, NodeHealth>>({});
  let profiles = $state<TuningProfile[]>([]);
  let msg = $state("");
  let form = $state({ name: "", address: "", port: 443, uuid: "", transport: "vision",
                      sni: "", public_key: "", short_id: "", fingerprint: "chrome" });
  let editId = $state<number | null>(null);
  let edit = $state({ name: "", address: "", port: 443, uuid: "", transport: "vision",
                      sni: "", public_key: "", short_id: "", fingerprint: "chrome" });

  let status = $state<Status | null>(null);
  const activeId = $derived(status?.active_node_id ?? null);

  async function refresh() {
    try {
      const [ns, hs, ps, st] = await Promise.all([
        api.listNodes(), api.listNodeHealth(), api.listProfiles(), api.getStatus()]);
      nodes = ns;
      health = Object.fromEntries(hs.map((h) => [h.node_id, h]));
      profiles = ps;
      status = st;
    } catch (err) { msg = err instanceof ApiError ? err.message : "load failed"; }
  }
  async function connect(id: number) {
    try { await api.apply(id); await refresh(); }
    catch (err) { msg = err instanceof ApiError ? err.message : "connect failed"; }
  }
  async function disconnect(id: number) {
    try { await api.disconnect(id); await refresh(); }
    catch (err) { msg = err instanceof ApiError ? err.message : "disconnect failed"; }
  }
  async function add(e: Event) {
    e.preventDefault();
    try { await api.addNode({ ...form }); form.name = ""; form.address = ""; form.uuid = ""; await refresh(); }
    catch (err) { msg = err instanceof ApiError ? err.message : "add failed"; }
  }
  function startEdit(n: Node) {
    editId = n.id;
    edit = { name: n.name, address: n.address, port: n.port, uuid: n.uuid, transport: n.transport,
             sni: n.sni, public_key: n.public_key, short_id: n.short_id, fingerprint: n.fingerprint };
  }
  async function saveEdit() {
    if (editId === null) return;
    try { await api.updateNode(editId, { ...edit }); editId = null; await refresh(); }
    catch (err) { msg = err instanceof ApiError ? err.message : "save failed"; }
  }
  async function del(id: number) {
    try { await api.deleteNode(id); await refresh(); }
    catch (err) { msg = err instanceof ApiError ? err.message : "delete failed"; }
  }
  async function assignProfile(n: Node, value: string) {
    try { await api.updateNode(n.id, { tuning_profile_id: value === "" ? null : Number(value) }); await refresh(); }
    catch (err) { msg = err instanceof ApiError ? err.message : "assign failed"; }
  }
  function profileName(id: number | null): string {
    return id === null ? "(default)" : (profiles.find((p) => p.id === id)?.name ?? `#${id}`);
  }

  $effect(() => { refresh(); });
</script>

{#if msg}<p class="msg">{msg}</p>{/if}

{#snippet healthCells(h: NodeHealth | undefined)}
  <td>{#if h?.last_tcp_ok}<span class="ok">✓ {h.last_tcp_ms}ms</span>{:else if h && h.last_tcp_ok === false}<span class="bad">✕</span>{:else}—{/if}</td>
  <td>{#if h?.last_real_ok}<span class="ok">✓ {h.last_real_ms}ms</span>{:else if h && h.last_real_ok === false}<span class="bad">✕</span>{:else}—{/if}</td>
  <td>{h?.egress_ip ?? "—"}</td>
{/snippet}

<div class="card">
  <table class="table">
    <thead><tr>
      <th>id</th><th>name</th><th>address</th><th>port</th><th>transport</th>
      <th>profile</th><th>TCP</th><th>real</th><th>egress</th><th>sub</th><th></th>
    </tr></thead>
    <tbody>
      {#each nodes as n (n.id)}
        {#if editId === n.id}
          <tr>
            <td>{n.id}</td>
            <td><input class="input" bind:value={edit.name} /></td>
            <td><input class="input" bind:value={edit.address} /></td>
            <td><input class="input" type="number" bind:value={edit.port} /></td>
            <td>
              <select class="input" bind:value={edit.transport}>
                <option value="vision">vision</option>
                <option value="xhttp">xhttp</option>
              </select>
            </td>
            <td>{profileName(n.tuning_profile_id)}</td>
            {@render healthCells(health[n.id])}
            <td>{n.subscription_id ?? "—"}</td>
            <td class="actions"><button class="btn btn-primary" onclick={saveEdit}>Save</button> <button class="btn" onclick={() => (editId = null)}>Cancel</button></td>
          </tr>
        {:else}
          <tr class:stale={n.stale} class:active={n.id === activeId}>
            <td>{n.id}</td>
            <td>{n.name}{#if n.id === activeId}<span class="connected">● connected</span>{/if}</td>
            <td>{n.address}</td><td>{n.port}</td><td>{n.transport}</td>
            <td>
              <select class="input" value={n.tuning_profile_id === null ? "" : String(n.tuning_profile_id)}
                      onchange={(e) => assignProfile(n, e.currentTarget.value)}>
                <option value="">(default)</option>
                {#each profiles as p (p.id)}<option value={String(p.id)}>{p.name}</option>{/each}
              </select>
            </td>
            {@render healthCells(health[n.id])}
            <td>{n.subscription_id ?? "—"}</td>
            <td class="actions">
              {#if n.id === activeId}
                <button class="btn" onclick={() => disconnect(n.id)}>Disconnect</button>
              {:else}
                <button class="btn btn-primary" onclick={() => connect(n.id)}>Connect</button>
              {/if}
              <button class="btn" onclick={() => startEdit(n)}>Edit</button>
              <button class="btn btn-danger" onclick={() => del(n.id)}>Delete</button>
            </td>
          </tr>
        {/if}
      {/each}
    </tbody>
  </table>
</div>

<form onsubmit={add} class="card add">
  <h3>Add node</h3>
  <div class="grid">
    <input class="input" bind:value={form.name} placeholder="name" required />
    <input class="input" bind:value={form.address} placeholder="address" required />
    <input class="input" type="number" bind:value={form.port} placeholder="port" required />
    <input class="input" bind:value={form.uuid} placeholder="uuid" required />
    <select class="input" bind:value={form.transport}>
      <option value="vision">vision</option>
      <option value="xhttp">xhttp</option>
    </select>
    <input class="input" bind:value={form.sni} placeholder="sni" />
    <input class="input" bind:value={form.public_key} placeholder="reality publicKey" />
    <input class="input" bind:value={form.short_id} placeholder="shortId" />
  </div>
  <div><button class="btn btn-primary">Add node</button></div>
</form>

<style>
  .add .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(11rem, 1fr)); gap: 0.5rem; }
  .actions { white-space: nowrap; }
  tr.stale { opacity: 0.55; }
  tr.active td { background: color-mix(in srgb, var(--accent) 12%, transparent); }
  tr.active td:first-child { box-shadow: inset 3px 0 0 var(--accent); }
  .connected { margin-left: 0.5rem; color: var(--accent); font-size: 0.72rem; font-weight: 700; white-space: nowrap; }
  .ok { color: var(--success); }
  .bad { color: var(--danger); }
</style>
