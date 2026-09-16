// System › Access: the password form's rules, its strength hint, and the two sentences a rotation needs — the one
// asked before it runs and the one said after. Pure and unit-tested; no value here is ever logged, cached or sent
// anywhere but `POST /password`.
import { z } from "zod";

/** `PasswordChangeIn`: new_password min 8, max 256; current_password max 256 (api/schemas.py). */
export const MIN_PASSWORD = 8;
export const MAX_PASSWORD = 256;

export const TOO_SHORT = `at least ${MIN_PASSWORD} characters`;
export const TOO_LONG = `at most ${MAX_PASSWORD} characters`;
export const MISMATCH = "does not match";
export const CURRENT_REQUIRED = "enter your current password";
/** The gateway's own answer to a wrong current password (routes.py), attached to that field. */
export const WRONG_CURRENT = "current password incorrect";

export interface PasswordFormValues {
  current: string;
  next: string;
  confirm: string;
}

export const passwordFormSchema = z
  .object({
    current: z.string().min(1, CURRENT_REQUIRED).max(MAX_PASSWORD, TOO_LONG),
    next: z.string().min(MIN_PASSWORD, TOO_SHORT).max(MAX_PASSWORD, TOO_LONG),
    confirm: z.string(),
  })
  .refine((values) => values.next === values.confirm, { message: MISMATCH, path: ["confirm"] }) satisfies z.ZodType<
    PasswordFormValues,
    PasswordFormValues
  >;

/** Submit is live only once all three rules hold — the Svelte panel's `pwOk`, in the schema's words. */
export function passwordFormValid(values: PasswordFormValues): boolean {
  return passwordFormSchema.safeParse(values).success;
}

/**
 * The strength hint, unchanged from the Svelte panel: case mix, a digit, a symbol and length ≥ 12, capped at "strong".
 * A hint, not a rule — the gateway enforces only the length. Client-only: never sent, never logged.
 */
export function strength(password: string): string {
  if (!password) return "";
  if (password.length < MIN_PASSWORD) return `too short (min ${MIN_PASSWORD})`;
  let score = 0;
  if (/[a-z]/.test(password) && /[A-Z]/.test(password)) score++;
  if (/\d/.test(password)) score++;
  if (/[^a-zA-Z0-9]/.test(password)) score++;
  if (password.length >= 12) score++;
  return ["weak", "ok", "good", "strong"][Math.min(score, 3)]!;
}

/**
 * Asked first, because of the third side effect: the rotation bumps the session epoch AND deletes every API token row
 * (require_auth short-circuits on a Bearer token and never consults the epoch, so the tokens themselves have to go).
 *
 * `count` is `undefined` when the token list has not loaded: saying "0 API tokens" would be a false promise about an
 * action with no undo, so the clause keeps its warning and loses only the number.
 */
export function passwordConfirm(count: number | undefined): string {
  const head = "Change the password? Every other signed-in session is signed out";
  const tail = "anything using them stops working until you issue new ones.";
  if (count === undefined) return `${head}, and every API token is deleted — ${tail}`;
  if (count === 0) return `${head}.`;
  return `${head}, and all ${count} API token${count === 1 ? "" : "s"} are deleted — ${tail}`;
}

/** Said afterwards. This session survives — the handler adopts the new epoch — and the screen says which ones did not. */
export function passwordChangedMessage(count: number | undefined): string {
  const head = "password changed · other sessions signed out";
  if (count === undefined) return `${head} · API tokens revoked`;
  if (count === 0) return head;
  return `${head} · ${count} API token${count === 1 ? "" : "s"} revoked`;
}

/** The card's own warning, with the count the confirm will repeat. */
export function passwordNote(count: number | undefined): string {
  const rules = "Minimum 8 characters; strength is a hint, not a rule.";
  if (count === 0) return `Changing it signs out every other session — this browser stays signed in. ${rules}`;
  const tokens = count === undefined ? "every API token" : `all ${count} API token${count === 1 ? "" : "s"}`;
  return `Changing it signs out every other session and deletes ${tokens} — this browser stays signed in. ${rules}`;
}

// --- the idle timeout, the other half of this screen's settings ---

/** The gateway's own message for a negative value (config.py SETTINGS_INT_BOUNDS). No invented floor above it. */
export const TIMEOUT_MESSAGE = "session_timeout_min must be >= 0";

export const idleTimeoutSchema = z.string().refine((value) => /^\d+$/.test(value.trim()), TIMEOUT_MESSAGE);

/** What the operator typed, as a whole number of minutes — or null when it is not one. */
export function parseIdleTimeout(value: string): number | null {
  return idleTimeoutSchema.safeParse(value).success ? Number(value.trim()) : null;
}

export function idleTimeoutMessage(minutes: number): string {
  return minutes === 0 ? "idle timeout off" : `idle timeout · ${minutes} min`;
}
