import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { Status } from "../../api/client";
import { useApiWrite } from "../../api/invalidation";
import { keys } from "../../api/keys";
import { Toggle } from "../../components/ui/Toggle";
import { notifyError } from "../../components/ui/Toaster";
import { cn } from "../../lib/cn";

export function XrayCard({ status, className }: { status?: Status; className?: string }) {
  const running = status?.xray_state === "working";
  const queryClient = useQueryClient();
  const start = useApiWrite("xrayStart");
  const stop = useApiWrite("xrayStop");
  const toggle = useMutation({
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
        <p className={cn("text-[11px]", running ? "text-ok" : "text-t3")}>{status?.xray_state ?? "—"}</p>
      </div>
      <div className="ml-auto">
        <Toggle label="xray-core" checked={running} disabled={!status || toggle.isPending} onCheckedChange={(on) => toggle.mutate(on)} />
      </div>
    </div>
  );
}
