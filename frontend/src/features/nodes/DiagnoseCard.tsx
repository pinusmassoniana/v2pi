import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, type Diagnosis, type Node } from "../../api/client";
import { useApiWrite } from "../../api/invalidation";
import { CardHeader } from "../../components/data/CardHeader";
import { Chip } from "../../components/data/Chip";
import { KeyValueRows } from "../../components/data/KeyValueRows";
import { Button } from "../../components/ui/Button";
import { GlassCard } from "../../components/ui/GlassCard";
import { notifyError } from "../../components/ui/Toaster";
import { DIAGNOSE_HINT, DIAGNOSE_NOTE, RUNNING_ELSEWHERE, VERDICT_LABEL, VERDICT_TONE, phaseRows } from "./diagnose";

/**
 * B3: "it connects and then everything crawls" asked as a measurement. The health probe answers
 * "did a real request come back?", which is the right question for a watchdog and the wrong one
 * here — a throttled node passes it.
 *
 * The result lives in this component and nowhere else: a diagnosis is a point-in-time
 * measurement, and one from last week shown beside a node would be read as its state.
 */
export function DiagnoseCard({ node }: { node: Node }) {
  const [result, setResult] = useState<Diagnosis | null>(null);
  const diagnose = useApiWrite("diagnoseNode");
  const run = useMutation({
    mutationFn: () => diagnose(node.id),
    onSuccess: setResult,
    onError: (error) => {
      setResult(null);
      // 409: another diagnosis holds the gateway's one slot — two measuring at once would each
      // be measuring the other's traffic.
      if (error instanceof ApiError && error.status === 409) notifyError(null, RUNNING_ELSEWHERE);
      else notifyError(error, "diagnosis failed");
    },
  });
  return (
    <GlassCard aria-label="Diagnose">
      <CardHeader
        title="Diagnose"
        detail="on demand"
        aside={result ? <Chip tone={VERDICT_TONE[result.verdict]}>{VERDICT_LABEL[result.verdict]}</Chip> : null}
      />
      <p className="text-[11.5px] leading-relaxed text-t2">{DIAGNOSE_HINT}</p>
      {result ? (
        <>
          <p className="mt-2 text-[13px] leading-relaxed text-t1">{result.detail}</p>
          <KeyValueRows className="mt-2 sm:grid-cols-4" rows={phaseRows(result)} />
          {result.error ? <p className="mt-2 break-all font-mono text-[11px] text-t3">{result.error}</p> : null}
        </>
      ) : null}
      <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-line pt-3">
        <p className="min-w-0 flex-1 text-[11px] leading-relaxed text-t3">{result ? DIAGNOSE_NOTE : "Takes up to half a minute."}</p>
        <Button variant="primary" disabled={run.isPending} onClick={() => run.mutate()}>
          {run.isPending ? "Measuring…" : result ? "Run again" : "Diagnose"}
        </Button>
      </div>
    </GlassCard>
  );
}
