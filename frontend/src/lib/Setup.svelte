<script lang="ts">
  import { api, ApiError } from "./api";
  import { BRAND } from "./brand";
  let { onDone }: { onDone: () => void } = $props();
  let username = $state("");
  let password = $state("");
  let confirm = $state("");
  let error = $state("");
  let busy = $state(false);

  async function submit(e: Event) {
    e.preventDefault();
    if (password !== confirm) { error = "passwords don't match"; return; }
    busy = true; error = "";
    try {
      await api.setup(username, password);   // creates the credential AND opens a session
      await api.ensureCsrf();
      onDone();
    } catch (err) {
      error = err instanceof ApiError ? err.message : "setup failed";
    } finally {
      busy = false;
    }
  }
</script>

<div class="auth">
  <form onsubmit={submit} class="card">
    <h1>{BRAND}</h1>
    <p class="muted lead">First-time setup — create your admin account.</p>
    <input class="input" bind:value={username} placeholder="username" autocomplete="username" />
    <input class="input" type="password" bind:value={password} placeholder="password" autocomplete="new-password" />
    <input class="input" type="password" bind:value={confirm} placeholder="confirm password" autocomplete="new-password" />
    <button class="btn-primary" disabled={busy || !username || !password || !confirm}>{busy ? "…" : "Create account"}</button>
    {#if error}<p class="err">{error}</p>{/if}
  </form>
</div>

<style>
  .auth { min-height: 100vh; display: grid; place-items: center; padding: 1rem; }
  .auth .card { width: 20rem; }
  .auth h1 { margin: 0; font-size: 1.4rem; color: var(--accent); text-align: center; }
  .lead { margin: 0; }
  .err { color: var(--danger); font-size: 0.85rem; margin: 0; }
</style>
