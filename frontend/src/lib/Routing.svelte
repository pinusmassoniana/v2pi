<script lang="ts">
  import { api, ApiError, type RoutingRuleIn, type PresetInfo } from "./api";
  import { confirmDialog } from "./confirm.svelte";
  import { I } from "./icons";

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
    if (defaultAction === "block" && !(await confirmDialog("Default action is BLOCK — all non-matching traffic from the segment will be dropped. Continue?")))
      return;
    try {
      const r = await api.putRouting({ rules: strip(nonEmpty), default_action: defaultAction, domain_strategy: domainStrategy });
      rules = toRows(r.rules); defaultAction = r.default_action; domainStrategy = r.domain_strategy;
      savedSnapshot = snapshot(); validateMsg = "";
      setMsg(hasActive ? "saved & applied" : "saved — applies on next Connect", "ok");
    } catch (err) { setMsg(errText(err, "save failed"), "err"); }
  }
  async function importPreset(name: string) {
    try {
      const r = await api.routingPreset(name);
      rules = toRows(r.rules);                       // staged, not yet applied (RC1)
      setMsg(`preset “${name}” staged — review and Save`, "ok");
    } catch (err) { setMsg(errText(err, "preset failed"), "err"); }
  }
  async function validate() {
    validateMsg = "validating…";
    try {
      const r = await api.validateRouting({ rules: strip(nonEmpty), default_action: defaultAction, domain_strategy: domainStrategy });
      validateMsg = r.ok ? "✓ ruleset valid" : `✗ ${r.error}`;
    } catch (err) { validateMsg = errText(err, "validate failed"); }
  }
  async function resetRules() {
    if (!(await confirmDialog("Reset to the default ruleset (no rules, default = proxy)?"))) return;
    rules = []; defaultAction = "proxy"; domainStrategy = "IPIfNonMatch";
  }
  function exportJSON() {
    const doc = JSON.stringify({ rules: strip(nonEmpty), default_action: defaultAction, domain_strategy: domainStrategy }, null, 2);
    navigator.clipboard?.writeText(doc).then(() => setMsg("ruleset copied as JSON", "ok"), () => setMsg("copy failed", "err"));
  }
  function doImportJSON() {
    try {
      const doc = JSON.parse(importText);
      const rs = Array.isArray(doc) ? doc : doc.rules;
      rules = toRows(rs);
      if (doc.default_action) defaultAction = doc.default_action;
      if (doc.domain_strategy) domainStrategy = doc.domain_strategy;
      importText = ""; importOpen = false; setMsg("imported — review and Save", "ok");
    } catch { setMsg("invalid JSON", "err"); }
  }

  // --- RN6 client-side destination tester (literal rule types only) ---
  function ipToInt(ip: string): number | null {
    const p = ip.split("."); if (p.length !== 4) return null;
    let n = 0; for (const x of p) { const v = +x; if (!Number.isInteger(v) || v < 0 || v > 255) return null; n = n * 256 + v; }
    return n >>> 0;
  }
  function inCidr(ip: string, cidr: string): boolean {
    const [base, bitsS] = cidr.split("/"); const bits = bitsS === undefined ? 32 : +bitsS;
    const a = ipToInt(ip), b = ipToInt(base); if (a === null || b === null) return false;
    if (bits <= 0) return true; const mask = (~((1 << (32 - bits)) - 1)) >>> 0;
    return (a & mask) === (b & mask);
  }
  function portMatches(spec: string, port: number): boolean {
    return spec.split(",").some((t) => { const [a, b] = t.trim().split("-").map(Number); return b === undefined ? port === a : port >= a && port <= b; });
  }
  const PRIV = ["10.0.0.0/8", "192.168.0.0/16", "172.16.0.0/12", "127.0.0.0/8"];
  function runTest() {
    const raw = testDest.trim(); if (!raw) { testResult = ""; return; }
    let host = raw, port = NaN;
    const m = raw.match(/^(.*):(\d+)$/); if (m) { host = m[1]; port = +m[2]; }
    const isIp = ipToInt(host) !== null;
    if (isIp && PRIV.some((c) => inCidr(host, c))) { testResult = `→ direct (private IP, always matched first)`; return; }
    let skippedGeo = false;
    for (const r of rules) {
      if (!r.enabled || !r.value.trim()) continue;
      const vals = r.value.split(/[,\n]/).map((v) => v.trim()).filter(Boolean);
      let hit = false;
      if (r.type === "domain") hit = vals.some((v) => host === v || host.endsWith("." + v.replace(/^\*\.?/, "")));
      else if (r.type === "ip") hit = isIp && vals.some((v) => inCidr(host, v));
      else if (r.type === "port") hit = !isNaN(port) && vals.some((v) => portMatches(v, port));
      else { skippedGeo = true; continue; }   // geoip/geosite — needs geo data, can't eval locally
      if (hit) { testResult = `→ ${r.action}  (matched ${r.type} “${r.value}”${r.label ? ` · ${r.label}` : ""})`; return; }
    }
    testResult = `→ ${defaultAction}  (default${skippedGeo ? " · geo rules not evaluated locally" : ""})`;
  }

  $effect(() => { load(); });
</script>

{#if msg || dirty}<p class="msg" class:err={msgKind === "err"} role={msgKind === "err" ? "alert" : "status"} aria-live="polite">{msg}{#if dirty} · <span class="dirty">unsaved changes</span>{/if}</p>{/if}

<div class="card">
  <h3>Routing rules <small class="muted">matched top-to-bottom · private IPs always go direct first</small></h3>
  <div class="table-wrap"><table class="table">
    <thead><tr><th>#</th><th>on</th><th>type</th><th>value</th><th>action</th><th>label</th><th></th></tr></thead>
    <tbody>
      <tr class="anchor"><td>—</td><td></td><td>ip</td><td>private ranges</td><td>direct</td><td class="muted">implicit, always first</td><td></td></tr>
      {#each rules as rule, i (rule.uid)}
        <tr class:disabled={!rule.enabled}>
          <td>{i + 1}</td>
          <td><input type="checkbox" bind:checked={rule.enabled} aria-label="enabled" /></td>
          <td>
            <select class="input" bind:value={rule.type}>
              <option value="geoip">geoip</option><option value="geosite">geosite</option>
              <option value="domain">domain</option><option value="ip">ip</option><option value="port">port</option>
            </select>
          </td>
          <td>
            <input class="input" list={rule.type === "geoip" || rule.type === "geosite" ? "geo-tokens" : undefined}
                   bind:value={rule.value} placeholder={placeholderFor(rule.type)} />
          </td>
          <td>
            <select class="input" bind:value={rule.action}>
              <option value="direct">direct</option><option value="proxy">proxy</option><option value="block">block</option>
            </select>
          </td>
          <td><input class="input label" bind:value={rule.label} placeholder="note (optional)" /></td>
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
      <tr class="anchor"><td>—</td><td></td><td>all</td><td>everything else</td><td>{defaultAction}</td><td class="muted">default catch-all, always last</td><td></td></tr>
    </tbody>
  </table></div>
  <datalist id="geo-tokens">{#each GEO_TOKENS as t}<option value={t}></option>{/each}</datalist>
  <p class="note muted">DNS / QUIC / stats rules are injected automatically when those features are enabled.</p>

  <div class="toolbar">
    <button class="btn" type="button" onclick={addRule}>+ Add rule</button>
    <select class="input auto" onchange={(e) => { const v = e.currentTarget.value; e.currentTarget.value = "__ph__"; if (v !== "__ph__") importPreset(v); }}>
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
    <button class="btn" type="button" onclick={validate}>Validate</button>
    <button class="btn btn-primary" type="button" onclick={save} disabled={!dirty}>Save</button>
  </div>
  {#if validateMsg}<p class="vmsg" class:bad={validateMsg.startsWith("✗")}>{validateMsg}</p>{/if}
</div>

<div class="card tester">
  <h3>Test a destination <small class="muted">how would this host/port be routed? (literal rules only — geo not evaluated locally)</small></h3>
  <div class="trow">
    <input class="input" bind:value={testDest} placeholder="example.com  |  1.2.3.4  |  1.2.3.4:443" onkeydown={(e) => e.key === "Enter" && runTest()} />
    <button class="btn" type="button" onclick={runTest}>Evaluate</button>
    {#if testResult}<span class="tres mono">{testResult}</span>{/if}
  </div>
</div>

{#if importOpen}
  <div class="card">
    <h3>Import ruleset JSON</h3>
    <textarea class="input ta" bind:value={importText} rows="6" placeholder={'{"rules":[{"type":"domain","value":"x.com","action":"block"}],"default_action":"proxy"}'}></textarea>
    <div class="toolbar">
      <button class="btn btn-primary" type="button" onclick={doImportJSON} disabled={!importText.trim()}>Load</button>
      <button class="btn" type="button" onclick={() => (importOpen = false)}>Cancel</button>
    </div>
  </div>
{/if}

<style>
  h3 small { font-weight: 400; font-size: 0.8rem; }
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
  .dirty { color: var(--danger); font-weight: 600; }
  .tester { margin-top: 0.8rem; }
  .trow { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
  .trow .input { max-width: 22rem; }
  .tres { font-size: 0.82rem; }
  .ta { font-family: var(--mono); font-size: 0.8rem; width: 100%; resize: vertical; }
</style>
