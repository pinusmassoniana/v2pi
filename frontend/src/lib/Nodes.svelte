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
  const blankForm = () => ({ name: "", address: "", port: 443, uuid: "", transport: "vision",
                            security: "reality", sni: "", public_key: "", short_id: "",
                            fingerprint: "chrome", path: "", host: "", mode: "", alpn: "" });
  let form = $state(blankForm());
  let editId = $state<number | null>(null);
  let edit = $state({ name: "", address: "", port: 443, uuid: "", transport: "vision",
                      security: "reality", sni: "", public_key: "", short_id: "",
                      fingerprint: "chrome", path: "", host: "", mode: "", alpn: "",
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
      form = blankForm(); addOpen = false;
      await refresh();
    } catch (err) { msg = err instanceof ApiError ? err.message : "add failed"; }
  }
  function startEdit(n: Node) {
    editId = n.id;
    edit = { name: n.name, address: n.address, port: n.port, uuid: n.uuid, transport: n.transport,
             security: n.security, sni: n.sni, public_key: n.public_key, short_id: n.short_id,
             fingerprint: n.fingerprint, path: n.path, host: n.host, mode: n.mode, alpn: n.alpn,
             tuning_profile_id: n.tuning_profile_id };
  }
  async function saveEdit(e: Event) {
    e.preventDefault();
    if (editId === null) return;
    try { await api.updateNode(editId, { ...edit }); editId = null; await refresh(); }
    catch (err) { msg = err instanceof ApiError ? err.message : "save failed"; }
  }
  async function del(n: Node) {
    if (!confirm(`Delete server “${n.name}” (${n.address})?`)) return;
    try { await api.deleteNode(n.id); await refresh(); }
    catch (err) { msg = err instanceof ApiError ? err.message : "delete failed"; }
  }
  // N8: reorder the manual (Servers) list by swapping a node with its neighbour.
  async function move(i: number, dir: -1 | 1) {
    const list = shown.map((n) => n.id);
    const j = i + dir;
    if (j < 0 || j >= list.length) return;
    [list[i], list[j]] = [list[j], list[i]];
    try { await api.reorderNodes(list); await refresh(); }
    catch (err) { msg = err instanceof ApiError ? err.message : "reorder failed"; }
  }
  // N4: import nodes from pasted subscription text as manual servers.
  let importOpen = $state(false);
  let importText = $state("");
  let importing = $state(false);
  async function doImport() {
    importing = true;
    try {
      const r = await api.importNodes(importText);
      msg = `imported ${r.added}/${r.total} node(s) (${r.format})`;
      importText = ""; importOpen = false;
      await refresh();
    } catch (err) { msg = err instanceof ApiError ? err.message : "import failed"; }
    finally { importing = false; }
  }
  // N9: connect to the healthiest node in the current group (sub id, or null for Servers).
  let connectingBest = $state(false);
  async function connectBest() {
    connectingBest = true;
    try {
      const r = await api.connectBest(tab === "servers" ? null : (tab as number));
      msg = `connected to node ${r.node_id}`;
      await refresh();
    } catch (err) { msg = err instanceof ApiError ? err.message : "connect-best failed"; }
    finally { connectingBest = false; }
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
    <button class="btn import-btn" onclick={() => (importOpen = true)}>Import</button>
    <button class="btn btn-primary" onclick={() => (addOpen = true)}>+ Add server</button>
  {/if}
</div>

<div class="ping-bar">
  <button class="btn" onclick={() => pingAll("tcp")} disabled={pinging !== null}>{pinging === "tcp" ? "TCP pinging…" : "TCP ping all"}</button>
  <button class="btn" onclick={() => pingAll("http")} disabled={pinging !== null}>{pinging === "http" ? "HTTP pinging…" : "HTTP ping all"}</button>
  <button class="btn" onclick={connectBest} disabled={connectingBest || shown.length === 0}
          title="Connect to the healthiest node in this group">{connectingBest ? "Connecting…" : "Connect best"}</button>
</div>

{#snippet hpill(ok: boolean | null | undefined, ms: number | null | undefined)}
  {#if ok}<span class="hpill ok"><span class="hp-dot"></span>{ms}<small>ms</small></span>
  {:else if ok === false}<span class="hpill bad"><span class="hp-dot"></span>fail</span>
  {:else}<span class="hp-none">—</span>{/if}
{/snippet}
{#snippet healthCells(h: NodeHealth | undefined)}
  <td>{@render hpill(h?.last_tcp_ok, h?.last_tcp_ms)}</td>
  <td>{@render hpill(h?.last_http_ok, h?.last_http_ms)}</td>
  <td>{@render hpill(h?.last_real_ok, h?.last_real_ms)}</td>
  <td class="egress mono">{h?.egress_ip ?? "—"}</td>
{/snippet}

<div class="card">
  <table class="table">
    <thead><tr>
      <th>id</th><th>name</th><th>address</th><th>port</th><th>transport</th>
      <th>TCP</th><th>HTTP</th><th>real</th><th>egress</th><th></th>
    </tr></thead>
    <tbody>
      {#each shown as n, i (n.id)}
        <tr class:stale={n.stale} class:active={n.id === activeId}>
          <td>{n.id}</td>
          <td>{n.name}{#if n.id === activeId}<span class="connected">● connected</span>{/if}</td>
          <td>{n.address}</td><td>{n.port}</td><td>{n.transport}</td>
          {@render healthCells(health[n.id])}
          <td class="actions">
            {#if tab === "servers"}
              <button class="btn ord" title="Move up" onclick={() => move(i, -1)} disabled={i === 0} aria-label="move up">▲</button>
              <button class="btn ord" title="Move down" onclick={() => move(i, 1)} disabled={i === shown.length - 1} aria-label="move down">▼</button>
            {/if}
            {#if n.id === activeId}
              <button class="btn" onclick={() => disconnect(n.id)}>Disconnect</button>
            {:else}
              <button class="btn btn-primary" onclick={() => connect(n.id)}>Connect</button>
            {/if}
            <button class="btn t-btn" title="Test this node — TCP + HTTP + real through it" onclick={() => probeNode(n.id)} disabled={probingId === n.id}>{probingId === n.id ? "…" : "T"}</button>
            <button class="btn" onclick={() => startEdit(n)}>Edit</button>
            {#if tab === "servers"}<button class="btn btn-danger" onclick={() => del(n)}>Delete</button>{/if}
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
      <select class="input" bind:value={form.security}>
        <option value="reality">reality</option><option value="tls">tls</option>
      </select>
      <input class="input" bind:value={form.sni} placeholder="sni" />
      {#if form.security === "reality"}
        <input class="input" bind:value={form.public_key} placeholder="reality publicKey" />
        <input class="input" bind:value={form.short_id} placeholder="shortId" />
      {:else}
        <input class="input" bind:value={form.alpn} placeholder="alpn (e.g. h2,http/1.1)" />
      {/if}
      {#if form.transport === "xhttp"}
        <input class="input" bind:value={form.path} placeholder="xhttp path" />
        <input class="input" bind:value={form.host} placeholder="xhttp host" />
        <input class="input" bind:value={form.mode} placeholder="xhttp mode (optional)" />
      {/if}
      <div class="form-actions"><button class="btn btn-primary">Add server</button></div>
    </form>
  </Modal>
{/if}

{#if importOpen}
  <Modal title="Import servers" onClose={() => (importOpen = false)}>
    <div class="import">
      <p class="muted">Paste a subscription body — base64/vless, a clash <code>proxies:</code> YAML, or a JSON node list. Parsed nodes are added as manual servers; duplicates are skipped.</p>
      <textarea class="input ta" bind:value={importText} rows="8" placeholder="vless://…  or  proxies:  or  [{'{'}…{'}'}]"></textarea>
      <div class="form-actions">
        <button class="btn btn-primary" onclick={doImport} disabled={importing || !importText.trim()}>{importing ? "Importing…" : "Import"}</button>
        <button class="btn" onclick={() => (importOpen = false)}>Cancel</button>
      </div>
    </div>
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
      <label class="field"><span>security</span>
        <select class="input" bind:value={edit.security}>
          <option value="reality">reality</option><option value="tls">tls</option>
        </select></label>
      <label class="field"><span>sni</span><input class="input" bind:value={edit.sni} /></label>
      <label class="field"><span>fingerprint</span><input class="input" bind:value={edit.fingerprint} /></label>
      {#if edit.security === "reality"}
        <label class="field"><span>reality publicKey</span><input class="input" bind:value={edit.public_key} /></label>
        <label class="field"><span>shortId</span><input class="input" bind:value={edit.short_id} /></label>
      {:else}
        <label class="field"><span>alpn</span><input class="input" bind:value={edit.alpn} placeholder="h2,http/1.1" /></label>
      {/if}
      {#if edit.transport === "xhttp"}
        <label class="field"><span>xhttp path</span><input class="input" bind:value={edit.path} /></label>
        <label class="field"><span>xhttp host</span><input class="input" bind:value={edit.host} /></label>
        <label class="field"><span>xhttp mode</span><input class="input" bind:value={edit.mode} /></label>
      {/if}
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
  .switcher .import-btn { margin-left: auto; }
  .import { display: grid; gap: 0.7rem; }
  .import .ta { font-family: var(--mono); font-size: 0.8rem; width: 100%; resize: vertical; }
  .ping-bar { display: flex; gap: 0.4rem; margin-bottom: 0.6rem; }
  .actions { white-space: nowrap; }
  .t-btn { font-weight: 700; min-width: 2rem; }
  .ord { min-width: 1.7rem; padding-left: 0.4rem; padding-right: 0.4rem; font-size: 0.72rem; }
  tr.stale { opacity: 0.55; }
  tr.active td { background: var(--accent-soft); }
  tr.active td:first-child { box-shadow: inset 3px 0 0 var(--accent); }
  .connected {
    margin-left: 0.5rem; display: inline-flex; align-items: center; gap: 0.25rem;
    padding: 0.05rem 0.45rem; border-radius: 999px;
    background: var(--accent-soft); color: var(--accent);
    font-size: 0.66rem; font-weight: 700; letter-spacing: 0.03em; text-transform: uppercase;
    white-space: nowrap; vertical-align: middle;
  }
  /* health status pills */
  .hpill {
    display: inline-flex; align-items: center; gap: 0.3rem;
    padding: 0.08rem 0.45rem; border-radius: 999px;
    font-family: var(--mono); font-variant-numeric: tabular-nums;
    font-size: 0.74rem; font-weight: 500;
  }
  .hpill small { opacity: 0.65; margin-left: 0.05rem; }
  .hpill .hp-dot { width: 0.42rem; height: 0.42rem; border-radius: 50%; flex: none; }
  .hpill.ok { color: var(--success); background: color-mix(in srgb, var(--success) 13%, transparent); }
  .hpill.ok .hp-dot { background: var(--success); }
  .hpill.bad { color: var(--danger); background: color-mix(in srgb, var(--danger) 13%, transparent); }
  .hpill.bad .hp-dot { background: var(--danger); }
  .hp-none { color: var(--faint); }
  .egress { color: var(--muted); font-size: 0.78rem; }
  .empty { text-align: center; padding: 1.2rem; }
  .grid-form { display: grid; grid-template-columns: repeat(auto-fill, minmax(13rem, 1fr)); gap: 0.6rem; }
  .form-actions { grid-column: 1 / -1; display: flex; gap: 0.5rem; }
  .field { display: grid; gap: 0.2rem; }
</style>
