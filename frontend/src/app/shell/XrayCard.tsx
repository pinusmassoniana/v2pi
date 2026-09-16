import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { Status } from "../../api/client";
import { CONNECTION_WRITE, useApiWrite, useConnectionBusy } from "../../api/invalidation";
import { keys } from "../../api/keys";
import type { Tone } from "../../components/data/types";
import { Toggle } from "../../components/ui/Toggle";
import { notifyError } from "../../components/ui/Toaster";
import { xrayLabel } from "../../features/home/derive";
import { cn } from "../../lib/cn";

const TEXT: Record<Tone, string> = { ok: "text-ok", warn: "text-warn", bad: "text-bad", neutral: "text-t3" };

export function XrayCard({ status, className }: { status?: Status; className?: string }) {
  // The same reading as Home's xray chip: running wins, then a supervisor error, then stopped.
  const xray = xrayLabel(status);
  const running = xray.label === "RUNNING";
  const queryClient = useQueryClient();
  const start = useApiWrite("xrayStart");
  const stop = useApiWrite("xrayStop");
  const busy = useConnectionBusy();
  const toggle = useMutation({
    mutationKey: CONNECTION_WRITE,
    mutationFn: async (on: boolean) => {
      await (on ? start() : stop());
      // The write succeeded: show the new state now instead of flicking back to the old one until the
      // status refetch (already started by the write's invalidation) lands — and stay busy until it has.
      queryClient.setQueryData<Status>(keys.status, (old) => (old ? { ...old, running: on, xray_state: on ? "working" : "stopped" } : old));
      await queryClient.getQueryCache().find({ queryKey: keys.status, exact: true })?.promise?.catch(() => undefined);
    },
    onError: (error) => notifyError(error, "xray-core toggle failed"),
  });
  return (
    <div className={cn("glass flex items-center gap-3 rounded-2xl p-3", className)}>
      <div className="min-w-0">
        <p className="text-xs font-semibold text-t1">xray-core</p>
        <p className={cn("text-[11px] font-semibold tracking-wide", TEXT[xray.tone])}>{xray.label}</p>
      </div>
      <div className="ml-auto">
        <Toggle label="xray-core" checked={running} disabled={!status || busy} onCheckedChange={(on) => toggle.mutate(on)} />
      </div>
    </div>
  );
}
