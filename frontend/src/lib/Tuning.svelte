<script lang="ts">
  import { api, ApiError, type TuningProfile, type ProfilePreset } from "./api";
  import Toggle from "./Toggle.svelte";
  import Alert from "./Alert.svelte";
  import { confirmDialog } from "./confirm.svelte";
  import { I } from "./icons";

  // editor doubles as "add" (id=null) and "edit" (id set) — one form, no duplication.
  const blank = () => ({
    id: null as number | null, name: "", fingerprint: "chrome",
    frag_enabled: false, frag_packets: "tlshello", frag_length: "100-200", frag_interval: "10-20",
    mux_enabled: false, doh_enabled: true, doh_url: "", quic: "allow",
    noise_enabled: false, noises: [] as { type: string; packet: string; delay: string }[],
    xhttp_padding: "", xmux_max_concurrency: "", xmux_max_connections: "",
    mux_concurrency: "", xudp_proxy_udp443: "", alpn: "", tls_min: "", tls_max: "",
  });

  const FINGERPRINTS = ["chrome", "firefox", "safari", "ios", "android", "edge", "random", "randomized", "randomizednoalpn", ""];

  let profiles = $state<TuningProfile[]>([]);
  let presets = $state<ProfilePreset[]>([]);
  let msg = $state("");
  let msgKind = $state<"ok" | "err">("ok");
  let validateMsg = $state("");
  let editor = $state(blank());

  function setMsg(t: string, kind: "ok" | "err" = "ok") { msg = t; msgKind = kind; }
  function errText(err: unknown, fb: string) { return err instanceof ApiError ? err.message : fb; }

  async function refresh() {
    try {
      const [ps, pr] = await Promise.all([api.listProfiles(), api.listProfilePresets()]);
      profiles = ps; presets = pr;
    } catch (err) { setMsg(errText(err, "load failed"), "err"); }
  }
  function edit(p: TuningProfile) {
    editor = {
      id: p.id, name: p.name, fingerprint: p.fingerprint, frag_enabled: p.frag_enabled,
      frag_packets: p.frag_packets, frag_length: p.frag_length, frag_interval: p.frag_interval,
      mux_enabled: p.mux_enabled, doh_enabled: p.doh_enabled, doh_url: p.doh_url, quic: p.quic,
      noise_enabled: p.noise_enabled, noises: p.noises.map((n) => ({ ...n })),
      xhttp_padding: p.xhttp_padding, xmux_max_concurrency: p.xmux_max_concurrency,
      xmux_max_connections: p.xmux_max_connections, mux_concurrency: p.mux_concurrency,
      xudp_proxy_udp443: p.xudp_proxy_udp443, alpn: p.alpn, tls_min: p.tls_min, tls_max: p.tls_max,
    };
    validateMsg = "";
  }
  function cloneOf(p: TuningProfile) { edit(p); editor.id = null; editor.name = p.name + " copy"; }
  function stagePreset(name: string) {
    const p = presets.find((x) => x.name === name);
    if (!p) return;
    editor = { ...editor, ...p.fields, id: editor.id, name: editor.name || name };
    setMsg(`preset “${name}” staged into the editor — Create/Save to apply`, "ok");
  }
  async function save(e: Event) {
    e.preventDefault();
    const { id, ...body } = editor;
    try {
      const saved = id === null ? await api.addProfile(body) : await api.updateProfile(id, body);
      editor = blank(); validateMsg = ""; await refresh();
      setMsg(saved.is_active ? "saved & applied to the live tunnel" : "saved", "ok");
    } catch (err) { setMsg(errText(err, "save failed"), "err"); }
  }
  async function validate() {
    const { id, ...body } = editor;
    validateMsg = "validating…";
    try { const r = await api.validateProfile(body); validateMsg = r.ok ? "✓ profile valid" : `✗ ${r.error}`; }
    catch (err) { validateMsg = errText(err, "validate failed"); }
  }
  async function del(p: TuningProfile) {
    if (!(await confirmDialog(`Delete profile “${p.name}”?${p.node_count ? `\n${p.node_count} node(s) using it fall back to the default.` : ""}`))) return;
    try { await api.deleteProfile(p.id); if (editor.id === p.id) editor = blank(); await refresh(); }
    catch (err) { setMsg(errText(err, "delete failed"), "err"); }
  }
  async function makeDefault(id: number) {
    try { await api.setDefaultProfile(id); await refresh(); setMsg("default updated", "ok"); }
    catch (err) { setMsg(errText(err, "set-default failed"), "err"); }
  }
  async function applyActive(id: number) {
    try { const r = await api.applyProfileActive(id); setMsg(`applied to active node ${r.node_id}`, "ok"); await refresh(); }
    catch (err) { setMsg(errText(err, "apply failed"), "err"); }
  }
  function addNoise() { editor.noises = [...editor.noises, { type: "rand", packet: "50-150", delay: "10-16" }]; }
  function rmNoise(i: number) { editor.noises = editor.noises.filter((_, idx) => idx !== i); }

  $effect(() => { refresh(); });
</script>

<Alert {msg} kind={msgKind} />

<div class="card">
  <h3>Tuning profiles</h3>
  <p class="muted hint">Global anti-DPI profiles. Assign one per node from its <strong>Edit</strong> on the Nodes tab. The profile governing the live tunnel right now is marked <span class="badge active-b">● active</span>.</p>
  <div class="table-wrap"><table class="table">
    <thead><tr><th>name</th><th>used by</th><th>fingerprint</th><th>frag</th><th>noise</th><th>mux</th><th>DoH</th><th>QUIC</th><th></th></tr></thead>
    <tbody>
      {#each profiles as p (p.id)}
        <tr class:active={p.is_active}>
          <td>
            {p.name}
            {#if p.is_active}<span class="badge active-b">● active</span>{/if}
            {#if p.is_default}<span class="badge">default</span>{/if}
          </td>
          <td class="mono">{p.node_count || "—"}</td>
          <td>{p.fingerprint || "—"}</td>
          <td><span class="onoff" class:on={p.frag_enabled}>{p.frag_enabled ? "on" : "off"}</span></td>
          <td><span class="onoff" class:on={p.noise_enabled}>{p.noise_enabled ? "on" : "off"}</span></td>
          <td><span class="onoff" class:on={p.mux_enabled}>{p.mux_enabled ? "on" : "off"}</span></td>
          <td><span class="onoff" class:on={p.doh_enabled}>{p.doh_enabled ? "on" : "off"}</span></td>
          <td class="mono">{p.quic}</td>
          <td>
            <div class="row-actions">
            <button class="btn iconbtn" title="Edit" aria-label="Edit profile" onclick={() => edit(p)}>{@html I.edit}</button>
            <button class="btn iconbtn" title="Clone" aria-label="Clone profile" onclick={() => cloneOf(p)}>{@html I.clone}</button>
            <button class="btn iconbtn" title="Apply to the active node and re-apply now" aria-label="Apply to active" onclick={() => applyActive(p.id)}>{@html I.zap}</button>
            {#if !p.is_default}
              <button class="btn iconbtn" title="Make default" aria-label="Make default" onclick={() => makeDefault(p.id)}>{@html I.star}</button>
              <button class="btn iconbtn btn-danger" title="Delete" aria-label="Delete profile" onclick={() => del(p)}>{@html I.trash}</button>
            {/if}
            </div>
          </td>
        </tr>
      {/each}
    </tbody>
  </table></div>
</div>

<form onsubmit={save} class="card editor">
  <div class="ed-head">
    <h3>{editor.id === null ? "New profile" : `Edit profile #${editor.id}`}</h3>
    <select class="input auto" onchange={(e) => { const v = e.currentTarget.value; e.currentTarget.value = "__ph__"; if (v !== "__ph__") stagePreset(v); }}>
      <option value="__ph__" disabled selected>Preset…</option>
      {#each presets as p (p.name)}<option value={p.name}>{p.title}</option>{/each}
    </select>
  </div>

  <label class="field"><span>name</span><input class="input" bind:value={editor.name} placeholder="name" required /></label>
  <label class="field"><span>fingerprint (uTLS)</span>
    <select class="input" bind:value={editor.fingerprint}>
      {#each FINGERPRINTS as f}<option value={f}>{f === "" ? "(no mimicry — not recommended)" : f}</option>{/each}
    </select>
  </label>

  <fieldset>
    <legend>TLS fragmentation</legend>
    <div class="check"><Toggle checked={editor.frag_enabled} onchange={(v) => (editor.frag_enabled = v)} label="TLS fragmentation" /> <span>enable</span></div>
    {#if editor.frag_enabled}
      <label class="field"><span>packets</span>
        <input class="input" list="frag-packets" bind:value={editor.frag_packets} />
        <datalist id="frag-packets"><option value="tlshello"></option><option value="1-3"></option></datalist></label>
      <label class="field"><span>length</span><input class="input" bind:value={editor.frag_length} placeholder="100-200" /></label>
      <label class="field"><span>interval (ms)</span><input class="input" bind:value={editor.frag_interval} placeholder="10-20" /></label>
    {/if}
  </fieldset>

  <fieldset>
    <legend>UDP noise <small class="muted">decoy packets vs DPI / active probing</small></legend>
    <div class="check"><Toggle checked={editor.noise_enabled} onchange={(v) => (editor.noise_enabled = v)} label="noise" /> <span>enable</span></div>
    {#if editor.noise_enabled}
      {#each editor.noises as n, i (i)}
        <div class="kv">
          <select class="input" bind:value={n.type}><option value="rand">rand</option><option value="str">str</option><option value="base64">base64</option><option value="hex">hex</option></select>
          <input class="input" bind:value={n.packet} placeholder="packet (e.g. 50-150)" />
          <input class="input" bind:value={n.delay} placeholder="delay ms (10-16)" />
          <button class="btn btn-ghost" type="button" onclick={() => rmNoise(i)} aria-label="remove">×</button>
        </div>
      {/each}
      <div><button class="btn" type="button" onclick={addNoise}>+ noise packet</button></div>
    {/if}
  </fieldset>

  <fieldset>
    <legend>Mux <small class="muted">xhttp nodes only — ignored on Vision</small></legend>
    <div class="check"><Toggle checked={editor.mux_enabled} onchange={(v) => (editor.mux_enabled = v)} label="mux" /> <span>enable</span></div>
    {#if editor.mux_enabled}
      <label class="field"><span>concurrency</span><input class="input" bind:value={editor.mux_concurrency} placeholder="(default)" /></label>
      <label class="field"><span>xudpProxyUDP443</span>
        <select class="input" bind:value={editor.xudp_proxy_udp443}>
          <option value="">(default)</option><option value="reject">reject</option><option value="allow">allow</option><option value="skip">skip</option>
        </select></label>
    {/if}
  </fieldset>

  <fieldset>
    <legend>XHTTP transport <small class="muted">xhttp nodes only</small></legend>
    <label class="field"><span>padding bytes (xPaddingBytes)</span><input class="input" bind:value={editor.xhttp_padding} placeholder="100-1000" /></label>
    <label class="field"><span>xmux maxConcurrency</span><input class="input" bind:value={editor.xmux_max_concurrency} placeholder="(off)" /></label>
    <label class="field"><span>xmux maxConnections</span><input class="input" bind:value={editor.xmux_max_connections} placeholder="(off)" /></label>
  </fieldset>

  <fieldset>
    <legend>TLS (tls-mode nodes)</legend>
    <label class="field"><span>ALPN</span><input class="input" bind:value={editor.alpn} placeholder="h2,http/1.1" /></label>
    <label class="field"><span>min version</span><input class="input" bind:value={editor.tls_min} placeholder="(default)" /></label>
    <label class="field"><span>max version</span><input class="input" bind:value={editor.tls_max} placeholder="(default)" /></label>
  </fieldset>

  <fieldset>
    <legend>DNS / QUIC</legend>
    <div class="check"><Toggle checked={editor.doh_enabled} onchange={(v) => (editor.doh_enabled = v)} label="DoH" /> <span>DoH</span></div>
    {#if editor.doh_enabled}
      <label class="field"><span>DoH URL</span><input class="input" bind:value={editor.doh_url} placeholder="(default resolver)" /></label>
    {/if}
    <label class="field"><span>QUIC</span>
      <select class="input" bind:value={editor.quic}>
        <option value="allow">allow</option><option value="drop">drop (block)</option><option value="proxy">proxy</option>
      </select>
    </label>
  </fieldset>

  {#if validateMsg}<div class="vmsg" class:bad={validateMsg.startsWith("✗")}>{validateMsg}</div>{/if}
  <div class="actions">
    <button class="btn btn-primary">{editor.id === null ? "Create" : "Save"}</button>
    <button class="btn" type="button" onclick={validate}>Validate</button>
    {#if editor.id !== null}<button class="btn" type="button" onclick={() => (editor = blank())}>New</button>{/if}
  </div>
</form>

<style>
  .editor { max-width: 34rem; }
  .ed-head { display: flex; align-items: center; gap: 0.6rem; }
  .ed-head h3 { margin-right: auto; }
  .ed-head .auto { width: auto; }
  .row-actions { display: flex; gap: 0.25rem; flex-wrap: nowrap; align-items: center; white-space: nowrap; }
  .check { display: flex; gap: 0.6rem; align-items: center; }
  .actions { display: flex; gap: 0.5rem; margin-top: 0.6rem; }
  .hint { margin: 0 0 0.6rem; font-size: 0.85rem; }
  fieldset { border: 1px solid var(--border); border-radius: var(--radius); padding: 0.7rem 0.9rem; display: grid; gap: 0.5rem; background: var(--surface-2); margin: 0.5rem 0; }
  legend small { font-weight: 400; }
  .field { display: grid; gap: 0.2rem; }
  .kv { display: flex; gap: 0.4rem; }
  .kv .input { flex: 1; }
  tr.active td { background: var(--accent-soft); }
  .badge.active-b { background: var(--accent-soft); color: var(--accent); }
  .vmsg { font-family: var(--mono); font-size: 0.8rem; color: var(--success); white-space: pre-wrap; }
  .vmsg.bad { color: var(--danger); }
</style>
