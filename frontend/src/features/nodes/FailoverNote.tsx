import { useNow } from "../../components/data/Ago";
import { cn } from "../../lib/cn";
import { agoLabel } from "../../lib/dashboard";

export interface FailoverNoteProps {
  /** The polled Status.failover_enabled; undefined until status first answers. */
  armed: boolean | undefined;
  lastFailoverAt: number | null;
  className?: string;
}

/** N20: "Auto-failover armed | off · last switch ‹ago›", while failover is on or has ever switched. */
export function FailoverNote({ armed, lastFailoverAt, className }: FailoverNoteProps) {
  const now = useNow();
  if (armed === undefined || (!armed && !lastFailoverAt)) return null;
  return (
    <p className={cn("text-xs text-t2", className)}>
      <span aria-hidden className="text-t3">⇄ </span>
      Auto-failover <b className={armed ? "font-semibold text-ok" : "font-semibold text-t3"}>{armed ? "armed" : "off"}</b>
      {lastFailoverAt ? ` · last switch ${agoLabel(lastFailoverAt, now / 1000)}` : ""}
    </p>
  );
}
