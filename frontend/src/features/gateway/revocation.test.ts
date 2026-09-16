import { describe, expect, it } from "vitest";
import type { RwRevocation } from "../../api/client";
import { RW } from "../../test/fixtures";
import { maskUuid, removedMessage, revocationIsError, revocationNote, rwSavedMessage, suspendedMessage } from "./revocation";

const OUTCOMES: Exclude<RwRevocation, "">[] = ["reapplied", "rebuilt", "cleaned", "stopped", "stop-failed", "not-live"];

describe("revocation outcomes", () => {
  it("a note for each outcome, for one client and for a narrowing save", () => {
    expect(OUTCOMES.map((how) => revocationNote(how, "client"))).toEqual([
      "live tunnel rebuilt without it",
      "previous config rebuilt and reloaded",
      "xray was down — the stored config was rewritten without it, so it cannot come back on the next start",
      "xray stopped — remote access is down for everyone until you reconnect",
      "SECURITY WARNING: xray's stop could not be confirmed, so the revoked device may still be able to connect. The panel keeps retrying; reboot the gateway if this does not clear.",
      "nothing was serving the inbound — no live access to cut",
    ]);
    expect(OUTCOMES.map((how) => revocationNote(how, "save"))).toEqual([
      "live tunnel rebuilt with the narrowed settings",
      "previous config rebuilt and reloaded with the narrowed settings",
      "xray was down — the stored config was rewritten with the narrowed settings, so the old ones cannot come back on the next start",
      "xray stopped — remote access is down for everyone until you reconnect",
      "SECURITY WARNING: xray's stop could not be confirmed, so the settings you just narrowed may still be live. The panel keeps retrying; reboot the gateway if this does not clear.",
      "nothing was serving the inbound — nothing live to narrow",
    ]);
    expect(revocationNote("", "client")).toBe("");
  });

  it("cleaned never reads as not-live, stopped never as stop-failed", () => {
    for (const subject of ["client", "save"] as const) {
      expect(revocationNote("cleaned", subject)).not.toBe(revocationNote("not-live", subject));
      expect(revocationNote("stopped", subject)).not.toBe(revocationNote("stop-failed", subject));
    }
  });

  it("stopped and stop-failed are errors; the rest are not", () => {
    expect(OUTCOMES.filter(revocationIsError)).toEqual(["stopped", "stop-failed"]);
    expect(revocationIsError("")).toBe(false);
  });
});

describe("write messages", () => {
  it("Save: its revocation, else whether a node could take it now", () => {
    expect(rwSavedMessage({ ...RW, revocation: "reapplied" }, true)).toEqual({ text: "saved · live tunnel rebuilt with the narrowed settings", error: false });
    expect(rwSavedMessage({ ...RW, revocation: "stop-failed" }, true)).toEqual({ text: `saved · ${revocationNote("stop-failed", "save")}`, error: true });
    expect(rwSavedMessage({ ...RW, revocation: "stopped" }, false).error).toBe(true);
    expect(rwSavedMessage(RW, true)).toEqual({ text: "saved · rebuilt into the live config", error: false });
    expect(rwSavedMessage(RW, false)).toEqual({ text: "saved · no active node right now, so it applies on the next connect", error: false });
  });

  it("Suspend and Remove: the device and how its revocation went", () => {
    expect(suspendedMessage({ ...RW, revocation: "rebuilt" })).toEqual({ text: "client suspended — its uuid is kept — previous config rebuilt and reloaded", error: false });
    expect(suspendedMessage({ ...RW, revocation: "stopped" })).toEqual({ text: "client suspended — its uuid is kept — xray stopped — remote access is down for everyone until you reconnect", error: true });
    expect(suspendedMessage(RW).text).toBe("client suspended — its uuid is kept");
    expect(removedMessage("ipad", { ...RW, revocation: "not-live" })).toEqual({ text: "removed ipad — nothing was serving the inbound — no live access to cut", error: false });
    expect(removedMessage("ipad", { ...RW, revocation: "stop-failed" }).error).toBe(true);
    expect(removedMessage("ipad", RW).text).toBe("removed ipad");
  });

  it("masks a uuid to its first four characters", () => {
    expect(maskUuid("3f2a9c1e-7b4d-4e8a-9c21-5d6e7f809a1b")).toBe("3f2a…••••");
  });
});
