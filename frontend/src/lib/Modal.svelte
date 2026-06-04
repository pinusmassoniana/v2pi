<script lang="ts">
  import type { Snippet } from "svelte";
  let { title = "", onClose, children }:
    { title?: string; onClose: () => void; children: Snippet } = $props();
</script>

<svelte:window onkeydown={(e) => e.key === "Escape" && onClose()} />
<div class="backdrop" role="presentation"
     onclick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
  <div class="modal" role="dialog" aria-modal="true">
    <div class="modal-head">
      <h3>{title}</h3>
      <button class="btn-ghost icon-btn" onclick={onClose} aria-label="Close">✕</button>
    </div>
    <div class="modal-body">{@render children()}</div>
  </div>
</div>

<style>
  .backdrop {
    position: fixed; inset: 0; background: rgba(0, 0, 0, 0.45);
    display: grid; place-items: center; z-index: 50; padding: 1rem;
  }
  .modal {
    background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius-sm);
    width: min(42rem, 100%); max-height: 90vh; overflow: auto;
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.35);
  }
  .modal-head {
    display: flex; align-items: center; gap: 0.6rem;
    padding: 0.8rem 1rem; border-bottom: 1px solid var(--border);
    position: sticky; top: 0; background: var(--bg);
  }
  .modal-head h3 { margin: 0; margin-right: auto; }
  .modal-body { padding: 1rem; }
</style>
