<script lang="ts">
  import Setup from "./lib/Setup.svelte";
  import Login from "./lib/Login.svelte";
  import Dashboard from "./lib/Dashboard.svelte";
  import Nodes from "./lib/Nodes.svelte";
  import Subscriptions from "./lib/Subscriptions.svelte";
  import Tuning from "./lib/Tuning.svelte";
  import Routing from "./lib/Routing.svelte";
  import Settings from "./lib/Settings.svelte";
  import Toggle from "./lib/Toggle.svelte";
  import { api, type Status } from "./lib/api";
  import { BRAND } from "./lib/brand";
  import { applyTheme, toggleTheme, type Theme } from "./lib/theme";

  type View = "dashboard" | "nodes" | "subs" | "tuning" | "routing" | "settings";
  let authed = $state(false);
  let needsSetup = $state(false);
  let ready = $state(false);
  let view = $state<View>("dashboard");
  // seeded from the attribute the anti-FOUC/main bootstrap already resolved
  let theme = $state<Theme>((document.documentElement.dataset.theme as Theme) || "light");
  let status = $state<Status | null>(null);

  // On load: if first-run (no credential) show Setup; else probe the session for auth.
  $effect(() => {
    api.getSetup()
      .then(async (s) => {
        needsSetup = s.needs_setup;
        if (!needsSetup) {
          try { await api.getStatus(); authed = true; } catch { authed = false; }
        }
      })
      .catch(() => { authed = false; })
      .finally(() => { ready = true; });
  });

  // Poll xray-core status for the sidebar box.
  async function pollStatus() { try { status = await api.getStatus(); } catch { status = null; } }
  $effect(() => {
    if (!authed) return;
    pollStatus();
    const t = setInterval(pollStatus, 4000);
    return () => clearInterval(t);
  });
  async function toggleXray(on: boolean) {
    try { if (on) await api.xrayStart(); else await api.xrayStop(); } catch {}
    await pollStatus();
  }

  const tabs: { id: View; label: string }[] = [
    { id: "dashboard", label: "Dashboard" },
    { id: "nodes", label: "Nodes" },
    { id: "subs", label: "Subscriptions" },
    { id: "tuning", label: "Tuning" },
    { id: "routing", label: "Routing" },
    { id: "settings", label: "Settings" },
  ];

  const svg = (p: string) =>
    `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;
  const icons: Record<string, string> = {
    dashboard: svg('<rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/><rect x="14" y="12" width="7" height="9"/><rect x="3" y="16" width="7" height="5"/>'),
    nodes: svg('<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="M7 8l4 8M17 8l-4 8"/>'),
    subs: svg('<path d="M4 11a9 9 0 0 1 9 9M4 4a16 16 0 0 1 16 16"/><circle cx="5" cy="19" r="1.5"/>'),
    tuning: svg('<path d="M4 6h16M4 12h16M4 18h16"/><circle cx="9" cy="6" r="2"/><circle cx="15" cy="12" r="2"/><circle cx="8" cy="18" r="2"/>'),
    routing: svg('<path d="M5 19V7a2 2 0 0 1 2-2h7"/><path d="M11 2l3 3-3 3"/><circle cx="5" cy="19" r="2"/>'),
    network: svg('<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M3 9h18M9 21V9"/>'),
    settings: svg('<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2"/>'),
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
    view = "dashboard";
  }
</script>

{#if !ready}
  <p class="boot">…</p>
{:else if needsSetup}
  <Setup onDone={() => { needsSetup = false; authed = true; }} />
{:else if authed}
  <div class="shell">
    <aside class="sidebar">
      <div class="brand">{BRAND}</div>
      <nav>
        {#each tabs as t (t.id)}
          <button class="nav-item" class:active={view === t.id} onclick={() => (view = t.id)}>
            {@html icons[t.id]}<span>{t.label}</span>
          </button>
        {/each}
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
        <h1 class="page-title">{tabs.find((t) => t.id === view)?.label}</h1>
        <button class="btn-ghost icon-btn" onclick={toggleThemeNow} aria-label="Toggle theme" title="Toggle theme">
          {@html theme === "dark" ? icons.sun : icons.moon}
        </button>
        <button class="btn-ghost" onclick={logout}>Log out</button>
      </header>
      <main class="page">
        {#if view === "dashboard"}
          <Dashboard />
        {:else if view === "nodes"}
          <Nodes />
        {:else if view === "subs"}
          <Subscriptions />
        {:else if view === "tuning"}
          <Tuning />
        {:else if view === "routing"}
          <Routing />
        {:else}
          <Settings />
        {/if}
      </main>
    </div>
  </div>
{:else}
  <Login onLogin={() => (authed = true)} />
{/if}

<style>
  .boot { padding: 2rem; color: var(--muted); }
  .shell { display: grid; grid-template-columns: 220px 1fr; min-height: 100vh; }
  .sidebar {
    background: var(--surface-2);
    border-right: 1px solid var(--border);
    padding: 1rem 0.7rem;
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }
  .xray-box {
    margin-top: 0.5rem;
    display: flex; align-items: center; gap: 0.5rem;
    padding: 0.5rem 0.6rem;
    border: 1px solid var(--border); border-radius: var(--radius-sm);
    background: var(--surface);
  }
  .xray-dot { width: 0.6rem; height: 0.6rem; border-radius: 50%; background: var(--muted); flex: none; }
  .xray-dot.working { background: var(--success); }
  .xray-dot.error { background: var(--danger); }
  .xray-label { font-size: 0.78rem; line-height: 1.15; margin-right: auto; }
  .xray-label small { color: var(--muted); font-size: 0.68rem; }
  .brand { font-weight: 750; font-size: 1.1rem; padding: 0.4rem 0.6rem 1rem; color: var(--accent); }
  .sidebar nav { display: grid; gap: 0.2rem; }
  .nav-item {
    display: flex;
    gap: 0.6rem;
    align-items: center;
    width: 100%;
    text-align: left;
    background: none;
    border: none;
    color: var(--muted);
    padding: 0.5rem 0.6rem;
    border-radius: var(--radius-sm);
    cursor: pointer;
    font: inherit;
  }
  .nav-item:hover { background: var(--surface); color: var(--text); }
  .nav-item.active { background: var(--surface); color: var(--accent); font-weight: 600; }
  .content { min-width: 0; }
  .topbar {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.7rem 1.25rem;
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
    background: var(--bg);
    z-index: 5;
  }
  .topbar .page-title { margin-right: auto; }
  .icon-btn { padding: 0.35rem; display: inline-grid; place-items: center; line-height: 0; }
  @media (max-width: 720px) {
    .shell { grid-template-columns: 60px 1fr; }
    .nav-item span, .brand, .xray-label { display: none; }
    .nav-item { justify-content: center; }
    .xray-box { flex-direction: column; gap: 0.35rem; padding: 0.45rem 0.3rem; }
  }
</style>
