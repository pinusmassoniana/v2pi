import { ArrowDown, ArrowUp, Info, Lock, Plus, Trash2 } from "lucide-react";
import { memo, useId } from "react";
import { Button } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import { Select } from "../../components/ui/Select";
import { Toggle } from "../../components/ui/Toggle";
import { cn } from "../../lib/cn";
import { ACTION_TEXT, ActionPill, TypeChip } from "./RuleParts";
import {
  GEO_TOKENS, MAX_RULES, MAX_RULE_FIELD, NO_RULES, RULE_ACTIONS, RULE_TYPES, RULES_FOOTNOTE, VALUE_PLACEHOLDERS, validateRuleRow,
  type RuleAction, type RulePatch, type RuleRow, type RuleType,
} from "./rules";

/** What a row does to the staged ruleset, by row key. Stable callbacks, so memoised rows keep them. */
export interface RuleCallbacks {
  onUpdate: (key: string, patch: RulePatch) => void;
  onMove: (key: string, delta: -1 | 1) => void;
  onRemove: (key: string) => void;
}

/** The geo suggestions, once per list that offers them. */
export function GeoTokenList({ id }: { id: string }) {
  return (
    <datalist id={id}>
      {GEO_TOKENS.map((token) => <option key={token} value={token} />)}
    </datalist>
  );
}

export const isGeo = (type: RuleType) => type === "geoip" || type === "geosite";

interface RowProps extends RuleCallbacks {
  row: RuleRow;
  index: number;
  first: boolean;
  last: boolean;
  geoListId: string;
}

/** One editable rule; memoised, so typing in one row re-renders that row only. */
const RuleTableRow = memo(function RuleTableRow({ row, index, first, last, geoListId, onUpdate, onMove, onRemove }: RowProps) {
  const n = index + 1;
  const problem = validateRuleRow(row);
  const errorId = `${geoListId}-${row.key}-error`;
  return (
    <tr data-rule-key={row.key} data-enabled={row.enabled} className="border-t border-line align-top">
      <td className="py-3.5 pl-3 pr-1 text-center font-mono text-xs text-t3">{n}</td>
      <td className="px-1.5 py-3">
        <Toggle label={`Rule ${n} enabled`} checked={row.enabled} onCheckedChange={(enabled) => onUpdate(row.key, { enabled })} />
      </td>
      <td className={cn("px-1.5 py-2", !row.enabled && "opacity-60")}>
        <Select aria-label={`Rule ${n} type`} value={row.type} onChange={(event) => onUpdate(row.key, { type: event.target.value as RuleType })} className="h-9 w-full">
          {RULE_TYPES.map((type) => <option key={type} value={type}>{type}</option>)}
        </Select>
      </td>
      <td className={cn("px-1.5 py-2", !row.enabled && "opacity-60")}>
        <Input
          aria-label={`Rule ${n} value`}
          value={row.value}
          placeholder={VALUE_PLACEHOLDERS[row.type]}
          maxLength={MAX_RULE_FIELD}
          list={isGeo(row.type) ? geoListId : undefined}
          autoComplete="off"
          spellCheck={false}
          aria-invalid={problem && row.value.trim() ? true : undefined}
          aria-describedby={problem ? errorId : undefined}
          onChange={(event) => onUpdate(row.key, { value: event.target.value })}
          className={cn("h-9 font-mono text-xs", problem && row.value.trim() && "border-bad/60")}
        />
        {problem ? <p id={errorId} className={cn("mt-1 text-[11px]", row.value.trim() ? "text-bad" : "text-t3")}>{problem}</p> : null}
      </td>
      <td className={cn("px-1.5 py-2", !row.enabled && "opacity-60")}>
        <Select
          aria-label={`Rule ${n} action`}
          value={row.action}
          onChange={(event) => onUpdate(row.key, { action: event.target.value as RuleAction })}
          className={cn("h-9 w-full font-semibold", ACTION_TEXT[row.action])}
        >
          {RULE_ACTIONS.map((action) => <option key={action} value={action}>{action}</option>)}
        </Select>
      </td>
      <td className={cn("px-1.5 py-2", !row.enabled && "opacity-60")}>
        <Input
          aria-label={`Rule ${n} label`}
          value={row.label}
          placeholder="label"
          maxLength={MAX_RULE_FIELD}
          onChange={(event) => onUpdate(row.key, { label: event.target.value })}
          className="h-9 text-xs"
        />
      </td>
      <td className="py-2 pl-1.5 pr-3">
        <div className="flex justify-end gap-1">
          <Button size="icon" variant="ghost" className="size-8" aria-label={`Move rule ${n} up`} disabled={first} onClick={() => onMove(row.key, -1)}>
            <ArrowUp size={15} aria-hidden />
          </Button>
          <Button size="icon" variant="ghost" className="size-8" aria-label={`Move rule ${n} down`} disabled={last} onClick={() => onMove(row.key, 1)}>
            <ArrowDown size={15} aria-hidden />
          </Button>
          <Button size="icon" variant="ghost" className="size-8 text-bad hover:text-bad" aria-label={`Remove rule ${n}`} onClick={() => onRemove(row.key)}>
            <Trash2 size={15} aria-hidden />
          </Button>
        </div>
      </td>
    </tr>
  );
});

/** A fixed row the backend adds around the rules: announced as fixed, not editable. */
function AnchorRow({ which, type, value, action, note }: { which: "first" | "last"; type: string; value: string; action: RuleAction; note: string }) {
  return (
    <tr data-anchor={which} className="border-t border-line bg-glass">
      <td className="py-3 pl-3 pr-1 text-center text-t3"><Lock size={13} aria-hidden className="mx-auto" /></td>
      <td className="px-1.5 py-3">
        <span className="rounded-full bg-glass-2 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-t3">fixed</span>
      </td>
      <td className="px-1.5 py-3"><TypeChip>{type}</TypeChip></td>
      <td className="px-1.5 py-3 text-sm text-t2">{value}</td>
      <td className="px-1.5 py-3"><ActionPill action={action} /></td>
      <td colSpan={2} className="px-1.5 py-3 text-xs text-t3">{note}</td>
    </tr>
  );
}

export interface RulesTableProps extends RuleCallbacks {
  rows: readonly RuleRow[];
  defaultAction: RuleAction;
  onAdd: () => void;
}

/** R1–R2 on a desktop: the rules between the two fixed rows, each editable in place, then Add rule and the footnote. */
export function RulesTable({ rows, defaultAction, onAdd, onUpdate, onMove, onRemove }: RulesTableProps) {
  const geoListId = useId();
  return (
    <div>
      <GeoTokenList id={geoListId} />
      <div className="overflow-x-auto">
        <table aria-label="Routing rules" className="w-full min-w-[720px] text-left text-sm">
          <colgroup>
            <col className="w-10" />
            <col className="w-14" />
            <col className="w-28" />
            <col />
            <col className="w-28" />
            <col className="w-36" />
            <col className="w-28" />
          </colgroup>
          <thead className="text-[10px] uppercase tracking-[.07em] text-t3">
            <tr>
              <th scope="col" className="py-2.5 pl-3 pr-1 text-center font-semibold">#</th>
              <th scope="col" className="px-1.5 font-semibold">On</th>
              <th scope="col" className="px-1.5 font-semibold">Type</th>
              <th scope="col" className="px-1.5 font-semibold">Value</th>
              <th scope="col" className="px-1.5 font-semibold">Action</th>
              <th scope="col" className="px-1.5 font-semibold">Label</th>
              <th scope="col" className="pl-1.5 pr-3 text-right font-semibold">Order</th>
            </tr>
          </thead>
          <tbody>
            <AnchorRow which="first" type="ip" value="private ranges" action="direct" note="implicit, always first" />
            {rows.length === 0 ? (
              <tr className="border-t border-line">
                <td colSpan={7} className="px-3 py-4 text-center text-sm text-t3">{NO_RULES}</td>
              </tr>
            ) : null}
            {rows.map((row, index) => (
              <RuleTableRow
                key={row.key}
                row={row}
                index={index}
                first={index === 0}
                last={index === rows.length - 1}
                geoListId={geoListId}
                onUpdate={onUpdate}
                onMove={onMove}
                onRemove={onRemove}
              />
            ))}
            <tr className="border-t border-line">
              <td colSpan={7} className="px-3 py-2.5">
                <div className="flex items-center gap-3">
                  <Button size="sm" variant="ghost" className="border border-dashed border-line" disabled={rows.length >= MAX_RULES} onClick={onAdd}>
                    <Plus size={14} aria-hidden />Add rule
                  </Button>
                  <span className="text-[11px] text-t3">{rows.length} / {MAX_RULES} rules</span>
                </div>
              </td>
            </tr>
            <AnchorRow which="last" type="all" value="everything else" action={defaultAction} note="default catch-all, always last" />
          </tbody>
        </table>
      </div>
      <p className="flex items-center gap-2 border-t border-line px-3 py-2.5 text-xs text-t3">
        <Info size={14} aria-hidden className="shrink-0" />{RULES_FOOTNOTE}
      </p>
    </div>
  );
}
