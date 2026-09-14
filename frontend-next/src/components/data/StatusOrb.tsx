import { cn } from "../../lib/cn";

export interface StatusOrbProps {
  /** Big figure: the live latency, "—" or "×". */
  value: string;
  /** Small line under it, e.g. "ms · ONLINE". */
  caption: string;
  /** ok wears the brand ring; neutral is grey; bad is rose. */
  tone: "ok" | "neutral" | "bad";
  className?: string;
}

/** The tunnel at a glance. Its ring changes with the state; nothing about it animates on its own. */
export function StatusOrb({ value, caption, tone, className }: StatusOrbProps) {
  return (
    <div role="img" aria-label={`${value} ${caption}`} data-tone={tone} className={cn("orb size-[94px] md:size-[120px]", className)}>
      <b className={cn("text-center text-[27px] font-bold leading-none tracking-tight md:text-[34px]", tone === "bad" && "text-bad", tone === "neutral" && "text-t2")}>
        {value}
        <small className="mt-1 block text-[10px] font-semibold tracking-wide text-t3">{caption}</small>
      </b>
    </div>
  );
}
