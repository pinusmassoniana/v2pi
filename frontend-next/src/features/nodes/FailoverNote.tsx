import { useQuery } from "@tanstack/react-query";
import type { Status } from "../../api/client";
import { serverNow } from "../../api/clock";
import { queries } from "../../api/keys";
import { cn } from "../../lib/cn";
import { agoLabel } from "../../lib/dashboard";

/** N20: "Auto-failover armed | off · last switch ‹ago›", while failover is on or has ever switched. */
export function FailoverNote({ status, className }: { status: Status | undefined; className?: string }) {
  const settings = useQuery(queries.settings());   // read once; nothing on this screen changes it
  if (!settings.data) return null;
  const armed = settings.data.failover_enabled;
  const last = status?.last_failover_at ?? null;
  if (!armed && !last) return null;
  return (
    <p className={cn("text-xs text-t2", className)}>
      <span aria-hidden className="text-t3">⇄ </span>
      Auto-failover <b className={armed ? "font-semibold text-ok" : "font-semibold text-t3"}>{armed ? "armed" : "off"}</b>
      {last ? ` · last switch ${agoLabel(last, serverNow() / 1000)}` : ""}
    </p>
  );
}
