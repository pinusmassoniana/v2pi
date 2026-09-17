import { describe, expect, it } from "vitest";
import { ApiError, type BackupFile, type RestoreResult } from "../../api/client";
import { BACKUP_FILES, RESTORE_RESULT } from "../../test/fixtures";
import {
  MAX_RESTORE_BYTES, backupFilename, backupPreChecks, backupWhen, checksPass, condensedBackupChecks, fileTooLarge,
  newestPreRestore, restoreConfirm, restoreRefusedMessage, restoredMessage, snapshotNote, undoConfirm,
} from "./backupFile";

const DOC = JSON.stringify({ schema_version: 2, profiles: [{ name: "p" }], routing: { rules: [], default_action: "proxy" } });

function result(over: Partial<RestoreResult["restored"]> = {}): RestoreResult {
  return { ...RESTORE_RESULT, restored: { ...RESTORE_RESULT.restored, ...over } };
}

describe("backupFilename", () => {
  it("names the file for the day it was taken, zero-padded", () => {
    expect(backupFilename(new Date(2026, 8, 6))).toBe("v2pi-backup-2026-09-06.json");
    expect(backupFilename(new Date(2026, 11, 31))).toBe("v2pi-backup-2026-12-31.json");
  });
});

describe("backupPreChecks", () => {
  it("passes a real document and says its size", () => {
    const checks = backupPreChecks(DOC, 214_000);
    expect(checks.map((check) => check.ok)).toEqual([true, true, true, true]);
    expect(checks.map((check) => check.label)).toEqual([
      "parses as JSON", "an object, not an array", "carries a schema_version", "214 KB — under the 2 MB the gateway accepts",
    ]);
    expect(checksPass(checks)).toBe(true);
  });

  it("a document with no nodes at all is valid and is sent", () => {
    // BackupDocument.nodes defaults to [] — the Svelte panel's extra `Array.isArray(nodes)` check
    // refused a gateway's own export in the browser and never sent it.
    const checks = backupPreChecks(JSON.stringify({ schema_version: 2 }), 100);
    expect(checksPass(checks)).toBe(true);
    expect(JSON.stringify(checks)).not.toContain("nodes");
  });

  it("names which one failed, and nothing else", () => {
    expect(backupPreChecks("{oops", 100).map((c) => c.ok)).toEqual([false, false, false, true]);
    expect(backupPreChecks("[1,2]", 100).map((c) => c.ok)).toEqual([true, false, false, true]);
    expect(backupPreChecks("null", 100).map((c) => c.ok)).toEqual([true, false, false, true]);
    expect(backupPreChecks('"a string"', 100).map((c) => c.ok)).toEqual([true, false, false, true]);
    expect(backupPreChecks(JSON.stringify({ nodes: [] }), 100).map((c) => c.ok)).toEqual([true, true, false, true]);
    expect(checksPass(backupPreChecks(JSON.stringify({ nodes: [] }), 100))).toBe(false);
  });

  it("the 2 MB boundary is the middleware's, to the byte", () => {
    expect(MAX_RESTORE_BYTES).toBe(2 * 1024 * 1024);
    expect(backupPreChecks(DOC, MAX_RESTORE_BYTES)[3]!.ok).toBe(true);
    const over = backupPreChecks(DOC, MAX_RESTORE_BYTES + 1)[3]!;
    expect(over.ok).toBe(false);
    expect(over.label).toBe("2.1 MB — over the 2 MB the gateway accepts");
  });

  it("the phone's three lines answer the same four questions", () => {
    expect(condensedBackupChecks(DOC, 214_000)).toEqual([
      { ok: true, label: "parses as JSON, and is an object" },
      { ok: true, label: "carries a schema_version" },
      { ok: true, label: "214 KB — under the 2 MB limit" },
    ]);
    expect(condensedBackupChecks("[1]", 214_000)[0]).toEqual({ ok: false, label: "parses as JSON, and is an object" });
    expect(condensedBackupChecks(DOC, MAX_RESTORE_BYTES + 1)[2]!.label).toBe("2.1 MB — over the 2 MB limit");
  });
});

describe("fileTooLarge", () => {
  it("only above the cap, and says why the gateway will not even look at it", () => {
    expect(fileTooLarge(MAX_RESTORE_BYTES)).toBeNull();
    expect(fileTooLarge(MAX_RESTORE_BYTES + 1)).toEqual({
      title: "That file is larger than 2 MB",
      text: "the gateway refuses it before it even looks at your session. Export a fresh backup instead of editing an old one.",
    });
  });
});

describe("restoreConfirm", () => {
  it("is the contract's sentence, then which file it means", () => {
    const text = restoreConfirm("v2pi-backup-2026-09-14.json", 214_000);
    expect(text).toContain(
      "Restore replaces every node, subscription, anti-DPI profile, routing rule and panel setting with the ones in this file, and disconnects the gateway.",
    );
    expect(text).toContain("The Reality private key and the remote-access client list are not restored. Continue?");
    expect(text.endsWith("\n\nv2pi-backup-2026-09-14.json · 214 KB")).toBe(true);
  });
});

describe("restoredMessage", () => {
  it("counts what came back and says the gateway is down, which is the normal outcome", () => {
    expect(restoredMessage(RESTORE_RESULT)).toEqual({
      message: "restored 24 nodes, 3 subscriptions, 6 profiles, 11 routing rules — the gateway is disconnected; Connect a node when you are ready",
      tone: "ok",
    });
  });

  it("a restore that turned remote access off is a warning, not a success", () => {
    const disabled = restoredMessage(result({ rw_disabled: "public key mismatch" }));
    expect(disabled.tone).toBe("warn");
    expect(disabled.message.endsWith(" · remote access was turned off: public key mismatch")).toBe(true);
  });

  it("names the snapshot as a line, since no route reads it back", () => {
    expect(snapshotNote(RESTORE_RESULT)).toBe(
      `A copy of what this replaced was saved on the gateway at ${RESTORE_RESULT.pre_restore_snapshot}.`,
    );
  });
});

describe("restoreRefusedMessage", () => {
  it("a model-level refusal loses both technical fragments", () => {
    // Every reference check, the settings-value check, the DHCP-range check and the lockout guard raise
    // from a model validator, whose `loc` is empty — hence the bare ": Value error, ".
    const error = new ApiError(400, "invalid backup: : Value error, the gateway segment in this file overlaps the network you are connected from");
    expect(restoreRefusedMessage(error)).toEqual({
      message: "not restored — the gateway segment in this file overlaps the network you are connected from",
      sticky: false,
    });
  });

  it("a nested model-level refusal keeps its loc but loses the technical fragment", () => {
    // BackupSubscription.bounded_injection is a model_validator on a child model, so its loc is
    // non-empty ("subscriptions.0") — "Value error, " lands after that loc, not at the very start.
    const error = new ApiError(400, "invalid backup: subscriptions.0: Value error, subscription injection is too large");
    expect(restoreRefusedMessage(error)).toEqual({
      message: "not restored — subscriptions.0: subscription injection is too large",
      sticky: false,
    });
  });

  it("a field refusal keeps its field", () => {
    expect(restoreRefusedMessage(new ApiError(400, "invalid backup: settings.health_interval: Input should be a valid integer"))).toEqual({
      message: "not restored — settings.health_interval: Input should be a valid integer",
      sticky: false,
    });
  });

  it("a body that is not a backup at all keeps the handler's own sentence", () => {
    expect(restoreRefusedMessage(new ApiError(400, "not a valid backup file")).message).toBe("not restored — not a valid backup file");
  });

  it("a 502 says the apply failed, and stays on screen when it names a recovery", () => {
    const plain = restoreRefusedMessage(new ApiError(502, "host provisioning failed: nft: no such file or directory"));
    expect(plain).toEqual({
      message: "not restored — the gateway could not apply it: host provisioning failed: nft: no such file or directory",
      sticky: false,
    });
    const recovered = restoreRefusedMessage(new ApiError(502, "host provisioning failed: nft missing; recovery: the previous configuration was put back and the guard reinstalled"));
    expect(recovered.sticky).toBe(true);
    expect(recovered.message.startsWith("not restored — the gateway could not apply it: ")).toBe(true);
  });
});


describe("the copies the gateway holds (A8)", () => {
  const snapshot = (createdAt: number): BackupFile =>
    ({ name: `pre-restore-${createdAt}-${"a".repeat(32)}.json`, bytes: 1, created_at: createdAt, kind: "pre-restore" });

  it("reads the stamp in the name on the viewer's clock", () => {
    expect(backupWhen(1_700_000_000)).toBe(new Date(1_700_000_000_000).toLocaleString([], {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }));
  });

  it("finds the snapshot an undo would use — the first pre-restore, because the list is newest first", () => {
    expect(newestPreRestore(BACKUP_FILES)).toBe(BACKUP_FILES[0]);
    expect(newestPreRestore([snapshot(2), snapshot(1)])!.created_at).toBe(2);
    expect(newestPreRestore(BACKUP_FILES.filter((file) => file.kind === "auto"))).toBeNull();
    expect(newestPreRestore([])).toBeNull();
  });

  it("names what the undo puts back, and says it replaces everything again", () => {
    const question = undoConfirm(snapshot(1_700_000_000));
    expect(question).toContain(backupWhen(1_700_000_000));
    expect(question).toContain("replaces everything again");
    expect(question).toContain("A copy of what it replaces is saved first");
  });
});
