import { useMutation } from "@tanstack/react-query";
import { memo, useId, useState } from "react";
import type { Reservation, RouteTest } from "../../api/client";
import { useApiWrite } from "../../api/invalidation";
import { CardHeader } from "../../components/data/CardHeader";
import { Button } from "../../components/ui/Button";
import { GlassCard } from "../../components/ui/GlassCard";
import { Input } from "../../components/ui/Input";
import { Select } from "../../components/ui/Select";
import { notifyError } from "../../components/ui/Toaster";
import { cn } from "../../lib/cn";
import { ACTION_TEXT } from "./RuleParts";
import {
  LIVE_DISAGREES, LIVE_SUBTITLE, TESTER_PLACEHOLDER, TESTER_SUBTITLE, addableRule, liveOutcome,
  testDestination, type RuleAction, type StagedRouting,
} from "./rules";

export interface DestinationTesterProps {
  state: StagedRouting;
  /** A4: pinned devices, so a `device` rule can be tried without a client to test from. */
  devices?: readonly Reservation[];
  /** R8b: stage a rule for what was just tested; absent while the ruleset is locked. */
  onAddRule?: (type: "domain" | "ip", value: string, action: RuleAction) => void;
  className?: string;
}

/**
 * R8: where a host, IP or IP:port would go. Two answers, deliberately:
 *
 *  * the staged one, worked out here as you type — it covers literal rules and what is on screen
 *    but not yet saved;
 *  * the live one, from the running xray — the only thing that can evaluate geoip/geosite (the
 *    data is tens of megabytes on the gateway) and IPv6, but it knows only what is saved.
 *
 * When they disagree, both are shown: that difference IS the answer to "why is this not working
 * yet" — the rule is staged, not applied.
 */
export const DestinationTester = memo(function DestinationTester({ state, devices = [], onAddRule, className }: DestinationTesterProps) {
  const id = useId();
  const [input, setInput] = useState("");
  const [network, setNetwork] = useState<"tcp" | "udp">("tcp");
  const [sourceIp, setSourceIp] = useState("");
  const [live, setLive] = useState<RouteTest | null>(null);
  const testRoute = useApiWrite("testRoute");
  const result = testDestination(input, state);

  const ask = useMutation({ mutationFn: () => testRoute(input.trim(), network, sourceIp) });

  // A live answer is about the destination it was asked for. Its callbacks ride the ask itself, which lands only for
  // the latest ask while this input is unchanged: every change resets it, so an answer still on its way is dropped
  // instead of shown — or offered as a rule — for the new destination. A stale answer is worse than none.
  function run() {
    ask.mutate(undefined, { onSuccess: setLive, onError: (error) => notifyError(error, "the live test failed") });
  }
  function forget() {
    setLive(null);
    ask.reset();
  }
  function update(value: string) {
    setInput(value);
    forget();
  }

  const addable = live?.ok ? addableRule(live) : null;
  return (
    <GlassCard aria-label="Destination tester" className={cn("flex flex-col gap-2.5", className)}>
      <CardHeader title="Destination tester" detail={LIVE_SUBTITLE} className="mb-0" />
      <label htmlFor={id} className="text-xs text-t3">{TESTER_SUBTITLE}</label>
      <div className="flex flex-wrap items-center gap-2">
        <Input
          id={id}
          value={input}
          placeholder={TESTER_PLACEHOLDER}
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => update(event.target.value)}
          className="min-w-0 flex-1 font-mono text-xs md:max-w-80"
        />
        <Select aria-label="Network" value={network} onChange={(event) => { setNetwork(event.target.value as "tcp" | "udp"); forget(); }} className="h-9 w-20">
          <option value="tcp">tcp</option>
          <option value="udp">udp</option>
        </Select>
        {devices.length ? (
          <Select aria-label="As device" value={sourceIp} onChange={(event) => { setSourceIp(event.target.value); forget(); }} className="h-9 w-40">
            <option value="">any device</option>
            {devices.map((device) => (
              <option key={device.ip} value={device.ip}>{device.name || device.ip}</option>
            ))}
          </Select>
        ) : null}
        <Button size="sm" disabled={!input.trim() || ask.isPending} onClick={run}>
          {ask.isPending ? "Asking…" : "Test live"}
        </Button>
      </div>

      <p role="status" className="min-h-5 text-sm">
        {result?.action ? (
          <>
            <b className={cn("font-bold", ACTION_TEXT[result.action])}>→ {result.action}</b> <span className="text-t2">{result.detail}</span>
          </>
        ) : result ? (
          <span className="text-t3">{result.detail}</span>
        ) : null}
      </p>

      {live ? (
        <p role="status" className="text-sm">
          {live.ok ? (
            <>
              <b className={cn("font-bold", ACTION_TEXT[live.outbound as RuleAction] ?? "text-t1")}>→ {live.outbound}</b>{" "}
              <span className="text-t2">{liveOutcome(live)}</span>
            </>
          ) : (
            <span className="text-warn">{live.error}</span>
          )}
        </p>
      ) : null}
      {live?.ok && result?.action && live.outbound !== result.action ? (
        <p className="text-[11px] text-warn">{LIVE_DISAGREES}</p>
      ) : null}

      {addable && onAddRule ? (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[11px] text-t3">Add a rule for {addable.value}:</span>
          {(["direct", "proxy", "block"] as const).map((action) => (
            <Button key={action} size="sm" variant="ghost" className={ACTION_TEXT[action]}
              onClick={() => onAddRule(addable.type, addable.value, action)}>
              {action}
            </Button>
          ))}
        </div>
      ) : null}
    </GlassCard>
  );
});
