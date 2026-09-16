import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Controller, useForm, useWatch } from "react-hook-form";
import { ApiError, isNoAnswer, type ApiToken, type ApiTokenCreated } from "../../api/client";
import { TOKEN_BUSY, TOKEN_WRITE, isTokenBusy, useApiWrite, useConnectionBusy, useTokenBusy } from "../../api/invalidation";
import { useUnsavedGuard } from "../../app/guard";
import { useNow } from "../../components/data/Ago";
import { CardHeader } from "../../components/data/CardHeader";
import { cardFallback } from "../../components/data/CardState";
import { AlertBanner } from "../../components/data/AlertBanner";
import { confirm } from "../../components/confirm";
import { Button } from "../../components/ui/Button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger, useAfterMenu } from "../../components/ui/DropdownMenu";
import { SegmentedField, TextField } from "../../components/ui/Field";
import { GlassCard } from "../../components/ui/GlassCard";
import { Pill } from "../../components/ui/Pill";
import { Sheet, SheetContent } from "../../components/ui/Sheet";
import { EmptyState } from "../../components/ui/States";
import { notifyError, notifyOk, notifyWarn } from "../../components/ui/Toaster";
import { copyText } from "../../lib/clipboard";
import { NO_ANSWER } from "../gateway/networkForm";
import {
  ALREADY_GONE, EXPIRY_CHOICES, EXPIRY_LABELS, FINISH_COPYING, SCOPE_COPY, TOKEN_SCOPES, expiresAt, expiryBadge,
  expiryHelper, expiryLabel, lastUsedLabel, localDate, revokeConfirm, scopeLabel, tokenCreatedMessage,
  tokenFormSchema, tokenRevokedMessage, type TokenFormValues,
} from "./tokenForm";

const EMPTY: TokenFormValues = { name: "", scope: "monitor", expiry: "never" };
const SCOPE_HELPER = "monitor is enough for a dashboard — it cannot read any secret";
const CREATE_FOOTER = "the secret is shown once, right after it is created";
const TOKENS_FOOTER_1 =
  "Last used is stamped at most once a minute, so a token used seconds ago can still read “—”. An expired token is " +
  "refused before the stamp, so it never gets one.";
const TOKENS_FOOTER_2 = "the prefix is not a secret — the secret itself is only ever shown once";
const SECRET_NOTE =
  "Copy it now, it is shown only once. The gateway keeps only its hash — if you lose it, revoke this token and issue another.";
const SECRET_WARN_TITLE = "Leaving this screen loses the secret.";
const SECRET_WARN_TEXT =
  "Navigation is blocked while it is on display; it is never written to the cache, a URL, a toast or the log.";

/** G7: the token list's two writes, and the secret the create answers with — which exists nowhere else. */
export function useTokens() {
  const queryClient = useQueryClient();
  const createToken = useApiWrite("createToken");
  const deleteToken = useApiWrite("deleteToken");
  const tokenBusy = useTokenBusy();
  const connectionBusy = useConnectionBusy();
  // A mutation RESULT, never a query: the secret cannot reach the cache from here.
  const [secret, setSecret] = useState<ApiTokenCreated | null>(null);
  const [formOpen, setFormOpen] = useState(false);

  // While it is on screen the guard is held whether or not anything was typed, and it goes on unmount.
  useUnsavedGuard(secret !== null);

  const create = useMutation({
    mutationKey: TOKEN_WRITE,
    mutationFn: async ({ name, scope, at }: { name: string; scope: ApiToken["scope"]; at: number | undefined }) => {
      const created = await createToken(name, scope, at);
      // The secret leaves the request HERE, into component state, and never becomes this mutation's
      // RESULT: TanStack keeps a settled mutation's `state.data` in the MutationCache until its gcTime
      // expires (5 minutes by default, and `reset()` only detaches the observer and schedules that
      // collection), so a result carrying the token would outlive both Done and this screen. Same
      // reasoning as the two passwords, which travel through a ref rather than as variables
      // (`AccessScreen.tsx`); there it was the variables, here it is the answer.
      setSecret(created);
      const row: Partial<ApiTokenCreated> = { ...created };
      delete row.token;   // a rest pattern would bind `token` and never use it, which the lint refuses
      return row as ApiToken;
    },
    onSuccess: (created) => notifyOk(tokenCreatedMessage(created.name)),
    onError: (error) => {
      if (isNoAnswer(error)) {
        notifyWarn(NO_ANSWER);
        void queryClient.invalidateQueries({ queryKey: ["tokens"] });
        return;
      }
      notifyError(error, "create failed");
    },
  });

  const revoke = useMutation({
    mutationKey: TOKEN_WRITE,
    mutationFn: ({ token }: { token: ApiToken }) => deleteToken(token.id),
    onSuccess: (_result, { token }) => notifyOk(tokenRevokedMessage(token.name)),
    onError: (error) => {
      if (error instanceof ApiError && error.status === 404) {
        notifyOk(ALREADY_GONE);
        void queryClient.invalidateQueries({ queryKey: ["tokens"] });
        return;
      }
      if (isNoAnswer(error)) {
        notifyWarn(NO_ANSWER);
        void queryClient.invalidateQueries({ queryKey: ["tokens"] });
        return;
      }
      notifyError(error, "revoke failed");
    },
  });

  // Done and unmount clear the row the create answered with as well as the secret, so nothing this
  // screen created is left settled in the MutationCache. `reset` is bound once per observer, so this
  // effect still runs exactly once.
  const { reset: resetCreate } = create;
  useEffect(() => () => { setSecret(null); resetCreate(); }, [resetCreate]);

  async function askRevoke(token: ApiToken) {
    if (!(await confirm(revokeConfirm(token), { confirmLabel: "Revoke" }))) return;
    if (isTokenBusy(queryClient)) return void notifyError(null, TOKEN_BUSY);
    revoke.mutate({ token });
  }

  function openForm() {
    // A second create while the first secret is still on display would replace it, unread.
    if (secret) return void notifyError(null, FINISH_COPYING);
    setFormOpen(true);
  }

  return {
    secret, formOpen, create, revoke, askRevoke, openForm,
    closeForm: () => setFormOpen(false),
    dismissSecret: () => { setSecret(null); setFormOpen(false); resetCreate(); },
    busy: tokenBusy || connectionBusy,
  };
}

export type TokensState = ReturnType<typeof useTokens>;

export function ScopesCard() {
  return (
    <GlassCard aria-label="Scopes">
      <CardHeader title="Scopes" detail="what a token can actually do" />
      <ul className="flex flex-col gap-2.5">
        {SCOPE_COPY.map((row) => (
          <li key={row.scope} className="flex items-start gap-2.5">
            <Pill className="mt-px shrink-0">{row.label}</Pill>
            <p className="min-w-0 text-[11.5px] leading-relaxed text-t3">
              <span className="font-semibold text-t1">{row.lead}</span> {row.body}
            </p>
          </li>
        ))}
      </ul>
    </GlassCard>
  );
}

/** The one-time secret, in place of the create form. Selectable text, never an input and never an attribute. */
export function SecretPanel({ tokens }: { tokens: TokensState }) {
  const [copied, setCopied] = useState(false);
  const secret = tokens.secret!;
  async function copy() {
    try {
      await copyText(secret.token);
      setCopied(true);
    } catch (error) {
      notifyError(error, "copy failed");
    }
  }
  return (
    <div role="group" aria-label={`New token ${secret.name}`} className="flex flex-col gap-2.5">
      <div className="flex items-center gap-2">
        <Pill tone="warn">shown once</Pill>
        <p className="min-w-0 truncate text-[12px] font-semibold text-t1">New token “{secret.name}” · {scopeLabel(secret.scope)}</p>
      </div>
      <p className="text-[11.5px] leading-relaxed text-t2">{SECRET_NOTE}</p>
      <p className="select-all break-all rounded-xl border border-line bg-glass px-3 py-2 font-mono text-[12px] text-t1">{secret.token}</p>
      <AlertBanner tone="warn" title={SECRET_WARN_TITLE} text={SECRET_WARN_TEXT} />
      <div className="flex flex-wrap justify-end gap-2">
        <Button onClick={() => void copy()}>{copied ? "Copied ✓" : "Copy"}</Button>
        <Button variant="primary" onClick={tokens.dismissSecret}>Done</Button>
      </div>
    </div>
  );
}

/** The create form's own state, shared by the desktop card and the phone sheet. */
export function useTokenForm(tokens: TokensState) {
  const { control, handleSubmit, register, reset, formState: { errors, isValid, isDirty } } = useForm<TokenFormValues>({
    resolver: zodResolver(tokenFormSchema), defaultValues: EMPTY, mode: "onChange",
  });
  // Spec §3 lists this form among the guarded ones: a typed name and a chosen scope are as easy to
  // lose to a stray tab as any other edit, and every other editor asks first.
  useUnsavedGuard(isDirty);
  const expiry = useWatch({ control, name: "expiry" });
  // The gateway's clock, not the browser's: it refuses an expires_at that is not in its own future, and a phone
  // whose clock runs behind would otherwise send a 30-day expiry the gateway reads as already past.
  const nowMs = useNow(60_000);
  const submit = handleSubmit((values) => {
    tokens.create.mutate(
      { name: values.name.trim(), scope: values.scope, at: expiresAt(values.expiry, nowMs) },
      { onSuccess: () => reset(EMPTY) },
    );
  });
  return { control, register, errors, isValid, expiry, nowMs, submit, discard: () => reset(EMPTY) };
}

export function TokenFormFields({ form }: { form: ReturnType<typeof useTokenForm> }) {
  const { control, register, errors, expiry, nowMs } = form;
  return (
    <>
      <TextField label="Name" hint="(1–64 characters)" placeholder="a name for this token" error={errors.name?.message} {...register("name")} />
      <Controller
        control={control}
        name="expiry"
        render={({ field }) => (
          <SegmentedField
            legend="Token expiry"
            options={EXPIRY_CHOICES.map((choice) => ({ value: choice, label: EXPIRY_LABELS[choice] }))}
            value={field.value}
            onValueChange={field.onChange}
          />
        )}
      />
      <p className="-mt-1 text-[11px] text-t3">{expiryHelper(expiry, nowMs)}</p>
      <Controller
        control={control}
        name="scope"
        render={({ field }) => (
          <SegmentedField
            legend="Token scope"
            options={TOKEN_SCOPES.map((scope) => ({ value: scope, label: scopeLabel(scope) }))}
            value={field.value}
            onValueChange={field.onChange}
          />
        )}
      />
      <p className="-mt-1 text-[11px] text-t3">{SCOPE_HELPER}</p>
    </>
  );
}

export function TokenFormCard({ tokens }: { tokens: TokensState }) {
  const form = useTokenForm(tokens);
  return (
    <GlassCard aria-label="New API token">
      <CardHeader
        title="New API token"
        aside={<Button size="icon" variant="ghost" aria-label="Close" onClick={tokens.closeForm}>×</Button>}
      />
      {tokens.secret ? (
        <SecretPanel tokens={tokens} />
      ) : (
        <div className="flex flex-col gap-2.5">
          <TokenFormFields form={form} />
          <div className="mt-1 flex flex-wrap items-center gap-2 border-t border-line pt-3">
            <p className="min-w-0 flex-1 text-[11px] text-t3">{CREATE_FOOTER}</p>
            <Button onClick={tokens.closeForm}>Cancel</Button>
            <Button variant="primary" disabled={!form.isValid || tokens.busy} onClick={() => void form.submit()}>
              {tokens.create.isPending ? "Creating…" : "Create token"}
            </Button>
          </div>
        </div>
      )}
    </GlassCard>
  );
}

/** The phone's create sheet: the same form, and after a create the secret in its place — with one sticky button. */
export function TokenSheet({ tokens, open, onOpenChange }: { tokens: TokensState; open: boolean; onOpenChange: (open: boolean) => void }) {
  const form = useTokenForm(tokens);
  // A backdrop tap or Escape is incidental on a 390 px screen; the desktop cannot lose the secret this way (its ×
  // only clears `formOpen`, `AccessScreen.tsx`), so the sheet refuses those two paths while the secret is on
  // display. Explicit dismissal — Done, or × (`Primitive.Close`, which never raises this event) — still works.
  const keepSecretOnScreen = (event: { preventDefault: () => void }) => { if (tokens.secret) event.preventDefault(); };
  // The desktop's form unmounts when it closes, which is what drops a half-typed name there. This one
  // outlives its sheet, so closing puts it back by hand — otherwise it would keep holding the unsaved-edit
  // guard, and the next tab would ask about edits that are nowhere on screen.
  const close = (next: boolean) => { if (!next) { tokens.dismissSecret(); form.discard(); } onOpenChange(next); };
  return (
    <Sheet open={open} onOpenChange={close}>
      <SheetContent title="New token" onEscapeKeyDown={keepSecretOnScreen} onPointerDownOutside={keepSecretOnScreen}>
        <div className="flex flex-col gap-2.5">
          {tokens.secret ? <SecretPanel tokens={tokens} /> : <TokenFormFields form={form} />}
          {tokens.secret ? <p className="text-[11px] text-t3">{FINISH_COPYING}</p> : <p className="text-[11px] text-t3">{CREATE_FOOTER}</p>}
          <div className="glass sticky bottom-0 -mx-1 flex bg-solid p-2">
            <Button
              variant="primary"
              className="flex-1"
              disabled={!form.isValid || tokens.busy || tokens.secret !== null}
              onClick={() => void form.submit()}
            >
              {tokens.create.isPending ? "Creating…" : "Create token"}
            </Button>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}

/** One token as a phone card: its facts on two lines, and a ⋯ menu whose Revoke opens after the menu closed. */
export function TokenCard({ token, tokens, nowSec }: { token: ApiToken; tokens: TokensState; nowSec: number }) {
  const menu = useAfterMenu();
  const badge = expiryBadge(token.expires_at, nowSec);
  return (
    <li className="rounded-xl border border-line bg-glass px-3 py-2">
      <div className="flex items-baseline gap-2">
        <span className="min-w-0 truncate text-[13px] font-semibold text-t1">{token.name}</span>
        <Pill className="shrink-0">{scopeLabel(token.scope)}</Pill>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button size="icon" variant="ghost" className="ml-auto" aria-label={`More actions for ${token.name}`}>⋯</Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent onCloseAutoFocus={menu.onCloseAutoFocus}>
            <DropdownMenuItem disabled={tokens.busy} onSelect={() => menu.after(() => void tokens.askRevoke(token))}>Revoke…</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      <p className="truncate font-mono text-[11px] text-t3">
        {token.prefix} · created {localDate(token.created_at)} · last used {lastUsedLabel(token.last_used_at)}
      </p>
      <p className="text-[11px] text-t3">
        {token.expires_at ? `expires ${expiryLabel(token.expires_at)}` : "never expires"}
        {badge ? <Pill tone="warn" className="ml-1.5">{badge}</Pill> : null}
      </p>
    </li>
  );
}

function TokenRow({ token, tokens, nowSec }: { token: ApiToken; tokens: TokensState; nowSec: number }) {
  const badge = expiryBadge(token.expires_at, nowSec);
  return (
    <tr className="border-t border-line">
      <td className="px-2 py-2 text-[12.5px] font-semibold text-t1">{token.name}</td>
      <td className="px-2 py-2 text-[12px] text-t2">{scopeLabel(token.scope)}</td>
      <td className="px-2 py-2 font-mono text-[12px] text-t3">{token.prefix}</td>
      <td className="px-2 py-2 text-[12px] text-t3">{localDate(token.created_at)}</td>
      <td className="px-2 py-2 text-[12px] text-t3">{lastUsedLabel(token.last_used_at)}</td>
      <td className="px-2 py-2 text-[12px] text-t3">
        {expiryLabel(token.expires_at)}
        {badge ? <Pill tone="warn" className="ml-1.5">{badge}</Pill> : null}
      </td>
      <td className="px-2 py-2 text-right">
        <Button size="sm" variant="danger" aria-label={`Revoke ${token.name}`} disabled={tokens.busy} onClick={() => void tokens.askRevoke(token)}>
          Revoke
        </Button>
      </td>
    </tr>
  );
}

const COLUMNS = ["Name", "Scope", "Prefix", "Created", "Last used", "Expires"];

export function TokensCard({ list, tokens, nowSec, phone = false }: { list: UseQueryResult<ApiToken[]>; tokens: TokensState; nowSec: number; phone?: boolean }) {
  const fallback = cardFallback([list], "API tokens did not load");
  const rows = list.data ?? [];
  return (
    <GlassCard aria-label="API tokens">
      <CardHeader
        title="API tokens"
        detail={list.data ? `${rows.length} issued` : undefined}
        aside={<Button size="sm" disabled={tokens.busy} onClick={tokens.openForm}>Create token</Button>}
      />
      {fallback ?? (rows.length === 0 ? (
        <EmptyState title="No tokens yet." />
      ) : phone ? (
        <ul aria-label="Tokens" className="flex flex-col gap-2">
          {rows.map((token) => <TokenCard key={token.id} token={token} tokens={tokens} nowSec={nowSec} />)}
        </ul>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[42rem] border-collapse text-left">
            <caption className="sr-only">API tokens</caption>
            <thead>
              <tr>
                {COLUMNS.map((column) => (
                  <th key={column} scope="col" className="px-2 pb-1 text-[10.5px] font-semibold uppercase tracking-[.07em] text-t3">{column}</th>
                ))}
                <th scope="col" className="px-2 pb-1"><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((token) => <TokenRow key={token.id} token={token} tokens={tokens} nowSec={nowSec} />)}
            </tbody>
          </table>
        </div>
      ))}
      <p className="mt-3 text-[11px] leading-relaxed text-t3">{TOKENS_FOOTER_1}</p>
      {phone ? null : <p className="mt-1 text-[11px] text-t3">{TOKENS_FOOTER_2}</p>}
    </GlassCard>
  );
}
