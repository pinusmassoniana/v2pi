import { ArrowDown, ArrowUp, Trash2 } from "lucide-react";
import { Button } from "../../components/ui/Button";
import { SegmentedField, TextField } from "../../components/ui/Field";
import { Sheet, SheetContent } from "../../components/ui/Sheet";
import { Toggle } from "../../components/ui/Toggle";
import { cn } from "../../lib/cn";
import { ACTION_CHECKED } from "./RuleParts";
import { isGeo, type RuleCallbacks } from "./RulesTable";
import {
  DATASET_LABELS, DEVICE_ORDER_NOTE, GEO_TOKENS, GEO_TOKENS_RU, MAX_RULE_FIELD, RULE_ACTIONS, RULE_DATASETS,
  RULE_TYPES, VALUE_PLACEHOLDERS,
  ruleTokens, validateRuleRow, type RuleAction, type RuleDataset, type RuleRow, type RuleType,
} from "./rules";

const ACTION_OPTIONS = RULE_ACTIONS.map((action) => ({ value: action, className: ACTION_CHECKED[action] }));

export interface RuleSheetProps extends RuleCallbacks {
  row: RuleRow;
  index: number;
  count: number;
  /** The rules are locked (a save or a preset is running): the fields and rule actions are off, Done still closes. */
  disabled?: boolean;
  onClose: () => void;
}

/**
 * R1–R2 on a phone: one rule in a bottom sheet. Every change goes straight into the staged ruleset — the page's
 * staged banner and navigation guard cover it — so closing the sheet never asks.
 */
export function RuleSheet({ row, index, count, disabled = false, onUpdate, onMove, onRemove, onClose }: RuleSheetProps) {
  const n = index + 1;
  const problem = validateRuleRow(row);
  const tokens = ruleTokens(row.value);

  function toggleToken(token: string) {
    const next = tokens.includes(token) ? tokens.filter((value) => value !== token) : [...tokens, token];
    onUpdate(row.key, { value: next.join(", ") });
  }

  return (
    <Sheet open onOpenChange={(open) => { if (!open) onClose(); }}>
      {/* Focus the sheet, not its first field: that is the first Type radio, which is usually not the rule's type. */}
      <SheetContent title={`Rule ${n} of ${count}`} initialFocus="overlay" className="max-md:max-h-[92dvh]">
        <div className="flex flex-col gap-3.5">
          {/* The page's own lock does not reach a sheet: it is portalled out of the page's fieldset. */}
          <fieldset disabled={disabled} className="m-0 flex min-w-0 flex-col gap-3.5 border-0 p-0">
            <SegmentedField
              legend="Type"
              name={`rule-${row.key}-type`}
              options={RULE_TYPES}
              value={row.type}
              onValueChange={(type) => onUpdate(row.key, { type: type as RuleType })}
            />
            {isGeo(row.type) ? (
              <SegmentedField
                legend="Geo data"
                name={`rule-${row.key}-dataset`}
                options={RULE_DATASETS.map((dataset) => ({ value: dataset, label: DATASET_LABELS[dataset] }))}
                value={row.dataset}
                onValueChange={(dataset) => onUpdate(row.key, { dataset: dataset as RuleDataset })}
              />
            ) : null}
            <TextField
              label="Value"
              hint={row.value.trim() ? undefined : "required"}
              error={row.value.trim() ? (problem ?? undefined) : undefined}
              value={row.value}
              placeholder={VALUE_PLACEHOLDERS[row.type]}
              maxLength={MAX_RULE_FIELD}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => onUpdate(row.key, { value: event.target.value })}
              className="font-mono text-xs"
            />
            {row.type === "device" ? <p className="-mt-1.5 text-[11px] text-t3">{DEVICE_ORDER_NOTE}</p> : null}
            {isGeo(row.type) ? (
              <div role="group" aria-label="Geo suggestions" className="-mt-1.5 flex flex-wrap gap-1.5">
                {(row.dataset === "ru" ? GEO_TOKENS_RU : GEO_TOKENS).map((token) => {
                  const on = tokens.includes(token);
                  return (
                    <button
                      key={token}
                      type="button"
                      aria-pressed={on}
                      onClick={() => toggleToken(token)}
                      className={cn(
                        "min-h-8 rounded-full border px-2.5 font-mono text-[11.5px] transition-colors duration-150",
                        on ? "border-g2/50 bg-g2/15 text-t1" : "border-line bg-glass-2 text-t2",
                      )}
                    >
                      {token}
                    </button>
                  );
                })}
              </div>
            ) : null}
            <SegmentedField
              legend="Action"
              name={`rule-${row.key}-action`}
              options={ACTION_OPTIONS}
              value={row.action}
              onValueChange={(action) => onUpdate(row.key, { action: action as RuleAction })}
            />
            <TextField label="Label" value={row.label} placeholder="label" maxLength={MAX_RULE_FIELD} onChange={(event) => onUpdate(row.key, { label: event.target.value })} />
            <div className="flex items-center justify-between gap-3 rounded-xl border border-line bg-glass px-3 py-2">
              <p className="text-sm font-semibold text-t1">Enabled</p>
              <Toggle label="Enabled" checked={row.enabled} onCheckedChange={(enabled) => onUpdate(row.key, { enabled })} />
            </div>
          </fieldset>
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" disabled={disabled || index === 0} onClick={() => onMove(row.key, -1)}><ArrowUp size={14} aria-hidden />Move up</Button>
            <Button size="sm" disabled={disabled || index === count - 1} onClick={() => onMove(row.key, 1)}><ArrowDown size={14} aria-hidden />Move down</Button>
            <Button
              size="sm"
              variant="danger"
              disabled={disabled}
              onClick={() => {
                onRemove(row.key);
                onClose();
              }}
            >
              <Trash2 size={14} aria-hidden />Delete
            </Button>
            <Button size="sm" variant="primary" className="ml-auto" onClick={onClose}>Done</Button>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}
