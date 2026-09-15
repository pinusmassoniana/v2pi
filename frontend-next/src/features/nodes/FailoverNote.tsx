import { useQuery } from "@tanstack/react-query";
import { queries } from "../../api/keys";
import { useNow } from "../../components/data/Ago";
import { cn } from "../../lib/cn";
import { agoLabel } from "../../lib/dashboard";

/** N20: "Auto-failover armed | off · last switch ‹ago›", while failover is on or has ever switched. */
export function FailoverNote({ lastFailoverAt, className }: { lastFailoverAt: number | null; className?: string }) {
  const settings = useQuery(queries.settings());   // read once; nothing on this screen changes it
  const now = useNow();
  if (!settings.data) return null;
  const armed = settings.data.failover_enabled;
  if (!armed && !lastFailoverAt) return null;
  return (
    <p className={cn("text-xs text-t2", className)}>
      <span aria-hidden className="text-t3">⇄ </span>
      Auto-failover <b className={armed ? "font-semibold text-ok" : "font-semibold text-t3"}>{armed ? "armed" : "off"}</b>
      {lastFailoverAt ? ` · last switch ${agoLabel(lastFailoverAt, now / 1000)}` : ""}
    </p>
  );
}
