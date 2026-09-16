// Gateway › Remote access: the inbound form's values, the full-replace body Save sends, the backend's rules in its own
// words (rw_inbound.py, schemas.py RwIn, routes.py put_rw), when a save provably narrows access, the SNI check and the
// warnings. Pure and unit-tested.
import { z } from "zod";
import type { Rw, RwIn } from "../../api/client";
import { ipv4Number, ipv4Span, isIPv6Address } from "../../lib/ip";

export interface HostRow {
  name: string;
  ip: string;
}

export interface RwFormValues {
  enabled: boolean;
  /** As typed; sent as a number. */
  port: string;
  endpoint: string;
  dest: string;
  serverNames: string[];
  shortIds: string[];
  publicKey: string;
  /** Write-only: never read from the gateway, cleared after a save. "" keeps the stored key. */
  privateKey: string;
  hosts: HostRow[];
  /** The routed-subnets override, as a csv; "" derives them from the network plan. */
  routedNets: string;
}

/** A csv as the backend splits it: on commas, trimmed, empty entries dropped. */
export function parseCsv(raw: string): string[] {
  return raw.split(",").map((part) => part.trim()).filter(Boolean);
}

/** The saved inbound as form values. The private key always starts blank: the gateway never sends it. */
export function rwToForm(rw: Rw): RwFormValues {
  return {
    enabled: rw.enabled, port: String(rw.port), endpoint: rw.endpoint, dest: rw.dest, serverNames: parseCsv(rw.server_names),
    shortIds: parseCsv(rw.short_ids), publicKey: rw.public_key, privateKey: "",
    hosts: Object.entries(rw.hosts).map(([name, ip]) => ({ name, ip })), routedNets: rw.routed_nets_override,
  };
}

/** A host row with anything typed in it; a blank row is left out of the save. */
function filledRow(row: HostRow): boolean {
  return row.name.trim() !== "" || row.ip.trim() !== "";
}

/** A host name as the backend stores it: trimmed, lower-cased, trailing dots dropped. */
export function normalizeHostName(name: string): string {
  return name.trim().toLowerCase().replace(/\.+$/, "");
}

/**
 * PUT /rw is a full replace — every field it omits resets to its default — so Save always sends every field: the
 * routed-subnets override as typed, and the private key as typed or "" to keep the stored one.
 */
export function formToRwIn(form: RwFormValues): RwIn {
  return {
    enabled: form.enabled,
    port: Number(form.port.trim()),
    dest: form.dest.trim(),
    server_names: form.serverNames.join(","),
    short_ids: form.shortIds.join(","),
    public_key: form.publicKey.trim(),
    endpoint: form.endpoint.trim(),
    private_key: form.privateKey.trim(),
    hosts: Object.fromEntries(form.hosts.filter(filledRow).map((row) => [row.name.trim(), row.ip.trim()])),
    routed_nets: form.routedNets.trim(),
  };
}

/** A string as Python's repr() prints it in the backend's messages: single quotes unless it holds only single quotes. */
export function pyRepr(value: string): string {
  if (value.includes("'") && !value.includes('"')) return `"${value.replace(/\\/g, "\\\\")}"`;
  return `'${value.replace(/\\/g, "\\\\").replace(/'/g, "\\'")}'`;
}

const MAX_DNS_NAME = 253;
const MAX_FIELD = 512;
export const MAX_HOSTS = 32;
export const MAX_HOST_NAME = 40;
export const MAX_CLIENTS = 16;
const ANY_HOSTNAME = /^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*$/;
const DOTTED_HOSTNAME = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$/;
const B64_KEY = /^[A-Za-z0-9+/_-]{43}=?$/;
const SHORT_ID = /^[0-9a-fA-F]+$/;

/**
 * Focus after removing item `index` out of `previousCount`: the index of the item that took its place, else the one
 * before it, else null once none remain. Shared by HostRows (react-hook-form indices) and the remote-access client
 * list (DOM order), which both restore focus this same way after a row disappears.
 */
export function focusIndexAfterRemove(index: number, previousCount: number): number | null {
  const remaining = previousCount - 1;
  return remaining > 0 ? Math.min(Math.max(index, 0), remaining - 1) : null;
}

/** rw_inbound.validate_hostname: a DNS name safe to put in the generated artifacts. */
function hostnameIssue(name: string, what: string): string | null {
  if (name === "") return `${what} must not be empty`;
  if (name.length > MAX_DNS_NAME) return `${what} is longer than ${MAX_DNS_NAME} characters`;
  if (!ANY_HOSTNAME.test(name) || name.split(".").some((label) => label.length > 63)) {
    return `${what} must be a host name (letters, digits, dashes and dots), got ${pyRepr(name)}`;
  }
  return null;
}

/** An integer as Python's int() reads one (surrounding spaces and a sign allowed); null otherwise. */
function pyInt(text: string): number | null {
  return /^\s*[+-]?\d+\s*$/.test(text) ? Number(text) : null;
}

/** rw_inbound.validate_port, as the listen port field reads. */
export function portIssue(raw: string): string | null {
  const port = pyInt(raw);
  if (port === null) return `rw_port must be an integer, got ${pyRepr(raw.trim())}`;
  return port >= 1 && port <= 65535 ? null : `rw_port out of range: ${port}`;
}

/** rw_inbound.validate_endpoint: "", a bracketed IPv6 literal, an IPv4 address or a host name. */
export function endpointIssue(raw: string): string | null {
  const endpoint = raw.trim();
  if (endpoint === "") return null;
  if (endpoint.startsWith("[") && endpoint.endsWith("]")) {
    return isIPv6Address(endpoint.slice(1, -1)) ? null : `the external endpoint has an invalid IPv6 literal: ${pyRepr(endpoint)}`;
  }
  if (ipv4Number(endpoint) !== null) return null;
  return hostnameIssue(endpoint, "the external endpoint");
}

/** rw_inbound.validate_dest: "" or host:port, the host a name or a bracketed IPv6 literal. */
export function destIssue(raw: string): string | null {
  const dest = raw.trim();
  if (dest === "") return null;
  const colon = dest.lastIndexOf(":");
  const host = colon < 0 ? "" : dest.slice(0, colon);
  const port = colon < 0 ? "" : dest.slice(colon + 1);
  if (colon < 0 || host === "" || port === "") return `rw_dest must be host:port (e.g. www.microsoft.com:443), got ${pyRepr(dest)}`;
  if (host.startsWith("[") && host.endsWith("]")) {
    if (!isIPv6Address(host.slice(1, -1))) return `rw_dest has an invalid IPv6 literal, got ${pyRepr(dest)}`;
  } else {
    const issue = hostnameIssue(host.trim(), "the rw_dest host");
    if (issue) return issue;
  }
  const parsed = pyInt(port);
  if (parsed === null) return `rw_dest port must be an integer, got ${pyRepr(port)}`;
  return parsed >= 1 && parsed <= 65535 ? null : `rw_dest port out of range: ${parsed}`;
}

/** The dest as the backend stores it once valid: its host and the port as a plain number ("a.b:0443" → "a.b:443"). */
export function normalizedDest(raw: string): string {
  const dest = raw.trim();
  const colon = dest.lastIndexOf(":");
  if (colon < 0 || destIssue(dest) !== null) return dest;
  return `${dest.slice(0, colon)}:${Number(dest.slice(colon + 1))}`;
}

/** rw_inbound.validate_key: 43 base64 characters (standard or url alphabet, one optional "=") decoding to 32 bytes. */
export function keyIssue(raw: string, which: "public" | "private"): string | null {
  const key = raw.trim();
  if (key === "") return null;
  const what = `the Reality ${which} key`;
  if (!B64_KEY.test(key)) return `${what} must be a base64 x25519 key — 43 characters, exactly as \`xray x25519\` prints it`;
  const bytes = atob(`${key.replace(/=$/, "").replace(/-/g, "+").replace(/_/g, "/")}=`).length;
  return bytes === 32 ? null : `${what} must decode to 32 bytes, got ${bytes}`;
}

export function serverNameIssue(name: string): string | null {
  return hostnameIssue(name, "server name");
}

export function shortIdIssue(id: string): string | null {
  return id.length % 2 === 0 && id.length >= 2 && id.length <= 16 && SHORT_ID.test(id) ? null : `short id must be 2-16 hex chars of even length, got ${pyRepr(id)}`;
}

/** One host row's problem (rw_inbound.validate_hosts, the half-filled check, and owner answer 3's duplicate). */
export function hostRowIssue(row: HostRow, earlier: readonly HostRow[]): { field: "name" | "ip"; message: string } | null {
  const rawName = row.name.trim();
  const ip = row.ip.trim();
  if (!filledRow(row)) return null;
  if (rawName === "" || ip === "") return { field: rawName === "" ? "name" : "ip", message: `host "${rawName || ip}" needs both a name and an IP` };
  const name = normalizeHostName(rawName);
  if (name.length > MAX_HOST_NAME) return { field: "name", message: `host name ${pyRepr(name)} is longer than ${MAX_HOST_NAME} characters` };
  if (!DOTTED_HOSTNAME.test(name)) return { field: "name", message: `invalid host name ${pyRepr(name)} (need a dotted name, e.g. nas.v2pi)` };
  if (name.endsWith(".local")) {
    return { field: "name", message: `${pyRepr(name)}: the .local suffix is captured by mDNS on iOS/macOS and never reaches the tunnel — use another suffix (e.g. .v2pi)` };
  }
  if (ipv4Number(ip) === null) return { field: "ip", message: `host ${pyRepr(name)} must map to an IPv4 address, got ${pyRepr(ip)}` };
  if (earlier.some((other) => filledRow(other) && normalizeHostName(other.name) === name)) return { field: "name", message: `host ${name} is listed twice` };
  return null;
}

/** rw_inbound.validate_nets over the override csv; null when it is fine. */
export function routedNetsIssue(raw: string): string | null {
  if (raw.trim().length > MAX_FIELD) return `routed_nets: String should have at most ${MAX_FIELD} characters`;
  const bad = parseCsv(raw).find((net) => ipv4Span(net) === null);
  return bad === undefined ? null : `invalid CIDR ${pyRepr(bad)}`;
}

/** The five things enabling the inbound needs (routes.py put_rw), in the backend's order. */
export const ENABLE_REQUIRES = {
  privateKey: "set the Reality private key before enabling the inbound (generate one with `xray x25519`)",
  shortIds: "set at least one short id before enabling the inbound",
  publicKey: "set the Reality public key before enabling the inbound",
  endpoint: "set the external endpoint before enabling the inbound",
  serverNames: "set at least one server name before enabling the inbound",
} as const;

/**
 * The inbound form's rules. `hasPrivateKey` is whether the gateway stores a key: enabling counts it as well as a typed
 * one. A malformed key is refused here, so it is never sent (a 422 would echo it back).
 */
export function rwFormSchema(hasPrivateKey: boolean) {
  const issue = (ctx: z.RefinementCtx, path: (string | number)[], message: string | null) => {
    if (message) ctx.addIssue({ code: "custom", path, message });
  };
  return z.object({
    enabled: z.boolean(),
    port: z.string(),
    endpoint: z.string(),
    dest: z.string(),
    serverNames: z.array(z.string()),
    shortIds: z.array(z.string()),
    publicKey: z.string(),
    privateKey: z.string(),
    hosts: z.array(z.object({ name: z.string(), ip: z.string() })),
    routedNets: z.string(),
  }).superRefine((form, ctx) => {
    issue(ctx, ["port"], portIssue(form.port));
    issue(ctx, ["endpoint"], endpointIssue(form.endpoint));
    issue(ctx, ["dest"], destIssue(form.dest));
    issue(ctx, ["serverNames"], form.serverNames.map(serverNameIssue).find(Boolean)
      ?? (form.serverNames.join(",").length > MAX_FIELD ? `server_names: String should have at most ${MAX_FIELD} characters` : null));
    issue(ctx, ["shortIds"], form.shortIds.map(shortIdIssue).find(Boolean)
      ?? (form.shortIds.join(",").length > MAX_FIELD ? `short_ids: String should have at most ${MAX_FIELD} characters` : null));
    issue(ctx, ["publicKey"], keyIssue(form.publicKey, "public"));
    issue(ctx, ["privateKey"], keyIssue(form.privateKey, "private"));
    const filled = form.hosts.filter(filledRow);
    if (filled.length > MAX_HOSTS) issue(ctx, ["hosts"], `at most ${MAX_HOSTS} host mappings, got ${filled.length}`);
    form.hosts.forEach((row, index) => {
      const problem = hostRowIssue(row, form.hosts.slice(0, index));
      if (problem) issue(ctx, ["hosts", index, problem.field], problem.message);
    });
    issue(ctx, ["routedNets"], routedNetsIssue(form.routedNets));
    if (!form.enabled) return;
    if (!hasPrivateKey && form.privateKey.trim() === "") issue(ctx, ["privateKey"], ENABLE_REQUIRES.privateKey);
    if (form.shortIds.length === 0) issue(ctx, ["shortIds"], ENABLE_REQUIRES.shortIds);
    if (form.publicKey.trim() === "") issue(ctx, ["publicKey"], ENABLE_REQUIRES.publicKey);
    if (form.endpoint.trim() === "") issue(ctx, ["endpoint"], ENABLE_REQUIRES.endpoint);
    if (form.serverNames.length === 0) issue(ctx, ["serverNames"], ENABLE_REQUIRES.serverNames);
  });
}

/** The client name rule (rw_inbound.validate_email), for Add. */
export const CLIENT_NAME_ISSUE = "client name must be 1-40 chars of letters, digits, dot, dash or underscore";

export function clientNameIssue(raw: string): string | null {
  return /^[A-Za-z0-9._-]{1,40}$/.test(raw.trim()) ? null : CLIENT_NAME_ISSUE;
}

/** Contract A2: switching the inbound ON needs a key, stored or typed. Switching it off is always allowed. */
export function canTurnOn(rw: Rw, form: Pick<RwFormValues, "privateKey">): boolean {
  return rw.has_private_key || form.privateKey.trim() !== "";
}

/**
 * Whether a save certainly takes access away (routes.py `_rw_narrows`, as far as the browser can see): the inbound was
 * enabled with a key, and it is switched off, the port moves, or a saved short id or server name is dropped (both
 * compared case-insensitively). A typed private key is never counted — the stored one is unknown, so that save may
 * widen. A narrowing save revokes and never starts xray; anything else may, so it gets decision 3's question.
 */
export function provablyNarrows(saved: Rw, form: RwFormValues): boolean {
  if (!saved.enabled || !saved.has_private_key) return false;
  const lower = (values: readonly string[]) => new Set(values.map((value) => value.toLowerCase()));
  const shortIds = lower(form.shortIds);
  const names = lower(form.serverNames);
  return !form.enabled
    || pyInt(form.port) !== saved.port
    || parseCsv(saved.short_ids).some((id) => !shortIds.has(id.toLowerCase()))
    || parseCsv(saved.server_names).some((name) => !names.has(name.toLowerCase()));
}

export const DEFAULT_DEST = "www.microsoft.com:443";
export const DEFAULT_SERVER_NAME = "www.microsoft.com";

/** The dest host Reality will probe, from the effective dest (the default when blank), brackets dropped, lower-cased. */
export function destHost(form: Pick<RwFormValues, "dest">): string {
  const dest = form.dest.trim() || DEFAULT_DEST;
  const colon = dest.lastIndexOf(":");
  if (colon <= 0) return "";
  return dest.slice(0, colon).replace(/^\[(.*)\]$/, "$1").toLowerCase();
}

/** The SNI does not name the dest host, on the values the gateway will actually use (defaults for blanks). */
export function sniMismatch(form: Pick<RwFormValues, "dest" | "serverNames">): boolean {
  const host = destHost(form);
  if (host === "") return false;
  const names = form.serverNames.length > 0 ? form.serverNames : [DEFAULT_SERVER_NAME];
  return !names.some((name) => name.toLowerCase() === host);
}

export interface RwWarning {
  key: string;
  tone: "warn" | "bad";
  title: string;
  text?: string;
  /** A tag shown with the banner ("revocation pending"). */
  badge?: string;
}

export const PENDING_TITLE = "SECURITY WARNING: xray's stop could not be confirmed, so the revoked device may still be able to connect.";
export const PENDING_TEXT = "The panel keeps retrying; reboot the gateway if this does not clear.";

/**
 * A1, in order: a revocation still pending (hidden while this tab's own remote-access write runs, whose reply says how
 * it went), malformed stored settings, then at most one of a missing key / no clients / not live, then SNI ≠ dest.
 */
export function rwWarnings(rw: Rw, form: Pick<RwFormValues, "dest" | "serverNames">, busyOwnWrite: boolean): RwWarning[] {
  const warnings: RwWarning[] = [];
  if (rw.revocation_pending && !busyOwnWrite) {
    warnings.push({ key: "revocation-pending", tone: "bad", badge: "revocation pending", title: PENDING_TITLE, text: PENDING_TEXT });
  }
  if (rw.state_error) {
    warnings.push({ key: "state-error", tone: "bad", title: "Stored settings are malformed and were ignored:", text: `${rw.state_error}. Save this form to overwrite them.` });
  }
  if (rw.enabled && !rw.has_private_key) {
    warnings.push({ key: "no-key", tone: "warn", title: "Enabled, but no private key is stored", text: "(keys are not restored from backups)" });
  } else if (rw.enabled && !rw.clients.some((client) => client.enabled)) {
    warnings.push({
      key: "no-clients", tone: "warn", title: "Enabled with no clients",
      text: "— nothing is listening. xray will not start on an inbound with an empty client list, so none is emitted until you add a client.",
    });
  } else if (rw.enabled && !rw.live) {
    warnings.push({
      key: "not-live", tone: "warn", title: "Stored, but not in the running config yet",
      text: "— either there is no active node to rebuild it from, or xray itself is not running. Connect a node (or bring xray back up) and the inbound comes up with it.",
    });
  }
  if (sniMismatch(form)) {
    warnings.push({
      key: "sni", tone: "warn", title: "Server name does not match dest.",
      text: "Reality needs the SNI to be what the dest host actually serves — a mismatch fails at handshake time with no useful error.",
    });
  }
  return warnings;
}
