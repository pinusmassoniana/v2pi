<script lang="ts">
  // Rolling SVG line graph — up/down bit/s over the last N samples. No chart dep.
  let { samples = [], width = 480, height = 120 }:
    { samples?: { up: number; down: number }[]; width?: number; height?: number } = $props();

  const max = $derived(Math.max(1, ...samples.flatMap((s) => [s.up, s.down])));
  const latest = $derived(samples.length ? samples[samples.length - 1] : { up: 0, down: 0 });

  function path(key: "up" | "down"): string {
    if (samples.length < 2) return "";
    const n = samples.length;
    return samples
      .map((s, i) => {
        const x = (i / (n - 1)) * width;
        const y = height - (s[key] / max) * height;
        return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
  }

  function fmt(bps: number): string {
    if (bps >= 1e6) return (bps / 1e6).toFixed(1) + " Mbit/s";
    if (bps >= 1e3) return (bps / 1e3).toFixed(0) + " kbit/s";
    return bps.toFixed(0) + " bit/s";
  }
</script>

<div class="graph">
  <svg viewBox="0 0 {width} {height}" preserveAspectRatio="none">
    <path d={path("down")} class="down" fill="none" />
    <path d={path("up")} class="up" fill="none" />
  </svg>
  <div class="legend">
    <span class="up">↑ {fmt(latest.up)}</span>
    <span class="down">↓ {fmt(latest.down)}</span>
  </div>
</div>

<style>
  .graph { display: grid; gap: 0.35rem; }
  svg { width: 100%; height: 120px; background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius-sm); }
  path { stroke-width: 2; vector-effect: non-scaling-stroke; }
  path.up { stroke: var(--success); }
  path.down { stroke: var(--accent); }
  .legend { display: flex; gap: 1rem; font-variant-numeric: tabular-nums; }
  .legend .up { color: var(--success); }
  .legend .down { color: var(--accent); }
</style>
