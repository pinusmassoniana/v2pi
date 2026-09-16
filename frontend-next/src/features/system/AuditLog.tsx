import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import type { AuditEntry } from "../../api/client";
import { queries } from "../../api/keys";
import { CardHeader } from "../../components/data/CardHeader";
import { Chip } from "../../components/data/Chip";
import { Button } from "../../components/ui/Button";
import { GlassCard } from "../../components/ui/GlassCard";
import { ErrorState } from "../../components/ui/States";
import { cn } from "../../lib/cn";
import {
  AUDIT_DESCRIPTION, AUDIT_EMPTY, AUDIT_FOOTER, AUDIT_LOADING, AUDIT_NOT_LOADED, actorKind, auditKey, auditTime,
  maskedAuditPath, statusTone,
} from "./audit";

const STATUS_TONE: Record<string, string> = { neutral: "text-t2", warn: "text-warn", bad: "text-bad" };
// The brand violet is already a token (--g2 = rgb(124,92,255), the mockup's accent), and it already carries this
// exact "not a plain neutral" meaning elsewhere (RoutingCard's "proxy", EventFeed's "info") — never a new literal.
const ACTOR_TONE: Record<string, string> = { user: "text-g2", token: "text-t2", anon: "text-t3" };
const AUDIT_413_NOTE = "the 413 was refused by the body limit before the session was even checked, and still recorded";

/** One path, masked while it carries a live remote-access credential. The full value enters the DOM only on Reveal. */
export function AuditPath({ path, revealed, onToggle }: { path: string; revealed: boolean; onToggle: () => void }) {
  const masked = maskedAuditPath(path);
  if (!masked) return <span className="min-w-0 truncate font-mono text-[12px] text-t2">{path}</span>;
  return (
    <span className="flex min-w-0 items-center gap-2">
      <span className="min-w-0 truncate font-mono text-[12px] text-t2">
        {revealed ? masked.full : masked.masked}
        {revealed ? null : <span className="sr-only"> uuid hidden</span>}
      </span>
      <button
        type="button"
        aria-pressed={revealed}
        aria-label={`Reveal uuid in ${masked.masked}`}
        onClick={onToggle}
        className="shrink-0 text-[11px] font-semibold text-t3 underline hover:text-t1 focus-visible:outline-2 focus-visible:outline-g2"
      >
        {revealed ? "Hide" : "Reveal"}
      </button>
    </span>
  );
}

function AuditRow({ row, revealed, onToggle, phone }: { row: AuditEntry; revealed: boolean; onToggle: () => void; phone: boolean }) {
  const tone = STATUS_TONE[statusTone(row.status)];
  const actor = ACTOR_TONE[actorKind(row.actor)];
  if (phone) {
    return (
      <li className="rounded-xl border border-line bg-glass px-3 py-2">
        <div className="flex items-baseline justify-between gap-2">
          <span className="font-mono text-[11.5px] text-t3">{auditTime(row.ts)}</span>
          <span className={cn("min-w-0 flex-1 truncate text-[11.5px]", actor)}>{row.actor}</span>
          <span className={cn("shrink-0 font-mono text-[11.5px]", tone)}>→ {row.status}</span>
        </div>
        <div className="mt-0.5 flex min-w-0 items-center gap-2">
          <span className="shrink-0 text-[11.5px] font-bold text-t1">{row.method}</span>
          <AuditPath path={row.path} revealed={revealed} onToggle={onToggle} />
        </div>
      </li>
    );
  }
  return (
    <li className="grid grid-cols-[92px_158px_minmax(0,1fr)_72px] items-center gap-2 border-t border-line py-1.5">
      <span className="font-mono text-[11.5px] text-t3">{auditTime(row.ts)}</span>
      <span className={cn("truncate text-[11.5px]", actor)}>{row.actor}</span>
      <span className="flex min-w-0 items-center gap-2">
        <span className="shrink-0 text-[11.5px] font-bold text-t1">{row.method}</span>
        <AuditPath path={row.path} revealed={revealed} onToggle={onToggle} />
      </span>
      <span className={cn("text-right font-mono text-[11.5px]", tone)}>→ {row.status}</span>
    </li>
  );
}

/**
 * G8. Nothing is fetched until Show is pressed, nothing polls it and nothing invalidates it: a write must not yank a
 * list the operator is reading. `queries.audit()` has no `enabled: false` of its own, so the screen supplies it.
 */
export function AuditCard({ phone = false }: { phone?: boolean }) {
  const [shown, setShown] = useState(false);
  const [revealed, setRevealed] = useState<string | null>(null);
  const audit = useQuery({ ...queries.audit(), enabled: shown });
  // Every row re-masks when the screen goes: the full path is never left behind in a detached tree.
  useEffect(() => () => setRevealed(null), []);
  const rows = audit.data ?? [];
  return (
    <GlassCard aria-label="Audit log">
      <CardHeader
        title="Audit log"
        detail={phone ? undefined : AUDIT_DESCRIPTION}
        aside={
          <>
            <Chip plain>{shown ? "last 100" : "not loaded"}</Chip>
            <Button size="sm" onClick={() => { setShown(true); if (shown) void audit.refetch(); }}>
              {shown ? "Retry" : "Show"}
            </Button>
          </>
        }
      />
      {phone ? <p className="mb-2 text-[11px] leading-relaxed text-t3">{AUDIT_DESCRIPTION} Newest first.</p> : null}
      {/*
       * Mounted empty on every render, before Show is ever pressed — exactly as ResultLine mounts its own
       * role="status" empty before an editor's first result. A live region a screen reader first sees already
       * holding text is a region it never announces; one it sees empty, then watches gain text, it does.
       */}
      <p role="status" className={shown && audit.isPending ? "text-[11.5px] text-t3" : "sr-only"}>
        {shown && audit.isPending ? AUDIT_LOADING : ""}
      </p>
      {!shown ? (
        <p className="text-[11.5px] leading-relaxed text-t3">{AUDIT_NOT_LOADED}</p>
      ) : audit.isError ? (
        <ErrorState message={audit.error.message} onRetry={() => void audit.refetch()} />
      ) : audit.isPending ? null : rows.length === 0 ? (
        <p className="text-[11.5px] text-t3">{AUDIT_EMPTY}</p>
      ) : (
        <ul aria-label="Audit entries" className={cn(phone && "flex flex-col gap-1.5")}>
          {rows.map((row, index) => {
            const key = auditKey(row, index);
            return (
              <AuditRow
                key={key}
                row={row}
                phone={phone}
                revealed={revealed === key}
                onToggle={() => setRevealed((current) => (current === key ? null : key))}
              />
            );
          })}
        </ul>
      )}
      <p className="mt-3 text-[11px] leading-relaxed text-t3">{AUDIT_FOOTER}</p>
      {shown && rows.some((row) => row.status === 413) ? <p className="mt-1 text-[11px] text-t3">{AUDIT_413_NOTE}</p> : null}
    </GlassCard>
  );
}
