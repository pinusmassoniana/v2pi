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
  <div class="auth-bg" aria-hidden="true"></div>
  <form onsubmit={submit} class="card auth-card">
    <div class="auth-brand" aria-label={BRAND}>
      <span class="auth-mark">{BRAND.slice(0, 2)}</span>
      <span class="auth-word">{BRAND.slice(2)}</span>
    </div>
    <input class="input" bind:value={username} placeholder="username" autocomplete="username" />
    <input class="input" type="password" bind:value={password} placeholder="password" autocomplete="current-password" />
    <button class="btn btn-primary block" disabled={busy || !username || !password}>{busy ? "…" : "Log in"}</button>
    {#if error}<p class="err">{error}</p>{/if}
  </form>
</div>

<style>
  .auth { position: relative; min-height: 100dvh; display: grid; place-items: center; padding: 1rem; overflow: hidden; }
  .auth-bg {
    position: absolute; inset: 0; pointer-events: none;
    background:
      radial-gradient(55rem 38rem at 50% -12%, var(--accent-soft), transparent 60%),
      radial-gradient(40rem 30rem at 115% 120%, color-mix(in srgb, var(--accent) 7%, transparent), transparent 60%);
  }
  .auth-card { position: relative; width: 21rem; max-width: 100%; padding: 1.6rem 1.5rem; gap: 0.7rem; border-radius: var(--radius-lg); box-shadow: var(--shadow-lg); }
  .auth-brand { display: flex; align-items: center; justify-content: center; gap: 0.55rem; margin-bottom: 0.5rem; }
  .auth-mark {
    width: 30px; height: 30px; border-radius: 9px; display: grid; place-items: center; color: #fff;
    font-weight: 800; font-size: 0.82rem; letter-spacing: -0.03em;
    background: linear-gradient(150deg, var(--accent), color-mix(in srgb, var(--accent) 58%, #000));
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.35), var(--shadow-sm);
  }
  .auth-word { font-weight: 720; font-size: 1.25rem; letter-spacing: -0.02em; color: var(--text); }
  .err { color: var(--danger); font-size: 0.85rem; margin: 0; }
</style>
