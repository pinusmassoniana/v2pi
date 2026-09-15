import { memo, useId, useState } from "react";
import { CardHeader } from "../../components/data/CardHeader";
import { GlassCard } from "../../components/ui/GlassCard";
import { Input } from "../../components/ui/Input";
import { cn } from "../../lib/cn";
import { ACTION_TEXT } from "./RuleParts";
import { TESTER_PLACEHOLDER, TESTER_SUBTITLE, testDestination, type StagedRouting } from "./rules";

/** R8: where a host, IP or IP:port would go under the staged rules, worked out here without asking the gateway. */
export const DestinationTester = memo(function DestinationTester({ state, className }: { state: StagedRouting; className?: string }) {
  const id = useId();
  const [input, setInput] = useState("");
  const result = testDestination(input, state);
  return (
    <GlassCard aria-label="Destination tester" className={cn("flex flex-col gap-2.5", className)}>
      <CardHeader title="Destination tester" detail="client-side · staged rules" className="mb-0" />
      <label htmlFor={id} className="text-xs text-t3">{TESTER_SUBTITLE}</label>
      <Input
        id={id}
        value={input}
        placeholder={TESTER_PLACEHOLDER}
        autoComplete="off"
        spellCheck={false}
        onChange={(event) => setInput(event.target.value)}
        className="font-mono text-xs md:max-w-80"
      />
      <p role="status" className="min-h-5 text-sm">
        {result?.action ? (
          <>
            <b className={cn("font-bold", ACTION_TEXT[result.action])}>→ {result.action}</b> <span className="text-t2">{result.detail}</span>
          </>
        ) : result ? (
          <span className="text-t3">{result.detail}</span>
        ) : null}
      </p>
    </GlassCard>
  );
});
