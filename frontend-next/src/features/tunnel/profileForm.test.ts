import { describe, expect, it } from "vitest";
import { PROFILE_PRESETS, TUNNEL_PROFILES } from "../../test/fixtures";
import {
  FINGERPRINTS, NEW_NOISE, QUIC_LABELS, applyActiveConfirmMessage, blankProfileForm, cloneProfileForm, deleteProfileMessage, formToProfileIn,
  inRange, presetStagedMessage, profileFormSchema, profileIssues, profileSavedMessage, profileToForm, sectionStates, stagePreset,
  type ProfileFormValues,
} from "./profileForm";

const [balanced, fragment, muxHeavy] = TUNNEL_PROFILES as [typeof TUNNEL_PROFILES[0], typeof TUNNEL_PROFILES[0], typeof TUNNEL_PROFILES[0]];

/** The issues for a blank profile named "p" with `patch` applied, as path → message. */
function issuesOf(patch: Partial<ProfileFormValues>): Record<string, string> {
  const form = { ...blankProfileForm(), name: "p", ...patch };
  return Object.fromEntries(profileIssues(form).map((issue) => [issue.path.join("."), issue.message]));
}

describe("inRange (the backend's _bounded_number)", () => {
  it("N or A-B inside the bounds, A ≤ B, ASCII digits only", () => {
    expect(inRange("1", 1, 65535)).toBe(true);
    expect(inRange("65535", 1, 65535)).toBe(true);
    expect(inRange("1-3", 1, 65535)).toBe(true);
    expect(inRange("5-5", 1, 65535)).toBe(true);
    expect(inRange("0", 1, 65535)).toBe(false);
    expect(inRange("65536", 1, 65535)).toBe(false);
    expect(inRange("3-1", 1, 65535)).toBe(false);
    expect(inRange("1-65536", 1, 65535)).toBe(false);
    for (const bad of ["", "-", "1-", "-1", "1-2-3", " 1", "1 -2", "+1", "1.5", "a", "١"]) expect([bad, inRange(bad, 0, 65535)]).toEqual([bad, false]);
  });

  it("a single number only, when a range is not allowed", () => {
    expect(inRange("8", 1, 1024, false)).toBe(true);
    expect(inRange("1-8", 1, 1024, false)).toBe(false);
  });
});

describe("profile limits", () => {
  it("a blank profile with a name passes, and so does every stored profile", () => {
    expect(issuesOf({})).toEqual({});
    for (const profile of TUNNEL_PROFILES) expect(profileIssues(profileToForm(profile))).toEqual([]);
  });

  it("name: required, at most 512", () => {
    expect(issuesOf({ name: "  " })).toEqual({ name: "name is required" });
    expect(issuesOf({ name: "n".repeat(512) })).toEqual({});
    expect(issuesOf({ name: "n".repeat(513) })).toEqual({ name: "name is at most 512 characters" });
  });

  it("fragmentation while on: packets tlshello or 1..65535, length 1..65535, interval 0..60000 — every bound", () => {
    const on = { frag_enabled: true };
    expect(issuesOf({ ...on, frag_packets: "tlshello" })).toEqual({});
    expect(issuesOf({ ...on, frag_packets: "1-3", frag_length: "1-65535", frag_interval: "0-60000" })).toEqual({});
    expect(issuesOf({ ...on, frag_packets: "0", frag_length: "0", frag_interval: "60001" })).toEqual({
      frag_packets: "packets: tlshello, N or A-B within 1..65535",
      frag_length: "length: N or A-B within 1..65535",
      frag_interval: "interval: N or A-B within 0..60000 ms",
    });
    expect(issuesOf({ ...on, frag_packets: "TLSHello", frag_length: "65536", frag_interval: "-1" })).toHaveProperty("frag_packets");
    expect(Object.keys(issuesOf({ ...on, frag_packets: "", frag_length: "", frag_interval: "" }))).toEqual(["frag_packets", "frag_length", "frag_interval"]);
    expect(issuesOf({ frag_enabled: false, frag_packets: "", frag_length: "0", frag_interval: "x" })).toEqual({});
  });

  it("noise rows: at most 32; packet range-checked only for rand; delay 0..60000 when set", () => {
    expect(issuesOf({ noises: Array.from({ length: 32 }, () => ({ ...NEW_NOISE })) })).toEqual({});
    expect(issuesOf({ noises: Array.from({ length: 33 }, () => ({ ...NEW_NOISE })) })).toEqual({ noises: "at most 32 noise rows" });
    expect(issuesOf({
      noises: [
        { type: "rand", packet: "0-10", delay: "" },
        { type: "hex", packet: "0a0b0c0d", delay: "60001" },
        { type: "str", packet: "hello", delay: "0" },
        { type: "rand", packet: "", delay: "10-16" },
        { type: "base64", packet: "p".repeat(257), delay: "10" },
      ],
    })).toEqual({
      "noises.0.packet": "packet: N or A-B within 1..65535",
      "noises.1.delay": "delay: N or A-B within 0..60000 ms",
      "noises.4.packet": "packet is at most 256 characters",
    });
    expect(issuesOf({ noises: [{ type: "rand", packet: "1-65535", delay: "0-60000" }] })).toEqual({});
  });

  it("mux concurrency: one number within 1..1024 when set, whether mux is on or not", () => {
    expect(issuesOf({ mux_concurrency: "" })).toEqual({});
    expect(issuesOf({ mux_concurrency: "1" })).toEqual({});
    expect(issuesOf({ mux_concurrency: "1024" })).toEqual({});
    for (const bad of ["0", "1025", "1-8"]) expect(issuesOf({ mux_concurrency: bad })).toEqual({ mux_concurrency: "concurrency: a number within 1..1024" });
  });

  it("XHTTP: padding 0..1,000,000 and the xmux fields 0..65535, ranges allowed, blank = off", () => {
    expect(issuesOf({ xhttp_padding: "0-1000000", xmux_max_concurrency: "0", xmux_max_connections: "16-65535" })).toEqual({});
    expect(issuesOf({ xhttp_padding: "1000001", xmux_max_concurrency: "65536", xmux_max_connections: "x" })).toEqual({
      xhttp_padding: "padding: N or A-B within 0..1,000,000",
      xmux_max_concurrency: "max concurrency: N or A-B within 0..65,535",
      xmux_max_connections: "max connections: N or A-B within 0..65,535",
    });
  });

  it("TLS: ALPN at most 128, versions at most 16", () => {
    expect(issuesOf({ alpn: "a".repeat(128), tls_min: "1".repeat(16), tls_max: "1.3" })).toEqual({});
    expect(issuesOf({ alpn: "a".repeat(129), tls_min: "1".repeat(17), tls_max: "1".repeat(17) })).toEqual({
      alpn: "ALPN is at most 128 characters", tls_min: "min version is at most 16 characters", tls_max: "max version is at most 16 characters",
    });
  });

  it("DoH URL: https:// with a host, or blank — checked even while DoH is off", () => {
    expect(issuesOf({ doh_url: "" })).toEqual({});
    expect(issuesOf({ doh_url: "https://1.1.1.1/dns-query" })).toEqual({});
    for (const bad of ["http://1.1.1.1/dns-query", "https://", "https:///dns-query", "8.8.8.8", "HTTPS://dns.example"]) {
      expect(issuesOf({ doh_enabled: false, doh_url: bad })).toEqual({ doh_url: "DoH URL must be https:// with a host, or blank" });
    }
    expect(issuesOf({ doh_url: `https://${"a".repeat(2041)}` })).toEqual({ doh_url: "DoH URL is at most 2048 characters" });
  });

  it("the zod schema reports every issue at its field, and refuses unknown fingerprints and QUIC modes", () => {
    const result = profileFormSchema.safeParse({ ...blankProfileForm(), name: "", frag_enabled: true, frag_length: "0", mux_concurrency: "2000" });
    expect(result.success).toBe(false);
    expect(result.error?.issues.map((issue) => [issue.path.join("."), issue.message])).toEqual([
      ["name", "name is required"], ["frag_length", "length: N or A-B within 1..65535"], ["mux_concurrency", "concurrency: a number within 1..1024"],
    ]);
    expect(profileFormSchema.safeParse({ ...blankProfileForm(), name: "p", fingerprint: "opera" }).success).toBe(false);
    expect(profileFormSchema.safeParse({ ...blankProfileForm(), name: "p", quic: "block" }).success).toBe(false);
    expect(profileFormSchema.safeParse({ ...blankProfileForm(), name: "p", fingerprint: "" }).success).toBe(true);
    expect(FINGERPRINTS).toEqual(["chrome", "firefox", "safari", "ios", "android", "edge", "random", "randomized", "randomizednoalpn", ""]);
    expect(QUIC_LABELS.drop).toBe("drop (block)");
  });
});

describe("mapping", () => {
  it("a blank profile carries the backend's defaults", () => {
    expect(blankProfileForm()).toEqual({
      name: "", fingerprint: "chrome", frag_enabled: false, frag_packets: "tlshello", frag_length: "100-200", frag_interval: "10-20",
      noise_enabled: false, noises: [], mux_enabled: false, mux_concurrency: "", xudp_proxy_udp443: "", xhttp_padding: "",
      xmux_max_concurrency: "", xmux_max_connections: "", alpn: "", tls_min: "", tls_max: "", doh_enabled: true, doh_url: "", quic: "allow",
    });
    expect(NEW_NOISE).toEqual({ type: "rand", packet: "50-150", delay: "10-16" });
  });

  it("a stored profile maps to the form and back to what Save sends, every field", () => {
    const form = profileToForm(muxHeavy);
    expect(form).toMatchObject({ name: "mux-heavy", fingerprint: "firefox", mux_enabled: true, mux_concurrency: "8", xudp_proxy_udp443: "skip", quic: "proxy", tls_max: "1.3" });
    const sent = formToProfileIn(form);
    expect({ ...muxHeavy, ...sent }).toEqual(muxHeavy);
    expect(Object.keys(sent).sort()).toEqual(Object.keys(muxHeavy).filter((key) => !["id", "is_default", "is_active", "node_count"].includes(key)).sort());
    expect(formToProfileIn({ ...form, name: "  spaced  " }).name).toBe("spaced");
  });

  it("noise rows are copied, never shared with the profile they came from", () => {
    const form = profileToForm(fragment);
    expect(form.noises).toEqual(fragment.noises);
    form.noises[0]!.packet = "1-2";
    expect(fragment.noises[0]!.packet).toBe("50-150");
    const sent = formToProfileIn(form);
    sent.noises![1]!.delay = "0";
    expect(form.noises[1]!.delay).toBe("20-40");
  });

  it("a clone keeps the values under the name + \" copy\"", () => {
    expect(cloneProfileForm(fragment)).toEqual({ ...profileToForm(fragment), name: "fragment-tls copy" });
  });
});

describe("presets", () => {
  const [ruHardened, stealth] = PROFILE_PRESETS as [typeof PROFILE_PRESETS[0], typeof PROFILE_PRESETS[0]];

  it("merges the preset's fields over the editor, keeping its name", () => {
    const staged = stagePreset(profileToForm(muxHeavy), ruHardened);
    expect(staged).toEqual({
      ...profileToForm(muxHeavy),
      fingerprint: "chrome", frag_enabled: true, frag_packets: "tlshello", frag_length: "100-200", frag_interval: "10-20", quic: "drop",
      noise_enabled: true, noises: [{ type: "rand", packet: "50-150", delay: "10-16" }],
    });
    expect(staged.noises).not.toBe(ruHardened.fields.noises);
  });

  it("takes the preset's name when the editor has none; ignores fields the editor does not have", () => {
    expect(stagePreset(blankProfileForm(), stealth).name).toBe("stealth-latency");
    expect(stagePreset({ ...blankProfileForm(), name: "   " }, stealth).name).toBe("stealth-latency");
    const odd = stagePreset(profileToForm(balanced), { name: "x", title: "X", fields: { name: "renamed", id: 99, is_default: true, quic: "drop" } });
    expect(odd).toEqual({ ...profileToForm(balanced), quic: "drop" });
  });

  it("says what it staged", () => {
    expect(presetStagedMessage("ru-hardened")).toBe('preset "ru-hardened" staged into the editor — Create/Save to apply');
  });
});

describe("words", () => {
  it("delete asks with the node count only when nodes use the profile", () => {
    expect(deleteProfileMessage(fragment)).toBe('Delete profile "fragment-tls"?\n3 node(s) using it fall back to the default.');
    expect(deleteProfileMessage(muxHeavy)).toBe('Delete profile "mux-heavy"?');
  });

  it("apply to active asks before moving the live tunnel", () => {
    expect(applyActiveConfirmMessage("mux-heavy", "nl-ams-03")).toBe("Apply mux-heavy to nl-ams-03 and re-apply the tunnel now? Devices may drop briefly.");
  });

  it("a save of the live profile says it was applied", () => {
    expect(profileSavedMessage({ is_active: true })).toBe("saved & applied to the live tunnel");
    expect(profileSavedMessage({ is_active: false })).toBe("saved");
  });
});

describe("phone sections", () => {
  it("opens the sections holding a feature that is on or a value that was set", () => {
    expect(sectionStates(blankProfileForm())).toEqual({
      fragmentation: { open: false, summary: "off" },
      noise: { open: false, summary: "off" },
      mux: { open: false, summary: "off" },
      xhttp: { open: false, summary: "defaults" },
      tls: { open: false, summary: "defaults" },
      dns: { open: true, summary: "DoH on · QUIC allow" },
    });
    expect(sectionStates(profileToForm(fragment))).toMatchObject({
      fragmentation: { open: true, summary: "on" }, noise: { open: true, summary: "on · 2 row(s)" }, dns: { open: true, summary: "DoH on · QUIC drop" },
    });
    expect(sectionStates(profileToForm(muxHeavy))).toMatchObject({
      mux: { open: true, summary: "on" }, xhttp: { open: true, summary: "custom" }, tls: { open: true, summary: "custom" }, dns: { open: true, summary: "DoH off · QUIC proxy" },
    });
    expect(sectionStates({ ...blankProfileForm(), doh_enabled: false }).dns).toEqual({ open: false, summary: "DoH off · QUIC allow" });
  });
});
