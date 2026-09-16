// System › Access: the create-token form's rules, the expiry it sends as an epoch, and how a row's dates read.
// Pure and unit-tested. The created secret itself never comes near this module — it exists only in component state.
import { z } from "zod";
import type { ApiToken, ApiTokenScope } from "../../api/client";

/** `TokenCreateIn`: name 1–64, scope one of three, expires_at an int ≥ 1 or omitted (api/schemas.py). */
export const MAX_TOKEN_NAME = 64;
export const NAME_REQUIRED = "a name for this token";
export const NAME_TOO_LONG = `at most ${MAX_TOKEN_NAME} characters`;

export const TOKEN_SCOPES = ["monitor", "read", "readwrite"] as const satisfies readonly ApiTokenScope[];
export const EXPIRY_CHOICES = ["never", "30d", "90d", "1y"] as const;
export type ExpiryChoice = (typeof EXPIRY_CHOICES)[number];

const EXPIRY_DAYS: Record<ExpiryChoice, number> = { never: 0, "30d": 30, "90d": 90, "1y": 365 };
export const EXPIRY_LABELS: Record<ExpiryChoice, string> = { never: "never", "30d": "30 days", "90d": "90 days", "1y": "1 year" };

export interface TokenFormValues {
  name: string;
  scope: ApiTokenScope;
  expiry: ExpiryChoice;
}

export const tokenFormSchema = z.object({
  name: z.string().trim().min(1, NAME_REQUIRED).max(MAX_TOKEN_NAME, NAME_TOO_LONG),
  scope: z.enum(TOKEN_SCOPES),
  expiry: z.enum(EXPIRY_CHOICES),
}) satisfies z.ZodType<TokenFormValues, TokenFormValues>;

/**
 * The epoch the backend wants, or `undefined` for "never" — never `null`: `TokenCreateIn` is strict and its field is
 * `int | None` with `ge=1`, so an explicit null is refused. Anything ≤ now is refused with 422 "expires_at must be in
 * the future", which the clock skew between a phone and the gateway can produce — the form stays open and says so.
 */
export function expiresAt(choice: ExpiryChoice, nowMs: number): number | undefined {
  const days = EXPIRY_DAYS[choice];
  if (!days) return undefined;
  return Math.floor(nowMs / 1000) + days * 86_400;
}

/** The helper under the Expires control: it is a timestamp, and this is the date it works out to. */
export function expiryHelper(choice: ExpiryChoice, nowMs: number): string {
  const at = expiresAt(choice, nowMs);
  if (at === undefined) return "sent as a timestamp · never expires";
  return `sent as a timestamp · ${localDate(at)}`;
}

/** A date in the browser's own zone, which is the only clock the operator can check this against. */
export function localDate(epochSec: number): string {
  const date = new Date(epochSec * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** A date and a time of day, for a stamp inside the last day. */
export function localDateTime(epochSec: number): string {
  const date = new Date(epochSec * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${localDate(epochSec)} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** How a row's Expires column reads: "never" is literal, and a near expiry earns a badge beside the date. */
export function expiryLabel(expiresAtSec: number | null | undefined): string {
  return expiresAtSec ? localDate(expiresAtSec) : "never";
}

/** "in ‹n› d" while the expiry is inside 30 days; "expired" once it has passed; nothing otherwise. */
export function expiryBadge(expiresAtSec: number | null | undefined, nowSec: number): string | null {
  if (!expiresAtSec) return null;
  const days = Math.ceil((expiresAtSec - nowSec) / 86_400);
  if (days <= 0) return "expired";
  return days <= 30 ? `in ${days} d` : null;
}

/**
 * The Last used column. `null` reads "—" literally, and it stays "—" for up to a minute after a real use: the stamp is
 * throttled to once a minute. An expired token is refused before the stamp, so it never gets one at all.
 */
export function lastUsedLabel(lastUsedAt: number | null): string {
  return lastUsedAt === null ? "—" : localDateTime(lastUsedAt);
}

/** What each scope can actually do (api/deps.py), which is the Scopes card's copy, word for word. */
export interface ScopeCopy {
  scope: ApiTokenScope;
  label: string;
  lead: string;
  body: string;
}

export const SCOPE_COPY: readonly ScopeCopy[] = [
  {
    scope: "monitor", label: "monitor", lead: "Status and traffic history only.",
    body: "/api/status, /api/traffic/history, /api/node-health, /api/network — every other path answers 403. For a dashboard or an uptime check. Refused on every screen in System.",
  },
  {
    scope: "read", label: "read", lead: "Every GET, including the backup.",
    body: "It can pull the whole configuration — subscription URLs, node uuids, Reality keys. Not a “safe” scope.",
  },
  {
    scope: "readwrite", label: "read/write", lead: "Every GET plus every mutation, with no CSRF header.",
    body: "Full control of the gateway. Issue one only for something that has to change the configuration.",
  },
];

export function scopeLabel(scope: ApiTokenScope): string {
  return SCOPE_COPY.find((row) => row.scope === scope)?.label ?? scope;
}

/** Asked before a revocation. Unchanged from the Svelte panel — it was already right. */
export function revokeConfirm(token: Pick<ApiToken, "name">): string {
  return `Revoke token “${token.name}”? Anything using it stops working immediately.`;
}

export function tokenCreatedMessage(name: string): string {
  return `token “${name}” created`;
}

export function tokenRevokedMessage(name: string): string {
  return `token “${name}” revoked`;
}

/** A second revoke answers 404, which is not a failure: the row was already gone. */
export const ALREADY_GONE = "that token is already gone";

/** Refused while the one-time secret is still on screen — clearing it is the only way to see it again. */
export const FINISH_COPYING = "Finish copying the visible token first.";
