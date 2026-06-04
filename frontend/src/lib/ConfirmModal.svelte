<script lang="ts">
  import Modal from "./Modal.svelte";
  import { confirmState, resolveConfirm } from "./confirm.svelte";
</script>

{#if confirmState.req}
  <Modal title="Confirm" onClose={() => resolveConfirm(false)}>
    <p class="cmsg">{confirmState.req.message}</p>
    <div class="cactions">
      <button class="btn" onclick={() => resolveConfirm(false)}>Cancel</button>
      <button class="btn btn-danger" onclick={() => resolveConfirm(true)}>Confirm</button>
    </div>
  </Modal>
{/if}

<style>
  .cmsg { white-space: pre-wrap; margin: 0 0 1rem; line-height: 1.5; }
  .cactions { display: flex; gap: 0.5rem; justify-content: flex-end; }
</style>
