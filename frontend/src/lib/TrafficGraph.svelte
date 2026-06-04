<script lang="ts">
  // Rich rolling throughput chart over a selectable window (1m/10m/1h). Renders at the
  // measured pixel width so axis text stays crisp (no SVG stretching). Zero chart deps.
  type Pt = { ts: number; up: number; down: number };
  let { series = [] }: { series?: Pt[] } = $props();

  const WINDOWS = [
    { label: "1m", sec: 60 },
    { label: "10m", sec: 600 },
    { label: "1h", sec: 3600 },
  ];
  let windowSec = $state(600);
  let w = $state(640);
  let hover = $state<{ x: number; up: number; down: number; ts: number } | null>(null);

  const H = 184, padL = 50, padR = 14, padT = 12, padB = 22;

  const clamp01 = (v: number) => (v < 0 ? 0 : v > 1 ? 1 : v);
  function fmt(bps: number): string {
    if (bps >= 1e6) return (bps / 1e6).toFixed(1) + " Mbit/s";
    if (bps >= 1e3) return (bps / 1e3).toFixed(0) + " kbit/s";
    return Math.round(bps) + " bit/s";
  }
  function niceMax(v: number): number {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    const f = v / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
  }
  function downsample(arr: Pt[], n: number): Pt[] {
    if (arr.length <= n) return arr;
    const step = arr.length / n, out: Pt[] = [];
    for (let i = 0; i < n; i++) out.push(arr[Math.floor(i * step)]);
    out[out.length - 1] = arr[arr.length - 1];
    return out;
  }
  function relLabel(secAgo: number): string {
    const s = Math.round(secAgo);
    if (s <= 0) return "now";
    if (windowSec <= 120) return `-${s}s`;
    const m = Math.round(s / 60);
    return m >= 60 ? `-${Math.round(m / 60)}h` : `-${m}m`;
  }
  function agoLabel(secAgo: number): string {
    const s = Math.max(0, Math.round(secAgo));
    if (s < 1) return "now";
    if (s < 60) return `${s}s ago`;
    const m = Math.floor(s / 60), r = s % 60;
    return r && m < 10 ? `${m}m ${r}s ago` : `${m}m ago`;
  }
  function absLabel(ts: number): string {
    return new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  const geom = $derived.by(() => {
    const plotW = Math.max(10, w - padL - padR), plotH = H - padT - padB;
    const tEnd = series.length ? series[series.length - 1].ts : Date.now();
    const tStart = tEnd - windowSec * 1000;
    const view = series.filter((s) => s.ts >= tStart);
    const rendered = downsample(view, 700);
    let peak = 1;   // single pass instead of a per-frame flatMap + spread
    for (const s of view) { if (s.up > peak) peak = s.up; if (s.down > peak) peak = s.down; }
    const vmax = niceMax(peak);
    const X = (ts: number) => padL + clamp01((ts - tStart) / (windowSec * 1000)) * plotW;
    const Y = (v: number) => padT + plotH - (v / vmax) * plotH;
    const line = (key: "up" | "down") =>
      rendered.length < 2 ? "" :
      rendered.map((s, i) => `${i ? "L" : "M"}${X(s.ts).toFixed(1)},${Y(s[key]).toFixed(1)}`).join(" ");
    const area = (key: "up" | "down") => {
      const l = line(key);
      if (!l) return "";
      const x0 = X(rendered[0].ts).toFixed(1), x1 = X(rendered[rendered.length - 1].ts).toFixed(1);
      const yb = (padT + plotH).toFixed(1);
      return `${l} L${x1},${yb} L${x0},${yb} Z`;
    };
    const yticks = [0, 0.25, 0.5, 0.75, 1].map((f) => ({ v: vmax * f, y: Y(vmax * f) }));
    const xticks = [0, 0.25, 0.5, 0.75, 1].map((f) => {
      const t = tStart + f * windowSec * 1000;
      return { x: X(t), label: relLabel((tEnd - t) / 1000) };
    });
    return { plotW, plotH, tEnd, tStart, view, rendered, X, Y,
             lineUp: line("up"), lineDown: line("down"), areaUp: area("up"), areaDown: area("down"),
             yticks, xticks };
  });

  const stats = $derived.by(() => {
    const v = geom.view;
    if (!v.length) return null;
    const cur = v[v.length - 1];
    let pu = 0, pd = 0;
    for (const s of v) { if (s.up > pu) pu = s.up; if (s.down > pd) pd = s.down; }
    return { curUp: cur.up, curDown: cur.down, peakUp: pu, peakDown: pd };
  });

  function onMove(e: PointerEvent) {
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const px = e.clientX - rect.left;
    const v = geom.view;
    if (!v.length || px < padL || px > w - padR) { hover = null; return; }
    const ts = geom.tStart + clamp01((px - padL) / geom.plotW) * windowSec * 1000;
    let best = v[0], bd = Infinity;
    for (const s of v) { const d = Math.abs(s.ts - ts); if (d < bd) { bd = d; best = s; } }
    hover = { x: geom.X(best.ts), up: best.up, down: best.down, ts: best.ts };
  }
</script>

<div class="tg">
  <div class="tg-head">
    <div class="win" role="tablist">
      {#each WINDOWS as wd (wd.sec)}
        <button class="win-btn" class:on={windowSec === wd.sec} onclick={() => (windowSec = wd.sec)}>{wd.label}</button>
      {/each}
    </div>
    {#if stats}
      <div class="readout">
        <span class="ro down"><span class="ro-dot"></span>↓ {fmt(stats.curDown)}<small>peak {fmt(stats.peakDown)}</small></span>
        <span class="ro up"><span class="ro-dot"></span>↑ {fmt(stats.curUp)}<small>peak {fmt(stats.peakUp)}</small></span>
      </div>
    {/if}
  </div>

  <div class="plot" bind:clientWidth={w} role="img" aria-label="throughput over time"
       onpointermove={onMove} onpointerleave={() => (hover = null)}>
    {#if geom.rendered.length < 2}
      <div class="tg-empty">waiting for traffic…</div>
    {:else}
      <svg width={w} height={H} aria-hidden="true">
        <defs>
          <linearGradient id="tg-down" x1="0" y1="0" x2="0" y2="1">
            <stop class="s-down" offset="0%" /><stop class="s-zero" offset="100%" />
          </linearGradient>
          <linearGradient id="tg-up" x1="0" y1="0" x2="0" y2="1">
            <stop class="s-up" offset="0%" /><stop class="s-zero" offset="100%" />
          </linearGradient>
        </defs>
        {#each geom.yticks as t}
          <line class="grid" x1={padL} x2={w - padR} y1={t.y} y2={t.y} />
          <text class="ylab" x={padL - 7} y={t.y}>{fmt(t.v)}</text>
        {/each}
        {#each geom.xticks as t}
          <text class="xlab" x={t.x} y={H - 6}>{t.label}</text>
        {/each}
        <path d={geom.areaDown} fill="url(#tg-down)" stroke="none" />
        <path d={geom.areaUp} fill="url(#tg-up)" stroke="none" />
        <path d={geom.lineDown} class="ln down" fill="none" />
        <path d={geom.lineUp} class="ln up" fill="none" />
        {#if hover}
          <line class="cross" x1={hover.x} x2={hover.x} y1={padT} y2={padT + geom.plotH} />
          <circle class="hd down" cx={hover.x} cy={geom.Y(hover.down)} r="3.2" />
          <circle class="hd up" cx={hover.x} cy={geom.Y(hover.up)} r="3.2" />
        {/if}
      </svg>
      {#if hover}
        <div class="tip" style="left:{Math.min(Math.max(hover.x, padL), w - padR)}px">
          <div class="tip-t">{agoLabel((geom.tEnd - hover.ts) / 1000)} · {absLabel(hover.ts)}</div>
          <div class="tip-r down">↓ {fmt(hover.down)}</div>
          <div class="tip-r up">↑ {fmt(hover.up)}</div>
        </div>
      {/if}
    {/if}
  </div>
</div>

<style>
  .tg { display: grid; gap: 0.55rem; }
  .tg-head { display: flex; align-items: center; justify-content: space-between; gap: 0.6rem; flex-wrap: wrap; }
  .win { display: inline-flex; gap: 2px; background: var(--surface-2); border: 1px solid var(--border); border-radius: 999px; padding: 2px; }
  .win-btn {
    border: none; background: none; color: var(--muted); cursor: pointer; font: inherit;
    font-size: 0.74rem; font-weight: 600; padding: 0.18rem 0.7rem; border-radius: 999px; transition: background 0.13s, color 0.13s;
  }
  .win-btn:hover { color: var(--text); }
  .win-btn.on { background: var(--surface); color: var(--accent); box-shadow: var(--shadow-sm); }
  .readout { display: flex; gap: 1rem; font-family: var(--mono); font-variant-numeric: tabular-nums; font-size: 0.78rem; }
  .ro { display: inline-flex; align-items: center; gap: 0.3rem; }
  .ro small { color: var(--faint); margin-left: 0.35rem; font-size: 0.68rem; }
  .ro-dot { width: 0.5rem; height: 0.5rem; border-radius: 50%; }
  .ro.down { color: var(--accent); } .ro.down .ro-dot { background: var(--accent); }
  .ro.up { color: var(--success); } .ro.up .ro-dot { background: var(--success); }

  .plot { position: relative; width: 100%; background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
  svg { display: block; touch-action: none; }
  .grid { stroke: var(--border); stroke-width: 1; }
  .ylab { fill: var(--faint); font-size: 0.6rem; font-family: var(--mono); text-anchor: end; dominant-baseline: middle; }
  .xlab { fill: var(--faint); font-size: 0.62rem; font-family: var(--mono); text-anchor: middle; }
  .ln { stroke-width: 2; }
  .ln.up { stroke: var(--success); }
  .ln.down { stroke: var(--accent); }
  .s-down { stop-color: var(--accent); stop-opacity: 0.2; }
  .s-up { stop-color: var(--success); stop-opacity: 0.2; }
  .s-zero { stop-color: var(--accent); stop-opacity: 0; }
  .cross { stroke: var(--border-strong); stroke-width: 1; stroke-dasharray: 3 3; }
  .hd.up { fill: var(--success); } .hd.down { fill: var(--accent); }
  .tg-empty { height: 184px; display: grid; place-items: center; color: var(--faint); font-size: 0.82rem; }
  .tip {
    position: absolute; top: 8px; transform: translateX(-50%); pointer-events: none;
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-sm);
    box-shadow: var(--shadow); padding: 0.35rem 0.5rem; font-family: var(--mono);
    font-variant-numeric: tabular-nums; font-size: 0.72rem; white-space: nowrap; z-index: 2;
  }
  .tip-t { color: var(--faint); font-size: 0.64rem; margin-bottom: 0.1rem; }
  .tip-r.down { color: var(--accent); }
  .tip-r.up { color: var(--success); }
</style>
