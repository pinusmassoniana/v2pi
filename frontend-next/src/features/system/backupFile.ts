// System › Backups: what the browser can decide about a picked file before anything is sent, and what the screen says
// after a restore answered. Pure and unit-tested; nothing here fetches, caches or keeps a document.
import type { ApiError, RestoreResult } from "../../api/client";
import { fmtBytes } from "../../lib/format";

/** `/api/restore`'s body limit (app.py RESTORE_BODY_LIMIT). The middleware refuses above it before auth, and still audits. */
export const MAX_RESTORE_BYTES = 2 * 1024 * 1024;

/** The caps `BackupDocument` enforces, for the card that says what the file can hold. */
export const BACKUP_CAPS = { nodes: 5000, subscriptions: 256, profiles: 256, rules: 256, settings: 64 } as const;

export interface PreCheck {
  ok: boolean;
  label: string;
}

/** A backup document named for the day it was taken. `GET /backup` sends no Content-Disposition — the client names it. */
export function backupFilename(now: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `v2pi-backup-${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}.json`;
}

function parsed(text: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
}

/**
 * The four things the browser can tell about a picked file, all before the confirm. Deliberately NOT a check that
 * `nodes` is an array: `BackupDocument.nodes` defaults to `[]`, so a gateway with no nodes exports a valid document
 * the Svelte panel refused in the browser and never sent. Everything else — references, setting bounds, the DHCP
 * range and the lockout guard — is the gateway's to refuse, while it is still serving.
 */
export function backupPreChecks(text: string, size: number): PreCheck[] {
  const value = parsed(text);
  const isObject = value !== null && typeof value === "object" && !Array.isArray(value);
  const underCap = size <= MAX_RESTORE_BYTES;
  return [
    { ok: value !== undefined, label: "parses as JSON" },
    { ok: isObject, label: "an object, not an array" },
    { ok: isObject && "schema_version" in (value as object), label: "carries a schema_version" },
    { ok: underCap, label: `${fmtBytes(size)} — ${underCap ? "under" : "over"} the 2 MB the gateway accepts` },
  ];
}

/** The same four answers in the three lines a phone sheet has room for. */
export function condensedBackupChecks(text: string, size: number): PreCheck[] {
  const [json, object, schema, cap] = backupPreChecks(text, size) as [PreCheck, PreCheck, PreCheck, PreCheck];
  return [
    { ok: json.ok && object.ok, label: "parses as JSON, and is an object" },
    schema,
    { ok: cap.ok, label: `${fmtBytes(size)} — ${cap.ok ? "under" : "over"} the 2 MB limit` },
  ];
}

export function checksPass(checks: readonly PreCheck[]): boolean {
  return checks.every((check) => check.ok);
}

/** Said instead of sending, so an oversized file gets a sentence rather than a 413 raised before the session is read. */
export function fileTooLarge(size: number): { title: string; text: string } | null {
  if (size <= MAX_RESTORE_BYTES) return null;
  return {
    title: "That file is larger than 2 MB",
    text: "the gateway refuses it before it even looks at your session. Export a fresh backup instead of editing an old one.",
  };
}

/** The contract's sentence, then which file it means. Rendered with the line break by the confirm dialog. */
export function restoreConfirm(filename: string, size: number): string {
  return (
    "Restore replaces every node, subscription, anti-DPI profile, routing rule and panel setting with the ones in this " +
    "file, and disconnects the gateway. The Reality private key and the remote-access client list are not restored. " +
    `Continue?\n\n${filename} · ${fmtBytes(size)}`
  );
}

export interface RestoredMessage {
  message: string;
  /** A restore that had to turn remote access off is not an "ok" — it took access away that nobody asked it to. */
  tone: "ok" | "warn";
}

/** What a successful restore says. Always names the disconnection: the gateway is down even though nothing failed. */
export function restoredMessage(result: RestoreResult): RestoredMessage {
  const { nodes, subscriptions, profiles, routing_rules: rules, rw_disabled: disabled } = result.restored;
  const message =
    `restored ${nodes} nodes, ${subscriptions} subscriptions, ${profiles} profiles, ${rules} routing rules — ` +
    "the gateway is disconnected; Connect a node when you are ready";
  if (!disabled) return { message, tone: "ok" };
  return { message: `${message} · remote access was turned off: ${disabled}`, tone: "warn" };
}

/** The persistent line, not a toast: no route reads a snapshot back, so the path is all the operator gets. */
export function snapshotNote(result: RestoreResult): string {
  return `A copy of what this replaced was saved on the gateway at ${result.pre_restore_snapshot}.`;
}

/**
 * `POST /restore` re-wraps its refusals by hand: a field failure reads `invalid backup: settings.health_interval: …`
 * and a model-level one — every reference check, the settings-value check, the DHCP-range check and the lockout guard —
 * has an empty `loc`, so it reads `invalid backup: : Value error, …`. A model validator on a nested model (for
 * example `BackupSubscription.bounded_injection`, keyed by list position) instead carries a non-empty loc, so the
 * fragment reads `invalid backup: subscriptions.0: Value error, …` — `Value error, ` can land right after any
 * `: `, not only at the very start. Strip the `invalid backup: ` prefix, the bare leading `: ` of an empty loc, and
 * `Value error, ` wherever pydantic put it, keeping a real loc (like `subscriptions.0`) so the operator still knows
 * which entry was refused.
 */
function refusalDetail(message: string): string {
  let detail = message.startsWith("invalid backup: ") ? message.slice("invalid backup: ".length) : message;
  if (detail.startsWith(": ")) detail = detail.slice(2);
  return detail.replace(/(^|: )Value error, /, "$1");
}

export interface RestoreRefusal {
  message: string;
  /** Kept until dismissed: a 502 whose detail names a recovery describes a gateway that was put back, not one that never moved. */
  sticky: boolean;
}

/**
 * Why a restore did not happen. A 400 is refused before anything is written and the tunnel never moves; a 502 comes
 * from the apply phase, after the rollback, the previous-host restore, the candidate undo and the previous guard.
 */
export function restoreRefusedMessage(error: ApiError): RestoreRefusal {
  if (error.status === 502) {
    return {
      message: `not restored — the gateway could not apply it: ${error.message}`,
      sticky: error.message.includes("recovery:"),
    };
  }
  return { message: `not restored — ${refusalDetail(error.message)}`, sticky: false };
}
