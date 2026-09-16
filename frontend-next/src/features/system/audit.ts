// System › Access, the audit list: what a row reads as, and which paths carry a live credential. Pure and unit-tested.
import type { AuditEntry } from "../../api/client";
import type { Tone } from "../../components/data/types";

/**
 * A remote-access client's path, matched BY ROUTE SHAPE — never by guessing whether a segment looks like a uuid. The
 * id in `/api/rw/clients/‹id›` is the credential the device connects with, so a PATCH or DELETE of one puts a live
 * bearer secret in the audit row. `/link` and `/config` never appear here: the middleware records only POST, PUT,
 * PATCH and DELETE, and both of those are GETs.
 */
const RW_CLIENT_PATH = /^\/api\/rw\/clients\/([^/]+)$/;

export interface MaskedPath {
  masked: string;
  full: string;
}

/** The prefix plus the first 8 characters of the id, or null when this path carries no credential at all. */
export function maskedAuditPath(path: string): MaskedPath | null {
  const match = RW_CLIENT_PATH.exec(path);
  if (!match) return null;
  const id = match[1]!;
  return { masked: `/api/rw/clients/${id.slice(0, 8)}…`, full: path };
}

/** 2xx is neutral — a recorded success is not a state to colour. 4xx warns, 5xx is bad. */
export function statusTone(status: number): Tone {
  if (status >= 500) return "bad";
  if (status >= 400) return "warn";
  return "neutral";
}

export type ActorKind = "user" | "token" | "anon";

/** `user:‹username›`, `token:‹prefix›`, or `anon` — setup, login, and anything whose principal could not be resolved. */
export function actorKind(actor: string): ActorKind {
  if (actor.startsWith("user:")) return "user";
  if (actor.startsWith("token:")) return "token";
  return "anon";
}

/** Time of day only: the list is one shot of recent rows, not a date range. */
export function auditTime(ts: number): string {
  const date = new Date(ts * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/** A stable key for a row: the list is not paginated and never merges two reads, so ts + method + path is enough. */
export function auditKey(row: AuditEntry, index: number): string {
  return `${row.ts}-${row.method}-${row.path}-${index}`;
}

/** The card's own description. The route's docstring says "successful mutations" and is wrong — every attempt is recorded. */
export const AUDIT_DESCRIPTION = "Every mutation attempt made through the panel or the API — who, what, and how it ended.";
export const AUDIT_FOOTER = "Newest first, one shot of at most 100 rows — nothing polls this and nothing refreshes it after a write.";
export const AUDIT_EMPTY = "No recorded changes yet.";
export const AUDIT_LOADING = "Loading audit log…";
export const AUDIT_NOT_LOADED = "Nothing is fetched until Show is pressed — and a write never yanks the list while you are reading it.";
/** How many rows one read asks for; the gateway clamps `limit` to 1…2000 and the table holds at most 2000. */
export const AUDIT_LIMIT = 100;
