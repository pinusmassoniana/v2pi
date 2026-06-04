<script lang="ts">
  import { api, ApiError, type Node, type NodeHealth, type TuningProfile, type Status, type Subscription, type Settings } from "./api";
  import Modal from "./Modal.svelte";
  import Alert from "./Alert.svelte";
  import { confirmDialog } from "./confirm.svelte";

  let nodes = $state<Node[]>([]);
  let health = $state<Record<number, NodeHealth>>({});
  let profiles = $state<TuningProfile[]>([]);
  let subs = $state<Subscription[]>([]);
  let status = $state<Status | null>(null);
  let settings = $state<Settings | null>(null);
  let msg = $state("");
  let msgKind = $state<"ok" | "err">("ok");
  let tab = $state<number | "servers" | null>(null);   // selected switcher tab (sub id | manual)
  let query = $state("");                                // NN5 search
  let sortKey = $state<"pos" | "name" | "address" | "tcp" | "http">("pos");
  let sortDir = $state<1 | -1>(1);
  let selected = $state<Set<number>>(new Set());         // NN3 bulk selection

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
  let validateMsg = $state("");

  function setMsg(t: string, kind: "ok" | "err" = "ok") { msg = t; msgKind = kind; }
  function errText(err: unknown, fb: string) { return err instanceof ApiError ? err.message : fb; }

  const activeId = $derived(status?.active_node_id ?? null);
  const inScope = $derived(nodes.filter((n) =>
    tab === "servers" ? n.subscription_id === null
    : tab === null ? false
    : n.subscription_id === tab));
  const shown = $derived.by(() => {
    const q = query.trim().toLowerCase();
    let list = q ? inScope.filter((n) => (n.name + " " + n.address).toLowerCase().includes(q)) : inScope;
    const cmp = (a: Node, b: Node) => {
      const h = (n: Node) => health[n.id];
      switch (sortKey) {
        case "name": return a.name.localeCompare(b.name);
        case "address": return a.address.localeCompare(b.address);
        case "tcp": return (h(a)?.last_tcp_ms ?? Infinity) - (h(b)?.last_tcp_ms ?? Infinity);
        case "http": return (h(a)?.last_http_ms ?? Infinity) - (h(b)?.last_http_ms ?? Infinity);
        default: return 0;   // pos = backend order
      }
    };
    return sortKey === "pos" ? list : [...list].sort((a, b) => cmp(a, b) * sortDir);
  });
  const scope = $derived(tab === "servers" ? "servers" : tab === null ? undefined : String(tab));

  // --- helpers ---
  function relTime(iso: string | number | null): string {
    if (!iso) return "—";
    const t = typeof iso === "number" ? iso * 1000 : new Date(iso).getTime();
    if (isNaN(t)) return String(iso);
    const s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  }
  function uptime(since: number | null | undefined): string {
    if (!since) return "";
    const s = Math.max(0, Math.round(Date.now() / 1000 - since));
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  }
  function spark(hist: number[] | undefined, w = 56, h = 14): string {
    if (!hist || hist.length < 2) return "";
    const max = Math.max(...hist), min = Math.min(...hist), range = max - min || 1;
    const step = w / (hist.length - 1);
    return hist.map((v, i) => `${(i * step).toFixed(1)},${(h - ((v - min) / range) * h).toFixed(1)}`).join(" ");
  }
  function vlessUri(n: Node): string {
    const flow = n.transport === "vision" ? "xtls-rprx-vision" : "";
    const p = new URLSearchParams();
    p.set("type", n.network === "xhttp" ? "xhttp" : "tcp");
    p.set("security", n.security);
    if (n.sni) p.set("sni", n.sni);
    if (n.security === "reality") { if (n.public_key) p.set("pbk", n.public_key); if (n.short_id) p.set("sid", n.short_id); }
    if (n.fingerprint) p.set("fp", n.fingerprint);
    if (flow) p.set("flow", flow);
    if (n.network === "xhttp") { if (n.path) p.set("path", n.path); if (n.host) p.set("host", n.host); if (n.mode) p.set("mode", n.mode); }
    if (n.alpn) p.set("alpn", n.alpn);
    return `vless://${n.uuid}@${n.address}:${n.port}?${p.toString()}#${encodeURIComponent(n.name)}`;
  }
  async function copy(text: string) {
    try { await navigator.clipboard.writeText(text); setMsg("copied", "ok"); }
    catch { setMsg("copy failed", "err"); }
  }

  async function refresh() {
    try {
      const [ns, hs, ps, ss, st, se] = await Promise.all([
        api.listNodes(), api.listNodeHealth(), api.listProfiles(), api.listSubs(), api.getStatus(), api.getSettings()]);
      nodes = ns;
      health = Object.fromEntries(hs.map((h) => [h.node_id, h]));
      profiles = ps; subs = ss; status = st; settings = se;
      if (tab === null || (tab !== "servers" && !ss.some((s) => s.id === tab)))
        tab = ss[0]?.id ?? "servers";
    } catch (err) { setMsg(errText(err, "load failed"), "err"); }
  }
  async function pollHealth() {
    try {
      const [hs, st] = await Promise.all([api.listNodeHealth(), api.getStatus()]);
      health = Object.fromEntries(hs.map((h) => [h.node_id, h]));
      status = st;
    } catch { /* transient; keep last values */ }
  }

  // --- CRUD / connect ---
  async function add(e: Event) {
    e.preventDefault();
    try { await api.addNode({ ...form }); form = blankForm(); validateMsg = ""; addOpen = false; await refresh(); }
    catch (err) { setMsg(errText(err, "add failed"), "err"); }
  }
  function startEdit(n: Node) {
    editId = n.id; validateMsg = "";
    edit = { name: n.name, address: n.address, port: n.port, uuid: n.uuid, transport: n.transport,
             security: n.security, sni: n.sni, public_key: n.public_key, short_id: n.short_id,
             fingerprint: n.fingerprint, path: n.path, host: n.host, mode: n.mode, alpn: n.alpn,
             tuning_profile_id: n.tuning_profile_id };
  }
  async function saveEdit(e: Event) {
    e.preventDefault();
    if (editId === null) return;
    try { await api.updateNode(editId, { ...edit }); editId = null; await refresh(); }
    catch (err) { setMsg(errText(err, "save failed"), "err"); }
  }
  function cloneNode(n: Node) {   // NN9
    form = { name: n.name + " copy", address: n.address, port: n.port, uuid: n.uuid,
             transport: n.transport, security: n.security, sni: n.sni, public_key: n.public_key,
             short_id: n.short_id, fingerprint: n.fingerprint, path: n.path, host: n.host,
             mode: n.mode, alpn: n.alpn };
    validateMsg = ""; addOpen = true;
  }
  async function del(n: Node) {
    if (!(await confirmDialog(`Delete server “${n.name}” (${n.address})?`))) return;
    try { await api.deleteNode(n.id); await refresh(); }
    catch (err) { setMsg(errText(err, "delete failed"), "err"); }
  }
  async function validateForm(f: typeof form | typeof edit) {   // NN10
    validateMsg = "validating…";
    try { const r = await api.validateNode({ ...f }); validateMsg = r.ok ? "✓ config valid" : `✗ ${r.error}`; }
    catch (err) { validateMsg = errText(err, "validate failed"); }
  }
  let applyingId = $state<number | null>(null);   // NF2
  async function connect(id: number) {
    applyingId = id;
    try { await api.apply(id); await refresh(); }
    catch (err) { setMsg(errText(err, "connect failed"), "err"); }
    finally { applyingId = null; }
  }
  async function disconnect(id: number) {
    applyingId = id;
    try { await api.disconnect(id); await refresh(); }
    catch (err) { setMsg(errText(err, "disconnect failed"), "err"); }
    finally { applyingId = null; }
  }
  // N8 reorder
  async function move(i: number, dir: -1 | 1) {
    const list = shown.map((n) => n.id);
    const j = i + dir;
    if (j < 0 || j >= list.length) return;
    [list[i], list[j]] = [list[j], list[i]];
    try { await api.reorderNodes(list); await refresh(); }
    catch (err) { setMsg(errText(err, "reorder failed"), "err"); }
  }
  // N4 import
  let importOpen = $state(false); let importText = $state(""); let importing = $state(false);
  async function doImport() {
    importing = true;
    try {
      const r = await api.importNodes(importText);
      setMsg(`imported ${r.added}/${r.total} node(s) (${r.format})`, "ok");
      importText = ""; importOpen = false; await refresh();
    } catch (err) { setMsg(errText(err, "import failed"), "err"); }
    finally { importing = false; }
  }
  // N9 connect-best
  let connectingBest = $state(false);
  async function connectBest() {
    connectingBest = true;
    try { const r = await api.connectBest(tab === "servers" ? null : (tab as number)); setMsg(`connected to node ${r.node_id}`, "ok"); await refresh(); }
    catch (err) { setMsg(errText(err, "connect-best failed"), "err"); }
    finally { connectingBest = false; }
  }
  // probes
  let pinging = $state<"tcp" | "http" | null>(null);
  async function pingAll(kind: "tcp" | "http") {
    pinging = kind;
    try {
      const hs = kind === "tcp" ? await api.probeTcp(scope) : await api.probeHttp(scope);
      health = Object.fromEntries(hs.map((h) => [h.node_id, h]));
    } catch (err) { setMsg(errText(err, "ping failed"), "err"); }
    finally { pinging = null; }
  }
  let probingId = $state<number | null>(null);
  async function probeNode(id: number) {
    probingId = id;
    try { const h = await api.probeNode(id); health = { ...health, [h.node_id]: h }; }
    catch (err) { setMsg(errText(err, "probe failed"), "err"); }
    finally { probingId = null; }
  }
  let testAllN = $state(0);   // NN2 real-test-all progress (0 = idle)
  async function testAllReal() {
    const list = shown.slice();
    testAllN = list.length;
    for (const n of list) {
      try { const h = await api.probeNode(n.id); health = { ...health, [h.node_id]: h }; }
      catch { /* keep going */ }
      testAllN -= 1;
    }
    testAllN = 0;
  }

  // NN3 bulk
  function toggleSel(id: number) { const s = new Set(selected); s.has(id) ? s.delete(id) : s.add(id); selected = s; }
  function selectAllShown() { selected = new Set(shown.map((n) => n.id)); }
  function clearSel() { selected = new Set(); }
  const selIds = $derived([...selected].filter((id) => shown.some((n) => n.id === id)));
  async function bulkDelete() {
    if (!selIds.length || !(await confirmDialog(`Delete ${selIds.length} server(s)?`))) return;
    try { for (const id of selIds) await api.deleteNode(id); clearSel(); await refresh(); }
    catch (err) { setMsg(errText(err, "bulk delete failed"), "err"); }
  }
  async function bulkDetach() {
    if (!selIds.length) return;
    try { await api.detachNodes(selIds); clearSel(); await refresh(); }
    catch (err) { setMsg(errText(err, "detach failed"), "err"); }
  }
  async function bulkProfile(v: string) {
    if (!selIds.length) return;
    const pid = v === "" ? null : Number(v);
    try { for (const id of selIds) await api.updateNode(id, { tuning_profile_id: pid }); clearSel(); await refresh(); }
    catch (err) { setMsg(errText(err, "assign failed"), "err"); }
  }

  // NN6 export
  let exportNode = $state<Node | null>(null);

  $effect(() => {
    refresh();
    const id = setInterval(() => { if (!document.hidden) pollHealth(); }, 20000);   // NN1 auto-poll
    return () => clearInterval(id);
  });

  const failoverArmed = $derived(settings?.failover_enabled ?? false);
</script>

<Alert {msg} kind={msgKind} />

<div class="switcher">
  {#each subs as s (s.id)}
    <button class="tab" class:active={tab === s.id} aria-current={tab === s.id ? "true" : undefined} onclick={() => { tab = s.id; clearSel(); }}>{s.name}</button>
  {/each}
  <button class="tab" class:active={tab === "servers"} aria-current={tab === "servers" ? "true" : undefined} onclick={() => { tab = "servers"; clearSel(); }}>Servers</button>
  {#if tab === "servers"}
    <button class="btn import-btn" onclick={() => (importOpen = true)}>Import</button>
    <button class="btn btn-primary" onclick={() => { form = blankForm(); validateMsg = ""; addOpen = true; }}>+ Add server</button>
  {/if}
</div>

<div class="ping-bar">
  <input class="input search" bind:value={query} placeholder="search name / address…" />
  <button class="btn" onclick={() => pingAll("tcp")} disabled={pinging !== null}>{pinging === "tcp" ? "TCP…" : "TCP ping"}</button>
  <button class="btn" onclick={() => pingAll("http")} disabled={pinging !== null}>{pinging === "http" ? "HTTP…" : "HTTP ping"}</button>
  <button class="btn" onclick={testAllReal} disabled={testAllN > 0 || shown.length === 0} title="Real request through every node in this group (sequential)">{testAllN > 0 ? `Testing ${testAllN}…` : "Test all (real)"}</button>
  <button class="btn" onclick={connectBest} disabled={connectingBest || shown.length === 0} title="Connect to the healthiest node here">{connectingBest ? "Connecting…" : "Connect best"}</button>
</div>

{#if selIds.length}
  <div class="bulk">
    <span>{selIds.length} selected</span>
    <select class="input" onchange={(e) => { const v = e.currentTarget.value; e.currentTarget.value = "__ph__"; if (v !== "__ph__") bulkProfile(v === "__none__" ? "" : v); }}>
      <option value="__ph__" disabled selected>assign profile…</option>
      <option value="__none__">(global default)</option>
      {#each profiles as p (p.id)}<option value={String(p.id)}>{p.name}</option>{/each}
    </select>
    {#if tab !== "servers"}<button class="btn" onclick={bulkDetach}>Detach to Servers</button>{/if}
    {#if tab === "servers"}<button class="btn btn-danger" onclick={bulkDelete}>Delete</button>{/if}
    <button class="btn btn-ghost" onclick={clearSel}>Clear</button>
  </div>
{/if}

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
  <td class="trend">
    {#if h && h.lat_history && h.lat_history.length > 1}
      <svg width="56" height="14" viewBox="0 0 56 14" preserveAspectRatio="none"><polyline points={spark(h.lat_history)} fill="none" stroke="currentColor" stroke-width="1.2"/></svg>
    {:else}<span class="hp-none">—</span>{/if}
  </td>
  <td class="muted small" title={h?.checked_at ?? ""}>{relTime(h?.checked_at ?? null)}</td>
{/snippet}

{#snippet sortTh(key: typeof sortKey, label: string)}
  <th class="sortable" onclick={() => { if (sortKey === key) sortDir = (sortDir * -1) as 1 | -1; else { sortKey = key; sortDir = 1; } }}>
    {label}{#if sortKey === key}<span class="caret">{sortDir === 1 ? "▲" : "▼"}</span>{/if}
  </th>
{/snippet}

<div class="card">
  <div class="table-wrap"><table class="table">
    <thead><tr>
      <th class="ck"><input type="checkbox" checked={shown.length > 0 && selIds.length === shown.length} onchange={(e) => e.currentTarget.checked ? selectAllShown() : clearSel()} aria-label="select all" /></th>
      <th>id</th>{@render sortTh("name", "name")}{@render sortTh("address", "address")}<th>port</th><th>transport</th>
      {@render sortTh("tcp", "TCP")}{@render sortTh("http", "HTTP")}<th>real</th><th>egress</th><th>trend</th><th>checked</th><th></th>
    </tr></thead>
    <tbody>
      {#each shown as n, i (n.id)}
        <tr class:stale={n.stale} class:active={n.id === activeId}>
          <td class="ck"><input type="checkbox" checked={selected.has(n.id)} onchange={() => toggleSel(n.id)} aria-label="select" /></td>
          <td>{n.id}</td>
          <td>
            {n.name}
            {#if n.stale}<span class="badge stale-b" title="Vanished from its subscription; kept because it was active or for history">stale</span>{/if}
            {#if n.id === activeId}<span class="connected">● connected{#if status?.active_since} · {uptime(status.active_since)}{/if}</span>{/if}
            {#if n.id === activeId && (health[n.id]?.fail_count ?? 0) > 0}<span class="badge warn-b" title="Consecutive real-request failures (auto-failover counter)">fail {health[n.id]?.fail_count}</span>{/if}
          </td>
          <td>{n.address}</td><td>{n.port}</td><td>{n.transport}{n.security === "tls" ? "·tls" : ""}</td>
          {@render healthCells(health[n.id])}
          <td class="actions">
            {#if tab === "servers" && sortKey === "pos"}
              <button class="btn ord" title="Move up" onclick={() => move(i, -1)} disabled={i === 0} aria-label="up">▲</button>
              <button class="btn ord" title="Move down" onclick={() => move(i, 1)} disabled={i === shown.length - 1} aria-label="down">▼</button>
            {/if}
            {#if n.id === activeId}
              <button class="btn" onclick={() => disconnect(n.id)} disabled={applyingId === n.id}>{applyingId === n.id ? "…" : "Disconnect"}</button>
            {:else}
              <button class="btn btn-primary" onclick={() => connect(n.id)} disabled={applyingId === n.id}>{applyingId === n.id ? "…" : "Connect"}</button>
            {/if}
            <button class="btn t-btn" title="Test this node — TCP + HTTP + real through it" onclick={() => probeNode(n.id)} disabled={probingId === n.id}>{probingId === n.id ? "…" : "Test"}</button>
            <button class="btn" onclick={() => startEdit(n)}>Edit</button>
            <button class="btn" title="Export / share" aria-label="Export / share" onclick={() => (exportNode = n)}>⤴</button>
            <button class="btn" title="Clone" aria-label="Clone" onclick={() => cloneNode(n)}>⧉</button>
            {#if tab === "servers"}<button class="btn btn-danger" onclick={() => del(n)}>Delete</button>{/if}
          </td>
        </tr>
      {/each}
      {#if shown.length === 0}
        <tr><td colspan="13" class="muted empty">No servers here{#if tab === "servers"} — add one with “+ Add server”.{/if}</td></tr>
      {/if}
    </tbody>
  </table></div>
</div>

{#if status?.last_failover_at || failoverArmed}
  <p class="fo muted">Auto-failover {failoverArmed ? "armed" : "off"}{#if status?.last_failover_at} · last switch {relTime(status.last_failover_at)}{/if}</p>
{/if}

{#if addOpen}
  <Modal title="Add server" onClose={() => (addOpen = false)}>
    <form onsubmit={add} class="grid-form">
      <input class="input" bind:value={form.name} placeholder="name" required />
      <input class="input" bind:value={form.address} placeholder="address" required />
      <input class="input" type="number" min="1" max="65535" bind:value={form.port} placeholder="port" required />
      <input class="input" bind:value={form.uuid} placeholder="uuid" required />
      <select class="input" bind:value={form.transport}><option value="vision">vision</option><option value="xhttp">xhttp</option></select>
      <select class="input" bind:value={form.security}><option value="reality">reality</option><option value="tls">tls</option></select>
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
      {#if validateMsg}<div class="vmsg" class:bad={validateMsg.startsWith("✗")}>{validateMsg}</div>{/if}
      <div class="form-actions">
        <button class="btn btn-primary">Add server</button>
        <button class="btn" type="button" onclick={() => validateForm(form)}>Validate</button>
      </div>
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

{#if exportNode}
  <Modal title={`Export “${exportNode.name}”`} onClose={() => (exportNode = null)}>
    <div class="import">
      <label class="field"><span>vless:// share link</span>
        <textarea class="input ta" rows="3" readonly>{vlessUri(exportNode)}</textarea></label>
      <label class="field"><span>JSON</span>
        <textarea class="input ta" rows="6" readonly>{JSON.stringify(exportNode, null, 2)}</textarea></label>
      <div class="form-actions">
        <button class="btn btn-primary" onclick={() => copy(vlessUri(exportNode!))}>Copy link</button>
        <button class="btn" onclick={() => copy(JSON.stringify(exportNode))}>Copy JSON</button>
      </div>
    </div>
  </Modal>
{/if}

{#if editId !== null}
  <Modal title="Edit node" onClose={() => (editId = null)}>
    <form onsubmit={saveEdit} class="grid-form">
      <label class="field"><span>name</span><input class="input" bind:value={edit.name} /></label>
      <label class="field"><span>address</span><input class="input" bind:value={edit.address} /></label>
      <label class="field"><span>port</span><input class="input" type="number" min="1" max="65535" bind:value={edit.port} /></label>
      <label class="field"><span>uuid</span><input class="input" bind:value={edit.uuid} /></label>
      <label class="field"><span>transport</span>
        <select class="input" bind:value={edit.transport}><option value="vision">vision</option><option value="xhttp">xhttp</option></select></label>
      <label class="field"><span>security</span>
        <select class="input" bind:value={edit.security}><option value="reality">reality</option><option value="tls">tls</option></select></label>
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
      {#if validateMsg}<div class="vmsg" class:bad={validateMsg.startsWith("✗")}>{validateMsg}</div>{/if}
      <div class="form-actions">
        <button class="btn btn-primary">Save</button>
        <button class="btn" type="button" onclick={() => validateForm(edit)}>Validate</button>
        <button class="btn" type="button" onclick={() => (editId = null)}>Cancel</button>
      </div>
    </form>
  </Modal>
{/if}

<style>
  .switcher { display: flex; flex-wrap: wrap; gap: 0.35rem; align-items: center; margin-bottom: 0.7rem; }
  .tab { background: var(--surface-2); border: 1px solid var(--border); color: var(--muted); padding: 0.35rem 0.75rem; border-radius: 999px; cursor: pointer; font: inherit; }
  .tab:hover { color: var(--text); }
  .tab.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .switcher .import-btn { margin-left: auto; }
  .ping-bar { display: flex; gap: 0.4rem; margin-bottom: 0.6rem; flex-wrap: wrap; align-items: center; }
  .ping-bar .search { max-width: 16rem; }
  .bulk { display: flex; gap: 0.5rem; align-items: center; margin-bottom: 0.6rem; padding: 0.45rem 0.7rem; background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius); }
  .actions { white-space: nowrap; display: flex; gap: 0.3rem; flex-wrap: wrap; }
  .t-btn { font-weight: 700; }
  .ord { min-width: 1.7rem; padding-left: 0.4rem; padding-right: 0.4rem; font-size: 0.72rem; }
  .ck { width: 1.6rem; text-align: center; }
  .sortable { cursor: pointer; user-select: none; white-space: nowrap; }
  .caret { font-size: 0.6rem; margin-left: 0.15rem; }
  tr.stale { opacity: 0.55; }
  tr.active td { background: var(--accent-soft); }
  tr.active td:first-child { box-shadow: inset 3px 0 0 var(--accent); }
  .badge { margin-left: 0.4rem; font-size: 0.62rem; font-weight: 700; padding: 0.02rem 0.35rem; border-radius: 999px; text-transform: uppercase; letter-spacing: 0.03em; }
  .stale-b { background: var(--surface-2); color: var(--muted); border: 1px solid var(--border); }
  .warn-b { background: color-mix(in srgb, var(--danger) 14%, transparent); color: var(--danger); }
  .connected { margin-left: 0.5rem; display: inline-flex; align-items: center; gap: 0.25rem; padding: 0.05rem 0.45rem; border-radius: 999px; background: var(--accent-soft); color: var(--accent); font-size: 0.66rem; font-weight: 700; letter-spacing: 0.03em; text-transform: uppercase; white-space: nowrap; vertical-align: middle; }
  .hpill { display: inline-flex; align-items: center; gap: 0.3rem; padding: 0.08rem 0.45rem; border-radius: 999px; font-family: var(--mono); font-variant-numeric: tabular-nums; font-size: 0.74rem; font-weight: 500; }
  .hpill small { opacity: 0.65; margin-left: 0.05rem; }
  .hpill .hp-dot { width: 0.42rem; height: 0.42rem; border-radius: 50%; flex: none; }
  .hpill.ok { color: var(--success); background: color-mix(in srgb, var(--success) 13%, transparent); }
  .hpill.ok .hp-dot { background: var(--success); }
  .hpill.bad { color: var(--danger); background: color-mix(in srgb, var(--danger) 13%, transparent); }
  .hpill.bad .hp-dot { background: var(--danger); }
  .hp-none { color: var(--faint); }
  .egress { color: var(--muted); font-size: 0.78rem; }
  .trend { color: var(--accent); }
  .small { font-size: 0.72rem; }
  .empty { text-align: center; padding: 1.2rem; }
  .fo { font-size: 0.74rem; margin-top: 0.5rem; }
  .grid-form { display: grid; grid-template-columns: repeat(auto-fill, minmax(13rem, 1fr)); gap: 0.6rem; }
  .form-actions { grid-column: 1 / -1; display: flex; gap: 0.5rem; }
  .field { display: grid; gap: 0.2rem; }
  .vmsg { grid-column: 1 / -1; font-family: var(--mono); font-size: 0.78rem; color: var(--success); white-space: pre-wrap; }
  .vmsg.bad { color: var(--danger); }
  .import { display: grid; gap: 0.7rem; }
  .import .ta { font-family: var(--mono); font-size: 0.8rem; width: 100%; resize: vertical; }
</style>
