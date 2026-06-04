<script lang="ts">
  import type { Snippet } from "svelte";
  // Shared status line — announced to screen readers (polite for ok, assertive for errors).
  let { msg = "", kind = "ok", children }:
    { msg?: string; kind?: "ok" | "err"; children?: Snippet } = $props();
</script>

{#if msg || children}
  <p class="msg" class:err={kind === "err"} role={kind === "err" ? "alert" : "status"}
     aria-live={kind === "err" ? "assertive" : "polite"}>{msg}{#if children}{@render children()}{/if}</p>
{/if}
