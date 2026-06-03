<script lang="ts">
  import { api, ApiError } from "./api";
  import { BRAND } from "./brand";
  let { onLogin }: { onLogin: () => void } = $props();
  let username = $state("");
  let password = $state("");
  let error = $state("");
  let busy = $state(false);

  async function submit(e: Event) {
    e.preventDefault();
    busy = true; error = "";
    try {
      await api.login(username, password);
      await api.ensureCsrf();
      onLogin();
    } catch (err) {
      error = err instanceof ApiError ? err.message : "login failed";
    } finally {
      busy = false;
    }
  }
</script>

<div class="auth">
  <form onsubmit={submit} class="card">
    <h1>{BRAND}</h1>
    <input class="input" bind:value={username} placeholder="username" autocomplete="username" />
    <input class="input" type="password" bind:value={password} placeholder="password" autocomplete="current-password" />
    <button class="btn-primary" disabled={busy || !username || !password}>{busy ? "…" : "Log in"}</button>
    {#if error}<p class="err">{error}</p>{/if}
  </form>
</div>

<style>
  .auth { min-height: 100vh; display: grid; place-items: center; padding: 1rem; }
  .auth .card { width: 20rem; }
  .auth h1 { margin: 0; font-size: 1.4rem; color: var(--accent); text-align: center; }
  .err { color: var(--danger); font-size: 0.85rem; margin: 0; }
</style>
