import { ChevronRight, Lock, Plus } from "lucide-react";
import { memo } from "react";
import { Button } from "../../components/ui/Button";
import { Toggle } from "../../components/ui/Toggle";
import { cn } from "../../lib/cn";
import { ActionPill, TypeChip } from "./RuleParts";
import { MAX_RULES, NO_RULES, validateRuleRow, type RuleAction, type RulePatch, type RuleRow } from "./rules";

interface CardProps {
  row: RuleRow;
  index: number;
  onOpen: (key: string) => void;
  onUpdate: (key: string, patch: RulePatch) => void;
}

/** One rule on a phone: a button over the card opens its sheet; the switch on top of it takes its own taps. Memoised. */
const RuleCard = memo(function RuleCard({ row, index, onOpen, onUpdate }: CardProps) {
  const n = index + 1;
  const problem = row.value.trim() ? validateRuleRow(row) : null;
  return (
    <li data-rule-key={row.key} data-enabled={row.enabled} className="glass relative flex items-center gap-3 p-3">
      <button
        type="button"
        aria-label={`Edit rule ${n}`}
        onClick={() => onOpen(row.key)}
        className="absolute inset-0 rounded-[18px] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-g2"
      />
      <span className="pointer-events-none relative w-5 shrink-0 text-center font-mono text-xs text-t3">{n}</span>
      <div className={cn("pointer-events-none relative min-w-0 flex-1", !row.enabled && "opacity-60")}>
        <p className="flex min-w-0 items-center gap-2">
          <TypeChip>{row.type}</TypeChip>
          <span className={cn("truncate font-mono text-[13px]", row.value ? "text-t1" : "text-t3")}>{row.value || "no value"}</span>
        </p>
        <p className="mt-1.5 flex min-w-0 items-center gap-2 text-[11.5px]">
          <ActionPill action={row.action} />
          <span className={cn("truncate", row.label ? "text-t2" : "text-t3")}>{row.label || "no label"}</span>
        </p>
        {problem ? <p className="mt-1 text-[11px] text-bad">{problem}</p> : null}
      </div>
      <div className="relative">
        <Toggle label={`Rule ${n} enabled`} checked={row.enabled} onCheckedChange={(enabled) => onUpdate(row.key, { enabled })} />
      </div>
      <ChevronRight size={16} aria-hidden className="pointer-events-none relative shrink-0 text-t3" />
    </li>
  );
});

function AnchorCard({ which, type, value, action, note }: { which: "first" | "last"; type: string; value: string; action: RuleAction; note: string }) {
  return (
    <li data-anchor={which} className="flex items-center gap-3 rounded-[18px] border border-dashed border-line px-3 py-2.5">
      <Lock size={13} aria-hidden className="w-5 shrink-0 text-t3" />
      <div className="min-w-0 flex-1">
        <p className="flex items-center gap-2 text-[13px] text-t2">
          <span className="rounded-full bg-glass-2 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-t3">fixed</span>
          <TypeChip>{type}</TypeChip>{value}
        </p>
        <p className="mt-1.5 flex items-center gap-2 text-[11.5px] text-t3"><ActionPill action={action} />{note}</p>
      </div>
    </li>
  );
}

export interface RuleCardsProps {
  rows: readonly RuleRow[];
  defaultAction: RuleAction;
  onOpen: (key: string) => void;
  onUpdate: (key: string, patch: RulePatch) => void;
  onAdd: () => void;
}

/** R1–R2 on a phone: the rules as cards between the two fixed ones; editing happens in a sheet. */
export function RuleCards({ rows, defaultAction, onOpen, onUpdate, onAdd }: RuleCardsProps) {
  return (
    <div className="flex flex-col gap-2">
      <ul aria-label="Routing rules" className="flex flex-col gap-2">
        <AnchorCard which="first" type="ip" value="private ranges" action="direct" note="implicit, always first" />
        {rows.length === 0 ? <li className="px-3 py-3 text-center text-sm text-t3">{NO_RULES}</li> : null}
        {rows.map((row, index) => <RuleCard key={row.key} row={row} index={index} onOpen={onOpen} onUpdate={onUpdate} />)}
        <AnchorCard which="last" type="all" value="everything else" action={defaultAction} note="default catch-all, always last" />
      </ul>
      <div className="flex items-center gap-3">
        <Button size="sm" variant="ghost" className="border border-dashed border-line" disabled={rows.length >= MAX_RULES} onClick={onAdd}>
          <Plus size={14} aria-hidden />Add rule
        </Button>
        <span className="text-[11px] text-t3">{rows.length} / {MAX_RULES} rules</span>
      </div>
    </div>
  );
}
