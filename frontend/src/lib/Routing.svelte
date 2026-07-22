<script lang="ts">
  import { api, ApiError, type RoutingRuleIn, type PresetInfo } from "./api";
  import { confirmDialog } from "./confirm.svelte";
  import { I } from "./icons";
  import { inIPv4Cidr, parseDestination } from "./routing";

  let { onDirtyChange }: { onDirtyChange?: (dirty: boolean) => void } = $props();

  type Row = RoutingRuleIn & { uid: number; enabled: boolean; label: string };
  let _uid = 0;
  const nextUid = () => ++_uid;

  let rules = $state<Row[]>([]);
  let defaultAction = $state("proxy");
  let domainStrategy = $state("IPIfNonMatch");
  let presets = $state<PresetInfo[]>([]);
  let msg = $state("");
  let msgKind = $state<"ok" | "err">("ok");
  let validateMsg = $state("");
  let savedSnapshot = $state("");
  let saving = $state(false);      // RR5: in-flight guard so save/validate/import can't double-fire
  let hasActive = $state(false);   // RR2: is a node connected (so a save applies live)?
  let importOpen = $state(false);
  let importText = $state("");
  // RN6 destination tester
  let testDest = $state("");
  let testResult = $state("");

  const GEO_TOKENS = ["ru", "cn", "private", "category-ru", "category-ads-all", "geolocation-!cn", "cn", "google", "telegram"];

  function setMsg(t: string, kind: "ok" | "err" = "ok") { msg = t; msgKind = kind; }
  function errText(err: unknown, fb: string) { return err instanceof ApiError ? err.message : fb; }
  const strip = (rs: Row[]): RoutingRuleIn[] =>
    rs.map(({ type, value, action, enabled, label }) => ({ type, value, action, enabled, label }));
  function snapshot(): string {
    return JSON.stringify({ rules: strip(rules), defaultAction, domainStrategy });
  }
  const dirty = $derived(snapshot() !== savedSnapshot);
  const nonEmpty = $derived(rules.filter((r) => r.value.trim()));

  function placeholderFor(t: string): string {
    return ({ geoip: "ru | private | cn (comma-sep ok)", geosite: "category-ads-all",
              domain: "example.com, *.ya.ru", ip: "1.2.3.0/24, 10.0.0.0/8",
              port: "443 | 1000-2000 | 80,443" } as Record<string, string>)[t] ?? "";
  }
  function toRows(rs: { type: string; value: string; action: string; enabled?: boolean; label?: string }[]): Row[] {
    return rs.map((r) => ({ uid: nextUid(), type: r.type, value: r.value, action: r.action,
                            enabled: r.enabled ?? true, label: r.label ?? "" }));
  }

  async function load() {
    try {
      const [r, ps, st] = await Promise.all([api.getRouting(), api.listRoutingPresets(), api.getStatus()]);
      rules = toRows(r.rules); defaultAction = r.default_action; domainStrategy = r.domain_strategy;
      presets = ps; hasActive = st.active_node_id !== null; savedSnapshot = snapshot();
    } catch (err) { setMsg(errText(err, "load failed"), "err"); }
  }
  function addRule() { rules = [...rules, { uid: nextUid(), type: "domain", value: "", action: "proxy", enabled: true, label: "" }]; }
  function removeRule(uid: number) { rules = rules.filter((r) => r.uid !== uid); }
  function move(i: number, d: number) {
    const j = i + d;
    if (j < 0 || j >= rules.length) return;
    const next = [...rules];
    [next[i], next[j]] = [next[j], next[i]];
    rules = next;
  }
  async function save() {
    if (saving) return;
    if (defaultAction === "block" && !(await confirmDialog("Default action is BLOCK — all non-matching traffic from the segment will be dropped. Continue?")))
      return;
    saving = true;
    try {
      // RR8: drop identical (type,value,action) rows before PUT — keep the ruleset clean, warn (don't block)
      const seen = new Set<string>();
      const deduped = nonEmpty.filter((r) => {
        const k = JSON.stringify([r.type, r.value.trim(), r.action]);
        if (seen.has(k)) return false;
        seen.add(k); return true;
      });
      const dupCount = nonEmpty.length - deduped.length;
      const r = await api.putRouting({ rules: strip(deduped), default_action: defaultAction, domain_strategy: domainStrategy });
      rules = toRows(r.rules); defaultAction = r.default_action; domainStrategy = r.domain_strategy;
      savedSnapshot = snapshot(); validateMsg = "";
      setMsg((hasActive ? "saved & applied" : "saved — applies on next Connect") + (dupCount ? ` · ${dupCount} duplicate row(s) dropped` : ""), "ok");
    } catch (err) { setMsg(errText(err, "save failed"), "err"); }
    finally { saving = false; }
  }
  async function importPreset(name: string) {
    if (dirty && !(await confirmDialog("Discard staged rules?"))) return;
    if (saving) return;
    saving = true;
    try {
      const r = await api.routingPreset(name);
      rules = toRows(r.rules);                       // all preset fields are staged, not yet applied
      defaultAction = r.default_action;
      domainStrategy = r.domain_strategy;
      setMsg(`preset “${name}” staged — review and Save`, "ok");
    } catch (err) { setMsg(errText(err, "preset failed"), "err"); }
    finally { saving = false; }
  }
  async function validate() {
    if (saving) return;
    saving = true;
    validateMsg = "validating…";
    try {
      const r = await api.validateRouting({ rules: strip(nonEmpty), default_action: defaultAction, domain_strategy: domainStrategy });
      validateMsg = r.ok ? "✓ ruleset valid" : `✗ ${r.error}`;
    } catch (err) { validateMsg = errText(err, "validate failed"); }
    finally { saving = false; }
  }
  async function resetRules() {
    if (!(await confirmDialog("Reset to the default ruleset (no rules, default = proxy)?"))) return;
    rules = []; defaultAction = "proxy"; domainStrategy = "IPIfNonMatch";
  }
  function exportJSON() {
    const doc = JSON.stringify({ rules: strip(nonEmpty), default_action: defaultAction, domain_strategy: domainStrategy }, null, 2);
    navigator.clipboard?.writeText(doc).then(() => setMsg("ruleset copied as JSON", "ok"), () => setMsg("copy failed", "err"));
  }
  async function doImportJSON() {
    if (dirty && !(await confirmDialog("Discard staged rules?"))) return;   // RC2: don't clobber staged edits
    let doc: any;
    try { doc = JSON.parse(importText); }
    catch { setMsg("invalid JSON", "err"); return; }
    const rs = Array.isArray(doc) ? doc : doc?.rules;
    if (!Array.isArray(rs)) { setMsg("no rules array / bad rule shape", "err"); return; }   // RC11: clearer than a rs.map throw
    rules = toRows(rs);
    if (doc.default_action) defaultAction = doc.default_action;
    if (doc.domain_strategy) domainStrategy = doc.domain_strategy;
    importText = ""; importOpen = false; setMsg("imported — review and Save", "ok");
  }

  // --- RN6 client-side destination tester (literal rule types only) ---
  function portMatches(spec: string, port: number): boolean {
    return spec.split(",").some((t) => { const [a, b] = t.trim().split("-").map(Number); return b === undefined ? port === a : port >= a && port <= b; });
  }
  const PRIV = ["10.0.0.0/8", "192.168.0.0/16", "172.16.0.0/12", "127.0.0.0/8"];
  function runTest() {
    const raw = testDest.trim(); if (!raw) { testResult = ""; return; }
    const parsed = parseDestination(raw);
    if (parsed.ipv6) { testResult = "IPv6 preview is not evaluated locally — Validate/Save uses Xray's matcher."; return; }
    const { host } = parsed, port = parsed.port ?? NaN;
    const isIp = inIPv4Cidr(host, `${host}/32`);
    if (isIp && PRIV.some((c) => inIPv4Cidr(host, c))) { testResult = `→ direct (private IP, always matched first)`; return; }
    let skippedGeo = false;
    for (const r of rules) {
      if (!r.enabled || !r.value.trim()) continue;
      const vals = r.value.split(/[,\n]/).map((v) => v.trim()).filter(Boolean);
      let hit = false;
      if (r.type === "domain") hit = vals.some((v) => { const base = v.replace(/^\*\.?/, ""); return host === base || host.endsWith("." + base); });
      else if (r.type === "ip") hit = isIp && vals.some((v) => inIPv4Cidr(host, v));
      else if (r.type === "port") hit = !isNaN(port) && vals.some((v) => portMatches(v, port));
      else { skippedGeo = true; continue; }   // geoip/geosite — needs geo data, can't eval locally
      if (hit) { testResult = `→ ${r.action}  (matched ${r.type} “${r.value}”${r.label ? ` · ${r.label}` : ""})`; return; }
    }
    testResult = `→ ${defaultAction}  (default${skippedGeo ? " · geo rules not evaluated locally" : ""})`;
  }

  $effect(() => { onDirtyChange?.(dirty); return () => onDirtyChange?.(false); });

  $effect(() => { load(); });
</script>

{#if msg && !dirty}<p class="msg" class:err={msgKind === "err"} role={msgKind === "err" ? "alert" : "status"} aria-live="polite">{msg}</p>{/if}

{#if dirty}
  <div class="staged" role="status">
    <span class="staged-chip">STAGED</span>
    <span class="staged-txt"><strong>{nonEmpty.length} rules</strong> edited — not yet applied to the live config.{#if msg} · {msg}{/if}</span>
    <button class="btn sm" type="button" onclick={load}>Discard</button>
    <button class="btn btn-primary sm" type="button" onclick={save} disabled={saving}>Apply staged</button>
  </div>
{/if}

<div class="card">
  <div class="card-top">
    <span class="eyebrow">Routing rules</span>
    <span class="muted-sm">{nonEmpty.length} rules · matched top→bottom · private IPs always direct first</span>
  </div>
  <div class="table-wrap"><table class="table">
    <thead><tr><th>#</th><th>on</th><th>type</th><th>value</th><th>action</th><th>label</th><th></th></tr></thead>
    <tbody>
      <tr class="anchor"><td>—</td><td></td><td>ip</td><td>private ranges</td><td class="act-direct">direct</td><td class="muted">implicit, always first</td><td></td></tr>
      {#each rules as rule, i (rule.uid)}
        <tr class:disabled={!rule.enabled}>
          <td>{i + 1}</td>
          <td><input type="checkbox" bind:checked={rule.enabled} aria-label={`Rule ${i + 1} enabled`} /></td>
          <td>
            <select class="input" bind:value={rule.type} aria-label={`Rule ${i + 1} type`}>
              <option value="geoip">geoip</option><option value="geosite">geosite</option>
              <option value="domain">domain</option><option value="ip">ip</option><option value="port">port</option>
            </select>
          </td>
          <td>
            <input class="input" list={rule.type === "geoip" || rule.type === "geosite" ? "geo-tokens" : undefined}
                   bind:value={rule.value} placeholder={placeholderFor(rule.type)} aria-label={`Rule ${i + 1} value`} />
          </td>
          <td>
            <select class="input act act-{rule.action}" bind:value={rule.action} aria-label={`Rule ${i + 1} action`}>
              <option value="direct">direct</option><option value="proxy">proxy</option><option value="block">block</option>
            </select>
          </td>
          <td><input class="input label" bind:value={rule.label} placeholder="note (optional)" aria-label={`Rule ${i + 1} label`} /></td>
          <td>
            <div class="row-actions">
            <button class="btn iconbtn" type="button" title="Move up" onclick={() => move(i, -1)} disabled={i === 0} aria-label="move up">{@html I.up}</button>
            <button class="btn iconbtn" type="button" title="Move down" onclick={() => move(i, 1)} disabled={i === rules.length - 1} aria-label="move down">{@html I.down}</button>
            <button class="btn iconbtn btn-danger" type="button" title="Remove rule" onclick={() => removeRule(rule.uid)} aria-label="remove rule">{@html I.trash}</button>
            </div>
          </td>
        </tr>
      {/each}
      {#if rules.length === 0}
        <tr><td colspan="7" class="empty muted">no rules — all traffic follows the default action</td></tr>
      {/if}
      <tr class="anchor"><td>—</td><td></td><td>all</td><td>everything else</td><td class="act-{defaultAction}">{defaultAction}</td><td class="muted">default catch-all, always last</td><td></td></tr>
    </tbody>
  </table></div>
  <datalist id="geo-tokens">{#each GEO_TOKENS as t}<option value={t}></option>{/each}</datalist>
  <p class="note muted">DNS / QUIC / stats rules are injected automatically when those features are enabled.</p>

  <div class="toolbar">
    <button class="btn" type="button" onclick={addRule}>+ Add rule</button>
    <select class="input auto" aria-label="Import routing preset" disabled={saving} onchange={(e) => { const v = e.currentTarget.value; e.currentTarget.value = "__ph__"; if (v !== "__ph__") importPreset(v); }}>
      <option value="__ph__" disabled selected>Import preset…</option>
      {#each presets as p (p.name)}<option value={p.name}>{p.title}</option>{/each}
    </select>
    <label class="inline">Default
      <select class="input auto" bind:value={defaultAction}>
        <option value="proxy">proxy</option><option value="direct">direct</option><option value="block">block</option>
      </select>
    </label>
    <label class="inline">Domain strategy
      <select class="input auto" bind:value={domainStrategy}>
        <option value="IPIfNonMatch">IPIfNonMatch</option><option value="AsIs">AsIs</option><option value="IPOnDemand">IPOnDemand</option>
      </select>
    </label>
    <span class="spacer"></span>
    <button class="btn" type="button" onclick={exportJSON} title="Copy ruleset as JSON">Export</button>
    <button class="btn" type="button" onclick={() => (importOpen = true)}>Import JSON</button>
    <button class="btn" type="button" onclick={resetRules}>Reset</button>
    <button class="btn" type="button" onclick={validate} disabled={saving}>Validate</button>
    <button class="btn btn-primary" type="button" onclick={save} disabled={!dirty || saving}>Save</button>
  </div>
  {#if validateMsg}<p class="vmsg" class:bad={validateMsg.startsWith("✗")}>{validateMsg}</p>{/if}
</div>

<div class="card tester">
  <h3>Test a destination <small class="muted">how would this host/port be routed? (literal rules only — geo not evaluated locally)</small></h3>
  <div class="trow">
    <input class="input" bind:value={testDest} aria-label="Destination to evaluate" placeholder="example.com  |  1.2.3.4  |  1.2.3.4:443" onkeydown={(e) => e.key === "Enter" && runTest()} />
    <button class="btn" type="button" onclick={runTest}>Evaluate</button>
    {#if testResult}<span class="tres mono">{testResult}</span>{/if}
  </div>
</div>

{#if importOpen}
  <div class="card">
    <h3>Import ruleset JSON</h3>
    <textarea class="input ta" bind:value={importText} rows="6" aria-label="Routing rules JSON" placeholder={'{"rules":[{"type":"domain","value":"x.com","action":"block"}],"default_action":"proxy"}'}></textarea>
    <div class="toolbar">
      <button class="btn btn-primary" type="button" onclick={doImportJSON} disabled={!importText.trim()}>Load</button>
      <button class="btn" type="button" onclick={() => (importOpen = false)}>Cancel</button>
    </div>
  </div>
{/if}

<style>
  h3 small { font-weight: 400; font-size: 0.8rem; }
  .card-top { display: flex; align-items: center; justify-content: space-between; gap: 0.6rem; flex-wrap: wrap; }
  .muted-sm { font-size: 0.72rem; color: var(--tx3); }
  /* staged banner — accent-tinted, maps the unsaved/preset-staged state to the spec */
  .staged { display: flex; align-items: center; gap: 0.7rem; padding: 0.6rem 0.9rem; margin-bottom: 0.2rem;
    background: color-mix(in srgb, var(--acc) 9%, var(--bg1)); border: 1px solid var(--acc); border-radius: 9px; }
  .staged-chip { font-size: 0.6rem; font-weight: 600; color: var(--acc); border: 1px solid var(--acc); border-radius: 4px; padding: 0.1rem 0.45rem; flex: none; }
  .staged-txt { flex: 1; font-size: 0.78rem; color: var(--tx2); }
  .staged-txt strong { color: var(--tx); }
  .btn.sm { padding: 0.3rem 0.7rem; font-size: 0.72rem; }
  /* colour-coded actions (select text + anchor cells) */
  .act { font-weight: 600; }
  .act-direct { color: var(--acc); font-weight: 600; }
  .act-proxy { color: var(--down); font-weight: 600; }
  .act-block { color: var(--err); font-weight: 600; }
  .row-actions { display: flex; gap: 0.25rem; align-items: center; white-space: nowrap; }
  .empty { font-style: italic; }
  tr.anchor td { background: var(--surface-2); color: var(--muted); font-size: 0.8rem; }
  tr.disabled { opacity: 0.5; }
  .label { max-width: 9rem; }
  .toolbar { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; margin-top: 0.7rem; }
  .toolbar .inline { display: flex; gap: 0.35rem; align-items: center; }
  .toolbar .auto { width: auto; }
  .toolbar .spacer { margin-left: auto; }
  .note { font-size: 0.75rem; margin-top: 0.5rem; }
  .vmsg { font-family: var(--mono); font-size: 0.8rem; color: var(--success); margin-top: 0.5rem; white-space: pre-wrap; }
  .vmsg.bad { color: var(--danger); }
  .tester { margin-top: 0.8rem; }
  .trow { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
  .trow .input { max-width: 22rem; }
  .tres { font-size: 0.82rem; overflow-wrap: anywhere; word-break: break-word; min-width: 0; }
  .ta { font-family: var(--mono); font-size: 0.8rem; width: 100%; resize: vertical; }
  /* RR12: on narrow screens let the rule table fit the viewport instead of forcing the 44rem min-width */
  @media (max-width: 600px) {
    .table-wrap > .table { min-width: 0; }
  }
</style>
