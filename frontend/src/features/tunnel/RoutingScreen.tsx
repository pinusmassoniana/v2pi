import { useQuery } from "@tanstack/react-query";
import { useCallback, useMemo, useState } from "react";
import { SLOW_POLL_MS } from "../../api/cadence";
import { useConnectionBusy } from "../../api/invalidation";
import { queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { useUnsavedGuard } from "../../app/guard";
import { notifyOk } from "../../components/ui/Toaster";
import { staleNotice } from "../../components/data/CardState";
import { CheckLine } from "../../components/data/CheckLine";
import { Button } from "../../components/ui/Button";
import { GlassCard } from "../../components/ui/GlassCard";
import { ErrorState, Skeleton } from "../../components/ui/States";
import { DESKTOP_QUERY, useMediaQuery } from "../../lib/media";
import { runKey } from "../../lib/staleResult";
import { DestinationTester } from "./DestinationTester";
import { ImportJsonSheet } from "./ImportJsonSheet";
import { RoutingDefaults } from "./RoutingDefaults";
import { RoutingMenu, RoutingToolbar } from "./RoutingToolbar";
import { RuleCards } from "./RuleCards";
import { RuleSheet } from "./RuleSheet";
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

/**
 * Tunnel › Routing (R1–R8): the polling owner of the ruleset while it is mounted. A table edited in place on a
 * desktop; on a phone, cards that open a rule's sheet, the ⋯ menu, a sticky staged banner and a sticky Validate / Save.
 */
export function Routing() {
  const routing = usePolledQuery(queries.routing(), SLOW_POLL_MS);
  // A4: read once — the pinned devices a `device` rule can name. Gateway › Network owns changes.
  const devices = useQuery(queries.reservations());
  const desktop = useMediaQuery(DESKTOP_QUERY);
  const editor = useRoutingEditor(routing.data);
  const actions = useRoutingActions(editor);
  const connectionBusy = useConnectionBusy();
  const [importing, setImporting] = useState(false);
  // The rule whose sheet is open on a phone, by row key.
  const [openKey, setOpenKey] = useState<string | null>(null);
  useUnsavedGuard(editor.staged);
  const { edit } = editor;
  const onUpdate = useCallback((key: string, patch: RulePatch) => edit((state) => updateRule(state, key, patch)), [edit]);
  const onMove = useCallback((key: string, delta: -1 | 1) => edit((state) => moveRule(state, key, delta)), [edit]);
  const onRemove = useCallback((key: string) => edit((state) => removeRule(state, key)), [edit]);
  const onAdd = useCallback(() => {
    const key = newRowKey();
    edit((state) => addRule(state, key));
    return key;
  }, [edit]);
  const onAddAndOpen = useCallback(() => setOpenKey(onAdd()), [onAdd]);
  // R8b: the tester stages a literal rule for what it just tested, at the TOP — a rule added
  // under the one that already decided this destination would change nothing, which is exactly
  // the confusion the button exists to end. Staged, never saved.
  const onAddTested = useCallback((type: "domain" | "ip", value: string, action: RuleAction) => {
    edit((state) => ({
      ...state,
      rows: [{ key: newRowKey(), id: null, type, value, action, enabled: true, label: "", dataset: "" as const }, ...state.rows],
    }));
    notifyOk(`${type} ${value} → ${action} staged at the top — review and Save`);
  }, [edit]);
  const onDefaultAction = useCallback((defaultAction: RuleAction) => edit((state) => ({ ...state, defaultAction })), [edit]);
  const onDomainStrategy = useCallback((domainStrategy: DomainStrategy) => edit((state) => ({ ...state, domainStrategy })), [edit]);

  const current = editor.current;
  const count = current?.rows.length ?? 0;
  // What Validate would check now: a result for anything else is marked as stale.
  const liveKey = useMemo(() => (current ? runKey(toRoutingIn(current).body) : ""), [current]);
  const openIndex = current && openKey !== null ? current.rows.findIndex((row) => row.key === openKey) : -1;
  const saveDisabled = !editor.staged || actions.saving || connectionBusy;
  // While a save runs, the ruleset it sends cannot be edited; while a preset loads, its reply would replace the edits.
  const rulesLocked = actions.saving || actions.presetBusy;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2 px-1">
        <div className="min-w-0 flex-1">
          <h2 className="text-[15px] font-bold text-t1">Ruleset</h2>
          <p className="text-xs text-t3">{current ? `${count} rule${count === 1 ? "" : "s"} · ` : ""}checked top to bottom, first match wins</p>
        </div>
        {current && desktop ? (
          <RoutingToolbar actions={actions} staged={editor.staged} connectionBusy={connectionBusy} onImportJson={() => setImporting(true)} />
        ) : null}
        {current && !desktop ? <RoutingMenu actions={actions} onImportJson={() => setImporting(true)} /> : null}
      </div>
      {/* Always mounted, so its live region exists before the first STAGED sentence arrives in it. */}
      <StagedBanner
        staged={editor.staged}
        changes={editor.changes}
        gatewayChanged={editor.gatewayChanged}
        onDiscard={actions.discard}
        className="max-md:sticky max-md:top-28 max-md:z-20"
      >
        <Button size="sm" variant="primary" disabled={actions.saving || connectionBusy} onClick={() => void actions.save()}>
          {actions.saving ? "Saving…" : "Apply staged"}
        </Button>
      </StagedBanner>
      {actions.saveError ? <p role="alert" className="glass whitespace-pre-wrap border-bad/40 p-3 text-sm text-bad">{actions.saveError}</p> : null}
      {desktop ? (
        // The result line stays mounted (empty, taking no space) so Validate's first answer lands in a live region.
        <div className={actions.check ? "flex min-w-0 items-center gap-2 px-1" : "contents"}>
          <CheckLine result={actions.check} liveKey={liveKey} />
          {actions.check ? <span className="shrink-0 text-[11.5px] text-t3">· Validate never saves</span> : null}
        </div>
      ) : null}
      {current ? staleNotice([routing], "Routing did not refresh") : null}
      {current ? (
        <>
          <fieldset disabled={rulesLocked} className="m-0 flex min-w-0 flex-col gap-3 border-0 p-0">
            {desktop ? (
              <GlassCard aria-label="Rules" className="overflow-hidden p-0">
                <RulesTable rows={current.rows} defaultAction={current.defaultAction} onAdd={onAdd} onUpdate={onUpdate} onMove={onMove} onRemove={onRemove} devices={devices.data?.reservations ?? []} />
              </GlassCard>
            ) : (
              <section aria-label="Rules">
                <RuleCards rows={current.rows} defaultAction={current.defaultAction} onOpen={setOpenKey} onUpdate={onUpdate} onAdd={onAddAndOpen} />
              </section>
            )}
            <div className="grid items-start gap-3 md:grid-cols-[minmax(0,4fr)_minmax(0,8fr)]">
              <RoutingDefaults
                defaultAction={current.defaultAction}
                domainStrategy={current.domainStrategy}
                onDefaultAction={onDefaultAction}
                onDomainStrategy={onDomainStrategy}
              />
              <DestinationTester
                state={current}
                devices={devices.data?.reservations ?? []}
                onAddRule={rulesLocked ? undefined : onAddTested}
              />
            </div>
          </fieldset>
          {!desktop ? (
            <div className="glass sticky bottom-24 z-20 flex items-center gap-2 bg-solid p-2.5">
              <div className="min-w-0 flex-1">
                <CheckLine result={actions.check} liveKey={liveKey} />
                {actions.check ? null : <p className="text-[11px] text-t3">Validate never saves</p>}
              </div>
              <Button size="sm" disabled={actions.validating} onClick={() => void actions.validate()}>{actions.validating ? "Validating…" : "Validate"}</Button>
              <Button size="sm" variant="primary" disabled={saveDisabled} onClick={() => void actions.save()}>{actions.saving ? "Saving…" : "Save"}</Button>
            </div>
          ) : null}
          {!desktop && openIndex >= 0 ? (
            <RuleSheet
              key={openKey}
              row={current.rows[openIndex]!}
              index={openIndex}
              count={count}
              disabled={rulesLocked}
              onUpdate={onUpdate}
              onMove={onMove}
              onRemove={onRemove}
              onClose={() => setOpenKey(null)}
            />
          ) : null}
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
