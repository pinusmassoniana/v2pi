import { useMutation } from "@tanstack/react-query";
import type { Status } from "../../api/client";
import { useApiWrite } from "../../api/invalidation";
import { Toggle } from "../../components/ui/Toggle";
import { notifyError } from "../../components/ui/Toaster";
import { cn } from "../../lib/cn";

export function XrayCard({ status, className }: { status?: Status; className?: string }) {
  const running = status?.xray_state === "working";
  const start = useApiWrite("xrayStart");
  const stop = useApiWrite("xrayStop");
  const toggle = useMutation({
    mutationFn: (on: boolean) => (on ? start() : stop()),
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
