<script lang="ts">
  import { api, ApiError, type RoutingRuleIn } from "./api";

  let rules = $state<RoutingRuleIn[]>([]);
  let defaultAction = $state("proxy");
  let msg = $state("");

  const strip = (rs: { type: string; value: string; action: string }[]): RoutingRuleIn[] =>
    rs.map(({ type, value, action }) => ({ type, value, action }));

  function placeholderFor(t: string): string {
    return ({ geoip: "ru | private | cn", geosite: "category-ads-all", domain: "example.com",
              ip: "1.2.3.0/24", port: "443 | 1000-2000" } as Record<string, string>)[t] ?? "";
  }

  async function load() {
    try { const r = await api.getRouting(); rules = strip(r.rules); defaultAction = r.default_action; }
    catch (err) { msg = err instanceof ApiError ? err.message : "load failed"; }
  }
  function addRule() { rules = [...rules, { type: "domain", value: "", action: "proxy" }]; }
  function removeRule(i: number) { rules = rules.filter((_, idx) => idx !== i); }
  function move(i: number, d: number) {
    const j = i + d;
    if (j < 0 || j >= rules.length) return;
    const next = [...rules];
    [next[i], next[j]] = [next[j], next[i]];
    rules = next;
  }
  async function save() {
    try { const r = await api.putRouting({ rules, default_action: defaultAction });
          rules = strip(r.rules); defaultAction = r.default_action; msg = "saved"; }
    catch (err) { msg = err instanceof ApiError ? err.message : "save failed"; }
  }
  async function importPreset() {
    try { const r = await api.routingPresetRuDirect();
          rules = strip(r.rules); defaultAction = r.default_action; msg = "RU-direct preset imported"; }
    catch (err) { msg = err instanceof ApiError ? err.message : "preset failed"; }
  }

  $effect(() => { load(); });
</script>

{#if msg}<p class="msg">{msg}</p>{/if}

<div class="card">
  <h3>Routing rules <small class="muted">matched top-to-bottom · private IPs always go direct first</small></h3>
  <table class="table">
    <thead><tr><th>#</th><th>type</th><th>value</th><th>action</th><th></th></tr></thead>
    <tbody>
      {#each rules as rule, i (i)}
        <tr>
          <td>{i + 1}</td>
          <td>
            <select class="input" bind:value={rule.type}>
              <option value="geoip">geoip</option>
              <option value="geosite">geosite</option>
              <option value="domain">domain</option>
              <option value="ip">ip</option>
              <option value="port">port</option>
            </select>
          </td>
          <td><input class="input" bind:value={rule.value} placeholder={placeholderFor(rule.type)} /></td>
          <td>
            <select class="input" bind:value={rule.action}>
              <option value="direct">direct</option>
              <option value="proxy">proxy</option>
              <option value="block">block</option>
            </select>
          </td>
          <td class="row-actions">
            <button class="btn btn-ghost" type="button" onclick={() => move(i, -1)} disabled={i === 0} aria-label="up">↑</button>
            <button class="btn btn-ghost" type="button" onclick={() => move(i, 1)} disabled={i === rules.length - 1} aria-label="down">↓</button>
            <button class="btn btn-ghost" type="button" onclick={() => removeRule(i)} aria-label="remove">✕</button>
          </td>
        </tr>
      {/each}
      {#if rules.length === 0}
        <tr><td colspan="5" class="empty muted">no rules — all traffic follows the default action</td></tr>
      {/if}
    </tbody>
  </table>

  <div class="toolbar">
    <button class="btn" type="button" onclick={addRule}>+ Add rule</button>
    <button class="btn" type="button" onclick={importPreset}>Import RU-direct preset</button>
    <label class="inline">Default action
      <select class="input auto" bind:value={defaultAction}>
        <option value="proxy">proxy</option>
        <option value="direct">direct</option>
        <option value="block">block</option>
      </select>
    </label>
    <span class="spacer"></span>
    <button class="btn btn-primary" type="button" onclick={save}>Save</button>
  </div>
</div>

<style>
  h3 small { font-weight: 400; font-size: 0.8rem; }
  .row-actions { display: flex; gap: 0.25rem; }
  .row-actions .btn { padding: 0.3rem 0.5rem; }
  .empty { font-style: italic; }
  .toolbar .inline { display: flex; gap: 0.35rem; align-items: center; }
  .toolbar .auto { width: auto; }
  .toolbar .spacer { margin-left: auto; }
</style>
