<script lang="ts">
  import { api, ApiError, type Node, type NodeHealth, type TuningProfile, type Status, type Subscription, type Settings } from "./api";
  import Modal from "./Modal.svelte";
  import Alert from "./Alert.svelte";
  import { confirmDialog } from "./confirm.svelte";
  import { serverNow } from "./status.svelte";
  import { sparkPath } from "./dashboard";
  import { flagEmoji } from "./flag";
  import { I } from "./icons";

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
  // N-E: row density, persisted
  let dense = $state(typeof localStorage !== "undefined" && localStorage.getItem("nodes-density") === "1");
  function toggleDensity() { dense = !dense; try { localStorage.setItem("nodes-density", dense ? "1" : "0"); } catch {} }

  let addOpen = $state(false);
  const blankForm = () => ({ name: "", address: "", port: 443, uuid: "", transport: "vision",
                            security: "reality", sni: "", public_key: "", short_id: "",
                            fingerprint: "chrome", path: "", host: "", mode: "", alpn: "", note: "" });
  let form = $state(blankForm());
  let editId = $state<number | null>(null);
  let edit = $state({ name: "", address: "", port: 443, uuid: "", transport: "vision",
                      security: "reality", sni: "", public_key: "", short_id: "",
                      fingerprint: "chrome", path: "", host: "", mode: "", alpn: "", note: "",
                      tuning_profile_id: null as number | null });
  let validateMsg = $state("");

  // compact action icons — shared set (src/lib/icons.ts), used across all data tables.

  function setMsg(t: string, kind: "ok" | "err" = "ok") { msg = t; msgKind = kind; }
  function errText(err: unknown, fb: string) { return err instanceof ApiError ? err.message : fb; }

  const activeId = $derived(status?.active_node_id ?? null);
  const inScope = $derived(nodes.filter((n) =>
    tab === "servers" ? n.subscription_id === null
    : tab === null ? false
    : n.subscription_id === tab));
  const shown = $derived.by(() => {
    const q = query.trim().toLowerCase();
    let list = q ? inScope.filter((n) => (n.name + " " + n.address + " " + n.note).toLowerCase().includes(q)) : inScope;
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
  // C2: reordering is only coherent over the full, unfiltered scope in pos order
  const canReorder = $derived(tab === "servers" && sortKey === "pos" && query.trim() === "");

  // --- helpers ---
  function relTime(iso: string | number | null): string {
    if (!iso) return "—";
    const t = typeof iso === "number" ? iso * 1000 : new Date(iso).getTime();
    if (isNaN(t)) return String(iso);
    const s = Math.max(0, Math.round((serverNow() - t) / 1000));   // C1: Pi-clock aligned
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  }
  function uptime(since: number | null | undefined): string {
    if (!since) return "";
    const s = Math.max(0, Math.round(serverNow() / 1000 - since));   // C1
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
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
  // D1: connect/disconnect change only nodes + status + health, not profiles/subs/settings
  async function refreshNodes() {
    try {
      const [ns, hs, st] = await Promise.all([api.listNodes(), api.listNodeHealth(), api.getStatus()]);
      nodes = ns;
      health = Object.fromEntries(hs.map((h) => [h.node_id, h]));
      status = st;
    } catch { /* transient */ }
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
             note: n.note, tuning_profile_id: n.tuning_profile_id };
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
             mode: n.mode, alpn: n.alpn, note: n.note };
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
    try { await api.apply(id); await refreshNodes(); }
    catch (err) { setMsg(errText(err, "connect failed"), "err"); }
    finally { applyingId = null; }
  }
  async function disconnect(id: number) {
    applyingId = id;
    try { await api.disconnect(id); await refreshNodes(); }
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
  <input class="input search" bind:value={query} placeholder="search name / address / note…" />
  <button class="btn" onclick={() => pingAll("tcp")} disabled={pinging !== null}>{pinging === "tcp" ? "TCP…" : "TCP ping"}</button>
  <button class="btn" onclick={() => pingAll("http")} disabled={pinging !== null}>{pinging === "http" ? "HTTP…" : "HTTP ping"}</button>
  <button class="btn" onclick={testAllReal} disabled={testAllN > 0 || shown.length === 0} title="Real request through every node in this group (sequential)">{testAllN > 0 ? `Testing ${testAllN}…` : "Test all (real)"}</button>
  <button class="btn" onclick={connectBest} disabled={connectingBest || shown.length === 0} title="Connect to the healthiest node here">{connectingBest ? "Connecting…" : "Connect best"}</button>
  <button class="btn btn-ghost density" onclick={toggleDensity} aria-pressed={dense} title="Toggle row density">{dense ? "Comfortable" : "Compact"}</button>
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
  <td data-label="TCP">{@render hpill(h?.last_tcp_ok, h?.last_tcp_ms)}</td>
  <td data-label="HTTP">{@render hpill(h?.last_http_ok, h?.last_http_ms)}</td>
  <td data-label="real">{@render hpill(h?.last_real_ok, h?.last_real_ms)}</td>
  <td class="egress mono col-egress" data-label="egress">{#if h?.egress_cc}<span class="flag" title={h.egress_cc}>{flagEmoji(h.egress_cc)}</span> {/if}{h?.egress_ip ?? "—"}{#if h?.egress_ip6}<span class="eg6" title={h.egress_ip6}>{#if h.egress_cc6}<span class="flag" title={h.egress_cc6}>{flagEmoji(h.egress_cc6)}</span> {/if}v6 {h.egress_ip6}</span>{/if}</td>
  <td class="trend col-trend" data-label="trend">
    {#if h && h.lat_history && h.lat_history.length > 1}
      <svg width="56" height="14" viewBox="0 0 56 14" preserveAspectRatio="none"><path d={sparkPath(h.lat_history, 56, 14)} fill="none" stroke="currentColor" stroke-width="1.2"/></svg>
    {:else}<span class="hp-none">—</span>{/if}
  </td>
  <td class="muted small col-checked" data-label="checked" title={h?.checked_at ?? ""}>{relTime(h?.checked_at ?? null)}</td>
{/snippet}

{#snippet sortTh(key: typeof sortKey, label: string)}
  <th class="sortable" aria-sort={sortKey === key ? (sortDir === 1 ? "ascending" : "descending") : "none"}>
    <button type="button" class="th-sort" onclick={() => { if (sortKey === key) sortDir = (sortDir * -1) as 1 | -1; else { sortKey = key; sortDir = 1; } }}>
      {label}{#if sortKey === key}<span class="caret">{sortDir === 1 ? "▲" : "▼"}</span>{/if}
    </button>
  </th>
{/snippet}

<div class="card">
  <div class="table-wrap"><table class="table nodes" class:dense>
    <thead><tr>
      <th class="ck"><input type="checkbox" checked={shown.length > 0 && selIds.length === shown.length} onchange={(e) => e.currentTarget.checked ? selectAllShown() : clearSel()} aria-label="select all" /></th>
      <th class="col-id">id</th>{@render sortTh("name", "name")}{@render sortTh("address", "address")}<th class="col-port">port</th><th class="col-transport">transport</th>
      {@render sortTh("tcp", "TCP")}{@render sortTh("http", "HTTP")}<th>real</th><th class="col-egress">egress</th><th class="col-trend">trend</th><th class="col-checked">checked</th><th><span class="sr-only">actions</span></th>
    </tr></thead>
    <tbody>
      {#each shown as n, i (n.id)}
        <tr class:stale={n.stale} class:active={n.id === activeId}>
          <td class="ck" data-label=""><input type="checkbox" checked={selected.has(n.id)} onchange={() => toggleSel(n.id)} aria-label={`select ${n.name}`} /></td>
          <td class="col-id" data-label="id">{n.id}</td>
          <td class="col-name" data-label="name">
            <span class="nm">{n.name}</span>
            {#if n.stale}<span class="badge stale-b" title="Vanished from its subscription; kept because it was active or for history">stale</span>{/if}
            {#if n.id === activeId}<span class="connected">● connected{#if status?.active_since} · {uptime(status.active_since)}{/if}</span>{/if}
            {#if n.id === activeId && (health[n.id]?.fail_count ?? 0) > 0}<span class="badge warn-b" title="Consecutive real-request failures (auto-failover counter)">fail {health[n.id]?.fail_count}</span>{/if}
            {#if n.note}<span class="note" title={n.note}>{@html I.note}{n.note}</span>{/if}
          </td>
          <td data-label="address">{n.address}</td><td class="col-port" data-label="port">{n.port}</td><td class="col-transport" data-label="transport">{n.transport}{n.security === "tls" ? "·tls" : ""}</td>
          {@render healthCells(health[n.id])}
          <td class="actions" data-label="">
            {#if canReorder}
              <button class="btn iconbtn ord" title="Move up" onclick={() => move(i, -1)} disabled={i === 0} aria-label="move up">{@html I.up}</button>
              <button class="btn iconbtn ord" title="Move down" onclick={() => move(i, 1)} disabled={i === shown.length - 1} aria-label="move down">{@html I.down}</button>
            {/if}
            {#if n.id === activeId}
              <button class="btn" onclick={() => disconnect(n.id)} disabled={applyingId === n.id}>{applyingId === n.id ? "…" : "Disconnect"}</button>
            {:else}
              <button class="btn btn-primary" onclick={() => connect(n.id)} disabled={applyingId === n.id}>{applyingId === n.id ? "…" : "Connect"}</button>
            {/if}
            <button class="btn iconbtn t-btn" title="Test — TCP + HTTP + real through this node" aria-label="Test node" onclick={() => probeNode(n.id)} disabled={probingId === n.id}>{#if probingId === n.id}…{:else}{@html I.test}{/if}</button>
            <button class="btn iconbtn" title="Edit" aria-label="Edit node" onclick={() => startEdit(n)}>{@html I.edit}</button>
            <button class="btn iconbtn" title="Export / share" aria-label="Export / share" onclick={() => (exportNode = n)}>{@html I.share}</button>
            <button class="btn iconbtn" title="Clone" aria-label="Clone node" onclick={() => cloneNode(n)}>{@html I.clone}</button>
            {#if tab === "servers"}<button class="btn iconbtn btn-danger" title="Delete" aria-label="Delete node" onclick={() => del(n)}>{@html I.trash}</button>{/if}
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
      <input class="input note-input" bind:value={form.note} placeholder="note / label (optional)" />
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
      <label class="field note-field"><span>note / label</span><input class="input" bind:value={edit.note} placeholder="e.g. paid until July · fast EU" /></label>
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
  .ping-bar .density { margin-left: auto; }
  .bulk { display: flex; gap: 0.5rem; align-items: center; margin-bottom: 0.6rem; padding: 0.45rem 0.7rem; background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius); flex-wrap: wrap; }

  /* compact, single-row action cell (A2) — icon buttons keep the column narrow */
  .actions { white-space: nowrap; display: flex; gap: 0.25rem; flex-wrap: nowrap; align-items: center; }
  .ord { color: var(--muted); }
  .t-btn { color: var(--accent); }
  .ck { width: 1.6rem; text-align: center; }

  /* keyboard- and SR-friendly sort headers (B1) */
  .sortable { white-space: nowrap; padding: 0; }
  .th-sort {
    width: 100%; background: none; border: none; cursor: pointer; user-select: none;
    color: inherit; font: inherit; letter-spacing: inherit; text-transform: inherit;
    padding: 0.5rem 0.6rem; display: inline-flex; align-items: center; gap: 0.15rem;
  }
  .th-sort:hover { color: var(--text); }
  .th-sort:focus-visible { outline: none; box-shadow: inset 0 0 0 2px var(--accent-ring); border-radius: var(--radius-sm); }
  .caret { font-size: 0.6rem; }

  tr.stale { opacity: 0.55; }
  tr.active td { background: var(--accent-soft); }
  tr.active td:first-child { box-shadow: inset 3px 0 0 var(--accent); }
  .nm { font-weight: 500; }
  .badge { margin-left: 0.4rem; font-size: 0.62rem; font-weight: 700; padding: 0.02rem 0.35rem; border-radius: 999px; text-transform: uppercase; letter-spacing: 0.03em; }
  .stale-b { background: var(--surface-2); color: var(--muted); border: 1px solid var(--border); }
  .warn-b { background: color-mix(in srgb, var(--danger) 14%, transparent); color: var(--danger); }
  .connected { margin-left: 0.5rem; display: inline-flex; align-items: center; gap: 0.25rem; padding: 0.05rem 0.45rem; border-radius: 999px; background: var(--accent-soft); color: var(--accent); font-size: 0.66rem; font-weight: 700; letter-spacing: 0.03em; text-transform: uppercase; white-space: nowrap; vertical-align: middle; }
  .note { display: inline-flex; align-items: center; gap: 0.22rem; margin-left: 0.5rem; max-width: 18rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); font-size: 0.74rem; }
  .note :global(svg) { width: 13px; height: 13px; flex: none; opacity: 0.7; }
  .hpill { display: inline-flex; align-items: center; gap: 0.3rem; padding: 0.08rem 0.45rem; border-radius: 999px; font-family: var(--mono); font-variant-numeric: tabular-nums; font-size: 0.74rem; font-weight: 500; }
  .hpill small { opacity: 0.65; margin-left: 0.05rem; }
  .hpill .hp-dot { width: 0.42rem; height: 0.42rem; border-radius: 50%; flex: none; }
  .hpill.ok { color: var(--success); background: color-mix(in srgb, var(--success) 13%, transparent); }
  .hpill.ok .hp-dot { background: var(--success); }
  .hpill.bad { color: var(--danger); background: color-mix(in srgb, var(--danger) 13%, transparent); }
  .hpill.bad .hp-dot { background: var(--danger); }
  .hp-none { color: var(--faint); }
  .egress { color: var(--muted); font-size: 0.78rem; }
  .eg6 { display: block; color: var(--faint); font-size: 0.72rem; }
  .trend { color: var(--accent); }
  .small { font-size: 0.72rem; }
  .empty { text-align: center; padding: 1.2rem; }
  .fo { font-size: 0.74rem; margin-top: 0.5rem; }
  .grid-form { display: grid; grid-template-columns: repeat(auto-fill, minmax(13rem, 1fr)); gap: 0.6rem; }
  .grid-form .note-input, .grid-form .note-field { grid-column: 1 / -1; }
  .form-actions { grid-column: 1 / -1; display: flex; gap: 0.5rem; }
  .field { display: grid; gap: 0.2rem; }
  .vmsg { grid-column: 1 / -1; font-family: var(--mono); font-size: 0.78rem; color: var(--success); white-space: pre-wrap; }
  .vmsg.bad { color: var(--danger); }
  .import { display: grid; gap: 0.7rem; }
  .import .ta { font-family: var(--mono); font-size: 0.8rem; width: 100%; resize: vertical; }

  /* density toggle (N-E) */
  .table.dense :global(th), .table.dense :global(td) { padding-top: 0.28rem; padding-bottom: 0.28rem; }
  .table.dense .th-sort { padding-top: 0.28rem; padding-bottom: 0.28rem; }

  /* Header pinned to the top of the table. NOTE: .table-wrap's `overflow-x` makes IT the sticky
     scroll-container (not the window), so `top` is measured from the table's top, not the viewport
     — any non-zero offset pushes the header DOWN over the first row (visual bug). Must be top:0. */
  .nodes :global(thead th) { position: sticky; top: 0; z-index: 1; background: var(--surface); }

  /* responsive column hiding (A1) — between the desktop table and the phone card view.
     Each breakpoint drops lower-priority columns; name · address · health · actions stay. */
  @media (max-width: 1100px) and (min-width: 601px) {
    .nodes .col-id, .nodes .col-port, .nodes .col-transport { display: none; }
  }
  @media (max-width: 880px) and (min-width: 601px) {
    .nodes .col-trend, .nodes .col-checked { display: none; }
  }
  @media (max-width: 740px) and (min-width: 601px) {
    .nodes .col-egress { display: none; }
  }

  /* phone card layout (N-A) — each node becomes a stacked card, all fields shown with labels */
  @media (max-width: 600px) {
    /* drop the desktop 44rem table min-width (app.css) so the card fits the phone instead of
       overflowing — otherwise each row stretches to 704px and the values flex off-screen. */
    .table-wrap > .nodes { min-width: 0; }
    .nodes, .nodes :global(thead), .nodes :global(tbody), .nodes :global(tr), .nodes :global(td) { display: block; }
    .nodes :global(thead) { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
    .nodes :global(tbody tr) {
      border: 1px solid var(--border); border-radius: var(--radius); margin-bottom: 0.6rem;
      padding: 0.3rem 0.2rem; background: var(--surface);
    }
    .nodes :global(tbody tr.active) { box-shadow: inset 3px 0 0 var(--accent); }
    .nodes :global(td) {
      display: flex; justify-content: space-between; align-items: center; gap: 1rem;
      border: none; padding: 0.32rem 0.7rem;
    }
    .nodes :global(td)::before {
      content: attr(data-label); color: var(--faint); font-size: 0.66rem; font-weight: 650;
      text-transform: uppercase; letter-spacing: 0.05em; flex: none;
    }
    .nodes :global(td[data-label=""])::before { content: none; }
    .nodes .col-name { display: block; padding-top: 0.5rem; }
    .nodes .col-name::before { content: none; }            /* name is the card title, no label */
    .nodes .col-name .nm { font-size: 0.95rem; font-weight: 600; }
    .nodes .col-name .note { max-width: none; white-space: normal; margin-left: 0; margin-top: 0.2rem; }
    .nodes .actions { justify-content: flex-start; flex-wrap: wrap; padding-top: 0.5rem; }
    .nodes .ck { justify-content: flex-start; }
    .nodes :global(.empty) { display: block; }
  }
</style>
