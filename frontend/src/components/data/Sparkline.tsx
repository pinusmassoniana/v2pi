import { cn } from "../../lib/cn";
import { sparkPath } from "../../lib/dashboard";
import { useSvgId } from "./svgId";

export interface SparklineProps {
  values: readonly number[];
  /** "down" draws the brand gradient; "up" the upload series colour. */
  series?: "down" | "up";
  width?: number;
  height?: number;
  className?: string;
}

/** A tiny trend line. Decorative: the number beside it carries the meaning. */
export function Sparkline({ values, series = "down", width = 200, height = 36, className }: SparklineProps) {
  const id = useSvgId("spark");
  const d = sparkPath([...values], width, height);
  return (
    <svg aria-hidden viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" className={cn("block h-9 w-full overflow-visible", className)}>
      <defs>
        {/* userSpaceOnUse: a flat line has a zero-height bounding box, which would hide a bounding-box gradient */}
        <linearGradient id={id} gradientUnits="userSpaceOnUse" x1="0" y1="0" x2={width} y2="0">
          <stop offset="0" style={{ stopColor: "var(--g1)" }} />
          <stop offset="1" style={{ stopColor: "var(--g2)" }} />
        </linearGradient>
      </defs>
      {d ? (
        <path
          d={d}
          fill="none"
          strokeWidth={2}
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
          style={{ stroke: series === "up" ? "var(--series-up)" : `url(#${id})` }}
        />
      ) : null}
    </svg>
  );
}
