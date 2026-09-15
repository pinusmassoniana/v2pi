import { useCallback } from "react";
import { SLOW_POLL_MS } from "../../api/cadence";
import { queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { staleNotice } from "../../components/data/CardState";
import { GlassCard } from "../../components/ui/GlassCard";
import { ErrorState, Skeleton } from "../../components/ui/States";
import { RoutingDefaults } from "./RoutingDefaults";
import { RulesTable } from "./RulesTable";
import { addRule, moveRule, newRowKey, removeRule, updateRule, type DomainStrategy, type RuleAction, type RulePatch } from "./rules";
import { StagedBanner } from "./StagedBanner";
import { useRoutingEditor } from "./useRoutingEditor";

function RulesSkeleton() {
  return (
    <GlassCard aria-label="Rules" aria-busy className="flex flex-col gap-2">
      {[0, 1, 2, 3, 4].map((row) => <Skeleton key={row} className="h-10" />)}
    </GlassCard>
  );
}

/** Tunnel › Routing (R1–R8): the polling owner of the ruleset while it is mounted. */
export function Routing() {
  const routing = usePolledQuery(queries.routing(), SLOW_POLL_MS);
  const editor = useRoutingEditor(routing.data);
  const { edit } = editor;
  const onUpdate = useCallback((key: string, patch: RulePatch) => edit((state) => updateRule(state, key, patch)), [edit]);
  const onMove = useCallback((key: string, delta: -1 | 1) => edit((state) => moveRule(state, key, delta)), [edit]);
  const onRemove = useCallback((key: string) => edit((state) => removeRule(state, key)), [edit]);
  const onAdd = useCallback(() => {
    const key = newRowKey();
    edit((state) => addRule(state, key));
  }, [edit]);
  const onDefaultAction = useCallback((defaultAction: RuleAction) => edit((state) => ({ ...state, defaultAction })), [edit]);
  const onDomainStrategy = useCallback((domainStrategy: DomainStrategy) => edit((state) => ({ ...state, domainStrategy })), [edit]);

  const current = editor.current;
  const count = current?.rows.length ?? 0;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2 px-1">
        <div className="min-w-0 flex-1">
          <h2 className="text-[15px] font-bold text-t1">Ruleset</h2>
          <p className="text-xs text-t3">{current ? `${count} rule${count === 1 ? "" : "s"} · ` : ""}checked top to bottom, first match wins</p>
        </div>
      </div>
      {editor.staged ? <StagedBanner changes={editor.changes} gatewayChanged={editor.gatewayChanged} onDiscard={editor.discard} /> : null}
      {current ? staleNotice([routing], "Routing did not refresh") : null}
      {current ? (
        <>
          <GlassCard aria-label="Rules" className="overflow-hidden p-0">
            <RulesTable rows={current.rows} defaultAction={current.defaultAction} onAdd={onAdd} onUpdate={onUpdate} onMove={onMove} onRemove={onRemove} />
          </GlassCard>
          <div className="grid items-start gap-3 md:grid-cols-[minmax(0,4fr)_minmax(0,8fr)]">
            <RoutingDefaults
              defaultAction={current.defaultAction}
              domainStrategy={current.domainStrategy}
              onDefaultAction={onDefaultAction}
              onDomainStrategy={onDomainStrategy}
            />
          </div>
        </>
      ) : routing.isError ? (
        <ErrorState message="Routing did not load" onRetry={() => void routing.refetch()} />
      ) : (
        <RulesSkeleton />
      )}
    </div>
  );
}
