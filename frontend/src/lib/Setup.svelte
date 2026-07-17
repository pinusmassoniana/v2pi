<script lang="ts">
  import { api, ApiError } from "./api";
  import { BRAND } from "./brand";
  let { onDone }: { onDone: () => void } = $props();
  let username = $state("");
  let password = $state("");
  let confirm = $state("");
  let error = $state("");
  let busy = $state(false);
  let showPw = $state(false);

  async function submit(e: Event) {
    e.preventDefault();
    if (busy) return;
    if (password.length < 8) { error = "password too short (min 8 characters)"; return; }
    if (password !== confirm) { error = "passwords don't match"; return; }
    busy = true; error = "";
    try {
      await api.setup(username, password);   // creates the credential AND opens a session
      await api.ensureCsrf();
      password = ""; confirm = "";           // don't leave the plaintext credential in memory
      onDone();
    } catch (err) {
      error = err instanceof ApiError ? err.message : "setup failed";
    } finally {
      busy = false;
    }
  }
</script>

<div class="auth">
  <div class="auth-bg" aria-hidden="true"></div>
  <form onsubmit={submit} class="card auth-card">
    <div class="auth-brand" aria-label={BRAND}>
      <img class="auth-mark" src="/brand-mark.png" alt="" />
      <span class="auth-word">{BRAND.slice(2)}</span>
    </div>
    <p class="muted lead">First-time setup — create your admin account.</p>
    <input class="input" name="username" id="username" bind:value={username} placeholder="username" autocomplete="username" />
    <div class="pw">
      <input class="input" name="new-password" id="new-password" type={showPw ? "text" : "password"} bind:value={password} placeholder="password" autocomplete="new-password" aria-describedby="pw-hint" />
      <button type="button" class="pw-toggle" tabindex="-1" onclick={() => (showPw = !showPw)} aria-label={showPw ? "Hide password" : "Show password"}>{showPw ? "Hide" : "Show"}</button>
    </div>
    <p id="pw-hint" class="muted hint">min 8 characters</p>
    <div class="pw">
      <input class="input" name="confirm-password" id="confirm-password" type={showPw ? "text" : "password"} bind:value={confirm} placeholder="confirm password" autocomplete="new-password" />
    </div>
    <button class="btn btn-primary block" disabled={busy || !username || !password || !confirm}>{busy ? "…" : "Create account"}</button>
    {#if error}<p class="err">{error}</p>{/if}
  </form>
</div>

<style>
  .hint { font-size: 0.72rem; margin: -0.35rem 0 0.1rem; }
  .auth { position: relative; min-height: 100dvh; display: grid; place-items: center; padding: 1rem; overflow: hidden; }
  .auth-bg {
    position: absolute; inset: 0; pointer-events: none;
    background:
      radial-gradient(55rem 38rem at 50% -12%, var(--accent-soft), transparent 60%),
      radial-gradient(40rem 30rem at 115% 120%, color-mix(in srgb, var(--accent) 7%, transparent), transparent 60%);
  }
  .auth-card { position: relative; width: 21rem; max-width: 100%; padding: 1.6rem 1.5rem; gap: 0.7rem; border-radius: var(--radius-lg); box-shadow: var(--shadow-lg); }
  .auth-brand { display: flex; align-items: center; justify-content: center; gap: 0.55rem; margin-bottom: 0.3rem; }
  .auth-mark {
    width: 30px; height: 30px; border-radius: 9px; display: block; object-fit: cover;
    box-shadow: var(--shadow-sm);
  }
  .auth-word { font-weight: 720; font-size: 1.25rem; letter-spacing: -0.02em; color: var(--text); }
  .lead { margin: 0 0 0.2rem; text-align: center; font-size: 0.85rem; }
  .pw { position: relative; display: flex; }
  .pw .input { flex: 1; padding-right: 3.4rem; }
  .pw-toggle { position: absolute; right: 0.4rem; top: 50%; transform: translateY(-50%); background: none; border: 0; padding: 0.2rem 0.4rem; font-size: 0.7rem; color: var(--muted); cursor: pointer; }
  .pw-toggle:hover { color: var(--text); }
  .err { color: var(--danger); font-size: 0.85rem; margin: 0; }
</style>
