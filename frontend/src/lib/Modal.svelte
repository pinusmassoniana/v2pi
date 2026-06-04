<script lang="ts">
  import type { Snippet } from "svelte";
  let { title = "", onClose, children }:
    { title?: string; onClose: () => void; children: Snippet } = $props();

  let dialogEl = $state<HTMLElement>();
  const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),'
    + 'select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

  // a11y: move focus into the dialog on open, restore it on close.
  $effect(() => {
    const prev = document.activeElement as HTMLElement | null;
    queueMicrotask(() => {
      const first = dialogEl?.querySelector<HTMLElement>(FOCUSABLE);
      (first ?? dialogEl)?.focus();
    });
    return () => prev?.focus?.();
  });

  function onKeydown(e: KeyboardEvent) {
    if (e.key === "Escape") { onClose(); return; }
    if (e.key !== "Tab" || !dialogEl) return;
    const f = [...dialogEl.querySelectorAll<HTMLElement>(FOCUSABLE)]
      .filter((el) => el.offsetParent !== null);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
</script>

<svelte:window onkeydown={onKeydown} />
<div class="backdrop" role="presentation"
     onclick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
  <div class="modal" role="dialog" aria-modal="true" aria-label={title} tabindex="-1" bind:this={dialogEl}>
    <div class="modal-head">
      <h3>{title}</h3>
      <button class="btn-ghost icon-btn close" onclick={onClose} aria-label="Close">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
      </button>
    </div>
    <div class="modal-body">{@render children()}</div>
  </div>
</div>

<style>
  .backdrop {
    position: fixed; inset: 0; background: rgba(8, 12, 20, 0.5);
    -webkit-backdrop-filter: blur(3px); backdrop-filter: blur(3px);
    display: grid; place-items: center; z-index: 50; padding: 1rem;
    animation: modal-fade 0.14s ease-out;
  }
  .modal {
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg);
    width: min(42rem, 100%); max-height: 90vh; overflow: auto;
    box-shadow: var(--shadow-lg);
    animation: modal-pop 0.16s cubic-bezier(0.16, 1, 0.3, 1);
  }
  .modal-head {
    display: flex; align-items: center; gap: 0.6rem;
    padding: 0.85rem 1.1rem; border-bottom: 1px solid var(--border);
    position: sticky; top: 0; z-index: 1;
    background: color-mix(in srgb, var(--surface) 88%, transparent);
    -webkit-backdrop-filter: blur(8px); backdrop-filter: blur(8px);
  }
  .modal-head h3 { margin: 0; margin-right: auto; font-size: 0.95rem; font-weight: 650; letter-spacing: -0.01em; }
  .close { color: var(--muted); }
  .close:hover { color: var(--text); }
  .modal-body { padding: 1.1rem; }
  @keyframes modal-fade { from { opacity: 0; } to { opacity: 1; } }
  @keyframes modal-pop { from { opacity: 0; transform: translateY(8px) scale(0.985); } to { opacity: 1; transform: none; } }
  @media (prefers-reduced-motion: reduce) { .backdrop, .modal { animation: none; } }
</style>
