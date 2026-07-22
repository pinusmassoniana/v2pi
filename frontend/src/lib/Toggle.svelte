<script lang="ts">
  // iOS-style switch — the shared replacement for checkboxes across the app.
  let { checked = false, onchange, disabled = false, label = "" }:
    { checked?: boolean; onchange?: (v: boolean) => void; disabled?: boolean; label?: string } = $props();
</script>

<button type="button" class="toggle" class:on={checked} {disabled}
        role="switch" aria-checked={checked} aria-label={label}
        onclick={() => onchange?.(!checked)}>
  <span class="knob"></span>
</button>

<style>
  .toggle {
    --w: 2.4rem; --h: 1.35rem;
    width: var(--w); height: var(--h); border-radius: 999px;
    border: none; padding: 0; cursor: pointer; position: relative; flex: none;
    background: var(--border-strong); transition: background 0.15s ease;
  }
  .toggle.on { background: var(--accent); }
  .toggle:disabled { opacity: 0.5; cursor: not-allowed; }
  .knob {
    position: absolute; top: 2px; left: 2px;
    width: calc(var(--h) - 4px); height: calc(var(--h) - 4px);
    border-radius: 50%; background: #fff;
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.3);
    transition: transform 0.15s ease;
  }
  .toggle.on .knob { transform: translateX(calc(var(--w) - var(--h))); }
</style>
