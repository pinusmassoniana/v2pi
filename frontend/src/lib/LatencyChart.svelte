<script lang="ts">
  // NF5: compact latency-over-time area chart from the active node's recent real-probe latencies
  // (lat_history). Dims when the last probe failed (U4). Pure SVG, dep-free.
  let { values = [], stale = false }: { values?: number[]; stale?: boolean } = $props();

  const H = 56, padX = 4, padT = 6, padB = 4;
  let g = $derived.by(() => {
    const v = (values ?? []).filter((x) => x != null && !Number.isNaN(x));
    if (v.length < 2) return null;
    const max = Math.max(...v), min = Math.min(...v), span = max - min || 1;
    const W = 100;   // viewBox units; SVG scales to container width (preserveAspectRatio none)
    const step = (W - padX * 2) / (v.length - 1);
    const y = (x: number) => padT + (H - padT - padB) * (1 - (x - min) / span);
    const pts = v.map((x, i) => [padX + i * step, y(x)] as const);
    const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
    const area = `${line} L${pts[pts.length - 1][0].toFixed(1)},${H - padB} L${pts[0][0].toFixed(1)},${H - padB} Z`;
    return { line, area, min, max, cur: v[v.length - 1] };
  });
</script>

<div class="card lat-card" class:stale>
  <div class="lat-head">
    <span class="eyebrow">Latency · recent</span>
    {#if g}
      <span class="lat-stats mono">
        <span class="cur">{Math.round(g.cur)} ms</span>
        <small>min {Math.round(g.min)} · max {Math.round(g.max)}</small>
      </span>
    {/if}
  </div>
  {#if g}
    <svg class="lat-svg" viewBox="0 0 100 {H}" preserveAspectRatio="none" aria-hidden="true">
      <path class="area" d={g.area} />
      <path class="line" d={g.line} />
    </svg>
  {:else}
    <div class="lat-empty">no probe history yet</div>
  {/if}
</div>

<style>
  .lat-card { display: grid; gap: 0.45rem; transition: opacity 0.2s; }
  .lat-card.stale { opacity: 0.5; }
  .lat-head { display: flex; align-items: baseline; justify-content: space-between; gap: 0.6rem; }
  .lat-stats { font-variant-numeric: tabular-nums; font-size: 0.8rem; color: var(--accent); }
  .lat-stats small { color: var(--faint); margin-left: 0.4rem; font-size: 0.68rem; }
  .lat-svg { width: 100%; height: 56px; display: block; }
  .lat-svg .area { fill: color-mix(in srgb, var(--accent) 14%, transparent); stroke: none; }
  .lat-svg .line { fill: none; stroke: var(--accent); stroke-width: 1; vector-effect: non-scaling-stroke; stroke-linejoin: round; }
  .lat-empty { height: 56px; display: grid; place-items: center; color: var(--faint); font-size: 0.78rem; }
</style>
