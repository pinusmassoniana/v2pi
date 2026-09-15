// The anti-DPI profile editor: its fields and limits (the backend's validate_profile and ProfileIn), the mapping to and
// from the API, presets, and the words of its confirmations and results. Pure and unit-tested.
import { z } from "zod";
import type { NoiseSpec, ProfileIn, ProfilePreset, TuningProfile } from "../../api/client";

export const FINGERPRINTS = ["chrome", "firefox", "safari", "ios", "android", "edge", "random", "randomized", "randomizednoalpn", ""] as const;
export type Fingerprint = (typeof FINGERPRINTS)[number];
export const NO_MIMICRY = "(no mimicry — not recommended)";
export const NOISE_TYPES = ["rand", "str", "base64", "hex"] as const;
export type NoiseType = (typeof NOISE_TYPES)[number];
export const QUIC_MODES = ["allow", "drop", "proxy"] as const;
export type QuicMode = (typeof QUIC_MODES)[number];
export const QUIC_LABELS: Readonly<Record<QuicMode, string>> = { allow: "allow", drop: "drop (block)", proxy: "proxy" };
export const XUDP_MODES = ["", "reject", "allow", "skip"] as const;
export type XudpMode = (typeof XUDP_MODES)[number];

/** backend ProfileIn / validate_profile limits. */
export const MAX_NAME = 512;
export const MAX_NOISES = 32;
export const MAX_ALPN = 128;
export const MAX_TLS_VERSION = 16;
export const MAX_URL = 2048;
const MAX_KNOB = 64;
const MAX_NOISE_PACKET = 256;

export const HEADER_HINT =
  "Evasion profiles. Assign one per node from its Edit on the Nodes tab. The profile governing the live tunnel right now is marked ● active.";
export const DISCARD_PROFILE_CONFIRM = "Discard unsaved profile changes?";
export const NO_ACTIVE_NODE = "No active node";

export interface NoiseRow {
  type: NoiseType;
  packet: string;
  delay: string;
}

/** The editor's values. Which profile it saves to (or none, for Create) is kept beside the form, not in it. */
export interface ProfileFormValues {
  name: string;
  fingerprint: Fingerprint;
  frag_enabled: boolean;
  frag_packets: string;
  frag_length: string;
  frag_interval: string;
  noise_enabled: boolean;
  noises: NoiseRow[];
  mux_enabled: boolean;
  mux_concurrency: string;
  xudp_proxy_udp443: XudpMode;
  xhttp_padding: string;
  xmux_max_concurrency: string;
  xmux_max_connections: string;
  alpn: string;
  tls_min: string;
  tls_max: string;
  doh_enabled: boolean;
  doh_url: string;
  quic: QuicMode;
}

/** A new noise row. */
export const NEW_NOISE: Readonly<NoiseRow> = { type: "rand", packet: "50-150", delay: "10-16" };

/**
 * The backend's `_bounded_number`: "N", or "A-B" when a range is allowed, in ASCII digits, every number within
 * [min, max] and A ≤ B. No spaces, no sign.
 */
export function inRange(value: string, min: number, max: number, allowRange = true): boolean {
  const parts = value.split("-");
  if (parts.length > (allowRange ? 2 : 1) || !parts.every((part) => /^\d+$/.test(part))) return false;
  const numbers = parts.map(Number);
  return numbers.every((number) => number >= min && number <= max) && numbers[0]! <= numbers.at(-1)!;
}

export interface ProfileIssue {
  path: (string | number)[];
  message: string;
}

const tooLong = (label: string, max: number) => `${label} is at most ${max} characters`;

/**
 * Everything validate_profile and ProfileIn would refuse, one issue per field, in form order. Fragmentation is checked
 * only while it is on; the other knobs whenever they are filled in, as the backend does.
 */
export function profileIssues(form: ProfileFormValues): ProfileIssue[] {
  const issues: ProfileIssue[] = [];
  const add = (path: (string | number)[], message: string) => issues.push({ path, message });
  if (!form.name.trim()) add(["name"], "name is required");
  else if (form.name.length > MAX_NAME) add(["name"], tooLong("name", MAX_NAME));
  if (form.frag_enabled) {
    if (form.frag_packets.length > MAX_KNOB || !(form.frag_packets === "tlshello" || inRange(form.frag_packets, 1, 65535))) {
      add(["frag_packets"], "packets: tlshello, N or A-B within 1..65535");
    }
    if (form.frag_length.length > MAX_KNOB || !inRange(form.frag_length, 1, 65535)) add(["frag_length"], "length: N or A-B within 1..65535");
    if (form.frag_interval.length > MAX_KNOB || !inRange(form.frag_interval, 0, 60000)) add(["frag_interval"], "interval: N or A-B within 0..60000 ms");
  }
  if (form.noises.length > MAX_NOISES) add(["noises"], `at most ${MAX_NOISES} noise rows`);
  form.noises.forEach((noise, index) => {
    if (noise.packet.length > MAX_NOISE_PACKET) add(["noises", index, "packet"], tooLong("packet", MAX_NOISE_PACKET));
    else if (noise.type === "rand" && noise.packet !== "" && !inRange(noise.packet, 1, 65535)) add(["noises", index, "packet"], "packet: N or A-B within 1..65535");
    if (noise.delay.length > MAX_KNOB || (noise.delay !== "" && !inRange(noise.delay, 0, 60000))) add(["noises", index, "delay"], "delay: N or A-B within 0..60000 ms");
  });
  if (form.mux_concurrency !== "" && !inRange(form.mux_concurrency, 1, 1024, false)) add(["mux_concurrency"], "concurrency: a number within 1..1024");
  const knobs: [keyof ProfileFormValues, string, number][] = [
    ["xhttp_padding", "padding", 1_000_000], ["xmux_max_concurrency", "max concurrency", 65535], ["xmux_max_connections", "max connections", 65535],
  ];
  for (const [field, label, max] of knobs) {
    const value = form[field] as string;
    if (value !== "" && (value.length > MAX_KNOB || !inRange(value, 0, max))) add([field], `${label}: N or A-B within 0..${max.toLocaleString("en-US")}`);
  }
  if (form.alpn.length > MAX_ALPN) add(["alpn"], tooLong("ALPN", MAX_ALPN));
  if (form.tls_min.length > MAX_TLS_VERSION) add(["tls_min"], tooLong("min version", MAX_TLS_VERSION));
  if (form.tls_max.length > MAX_TLS_VERSION) add(["tls_max"], tooLong("max version", MAX_TLS_VERSION));
  if (form.doh_url.length > MAX_URL) add(["doh_url"], tooLong("DoH URL", MAX_URL));
  else if (form.doh_url !== "" && !/^https:\/\/[^/?#\s]+/.test(form.doh_url)) add(["doh_url"], "DoH URL must be https:// with a host, or blank");
  return issues;
}

const noiseSchema = z.object({ type: z.enum(NOISE_TYPES), packet: z.string(), delay: z.string() });

export const profileFormSchema = z
  .object({
    name: z.string(),
    fingerprint: z.enum(FINGERPRINTS),
    frag_enabled: z.boolean(),
    frag_packets: z.string(),
    frag_length: z.string(),
    frag_interval: z.string(),
    noise_enabled: z.boolean(),
    noises: z.array(noiseSchema),
    mux_enabled: z.boolean(),
    mux_concurrency: z.string(),
    xudp_proxy_udp443: z.enum(XUDP_MODES),
    xhttp_padding: z.string(),
    xmux_max_concurrency: z.string(),
    xmux_max_connections: z.string(),
    alpn: z.string(),
    tls_min: z.string(),
    tls_max: z.string(),
    doh_enabled: z.boolean(),
    doh_url: z.string(),
    quic: z.enum(QUIC_MODES),
  })
  .superRefine((form, ctx) => {
    for (const issue of profileIssues(form)) ctx.addIssue({ code: "custom", path: issue.path, message: issue.message });
  }) satisfies z.ZodType<ProfileFormValues, ProfileFormValues>;

/** A new profile with the backend's defaults. */
export function blankProfileForm(): ProfileFormValues {
  return {
    name: "", fingerprint: "chrome",
    frag_enabled: false, frag_packets: "tlshello", frag_length: "100-200", frag_interval: "10-20",
    noise_enabled: false, noises: [],
    mux_enabled: false, mux_concurrency: "", xudp_proxy_udp443: "",
    xhttp_padding: "", xmux_max_concurrency: "", xmux_max_connections: "",
    alpn: "", tls_min: "", tls_max: "",
    doh_enabled: true, doh_url: "", quic: "allow",
  };
}

const copyNoises = (noises: readonly NoiseSpec[]): NoiseRow[] => noises.map((noise) => ({ type: noise.type as NoiseType, packet: noise.packet, delay: noise.delay }));

/** A stored profile as the editor's values. */
export function profileToForm(profile: TuningProfile): ProfileFormValues {
  return {
    name: profile.name, fingerprint: profile.fingerprint as Fingerprint,
    frag_enabled: profile.frag_enabled, frag_packets: profile.frag_packets, frag_length: profile.frag_length, frag_interval: profile.frag_interval,
    noise_enabled: profile.noise_enabled, noises: copyNoises(profile.noises),
    mux_enabled: profile.mux_enabled, mux_concurrency: profile.mux_concurrency, xudp_proxy_udp443: profile.xudp_proxy_udp443 as XudpMode,
    xhttp_padding: profile.xhttp_padding, xmux_max_concurrency: profile.xmux_max_concurrency, xmux_max_connections: profile.xmux_max_connections,
    alpn: profile.alpn, tls_min: profile.tls_min, tls_max: profile.tls_max,
    doh_enabled: profile.doh_enabled, doh_url: profile.doh_url, quic: profile.quic as QuicMode,
  };
}

/** T2 Clone: the profile's values under "‹name› copy", saved as a new profile. */
export function cloneProfileForm(profile: TuningProfile): ProfileFormValues {
  return { ...profileToForm(profile), name: `${profile.name} copy` };
}

/** What Create and Save send: every field (hidden ones keep their values), the name trimmed. */
export function formToProfileIn(values: ProfileFormValues): ProfileIn {
  return { ...values, name: values.name.trim(), noises: copyNoises(values.noises) };
}

/**
 * T4: a preset's fields merged into the editor, keeping its name — or taking the preset's name when the name is empty.
 * Fields the editor does not have are ignored.
 */
export function stagePreset(form: ProfileFormValues, preset: ProfilePreset): ProfileFormValues {
  const next: Record<string, unknown> = { ...form };
  for (const [field, value] of Object.entries(preset.fields)) {
    if (field === "name" || !(field in form)) continue;
    next[field] = field === "noises" && Array.isArray(value) ? copyNoises(value as NoiseSpec[]) : value;
  }
  next.name = form.name.trim() ? form.name : preset.name;
  return next as unknown as ProfileFormValues;
}

/** T2 Delete's confirmation, word for word. */
export function deleteProfileMessage(profile: Pick<TuningProfile, "name" | "node_count">): string {
  const fallback = profile.node_count > 0 ? `\n${profile.node_count} node(s) using it fall back to the default.` : "";
  return `Delete profile "${profile.name}"?${fallback}`;
}

/** The question before ⚡ Apply to active. */
export function applyActiveConfirmMessage(profileName: string, nodeName: string): string {
  return `Apply ${profileName} to ${nodeName} and re-apply the tunnel now? Devices may drop briefly.`;
}

/** T5: what a save says — the live tunnel re-applied, or just saved. */
export function profileSavedMessage(saved: Pick<TuningProfile, "is_active">): string {
  return saved.is_active ? "saved & applied to the live tunnel" : "saved";
}

/** T4: said once a preset is in the editor. */
export function presetStagedMessage(name: string): string {
  return `preset "${name}" staged into the editor — Create/Save to apply`;
}

/** The collapsible editor sections, in contract order ("profile" — name and fingerprint — never collapses). */
export const PROFILE_SECTIONS = ["fragmentation", "noise", "mux", "xhttp", "tls", "dns"] as const;
export type ProfileSection = (typeof PROFILE_SECTIONS)[number];

export interface SectionState {
  /** Opened on a phone when the editor loads: the section holds a feature that is on or a value that was set. */
  open: boolean;
  /** The collapsed header's short state. */
  summary: string;
}

/** Which sections a phone opens for these values, and what each collapsed header says. */
export function sectionStates(form: ProfileFormValues): Record<ProfileSection, SectionState> {
  const xhttp = form.xhttp_padding !== "" || form.xmux_max_concurrency !== "" || form.xmux_max_connections !== "";
  const tls = form.alpn !== "" || form.tls_min !== "" || form.tls_max !== "";
  return {
    fragmentation: { open: form.frag_enabled, summary: form.frag_enabled ? "on" : "off" },
    noise: { open: form.noise_enabled, summary: form.noise_enabled ? `on · ${form.noises.length} row(s)` : "off" },
    mux: { open: form.mux_enabled, summary: form.mux_enabled ? "on" : "off" },
    xhttp: { open: xhttp, summary: xhttp ? "custom" : "defaults" },
    tls: { open: tls, summary: tls ? "custom" : "defaults" },
    dns: { open: form.doh_enabled || form.quic !== "allow", summary: `DoH ${form.doh_enabled ? "on" : "off"} · QUIC ${form.quic}` },
  };
}

/** Which section holds a field, for opening the one an invalid submit's issue lives in. "Profile" never collapses, so it needs no entry. */
const ISSUE_SECTIONS: Partial<Record<string, ProfileSection>> = {
  frag_packets: "fragmentation", frag_length: "fragmentation", frag_interval: "fragmentation",
  noises: "noise",
  mux_concurrency: "mux",
  xhttp_padding: "xhttp", xmux_max_concurrency: "xhttp", xmux_max_connections: "xhttp",
  alpn: "tls", tls_min: "tls", tls_max: "tls",
  doh_url: "dns",
};

/**
 * T5 (fix round 1): the sections holding at least one of `issues`' fields — a hidden switch or a collapsed phone
 * section can still carry an invalid value, since the backend checks fragmentation only while its toggle is on but
 * checks noises, Mux concurrency and the other knobs whether or not their section is open.
 */
export function issueSections(issues: readonly ProfileIssue[]): ProfileSection[] {
  const sections = new Set<ProfileSection>();
  for (const issue of issues) {
    const section = ISSUE_SECTIONS[String(issue.path[0])];
    if (section) sections.add(section);
  }
  return [...sections];
}
