<script lang="ts">
  import { api, ApiError, type Node, type NodeHealth, type TuningProfile, type Status, type Subscription } from "./api";
  import Modal from "./Modal.svelte";

  let nodes = $state<Node[]>([]);
  let health = $state<Record<number, NodeHealth>>({});
  let profiles = $state<TuningProfile[]>([]);
  let subs = $state<Subscription[]>([]);
  let status = $state<Status | null>(null);
  let msg = $state("");
  let tab = $state<number | "servers" | null>(null);   // selected switcher tab (sub id | manual)
  let addOpen = $state(false);
  let form = $state({ name: "", address: "", port: 443, uuid: "", transport: "vision",
                      sni: "", public_key: "", short_id: "", fingerprint: "chrome" });
  let editId = $state<number | null>(null);
  let edit = $state({ name: "", address: "", port: 443, uuid: "", transport: "vision",
                      sni: "", public_key: "", short_id: "", fingerprint: "chrome",
                      tuning_profile_id: null as number | null });

  const activeId = $derived(status?.active_node_id ?? null);
  const shown = $derived(nodes.filter((n) =>
    tab === "servers" ? n.subscription_id === null
    : tab === null ? false
    : n.subscription_id === tab));

  async function refresh() {
    try {
      const [ns, hs, ps, ss, st] = await Promise.all([
        api.listNodes(), api.listNodeHealth(), api.listProfiles(), api.listSubs(), api.getStatus()]);
      nodes = ns;
      health = Object.fromEntries(hs.map((h) => [h.node_id, h]));
      profiles = ps;
      subs = ss;
      status = st;
      if (tab === null || (tab !== "servers" && !ss.some((s) => s.id === tab)))
        tab = ss[0]?.id ?? "servers";
    } catch (err) { msg = err instanceof ApiError ? err.message : "load failed"; }
  }
  async function add(e: Event) {
    e.preventDefault();
    try {
      await api.addNode({ ...form });
      form.name = ""; form.address = ""; form.uuid = ""; addOpen = false;
      await refresh();
    } catch (err) { msg = err instanceof ApiError ? err.message : "add failed"; }
  }
  function startEdit(n: Node) {
    editId = n.id;
    edit = { name: n.name, address: n.address, port: n.port, uuid: n.uuid, transport: n.transport,
             sni: n.sni, public_key: n.public_key, short_id: n.short_id, fingerprint: n.fingerprint,
             tuning_profile_id: n.tuning_profile_id };
  }
  async function saveEdit(e: Event) {
    e.preventDefault();
    if (editId === null) return;
    try { await api.updateNode(editId, { ...edit }); editId = null; await refresh(); }
    catch (err) { msg = err instanceof ApiError ? err.message : "save failed"; }
  }
  async function del(id: number) {
    try { await api.deleteNode(id); await refresh(); }
    catch (err) { msg = err instanceof ApiError ? err.message : "delete failed"; }
  }
  async function connect(id: number) {
    try { await api.apply(id); await refresh(); }
    catch (err) { msg = err instanceof ApiError ? err.message : "connect failed"; }
  }
  async function disconnect(id: number) {
    try { await api.disconnect(id); await refresh(); }
    catch (err) { msg = err instanceof ApiError ? err.message : "disconnect failed"; }
  }
  let pinging = $state<"tcp" | "http" | null>(null);
  async function pingAll(kind: "tcp" | "http") {
    pinging = kind;
    try {
      const hs = kind === "tcp" ? await api.probeTcp() : await api.probeHttp();
      health = Object.fromEntries(hs.map((h) => [h.node_id, h]));
    } catch (err) { msg = err instanceof ApiError ? err.message : "ping failed"; }
    finally { pinging = null; }
  }
  let probingId = $state<number | null>(null);
  async function probeNode(id: number) {
    probingId = id;
    try { const h = await api.probeNode(id); health = { ...health, [h.node_id]: h }; }
    catch (err) { msg = err instanceof ApiError ? err.message : "probe failed"; }
    finally { probingId = null; }
  }

  $effect(() => { refresh(); });
</script>

{#if msg}<p class="msg">{msg}</p>{/if}

<div class="switcher">
  {#each subs as s (s.id)}
    <button class="tab" class:active={tab === s.id} onclick={() => (tab = s.id)}>{s.name}</button>
  {/each}
  <button class="tab" class:active={tab === "servers"} onclick={() => (tab = "servers")}>Servers</button>
  {#if tab === "servers"}
    <button class="btn btn-primary add-btn" onclick={() => (addOpen = true)}>+ Add server</button>
  {/if}
</div>

<div class="ping-bar">
  <button class="btn" onclick={() => pingAll("tcp")} disabled={pinging !== null}>{pinging === "tcp" ? "TCP pinging…" : "TCP ping all"}</button>
  <button class="btn" onclick={() => pingAll("http")} disabled={pinging !== null}>{pinging === "http" ? "HTTP pinging…" : "HTTP ping all"}</button>
</div>

{#snippet healthCells(h: NodeHealth | undefined)}
  <td>{#if h?.last_tcp_ok}<span class="ok">✓ {h.last_tcp_ms}ms</span>{:else if h && h.last_tcp_ok === false}<span class="bad">✕</span>{:else}—{/if}</td>
  <td>{#if h?.last_http_ok}<span class="ok">✓ {h.last_http_ms}ms</span>{:else if h && h.last_http_ok === false}<span class="bad">✕</span>{:else}—{/if}</td>
  <td>{#if h?.last_real_ok}<span class="ok">✓ {h.last_real_ms}ms</span>{:else if h && h.last_real_ok === false}<span class="bad">✕</span>{:else}—{/if}</td>
  <td>{h?.egress_ip ?? "—"}</td>
{/snippet}

<div class="card">
  <table class="table">
    <thead><tr>
      <th>id</th><th>name</th><th>address</th><th>port</th><th>transport</th>
      <th>TCP</th><th>HTTP</th><th>real</th><th>egress</th><th></th>
    </tr></thead>
    <tbody>
      {#each shown as n (n.id)}
        <tr class:stale={n.stale} class:active={n.id === activeId}>
          <td>{n.id}</td>
          <td>{n.name}{#if n.id === activeId}<span class="connected">● connected</span>{/if}</td>
          <td>{n.address}</td><td>{n.port}</td><td>{n.transport}</td>
          {@render healthCells(health[n.id])}
          <td class="actions">
            {#if n.id === activeId}
              <button class="btn" onclick={() => disconnect(n.id)}>Disconnect</button>
            {:else}
              <button class="btn btn-primary" onclick={() => connect(n.id)}>Connect</button>
            {/if}
            <button class="btn t-btn" title="Test this node — TCP + HTTP + real through it" onclick={() => probeNode(n.id)} disabled={probingId === n.id}>{probingId === n.id ? "…" : "T"}</button>
            <button class="btn" onclick={() => startEdit(n)}>Edit</button>
            {#if tab === "servers"}<button class="btn btn-danger" onclick={() => del(n.id)}>Delete</button>{/if}
          </td>
        </tr>
      {/each}
      {#if shown.length === 0}
        <tr><td colspan="10" class="muted empty">No servers here{#if tab === "servers"} — add one with “+ Add server”.{/if}</td></tr>
      {/if}
    </tbody>
  </table>
</div>

{#if addOpen}
  <Modal title="Add server" onClose={() => (addOpen = false)}>
    <form onsubmit={add} class="grid-form">
      <input class="input" bind:value={form.name} placeholder="name" required />
      <input class="input" bind:value={form.address} placeholder="address" required />
      <input class="input" type="number" bind:value={form.port} placeholder="port" required />
      <input class="input" bind:value={form.uuid} placeholder="uuid" required />
      <select class="input" bind:value={form.transport}>
        <option value="vision">vision</option><option value="xhttp">xhttp</option>
      </select>
      <input class="input" bind:value={form.sni} placeholder="sni" />
      <input class="input" bind:value={form.public_key} placeholder="reality publicKey" />
      <input class="input" bind:value={form.short_id} placeholder="shortId" />
      <div class="form-actions"><button class="btn btn-primary">Add server</button></div>
    </form>
  </Modal>
{/if}

{#if editId !== null}
  <Modal title="Edit node" onClose={() => (editId = null)}>
    <form onsubmit={saveEdit} class="grid-form">
      <label class="field"><span>name</span><input class="input" bind:value={edit.name} /></label>
      <label class="field"><span>address</span><input class="input" bind:value={edit.address} /></label>
      <label class="field"><span>port</span><input class="input" type="number" bind:value={edit.port} /></label>
      <label class="field"><span>uuid</span><input class="input" bind:value={edit.uuid} /></label>
      <label class="field"><span>transport</span>
        <select class="input" bind:value={edit.transport}>
          <option value="vision">vision</option><option value="xhttp">xhttp</option>
        </select></label>
      <label class="field"><span>sni</span><input class="input" bind:value={edit.sni} /></label>
      <label class="field"><span>fingerprint</span><input class="input" bind:value={edit.fingerprint} /></label>
      <label class="field"><span>reality publicKey</span><input class="input" bind:value={edit.public_key} /></label>
      <label class="field"><span>shortId</span><input class="input" bind:value={edit.short_id} /></label>
      <label class="field"><span>tuning profile</span>
        <select class="input" value={edit.tuning_profile_id === null ? "" : String(edit.tuning_profile_id)}
                onchange={(e) => (edit.tuning_profile_id = e.currentTarget.value === "" ? null : Number(e.currentTarget.value))}>
          <option value="">(default)</option>
          {#each profiles as p (p.id)}<option value={String(p.id)}>{p.name}</option>{/each}
        </select></label>
      <div class="form-actions"><button class="btn btn-primary">Save</button>
        <button class="btn" type="button" onclick={() => (editId = null)}>Cancel</button></div>
    </form>
  </Modal>
{/if}

<style>
  .switcher { display: flex; flex-wrap: wrap; gap: 0.35rem; align-items: center; margin-bottom: 0.7rem; }
  .tab {
    background: var(--surface-2); border: 1px solid var(--border); color: var(--muted);
    padding: 0.35rem 0.75rem; border-radius: 999px; cursor: pointer; font: inherit;
  }
  .tab:hover { color: var(--text); }
  .tab.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .switcher .add-btn { margin-left: auto; }
  .ping-bar { display: flex; gap: 0.4rem; margin-bottom: 0.6rem; }
  .actions { white-space: nowrap; }
  .t-btn { font-weight: 700; min-width: 2rem; }
  tr.stale { opacity: 0.55; }
  tr.active td { background: color-mix(in srgb, var(--accent) 12%, transparent); }
  tr.active td:first-child { box-shadow: inset 3px 0 0 var(--accent); }
  .connected { margin-left: 0.5rem; color: var(--accent); font-size: 0.72rem; font-weight: 700; white-space: nowrap; }
  .ok { color: var(--success); }
  .bad { color: var(--danger); }
  .empty { text-align: center; padding: 1.2rem; }
  .grid-form { display: grid; grid-template-columns: repeat(auto-fill, minmax(13rem, 1fr)); gap: 0.6rem; }
  .form-actions { grid-column: 1 / -1; display: flex; gap: 0.5rem; }
  .field { display: grid; gap: 0.2rem; }
</style>
