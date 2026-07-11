<script lang="ts">
  import Setup from "./lib/Setup.svelte";
  import Login from "./lib/Login.svelte";
  import Dashboard from "./lib/Dashboard.svelte";
  import Health from "./lib/Health.svelte";
  import Nodes from "./lib/Nodes.svelte";
  import Subscriptions from "./lib/Subscriptions.svelte";
  import Tuning from "./lib/Tuning.svelte";
  import Routing from "./lib/Routing.svelte";
  import Network from "./lib/Network.svelte";
  import Operations from "./lib/Operations.svelte";
  import Settings from "./lib/Settings.svelte";
  import Toggle from "./lib/Toggle.svelte";
  import ConfirmModal from "./lib/ConfirmModal.svelte";
  import { api, setOnUnauthorized } from "./lib/api";
  import { statusStore, subscribeStatus, pollStatusOnce, resetStatus } from "./lib/status.svelte";
  import { BRAND } from "./lib/brand";
  import { applyTheme, toggleTheme, type Theme } from "./lib/theme";

  type View = "dashboard" | "health" | "nodes" | "tuning" | "routing" | "network" | "operations" | "settings";
  let authed = $state(false);
  let needsSetup = $state(false);
  let ready = $state(false);
  let bootError = $state(false);   // server unreachable at boot (vs. genuinely unauthenticated)
  let view = $state<View>("dashboard");
  // seeded from the attribute the anti-FOUC/main bootstrap already resolved
  let theme = $state<Theme>((document.documentElement.dataset.theme as Theme) || "dark");
  const status = $derived(statusStore.value);   // shared, visibility-aware poller

  // F1: any 401 mid-session (idle timeout, password change elsewhere) drops the whole app
  // back to the Login screen instead of leaving panels showing dead-request errors.
  setOnUnauthorized(() => { authed = false; resetStatus(); });

  // On load: if first-run (no credential) show Setup; else probe the session for auth.
  // F3: a failed /setup probe means the server is unreachable — distinct from "not logged in".
  // Show a retry/offline screen instead of dropping the operator onto a login form that can't work.
  function boot() {
    bootError = false;
    ready = false;
    api.getSetup()
      .then(async (s) => {
        needsSetup = s.needs_setup;
        if (!needsSetup) {
          try { await api.getStatus(); authed = true; } catch { authed = false; }
        }
      })
      .catch(() => { bootError = true; })
      .finally(() => { ready = true; });
  }
  $effect(() => { boot(); });

  // Shared status poller drives the sidebar xray box (and the Dashboard) — one timer, paused
  // when the tab is hidden.
  $effect(() => {
    if (!authed) return;
    return subscribeStatus(4000);
  });
  async function toggleXray(on: boolean) {
    try { if (on) await api.xrayStart(); else await api.xrayStop(); } catch {}
    await pollStatusOnce();
  }

  const tabs: { id: View; label: string }[] = [
    { id: "dashboard", label: "Overview" },
    { id: "health", label: "Health & Traffic" },
    { id: "nodes", label: "Nodes" },
    { id: "tuning", label: "Anti-DPI" },
    { id: "routing", label: "Routing" },
    { id: "network", label: "Network" },
    { id: "operations", label: "Operations" },
    { id: "settings", label: "Settings" },
  ];
  // NOC nav: grouped under dim section labels. Settings sits on its own below the groups
  // (the spec's 8th screen). Health & Traffic / Operations arrive with later Phase-2 screens.
  const navGroups: { label: string; ids: View[] }[] = [
    { label: "MONITOR", ids: ["dashboard", "health"] },
    { label: "CONFIGURE", ids: ["nodes", "tuning", "routing", "network", "operations"] },
  ];

  // 24-viewBox icons (theme toggle) — thin line, currentColor
  const svg = (p: string) =>
    `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;
  // 16-viewBox nav glyphs — match the handoff's terse line icons (1.4 stroke)
  const nsvg = (p: string) =>
    `<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;
  const icons: Record<string, string> = {
    dashboard: nsvg('<rect x="2" y="2" width="5" height="5" rx="1"/><rect x="9" y="2" width="5" height="5" rx="1"/><rect x="2" y="9" width="5" height="5" rx="1"/><rect x="9" y="9" width="5" height="5" rx="1"/>'),
    health: nsvg('<polyline points="1,9 4,9 6,3 9,13 11,7 15,7"/>'),
    nodes: nsvg('<rect x="2" y="2.5" width="12" height="4" rx="1"/><rect x="2" y="9.5" width="12" height="4" rx="1"/><circle cx="5" cy="4.5" r=".7" fill="currentColor" stroke="none"/><circle cx="5" cy="11.5" r=".7" fill="currentColor" stroke="none"/>'),
    subs: nsvg('<path d="M2.5 9a6 6 0 0 1 6 6M2.5 4.5a10.5 10.5 0 0 1 10.5 10.5"/><circle cx="3.2" cy="13.3" r="1.1" fill="currentColor" stroke="none"/>'),
    tuning: nsvg('<path d="M8 1.5l5 2v4c0 3.2-2.2 4.8-5 6-2.8-1.2-5-2.8-5-6v-4z"/>'),
    routing: nsvg('<circle cx="3.5" cy="8" r="2"/><circle cx="12.5" cy="3.5" r="1.6"/><circle cx="12.5" cy="12.5" r="1.6"/><path d="M5.5 8h3l2.5-4M8.5 8l2.5 4"/>'),
    network: nsvg('<circle cx="8" cy="8" r="6"/><path d="M2 8h12M8 2c2 2 2 10 0 12M8 2c-2 2-2 10 0 12"/>'),
    operations: nsvg('<rect x="2" y="3" width="12" height="3.5" rx="1"/><path d="M3 6.5v6a.8.8 0 0 0 .8.8h8.4a.8.8 0 0 0 .8-.8v-6"/><path d="M6.5 9.5h3"/>'),
    settings: nsvg('<line x1="3" y1="4" x2="13" y2="4"/><circle cx="10" cy="4" r="1.6"/><line x1="3" y1="8" x2="13" y2="8"/><circle cx="6" cy="8" r="1.6"/><line x1="3" y1="12" x2="13" y2="12"/><circle cx="11" cy="12" r="1.6"/>'),
    moon: svg('<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>'),
    sun: svg('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M19 5l-1.5 1.5M6.5 17.5L5 19"/>'),
  };

  function toggleThemeNow() {
    theme = toggleTheme(theme);
    applyTheme(theme);
  }

  async function logout() {
    await api.logout();
    authed = false;
    resetStatus();
    view = "dashboard";
  }

  // a11y: move focus to the page heading when the view *changes* so SR users hear the new screen.
  // Skip the initial mount (guard) so we don't steal focus / scroll-to-top on first paint.
  let titleEl = $state<HTMLElement>();
  let mountedOnce = false;
  $effect(() => {
    view;
    if (mountedOnce) titleEl?.focus();
    else mountedOnce = true;
  });
</script>

{#if !ready}
  <div class="boot"><span class="spinner" role="status" aria-label="Loading"></span></div>
{:else if bootError}
  <div class="boot boot-err">
    <p class="msg err">Can't reach the panel server.</p>
    <button class="btn" onclick={boot}>Retry</button>
  </div>
{:else if needsSetup}
  <Setup onDone={() => { needsSetup = false; authed = true; }} />
{:else if authed}
  <div class="shell">
    <aside class="sidebar">
      <div class="brand" aria-label={BRAND}>
        <span class="brand-mark" aria-hidden="true">v2</span>
        <span class="brand-text">
          <span class="brand-word">{BRAND}</span>
          <span class="brand-caption">gateway ctl</span>
        </span>
      </div>
      <nav>
        {#each navGroups as g (g.label)}
          <div class="nav-group">{g.label}</div>
          {#each g.ids as id (id)}
            {@const t = tabs.find((x) => x.id === id)!}
            <button class="nav-item" class:active={view === id} onclick={() => (view = id)}
                    aria-label={t.label} aria-current={view === id ? "page" : undefined}>
              {@html icons[id]}<span>{t.label}</span>
            </button>
          {/each}
        {/each}
        <div class="nav-spacer"></div>
        <button class="nav-item" class:active={view === "settings"} onclick={() => (view = "settings")}
                aria-label="Settings" aria-current={view === "settings" ? "page" : undefined}>
          {@html icons.settings}<span>Settings</span>
        </button>
      </nav>
      {#if status}
        <div class="xray-box">
          <span class="xray-dot {status.xray_state}"></span>
          <span class="xray-label">xray-core<br /><small>{status.xray_state}</small></span>
          <Toggle checked={status.xray_state === "working"} onchange={toggleXray} label="xray-core" />
        </div>
      {/if}
    </aside>
    <div class="content">
      <header class="topbar">
        <h1 class="page-title" tabindex="-1" bind:this={titleEl}>{tabs.find((t) => t.id === view)?.label}</h1>
        <button class="btn-ghost icon-btn" onclick={toggleThemeNow} aria-label="Toggle theme" title="Toggle theme">
          {@html theme === "dark" ? icons.sun : icons.moon}
        </button>
        <button class="btn-ghost" onclick={logout}>Log out</button>
        {#if status}
          <div class="conn" class:up={status.xray_state === "working"} aria-live="polite">
            <span class="conn-dot"></span>
            <span class="conn-label">{status.xray_state === "working" ? "ONLINE" : "OFFLINE"}</span>
          </div>
        {/if}
      </header>
      <main class="page" class:wide={view === "nodes"}>
        {#if view === "dashboard"}
          <Dashboard />
        {:else if view === "health"}
          <Health />
        {:else if view === "nodes"}
          <Nodes />
          <div class="fold-head"><span class="eyebrow">Subscriptions</span></div>
          <Subscriptions />
        {:else if view === "tuning"}
          <Tuning />
        {:else if view === "routing"}
          <Routing />
        {:else if view === "network"}
          <Network />
        {:else if view === "operations"}
          <Operations />
        {:else}
          <Settings />
        {/if}
      </main>
    </div>
  </div>
  <ConfirmModal />
{:else}
  <Login onLogin={() => (authed = true)} />
{/if}

<style>
  .boot { min-height: 100dvh; display: grid; place-items: center; }
  .boot-err { gap: 0.9rem; align-content: center; }
  .page-title:focus { outline: none; }   /* programmatic focus for SR, no visible ring */
  .shell { display: grid; grid-template-columns: 212px 1fr; min-height: 100dvh; }
  .sidebar {
    background: var(--bg1);
    border-right: 1px solid var(--bd);
    display: flex;
    flex-direction: column;
    position: sticky;
    top: 0;
    height: 100dvh;
    overflow: hidden;
  }
  /* brand — green chip + inner square + glow, wordmark + caption, bottom hairline */
  .brand {
    display: flex; align-items: center; gap: 0.65rem;
    padding: 1rem 1rem 0.9rem;
    border-bottom: 1px solid var(--bd);
  }
  .brand-mark {
    width: 26px; height: 26px; border-radius: 6px; flex: none;
    display: grid; place-items: center;
    background: var(--acc);
    box-shadow: 0 0 14px -2px var(--acc);
    color: var(--on-acc); font-weight: 800; font-size: 13px; letter-spacing: -0.04em; line-height: 1;
  }
  .brand-text { line-height: 1.1; display: grid; }
  .brand-word { font-weight: 700; font-size: 0.95rem; letter-spacing: 0.04em; color: var(--tx); }
  .brand-caption { font-size: 0.62rem; color: var(--tx3); letter-spacing: 0.18em; text-transform: uppercase; }
  /* nav — grouped, scrollable, fills the column so Settings sits at the bottom */
  .sidebar nav {
    display: flex; flex-direction: column; gap: 2px;
    flex: 1; min-height: 0; overflow-y: auto;
    padding: 0.75rem 0.6rem;
  }
  .nav-group {
    font-size: 0.62rem; color: var(--tx3); letter-spacing: 0.16em;
    padding: 0.6rem 0.5rem 0.25rem;
  }
  .nav-group:first-child { padding-top: 0.25rem; }
  .nav-spacer { flex: 1; min-height: 0.5rem; }
  .nav-item {
    display: flex;
    gap: 0.7rem;
    align-items: center;
    width: 100%;
    text-align: left;
    background: none;
    border: none;
    color: var(--tx2);
    padding: 0.5rem 0.7rem;
    border-radius: 7px;
    cursor: pointer;
    font: 500 0.82rem/1 var(--font);
    transition: background 0.13s, color 0.13s, box-shadow 0.13s;
  }
  .nav-item :global(svg) { flex: none; }
  .nav-item:hover { background: var(--bg2); color: var(--tx); }
  .nav-item.active {
    background: var(--bg2);
    color: var(--tx);
    box-shadow: inset 2px 0 0 var(--acc);
  }
  /* xray-core footer box — top hairline, restyled by tokens */
  .xray-box {
    display: flex; align-items: center; gap: 0.55rem;
    padding: 0.7rem 0.85rem;
    border-top: 1px solid var(--bd);
    background: var(--bg1);
  }
  .xray-dot { width: 0.55rem; height: 0.55rem; border-radius: 50%; background: var(--tx3); flex: none; }
  .xray-dot.working { background: var(--acc); box-shadow: 0 0 8px var(--acc); animation: pulse-ring 2.4s ease-out infinite; }
  .xray-dot.error { background: var(--err); }
  .xray-label { font-size: 0.72rem; line-height: 1.2; margin-right: auto; font-weight: 500; }
  .xray-label small { color: var(--tx3); font-size: 0.62rem; font-weight: 400; text-transform: capitalize; }
  .content { min-width: 0; display: flex; flex-direction: column; }
  /* topbar — 54px, solid bg1, bottom hairline (no blur) */
  .topbar {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    height: 54px;
    padding: 0 1.25rem;
    border-bottom: 1px solid var(--bd);
    background: var(--bg1);
    position: sticky;
    top: 0;
    z-index: 5;
  }
  /* min-width:0 + ellipsis so a long title truncates instead of shoving the theme/logout/conn
     controls off the narrow (60px-rail) mobile topbar. */
  .topbar .page-title { margin-right: auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 0.95rem; font-weight: 700; letter-spacing: 0.01em; }
  .icon-btn { padding: 0.4rem; display: inline-grid; place-items: center; line-height: 0; border-radius: var(--radius-sm); }
  /* folded-in section divider (Subscriptions under Nodes) */
  .fold-head { border-top: 1px solid var(--bd); padding-top: 0.9rem; margin-top: 0.3rem; }
  /* online/offline status indicator */
  .conn { display: flex; align-items: center; gap: 0.4rem; padding-left: 0.25rem; }
  .conn-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--err); }
  .conn-label { font-size: 0.68rem; font-weight: 600; letter-spacing: 0.1em; color: var(--err); }
  .conn.up .conn-dot { background: var(--acc); box-shadow: 0 0 8px var(--acc); animation: v2pulse 2.4s infinite; }
  .conn.up .conn-label { color: var(--acc); }
  @media (prefers-reduced-motion: reduce) { .conn.up .conn-dot { animation: none; } }
  @media (max-width: 760px) {
    .shell { grid-template-columns: 60px 1fr; }
    .nav-item span, .brand-text, .xray-label, .nav-group, .conn-label { display: none; }
    /* 44px min touch target for the collapsed rail icons (WCAG target size) */
    .nav-item { justify-content: center; padding: 0.55rem; min-height: 44px; }
    .brand { justify-content: center; padding: 0.8rem 0; }
    .xray-box { flex-direction: column; gap: 0.35rem; padding: 0.5rem 0.3rem; }
    /* topbar controls: comfortable tap targets on touch */
    .icon-btn, .topbar .btn-ghost { min-height: 44px; min-width: 44px; }
  }
</style>
