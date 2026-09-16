// A peek — Validate, a request preview, a dry-run — answers for the input it ran on. Once the form moves past that
// input, its result is marked as stale instead of shown as if it were current. Pure and unit-tested.

/** What a peek ran on, as a key to compare with the form's input now. */
export function runKey(input: unknown): string {
  return JSON.stringify(input);
}

/** Shown in place of a result the form has moved past. */
export const FORM_CHANGED = "Form changed since this run — run it again.";

/** Whether a result that ran on `ranKey` no longer describes the form, whose input is `liveKey` now. No result is never stale. */
export function isStaleRun(ranKey: string | null | undefined, liveKey: string): boolean {
  return ranKey !== null && ranKey !== undefined && ranKey !== liveKey;
}

/** A Validate outcome: whether it passed, the line it reads as, and the key of the input it checked. */
export interface CheckResult {
  ok: boolean;
  text: string;
  key: string;
}

/** A validate reply as its result line: "✓ ‹okText›" or "✗ ‹error›". */
export function checkResult(reply: { ok: boolean; error: string }, okText: string, key: string): CheckResult {
  return reply.ok ? { ok: true, text: `✓ ${okText}`, key } : { ok: false, text: `✗ ${reply.error}`, key };
}
