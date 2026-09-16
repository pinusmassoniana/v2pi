import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useForm, useWatch } from "react-hook-form";
import { ApiError, isNoAnswer, type Settings } from "../../api/client";
import { SLOW_POLL_MS } from "../../api/cadence";
import {
  PASSWORD_WRITE, SETTINGS_BUSY, TOKEN_BUSY, isSettingsBusy, isTokenBusy, settingsWriteKey, useApiWrite,
  useConnectionBusy, usePasswordBusy, useSettingsBusy,
} from "../../api/invalidation";
import { keys, queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { useUnsavedGuard } from "../../app/guard";
import { CardHeader } from "../../components/data/CardHeader";
import { Chip } from "../../components/data/Chip";
import { confirm } from "../../components/confirm";
import { Button } from "../../components/ui/Button";
import { TextField } from "../../components/ui/Field";
import { GlassCard } from "../../components/ui/GlassCard";
import { notifyError, notifyOk, notifyWarn } from "../../components/ui/Toaster";
import { cn } from "../../lib/cn";
import { NO_ANSWER } from "../gateway/networkForm";
import {
  MISMATCH, TIMEOUT_MESSAGE, WRONG_CURRENT, idleTimeoutMessage, parseIdleTimeout, passwordChangedMessage, passwordConfirm,
  passwordFormSchema, passwordNote, strength, type PasswordFormValues,
} from "./passwordForm";
import { ResultLine } from "./ResultLine";

const EMPTY: PasswordFormValues = { current: "", next: "", confirm: "" };
const STRENGTH_BARS: Record<string, number> = { weak: 1, ok: 2, good: 3, strong: 4 };
const SESSION_NOTE =
  "Signs you out after this long with no activity. An open, visible tab keeps polling, so this measures time with the " +
  "panel closed or in the background.";
const SESSION_HINT =
  "Changing it ends nobody's session now — it only decides when a future idle period bites. 0 turns it off; any whole " +
  "number of minutes from 1 is accepted.";

/**
 * The strength hint as a bar plus its word — the word is real visible text, not only the bar.
 *
 * Deviation from the brief: the brief puts `aria-label={\`password strength: ${word}\`}` on this outer `<span>`.
 * A bare `<span>` maps to `role=generic`, which prohibits naming (`aria-label`/`aria-labelledby`) under ARIA 1.2, so
 * a conformant screen reader would ignore it — only `dom-accessibility-api` (what the tests use) honours it
 * regardless of the role restriction, which made the test pass without proving anything about real accessibility.
 * Dropped rather than given a role, since the word is already plain visible text a screen reader reads on its own.
 */
export function StrengthMeter({ password }: { password: string }) {
  const word = strength(password);
  if (!word) return null;
  const filled = STRENGTH_BARS[word] ?? 0;
  const tone = filled >= 4 ? "bg-ok" : filled >= 2 ? "bg-warn" : "bg-bad";
  return (
    <span className="flex items-center gap-1.5">
      <span aria-hidden className="flex gap-0.5">
        {[1, 2, 3, 4].map((step) => (
          <span key={step} className={cn("h-1 w-4 rounded-full", step <= filled ? tone : "bg-glass-2")} />
        ))}
      </span>
      <span className="text-[11px] text-t3">{word}</span>
    </span>
  );
}

/**
 * P4. The rotation has three side effects, not one — the hash, the session epoch and every API token row — so it asks
 * first, naming how many tokens it will delete, and re-reads the list afterwards (INVALIDATES.changePassword).
 *
 * Deviation from the brief: the brief's `mutationFn` takes `values: PasswordFormValues` and `submit` is
 * `handleSubmit(async (values) => { ...; mutation.mutate(values) })`, which makes both passwords a mutation
 * variable. TanStack keeps a mutation's `state.variables` in the MutationCache for its gcTime (default 5 min) after
 * it settles — after success and after a failed attempt alike — so they would sit there reachable long after
 * `reset(EMPTY)` blanked the fields. The repo already paid for this once, for the remote-access private key
 * (`features/gateway/useRwSave.ts`): the values travel through a ref instead (`pending`), read once by `mutationFn`
 * and cleared the moment they are, so the only thing `mutate()` ever hands TanStack is nothing at all.
 *
 * That also rules out `handleSubmit` for `submit`: `handleSubmit(cb)` is called at render time to build the bound
 * submit function, so the lint rule that catches a ref read during render (`react-hooks/refs`) cannot prove `cb` —
 * which touches `pending` — runs only later, as an event handler, and refuses it. `submit` instead calls
 * `form.trigger()` itself (the same resolver-driven validation `handleSubmit` runs, with `{ shouldFocus: true }` to
 * keep its on-refusal focus behaviour) and reads `form.getValues()` once the form is valid — exactly how `useRwSave`
 * already works around the same rule.
 */
export function usePasswordChange(tokenCount: number | undefined) {
  const queryClient = useQueryClient();
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const form = useForm<PasswordFormValues>({ resolver: zodResolver(passwordFormSchema), defaultValues: EMPTY, mode: "onChange" });
  const { formState, reset, setError } = form;
  const changePassword = useApiWrite("changePassword");
  // Both flags are read unconditionally — `a() || b()` would make the second a conditional hook call.
  const passwordBusy = usePasswordBusy();
  const connectionBusy = useConnectionBusy();
  const busy = passwordBusy || connectionBusy;
  const pending = useRef<PasswordFormValues | null>(null);

  // Never leave three password fields in a detached React tree.
  useEffect(() => () => reset(EMPTY), [reset]);

  const mutation = useMutation({
    mutationKey: PASSWORD_WRITE,
    mutationFn: () => {
      const values = pending.current;
      pending.current = null;
      if (!values) throw new Error("usePasswordChange: submit fired with no pending values");
      return changePassword(values.current, values.next);
    },
    onSuccess: () => {
      // Reset, not clear-by-hand: later polls must be able to update the form again.
      reset(EMPTY);
      setResult({ ok: true, text: "password changed" });
      notifyOk(passwordChangedMessage(tokenCount));
    },
    onError: (error) => {
      if (isNoAnswer(error)) {
        // The rotation may have committed: nothing is cleared, and the token list is re-read.
        notifyWarn(NO_ANSWER);
        setResult({ ok: false, text: "no answer yet" });
        void queryClient.invalidateQueries({ queryKey: keys.tokens });
        return;
      }
      if (error instanceof ApiError && error.status === 403 && error.message === WRONG_CURRENT) {
        setError("current", { message: WRONG_CURRENT }, { shouldFocus: true });
        setResult({ ok: false, text: WRONG_CURRENT });
        return;
      }
      notifyError(error, "change failed");
      setResult({ ok: false, text: error instanceof ApiError ? error.message : "change failed" });
    },
  });

  async function submit() {
    if (!(await form.trigger(undefined, { shouldFocus: true }))) return;
    const values = form.getValues();
    if (!(await confirm(passwordConfirm(tokenCount), { confirmLabel: "Change password" }))) return;
    // The question was open while anything could have started: check again before sending.
    if (isTokenBusy(queryClient)) return void notifyError(null, TOKEN_BUSY);
    if (isSettingsBusy(queryClient)) return void notifyError(null, SETTINGS_BUSY);
    pending.current = values;
    mutation.mutate();
  }

  function clear() {
    reset(EMPTY);
    setResult(null);
  }

  return { form, result, submit, clear, busy: busy || mutation.isPending, saving: mutation.isPending, dirty: formState.isDirty };
}

export function PasswordCard({ password, tokenCount }: { password: ReturnType<typeof usePasswordChange>; tokenCount: number | undefined }) {
  const [shown, setShown] = useState(false);
  const { form, result } = password;
  const { register, control, formState: { errors, isValid } } = form;
  const next = useWatch({ control, name: "next" });
  useUnsavedGuard(password.dirty);
  const type = shown ? "text" : "password";
  return (
    <GlassCard aria-label="Password">
      <CardHeader
        title="Password"
        aside={
          <Button size="sm" variant="ghost" aria-pressed={shown} onClick={() => setShown((value) => !value)}>
            {shown ? "Hide" : "Show"}
          </Button>
        }
      />
      <div className="flex flex-col gap-2.5">
        <TextField
          label="Current password" type={type} autoComplete="current-password" disabled={password.busy}
          error={errors.current?.message} {...register("current")}
        />
        <TextField
          label="New password" type={type} autoComplete="new-password" disabled={password.busy}
          hint={<StrengthMeter password={next} />} error={errors.next?.message} {...register("next")}
        />
        <TextField
          label="Confirm new password" type={type} autoComplete="new-password" disabled={password.busy}
          error={errors.confirm?.message} {...register("confirm")}
        />
        <p className="text-[11px] leading-relaxed text-t3">{passwordNote(tokenCount)}</p>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-line pt-3">
        <ResultLine result={result ?? (errors.confirm?.message === MISMATCH ? { ok: false, text: "passwords do not match" } : null)} className="min-w-0 flex-1" />
        <Button disabled={password.busy} onClick={password.clear}>Clear</Button>
        <Button variant="danger" disabled={!isValid || password.busy} onClick={() => void password.submit()}>
          {password.saving ? "Changing…" : "Change password"}
        </Button>
      </div>
    </GlassCard>
  );
}

/**
 * G5's idle-timeout half. A number field with no Save button: it is written on blur or Enter, only when the value is
 * valid AND differs from what the gateway holds — saving per keystroke would send "3", "30", "300".
 */
export function useIdleTimeout(settings: Settings | undefined) {
  const queryClient = useQueryClient();
  const saved = settings === undefined ? "" : String(settings.session_timeout_min);
  // `null` means "follow the gateway": the field is derived, not copied in an effect, so a poll updates an untouched
  // field without a second render and what the operator typed is never overwritten under their hands.
  const [typed, setTyped] = useState<string | null>(null);
  const value = typed ?? saved;
  const putSettings = useApiWrite("putSettings");
  const settingsBusy = useSettingsBusy();
  const connectionBusy = useConnectionBusy();
  const busy = settingsBusy || connectionBusy;

  const mutation = useMutation({
    mutationKey: settingsWriteKey({ session_timeout_min: 0 }),
    mutationFn: (minutes: number) => putSettings({ session_timeout_min: minutes }),
    onMutate: (minutes) => {
      const before = queryClient.getQueryData<Settings>(keys.settings)?.session_timeout_min;
      queryClient.setQueryData<Settings>(keys.settings, (old) => (old ? { ...old, session_timeout_min: minutes } : old));
      return { before };
    },
    onSuccess: (_saved, minutes) => { setTyped(null); notifyOk(idleTimeoutMessage(minutes)); },
    onError: (error, _minutes, context) => {
      if (context?.before !== undefined) {
        const before = context.before;
        queryClient.setQueryData<Settings>(keys.settings, (old) => (old ? { ...old, session_timeout_min: before } : old));
        setTyped(null);
      }
      if (isNoAnswer(error)) notifyWarn(NO_ANSWER);
      else notifyError(error, "idle timeout was not saved");
      void queryClient.invalidateQueries({ queryKey: keys.settings });
    },
  });

  const issue = value === saved ? null : parseIdleTimeout(value) === null ? TIMEOUT_MESSAGE : null;

  function commit() {
    const minutes = parseIdleTimeout(value);
    if (minutes === null || String(minutes) === saved || busy || mutation.isPending) return;
    mutation.mutate(minutes);
  }

  return {
    value, issue, busy: busy || mutation.isPending,
    change(next: string) { setTyped(next); },
    commit,
  };
}

export function SessionCard({ timeout }: { timeout: ReturnType<typeof useIdleTimeout> }) {
  return (
    <GlassCard aria-label="Session">
      <CardHeader title="Session" aside={<Chip plain>saved as soon as you change it</Chip>} />
      <TextField
        label="Idle timeout"
        hint="(minutes, 0 = off)"
        type="number"
        inputMode="numeric"
        min={0}
        disabled={timeout.busy}
        error={timeout.issue ?? undefined}
        value={timeout.value}
        onChange={(event) => timeout.change(event.target.value)}
        onBlur={timeout.commit}
        onKeyDown={(event) => { if (event.key === "Enter") timeout.commit(); }}
      />
      <p className="mt-2 text-[11px] leading-relaxed text-t2">{SESSION_NOTE}</p>
      <p className="mt-2 text-[11px] leading-relaxed text-t3">{SESSION_HINT}</p>
    </GlassCard>
  );
}

/** System › Access (P4 and G5's idle-timeout half for now). It owns `settings` and `tokens` at the slow cadence. */
export function Access() {
  const settings = usePolledQuery(queries.settings(), SLOW_POLL_MS);
  const tokens = usePolledQuery(queries.tokens(), SLOW_POLL_MS);
  const tokenCount = tokens.data?.length;
  const passwordState = usePasswordChange(tokenCount);
  const timeout = useIdleTimeout(settings.data);
  return (
    <div className="grid gap-3 md:grid-cols-[5fr_7fr] md:items-start">
      <div className="flex flex-col gap-3">
        <PasswordCard password={passwordState} tokenCount={tokenCount} />
        <SessionCard timeout={timeout} />
      </div>
      <div className="flex flex-col gap-3" />
    </div>
  );
}
