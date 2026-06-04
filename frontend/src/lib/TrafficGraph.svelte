<script lang="ts">
  // Rolling SVG line graph — up/down bit/s over the last N samples. No chart dep.
  let { samples = [], width = 480, height = 140 }:
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
  // close the line down to the baseline for a filled area
  function area(key: "up" | "down"): string {
    const d = path(key);
    return d ? `${d} L${width},${height} L0,${height} Z` : "";
  }

  function fmt(bps: number): string {
    if (bps >= 1e6) return (bps / 1e6).toFixed(1) + " Mbit/s";
    if (bps >= 1e3) return (bps / 1e3).toFixed(0) + " kbit/s";
    return bps.toFixed(0) + " bit/s";
  }
</script>

<div class="graph">
  <svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" aria-hidden="true">
    <defs>
      <linearGradient id="tg-down" x1="0" y1="0" x2="0" y2="1">
        <stop class="stop-down-0" offset="0%" /><stop class="stop-0" offset="100%" />
      </linearGradient>
      <linearGradient id="tg-up" x1="0" y1="0" x2="0" y2="1">
        <stop class="stop-up-0" offset="0%" /><stop class="stop-0" offset="100%" />
      </linearGradient>
    </defs>
    <path d={area("down")} fill="url(#tg-down)" stroke="none" />
    <path d={area("up")} fill="url(#tg-up)" stroke="none" />
    <path d={path("down")} class="down" fill="none" />
    <path d={path("up")} class="up" fill="none" />
  </svg>
  <div class="legend">
    <span class="down"><span class="lg-dot"></span> ↓ {fmt(latest.down)}</span>
    <span class="up"><span class="lg-dot"></span> ↑ {fmt(latest.up)}</span>
  </div>
</div>

<style>
  .graph { display: grid; gap: 0.5rem; }
  svg {
    width: 100%; height: 140px; display: block;
    background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius);
  }
  path { stroke-width: 2; vector-effect: non-scaling-stroke; }
  path.up { stroke: var(--success); }
  path.down { stroke: var(--accent); }
  .stop-down-0 { stop-color: var(--accent); stop-opacity: 0.22; }
  .stop-up-0 { stop-color: var(--success); stop-opacity: 0.22; }
  .stop-0 { stop-color: var(--accent); stop-opacity: 0; }
  .legend { display: flex; gap: 1.2rem; font-family: var(--mono); font-variant-numeric: tabular-nums; font-size: 0.8rem; }
  .legend .lg-dot { display: inline-block; width: 0.5rem; height: 0.5rem; border-radius: 50%; vertical-align: middle; margin-right: 0.1rem; }
  .legend .up { color: var(--success); }
  .legend .down { color: var(--accent); }
  .legend .up .lg-dot { background: var(--success); }
  .legend .down .lg-dot { background: var(--accent); }
</style>
