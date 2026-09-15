// How a remote-access revocation reached the running xray, in the words the operator reads, and the messages of the
// writes that return one. Pure and unit-tested.
import type { Rw, RwRevocation } from "../../api/client";

type Outcome = Exclude<RwRevocation, "">;

/**
 * Per outcome, the note for one revoked client and for a save that narrowed access. `cleaned` (xray was down and the
 * stored config was rewritten) never reads like `not-live` (nothing was there to cut), and `stopped` (confirmed down)
 * never like `stop-failed` (the revoked credential may still be live — a security warning).
 */
const NOTES: Record<Outcome, { client: string; save: string }> = {
  reapplied: { client: "live tunnel rebuilt without it", save: "live tunnel rebuilt with the narrowed settings" },
  rebuilt: { client: "previous config rebuilt and reloaded", save: "previous config rebuilt and reloaded with the narrowed settings" },
  cleaned: {
    client: "xray was down — the stored config was rewritten without it, so it cannot come back on the next start",
    save: "xray was down — the stored config was rewritten with the narrowed settings, so the old ones cannot come back on the next start",
  },
  stopped: {
    client: "xray stopped — remote access is down for everyone until you reconnect",
    save: "xray stopped — remote access is down for everyone until you reconnect",
  },
  "stop-failed": {
    client: "SECURITY WARNING: xray's stop could not be confirmed, so the revoked device may still be able to connect. The panel keeps retrying; reboot the gateway if this does not clear.",
    save: "SECURITY WARNING: xray's stop could not be confirmed, so the settings you just narrowed may still be live. The panel keeps retrying; reboot the gateway if this does not clear.",
  },
  "not-live": { client: "nothing was serving the inbound — no live access to cut", save: "nothing was serving the inbound — nothing live to narrow" },
};

/** The note for an outcome; "" when the write was not a revocation. */
export function revocationNote(how: RwRevocation, subject: "client" | "save"): string {
  return how === "" ? "" : NOTES[how][subject];
}

/** An outcome the operator must not read as success: an outage, or a stop that could not be confirmed. */
export function revocationIsError(how: RwRevocation): boolean {
  return how === "stopped" || how === "stop-failed";
}

export interface RwMessage {
  text: string;
  error: boolean;
}

/** A4 Save's message: how its revocation went, or — for a save that revoked nothing — whether a node could take it now. */
export function rwSavedMessage(rw: Rw, activeAtSend: boolean): RwMessage {
  if (rw.revocation) return { text: `saved · ${revocationNote(rw.revocation, "save")}`, error: revocationIsError(rw.revocation) };
  return { text: activeAtSend ? "saved · rebuilt into the live config" : "saved · no active node right now, so it applies on the next connect", error: false };
}

/** A6 Suspend: the uuid is kept, and how the revocation went. */
export function suspendedMessage(rw: Rw): RwMessage {
  const note = revocationNote(rw.revocation, "client");
  return { text: `client suspended — its uuid is kept${note ? ` — ${note}` : ""}`, error: revocationIsError(rw.revocation) };
}

/** A6 Remove: which device, and how the revocation went. */
export function removedMessage(name: string, rw: Rw): RwMessage {
  const note = revocationNote(rw.revocation, "client");
  return { text: `removed ${name}${note ? ` — ${note}` : ""}`, error: revocationIsError(rw.revocation) };
}

/** A client uuid is a credential: shown as its first four characters and dots until revealed. */
export function maskUuid(id: string): string {
  return `${id.slice(0, 4)}…••••`;
}
