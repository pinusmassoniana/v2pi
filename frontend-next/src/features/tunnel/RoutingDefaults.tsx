import { useId } from "react";
import { CardHeader } from "../../components/data/CardHeader";
import { FieldShell } from "../../components/ui/Field";
import { GlassCard } from "../../components/ui/GlassCard";
import { Select } from "../../components/ui/Select";
import { cn } from "../../lib/cn";
import { ACTION_TEXT } from "./RuleParts";
import { DOMAIN_STRATEGIES, RULE_ACTIONS, type DomainStrategy, type RuleAction } from "./rules";

export interface RoutingDefaultsProps {
  defaultAction: RuleAction;
  domainStrategy: DomainStrategy;
  onDefaultAction: (action: RuleAction) => void;
  onDomainStrategy: (strategy: DomainStrategy) => void;
  className?: string;
}

/** R3: where everything no rule matched goes, and how domains are matched against IP rules. */
export function RoutingDefaults({ defaultAction, domainStrategy, onDefaultAction, onDomainStrategy, className }: RoutingDefaultsProps) {
  const id = useId();
  return (
    <GlassCard aria-label="Defaults" className={cn("flex flex-col gap-3", className)}>
      <CardHeader title="Defaults" className="mb-0" />
      <FieldShell id={`${id}-action`} label="Default action">
        <Select
          id={`${id}-action`}
          value={defaultAction}
          onChange={(event) => onDefaultAction(event.target.value as RuleAction)}
          className={cn("font-semibold", ACTION_TEXT[defaultAction])}
        >
          {RULE_ACTIONS.map((action) => <option key={action} value={action}>{action}</option>)}
        </Select>
        <p className="text-[11px] text-t3">direct · proxy · block — used by the catch-all row</p>
      </FieldShell>
      <FieldShell id={`${id}-strategy`} label="Domain strategy">
        <Select id={`${id}-strategy`} value={domainStrategy} onChange={(event) => onDomainStrategy(event.target.value as DomainStrategy)}>
          {DOMAIN_STRATEGIES.map((strategy) => <option key={strategy} value={strategy}>{strategy}</option>)}
        </Select>
        <p className="text-[11px] text-t3">IPIfNonMatch (default) · AsIs · IPOnDemand</p>
      </FieldShell>
      <p className="flex items-center gap-2 text-[11.5px] text-t3">
        <span aria-hidden className="grid size-5 shrink-0 place-items-center rounded-full bg-warn/15 text-[11px] font-bold text-warn">!</span>
        Saving with default = block asks to confirm first.
      </p>
    </GlassCard>
  );
}
