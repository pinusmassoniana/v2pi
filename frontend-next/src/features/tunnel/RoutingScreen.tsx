import { useCallback, useMemo, useState } from "react";
import { SLOW_POLL_MS } from "../../api/cadence";
import { useConnectionBusy } from "../../api/invalidation";
import { queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { useUnsavedGuard } from "../../app/guard";
import { staleNotice } from "../../components/data/CardState";
import { CheckLine } from "../../components/data/CheckLine";
import { Button } from "../../components/ui/Button";
import { GlassCard } from "../../components/ui/GlassCard";
import { ErrorState, Skeleton } from "../../components/ui/States";
import { runKey } from "../../lib/staleResult";
import { DestinationTester } from "./DestinationTester";
import { ImportJsonSheet } from "./ImportJsonSheet";
import { RoutingDefaults } from "./RoutingDefaults";
import { RoutingToolbar } from "./RoutingToolbar";
import { RulesTable } from "./RulesTable";
import { addRule, moveRule, newRowKey, removeRule, toRoutingIn, updateRule, type DomainStrategy, type RuleAction, type RulePatch } from "./rules";
import { StagedBanner } from "./StagedBanner";
import { useRoutingActions } from "./useRoutingActions";
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
  const actions = useRoutingActions(editor);
  const connectionBusy = useConnectionBusy();
  const [importing, setImporting] = useState(false);
  useUnsavedGuard(editor.staged);
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
  // What Validate would check now: a result for anything else is marked as stale.
  const liveKey = useMemo(() => (current ? runKey(toRoutingIn(current).body) : ""), [current]);
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2 px-1">
        <div className="min-w-0 flex-1">
          <h2 className="text-[15px] font-bold text-t1">Ruleset</h2>
          <p className="text-xs text-t3">{current ? `${count} rule${count === 1 ? "" : "s"} · ` : ""}checked top to bottom, first match wins</p>
        </div>
        {current ? <RoutingToolbar actions={actions} staged={editor.staged} connectionBusy={connectionBusy} onImportJson={() => setImporting(true)} /> : null}
      </div>
      {editor.staged ? (
        <StagedBanner changes={editor.changes} gatewayChanged={editor.gatewayChanged} onDiscard={actions.discard}>
          <Button size="sm" variant="primary" disabled={actions.saving || connectionBusy} onClick={() => void actions.save()}>
            {actions.saving ? "Saving…" : "Apply staged"}
          </Button>
        </StagedBanner>
      ) : null}
      {actions.saveError ? <p role="alert" className="glass whitespace-pre-wrap border-bad/40 p-3 text-sm text-bad">{actions.saveError}</p> : null}
      {actions.check ? (
        <div className="flex min-w-0 items-center gap-2 px-1">
          <CheckLine result={actions.check} liveKey={liveKey} />
          <span className="shrink-0 text-[11.5px] text-t3">· Validate never saves</span>
        </div>
      ) : null}
      {current ? staleNotice([routing], "Routing did not refresh") : null}
      {current ? (
        <>
          {/* While a save runs, the ruleset it sends cannot be edited. */}
          <fieldset disabled={actions.saving} className="m-0 flex min-w-0 flex-col gap-3 border-0 p-0">
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
              <DestinationTester state={current} />
            </div>
          </fieldset>
          {importing ? <ImportJsonSheet current={current} onImport={actions.importRules} onClose={() => setImporting(false)} /> : null}
        </>
      ) : routing.isError ? (
        <ErrorState message="Routing did not load" onRetry={() => void routing.refetch()} />
      ) : (
        <RulesSkeleton />
      )}
    </div>
  );
}
