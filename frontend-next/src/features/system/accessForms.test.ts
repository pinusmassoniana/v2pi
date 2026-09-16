import { describe, expect, it } from "vitest";
import { AUDIT, NOW_SEC, TOKENS } from "../../test/fixtures";
import { actorKind, auditTime, maskedAuditPath, statusTone } from "./audit";
import {
  CURRENT_REQUIRED, MISMATCH, TIMEOUT_MESSAGE, TOO_SHORT, idleTimeoutMessage, parseIdleTimeout, passwordChangedMessage,
  passwordConfirm, passwordFormSchema, passwordFormValid, passwordNote, strength,
} from "./passwordForm";
import {
  EXPIRY_CHOICES, SCOPE_COPY, expiresAt, expiryBadge, expiryHelper, expiryLabel, lastUsedLabel, localDate,
  revokeConfirm, tokenFormSchema,
} from "./tokenForm";

const NOW_MS = NOW_SEC * 1000;

function issues(values: { current: string; next: string; confirm: string }): string[] {
  const result = passwordFormSchema.safeParse(values);
  return result.success ? [] : result.error.issues.map((issue) => issue.message);
}

describe("passwordForm", () => {
  it("wants a current password, eight characters and a matching confirmation", () => {
    expect(issues({ current: "", next: "longenough", confirm: "longenough" })).toEqual([CURRENT_REQUIRED]);
    expect(issues({ current: "old", next: "short", confirm: "short" })).toEqual([TOO_SHORT]);
    expect(issues({ current: "old", next: "longenough", confirm: "different" })).toEqual([MISMATCH]);
    expect(issues({ current: "old", next: "longenough", confirm: "longenough" })).toEqual([]);
    expect(issues({ current: "old", next: "x".repeat(257), confirm: "x".repeat(257) })).toEqual(["at most 256 characters"]);
    expect(passwordFormValid({ current: "old", next: "longenough", confirm: "longenough" })).toBe(true);
    expect(passwordFormValid({ current: "old", next: "longenough", confirm: "" })).toBe(false);
  });

  it("the strength hint is the Svelte meter, band for band", () => {
    expect(strength("")).toBe("");
    expect(strength("short")).toBe("too short (min 8)");
    expect(strength("aaaaaaaa")).toBe("weak");                  // nothing scores
    expect(strength("aaaaAAAA")).toBe("ok");                    // case mix
    expect(strength("aaaaAAA1")).toBe("good");                  // + a digit
    expect(strength("aaaaAAA1!")).toBe("strong");               // + a symbol
    expect(strength("aaaaAAA1!longer")).toBe("strong");         // capped, four points still "strong"
    expect(strength("aaaaaaaaaaaa")).toBe("ok");                // length alone scores one
  });

  it("the confirm names the tokens it will delete — and never invents a number it does not have", () => {
    expect(passwordConfirm(3)).toBe(
      "Change the password? Every other signed-in session is signed out, and all 3 API tokens are deleted — anything using them stops working until you issue new ones.",
    );
    expect(passwordConfirm(1)).toContain("all 1 API token are deleted");
    // Nothing to lose: the clause goes, rather than saying "0 API tokens".
    expect(passwordConfirm(0)).toBe("Change the password? Every other signed-in session is signed out.");
    // The list failed or has not loaded. "not known" is not 0, and this action has no undo.
    expect(passwordConfirm(undefined)).toBe(
      "Change the password? Every other signed-in session is signed out, and every API token is deleted — anything using them stops working until you issue new ones.",
    );
  });

  it("what it says afterwards follows the same rule", () => {
    expect(passwordChangedMessage(3)).toBe("password changed · other sessions signed out · 3 API tokens revoked");
    expect(passwordChangedMessage(1)).toBe("password changed · other sessions signed out · 1 API token revoked");
    expect(passwordChangedMessage(0)).toBe("password changed · other sessions signed out");
    expect(passwordChangedMessage(undefined)).toBe("password changed · other sessions signed out · API tokens revoked");
  });

  it("the card's own note carries the same count", () => {
    expect(passwordNote(3)).toBe(
      "Changing it signs out every other session and deletes all 3 API tokens — this browser stays signed in. Minimum 8 characters; strength is a hint, not a rule.",
    );
    expect(passwordNote(0)).toBe(
      "Changing it signs out every other session — this browser stays signed in. Minimum 8 characters; strength is a hint, not a rule.",
    );
    expect(passwordNote(undefined)).toContain("deletes every API token");
  });
});

describe("the idle timeout", () => {
  it("accepts 0 and any whole number from 1, with the gateway's own message and no invented floor", () => {
    expect(TIMEOUT_MESSAGE).toBe("session_timeout_min must be >= 0");
    expect(parseIdleTimeout("0")).toBe(0);
    expect(parseIdleTimeout("1")).toBe(1);
    expect(parseIdleTimeout(" 30 ")).toBe(30);
    expect(parseIdleTimeout("100000")).toBe(100_000);
    for (const bad of ["", "-1", "1.5", "abc", "1e3", "+5"]) expect(parseIdleTimeout(bad)).toBeNull();
  });

  it("says which way it went", () => {
    expect(idleTimeoutMessage(0)).toBe("idle timeout off");
    expect(idleTimeoutMessage(30)).toBe("idle timeout · 30 min");
  });
});

describe("tokenForm", () => {
  it("wants a name of 1–64 characters and one of the three scopes", () => {
    expect(tokenFormSchema.safeParse({ name: "", scope: "monitor", expiry: "never" }).success).toBe(false);
    expect(tokenFormSchema.safeParse({ name: "   ", scope: "monitor", expiry: "never" }).success).toBe(false);
    expect(tokenFormSchema.safeParse({ name: "x".repeat(64), scope: "monitor", expiry: "never" }).success).toBe(true);
    expect(tokenFormSchema.safeParse({ name: "x".repeat(65), scope: "monitor", expiry: "never" }).success).toBe(false);
    expect(tokenFormSchema.safeParse({ name: "ci", scope: "admin", expiry: "never" }).success).toBe(false);
    for (const scope of ["monitor", "read", "readwrite"]) {
      expect(tokenFormSchema.safeParse({ name: "ci", scope, expiry: "never" }).success).toBe(true);
    }
  });

  it("an expiry is sent as an epoch, and 'never' sends nothing — never a null", () => {
    expect(EXPIRY_CHOICES).toEqual(["never", "30d", "90d", "1y"]);
    expect(expiresAt("never", NOW_MS)).toBeUndefined();
    expect(expiresAt("30d", NOW_MS)).toBe(NOW_SEC + 30 * 86_400);
    expect(expiresAt("90d", NOW_MS)).toBe(NOW_SEC + 90 * 86_400);
    expect(expiresAt("1y", NOW_MS)).toBe(NOW_SEC + 365 * 86_400);
    // `undefined` is not `null`: a strict schema with `int | None` and ge=1 refuses an explicit null.
    expect(expiresAt("never", NOW_MS)).not.toBeNull();
    expect(expiryHelper("30d", NOW_MS)).toBe(`sent as a timestamp · ${localDate(NOW_SEC + 30 * 86_400)}`);
    expect(expiryHelper("never", NOW_MS)).toBe("sent as a timestamp · never expires");
  });

  it("a row's dates: 'never' and '—' are literal, and a near expiry earns a badge", () => {
    expect(expiryLabel(null)).toBe("never");
    expect(expiryLabel(undefined)).toBe("never");
    expect(expiryLabel(NOW_SEC)).toBe(localDate(NOW_SEC));
    expect(expiryBadge(null, NOW_SEC)).toBeNull();
    expect(expiryBadge(NOW_SEC + 30 * 86_400, NOW_SEC)).toBe("in 30 d");
    expect(expiryBadge(NOW_SEC + 86_400, NOW_SEC)).toBe("in 1 d");
    expect(expiryBadge(NOW_SEC + 31 * 86_400, NOW_SEC)).toBeNull();
    expect(expiryBadge(NOW_SEC - 1, NOW_SEC)).toBe("expired");
    // The stamp is throttled to once a minute, and an expired token is refused before it is stamped.
    expect(lastUsedLabel(null)).toBe("—");
    expect(lastUsedLabel(TOKENS[1]!.last_used_at)).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
    expect(TOKENS.map((token) => lastUsedLabel(token.last_used_at)).filter((label) => label === "—")).toHaveLength(1);
  });

  it("says what each scope can actually do, and asks before a revocation", () => {
    expect(SCOPE_COPY.map((row) => row.scope)).toEqual(["monitor", "read", "readwrite"]);
    expect(SCOPE_COPY[0]!.body).toContain("every other path answers 403");
    expect(SCOPE_COPY[1]!.body).toContain("Not a “safe” scope.");
    expect(SCOPE_COPY[2]!.lead).toBe("Every GET plus every mutation, with no CSRF header.");
    expect(revokeConfirm({ name: "ci-deploy" })).toBe("Revoke token “ci-deploy”? Anything using it stops working immediately.");
  });
});

describe("audit", () => {
  it("masks a remote-access client path by route shape, and nothing else", () => {
    const masked = maskedAuditPath("/api/rw/clients/3f2a1c4e-77b0-4d51-9a2e-8c1b6f0d4a75");
    expect(masked).toEqual({ masked: "/api/rw/clients/3f2a1c4e…", full: "/api/rw/clients/3f2a1c4e-77b0-4d51-9a2e-8c1b6f0d4a75" });
    // Not by guessing at uuid-ness: a node id that looks like one is not a credential, and the two
    // sub-routes that DO carry the id are GETs the middleware never records.
    for (const path of [
      "/api/rw/clients",
      "/api/rw/clients/3f2a1c4e-77b0-4d51-9a2e-8c1b6f0d4a75/link",
      "/api/rw/clients/3f2a1c4e-77b0-4d51-9a2e-8c1b6f0d4a75/config",
      "/api/nodes/3f2a1c4e-77b0-4d51-9a2e-8c1b6f0d4a75",
      "/api/settings",
      "/api/tokens/4",
    ]) {
      expect(maskedAuditPath(path)).toBeNull();
    }
    // exactly one row of the fixture list is masked, and its full path is only ever the `full` field
    expect(AUDIT.filter((row) => maskedAuditPath(row.path))).toHaveLength(1);
  });

  it("colours a status by its class, never a success", () => {
    expect([200, 201, 204].map(statusTone)).toEqual(["neutral", "neutral", "neutral"]);
    expect([400, 401, 403, 413, 422].map(statusTone)).toEqual(["warn", "warn", "warn", "warn", "warn"]);
    expect([500, 502].map(statusTone)).toEqual(["bad", "bad"]);
  });

  it("reads the actor's kind off its prefix", () => {
    expect(actorKind("user:admin")).toBe("user");
    expect(actorKind("token:pgwp_Vt9pLs1")).toBe("token");
    expect(actorKind("anon")).toBe("anon");
    expect(actorKind("")).toBe("anon");
    expect(new Set(AUDIT.map((row) => actorKind(row.actor)))).toEqual(new Set(["user", "token", "anon"]));
  });

  it("shows the time of day only", () => {
    expect(auditTime(AUDIT[0]!.ts)).toMatch(/^\d{2}:\d{2}:\d{2}$/);
  });
});
