import { describe, expect, it } from "vitest";
import type { Rw } from "../../api/client";
import { RW, RW_CLIENTS, RW_PRIVATE_KEY, RW_PUBLIC_KEY } from "../../test/fixtures";
import {
  ENABLE_REQUIRES, PENDING_TEXT, PENDING_TITLE, canTurnOn, clientNameIssue, destHost, destIssue, endpointIssue, formToRwIn, hostRowIssue, keyIssue,
  normalizedDest, parseCsv, portIssue, provablyNarrows, pyRepr, routedNetsIssue, rwFormSchema, rwToForm, rwWarnings, serverNameIssue, shortIdIssue,
  sniMismatch, type RwFormValues,
} from "./rwForm";

const FORM = rwToForm(RW);

function issues(patch: Partial<RwFormValues>, hasPrivateKey = RW.has_private_key): Record<string, string> {
  const result = rwFormSchema(hasPrivateKey).safeParse({ ...FORM, ...patch });
  if (result.success) return {};
  const out: Record<string, string> = {};
  for (const issue of result.error.issues) out[issue.path.join(".")] ??= issue.message;
  return out;
}

describe("remote-access form values", () => {
  it("maps the saved inbound into the form; the private key always starts blank", () => {
    expect(FORM).toEqual({
      enabled: true, port: "8443", endpoint: "vpn.example.net", dest: "www.microsoft.com:443", serverNames: ["www.microsoft.com", "learn.microsoft.com"],
      shortIds: ["3a9e", "6ba85179e3d4fc21"], publicKey: RW_PUBLIC_KEY, privateKey: "",
      hosts: [{ name: "nas.v2pi", ip: "192.168.1.10" }, { name: "printer.v2pi", ip: "192.168.1.20" }], routedNets: "",
    });
    expect(parseCsv(" a, ,b ,,c")).toEqual(["a", "b", "c"]);
    expect(rwToForm({ ...RW, server_names: "", short_ids: "", hosts: {} })).toMatchObject({ serverNames: [], shortIds: [], hosts: [] });
  });

  it("Save sends every field of the full replace, trimmed, with private_key \"\" to keep the stored one", () => {
    const body = formToRwIn({
      ...FORM, port: " 9443 ", dest: " www.microsoft.com:443 ", endpoint: " vpn.example.net ", publicKey: ` ${RW_PUBLIC_KEY} `,
      hosts: [{ name: " NAS.v2pi ", ip: " 192.168.1.10 " }, { name: "", ip: "" }], routedNets: " 10.9.0.0/24 ",
    });
    expect(body).toEqual({
      enabled: true, port: 9443, dest: "www.microsoft.com:443", server_names: "www.microsoft.com,learn.microsoft.com", short_ids: "3a9e,6ba85179e3d4fc21",
      public_key: RW_PUBLIC_KEY, endpoint: "vpn.example.net", private_key: "", hosts: { "NAS.v2pi": "192.168.1.10" }, routed_nets: "10.9.0.0/24",
    });
    expect(Object.keys(body).sort()).toEqual(["dest", "enabled", "endpoint", "hosts", "port", "private_key", "public_key", "routed_nets", "server_names", "short_ids"]);
    expect(formToRwIn({ ...FORM, privateKey: ` ${RW_PRIVATE_KEY} ` }).private_key).toBe(RW_PRIVATE_KEY);
    expect(formToRwIn({ ...FORM, serverNames: [], shortIds: [], hosts: [] })).toMatchObject({ server_names: "", short_ids: "", hosts: {} });
  });
});

describe("remote-access rules, in the backend's words", () => {
  it("the saved gateway is valid", () => {
    expect(issues({})).toEqual({});
  });

  it("Python's repr of a value in a message", () => {
    expect(pyRepr("abc")).toBe("'abc'");
    expect(pyRepr("it's")).toBe(`"it's"`);
    expect(pyRepr(`a'b"c`)).toBe(`'a\\'b"c'`);
  });

  it("port: an integer 1–65535", () => {
    for (const port of ["1", "443", "65535", " 8443 ", "+8443"]) expect(portIssue(port)).toBeNull();
    expect(portIssue("0")).toBe("rw_port out of range: 0");
    expect(portIssue("65536")).toBe("rw_port out of range: 65536");
    expect(portIssue("44x")).toBe("rw_port must be an integer, got '44x'");
    expect(portIssue("")).toBe("rw_port must be an integer, got ''");
    expect(issues({ port: "8.5" })).toEqual({ port: "rw_port must be an integer, got '8.5'" });
  });

  it("endpoint: blank, a bracketed IPv6, an IPv4 address or a host name", () => {
    for (const endpoint of ["", "home.example.org", "HOME-1.example.org", "203.0.113.7", "[2001:db8::7]", "localhost", "1.2.3"]) expect(endpointIssue(endpoint)).toBeNull();
    expect(endpointIssue("[2001:db8::zz]")).toBe("the external endpoint has an invalid IPv6 literal: '[2001:db8::zz]'");
    expect(endpointIssue("2001:db8::7")).toBe("the external endpoint must be a host name (letters, digits, dashes and dots), got '2001:db8::7'");
    expect(endpointIssue("home.example.org,evil")).toBe("the external endpoint must be a host name (letters, digits, dashes and dots), got 'home.example.org,evil'");
    expect(endpointIssue("-bad.example")).toMatch(/must be a host name/);
    expect(endpointIssue(`${"a".repeat(64)}.example`)).toMatch(/must be a host name/);
    expect(endpointIssue(`${"a.".repeat(127)}ab`)).toBe("the external endpoint is longer than 253 characters");
  });

  it("dest: blank or host:port, stored normalised", () => {
    for (const dest of ["", "www.microsoft.com:443", "[2001:db8::1]:443", "cdn.example:8443"]) expect(destIssue(dest)).toBeNull();
    expect(destIssue("www.microsoft.com")).toBe("rw_dest must be host:port (e.g. www.microsoft.com:443), got 'www.microsoft.com'");
    expect(destIssue(":443")).toBe("rw_dest must be host:port (e.g. www.microsoft.com:443), got ':443'");
    expect(destIssue("www.microsoft.com:")).toBe("rw_dest must be host:port (e.g. www.microsoft.com:443), got 'www.microsoft.com:'");
    expect(destIssue("[2001:db8::zz]:443")).toBe("rw_dest has an invalid IPv6 literal, got '[2001:db8::zz]:443'");
    expect(destIssue("this is not:443")).toBe("the rw_dest host must be a host name (letters, digits, dashes and dots), got 'this is not'");
    expect(destIssue("www.microsoft.com:https")).toBe("rw_dest port must be an integer, got 'https'");
    expect(destIssue("www.microsoft.com:70000")).toBe("rw_dest port out of range: 70000");
    expect(normalizedDest(" www.microsoft.com:0443 ")).toBe("www.microsoft.com:443");
    expect(normalizedDest("[2001:db8::1]:+8443")).toBe("[2001:db8::1]:8443");
    expect(normalizedDest("not a dest")).toBe("not a dest");
  });

  it("server names and short ids, one chip at a time, 512 characters in all", () => {
    expect(serverNameIssue("www.microsoft.com")).toBeNull();
    expect(serverNameIssue("bad name")).toBe("server name must be a host name (letters, digits, dashes and dots), got 'bad name'");
    expect(issues({ serverNames: ["www.microsoft.com", "bad_name"] })).toEqual({ serverNames: "server name must be a host name (letters, digits, dashes and dots), got 'bad_name'" });
    for (const id of ["ab", "AB12", "0123456789abcdef"]) expect(shortIdIssue(id)).toBeNull();
    for (const id of ["a", "abc", "0123456789abcdef01", "zz"]) expect(shortIdIssue(id)).toBe(`short id must be 2-16 hex chars of even length, got '${id}'`);
    expect(issues({ shortIds: ["3a9e", "xyz1"] })).toEqual({ shortIds: "short id must be 2-16 hex chars of even length, got 'xyz1'" });
    expect(issues({ serverNames: Array.from({ length: 30 }, (_, index) => `name-${index}.example.org`) })).toEqual({ serverNames: "server_names: String should have at most 512 characters" });
    expect(issues({ shortIds: Array.from({ length: 31 }, () => "0123456789abcdef") })).toEqual({ shortIds: "short_ids: String should have at most 512 characters" });
  });

  it("keys: 43 base64 characters decoding to 32 bytes; the value is never echoed", () => {
    for (const key of [RW_PUBLIC_KEY, `${RW_PUBLIC_KEY}=`, RW_PUBLIC_KEY.replace(/-/g, "+").replace(/_/g, "/"), ""]) expect(keyIssue(key, "public")).toBeNull();
    const shape = "the Reality private key must be a base64 x25519 key — 43 characters, exactly as `xray x25519` prints it";
    expect(keyIssue("PRIV", "private")).toBe(shape);
    expect(keyIssue(`${RW_PRIVATE_KEY}A`, "private")).toBe(shape);
    expect(keyIssue(`${RW_PRIVATE_KEY.slice(0, 42)}!`, "private")).toBe(shape);
    expect(issues({ privateKey: "not-a-key", publicKey: "short" })).toEqual({
      privateKey: shape, publicKey: "the Reality public key must be a base64 x25519 key — 43 characters, exactly as `xray x25519` prints it",
    });
    expect(issues({ privateKey: "not-a-key" }).privateKey).not.toContain("not-a-key");
  });

  it("hosts: dotted names (lower-cased, trailing dot dropped), not .local, ≤ 40 characters, IPv4, both halves, no duplicates, ≤ 32", () => {
    const row = (name: string, ip = "192.168.1.10") => ({ name, ip });
    expect(hostRowIssue(row("NAS.v2pi."), [])).toBeNull();
    expect(hostRowIssue(row("", ""), [])).toBeNull();
    expect(hostRowIssue(row("nas.v2pi", ""), [])).toEqual({ field: "ip", message: 'host "nas.v2pi" needs both a name and an IP' });
    expect(hostRowIssue(row("", "192.168.1.10"), [])).toEqual({ field: "name", message: 'host "192.168.1.10" needs both a name and an IP' });
    expect(hostRowIssue(row("nas"), [])).toEqual({ field: "name", message: "invalid host name 'nas' (need a dotted name, e.g. nas.v2pi)" });
    expect(hostRowIssue(row("nas.local"), [])).toEqual({
      field: "name", message: "'nas.local': the .local suffix is captured by mDNS on iOS/macOS and never reaches the tunnel — use another suffix (e.g. .v2pi)",
    });
    expect(hostRowIssue(row(`${"a".repeat(36)}.v2pi`), [])).toEqual({ field: "name", message: `host name '${"a".repeat(36)}.v2pi' is longer than 40 characters` });
    expect(hostRowIssue(row("nas.v2pi", "2001:db8::10"), [])).toEqual({ field: "ip", message: "host 'nas.v2pi' must map to an IPv4 address, got '2001:db8::10'" });
    expect(hostRowIssue(row("Nas.V2pi."), [row("nas.v2pi")])).toEqual({ field: "name", message: "host nas.v2pi is listed twice" });
    expect(issues({ hosts: [row("nas.v2pi"), row("nas.v2pi.", "192.168.1.11")] })).toEqual({ "hosts.1.name": "host nas.v2pi is listed twice" });
    expect(issues({ hosts: Array.from({ length: 33 }, (_, index) => row(`h${index}.v2pi`)) })).toEqual({ hosts: "at most 32 host mappings, got 33" });
    expect(issues({ hosts: [...Array.from({ length: 32 }, (_, index) => row(`h${index}.v2pi`)), row("", "")] })).toEqual({});
  });

  it("routed subnets: IPv4 CIDRs with host bits allowed", () => {
    for (const nets of ["", "192.168.1.0/24", "192.168.1.7/24, 10.0.0.1", "10.0.0.0/8,10.0.0.0/8"]) expect(routedNetsIssue(nets)).toBeNull();
    expect(routedNetsIssue("192.168.1.0/24,10.0.0.0/33")).toBe("invalid CIDR '10.0.0.0/33'");
    expect(routedNetsIssue("2001:db8::/64")).toBe("invalid CIDR '2001:db8::/64'");
    expect(routedNetsIssue(`${"10.0.0.0/8,".repeat(47)}`)).toBe("routed_nets: String should have at most 512 characters");
  });

  it("enabling needs a stored or typed private key, a short id, the public key, the endpoint and a server name", () => {
    const empty = { enabled: true, shortIds: [], publicKey: "", endpoint: "", serverNames: [] };
    expect(issues(empty, false)).toEqual({
      privateKey: ENABLE_REQUIRES.privateKey, shortIds: ENABLE_REQUIRES.shortIds, publicKey: ENABLE_REQUIRES.publicKey,
      endpoint: ENABLE_REQUIRES.endpoint, serverNames: ENABLE_REQUIRES.serverNames,
    });
    expect(issues(empty, true).privateKey).toBeUndefined();
    expect(issues({ ...empty, privateKey: RW_PRIVATE_KEY }, false).privateKey).toBeUndefined();
    expect(issues({ ...empty, enabled: false }, false)).toEqual({});
    expect(ENABLE_REQUIRES.privateKey).toBe("set the Reality private key before enabling the inbound (generate one with `xray x25519`)");
  });

  it("client names for Add", () => {
    for (const name of ["iphone", "e2e-phone", "a.b_c-1", "x".repeat(40)]) expect(clientNameIssue(name)).toBeNull();
    for (const name of ["", "my phone", "x".repeat(41), "iphone!"]) expect(clientNameIssue(name)).toBe("client name must be 1-40 chars of letters, digits, dot, dash or underscore");
  });
});

describe("enable, narrowing and SNI", () => {
  const unkeyed: Rw = { ...RW, has_private_key: false, enabled: false };

  it("ON needs a key, stored or typed", () => {
    expect(canTurnOn(RW, { privateKey: "" })).toBe(true);
    expect(canTurnOn(unkeyed, { privateKey: "" })).toBe(false);
    expect(canTurnOn(unkeyed, { privateKey: "  " })).toBe(false);
    expect(canTurnOn(unkeyed, { privateKey: RW_PRIVATE_KEY })).toBe(true);
  });

  it("provably narrows: switched off, the port moved, a saved short id or server name dropped (case-insensitive)", () => {
    expect(provablyNarrows(RW, FORM)).toBe(false);
    expect(provablyNarrows(RW, { ...FORM, enabled: false })).toBe(true);
    expect(provablyNarrows(RW, { ...FORM, port: "9443" })).toBe(true);
    expect(provablyNarrows(RW, { ...FORM, port: " 8443 " })).toBe(false);
    expect(provablyNarrows(RW, { ...FORM, shortIds: ["3a9e"] })).toBe(true);
    expect(provablyNarrows(RW, { ...FORM, shortIds: ["3A9E", "6BA85179E3D4FC21"] })).toBe(false);
    expect(provablyNarrows(RW, { ...FORM, shortIds: [...FORM.shortIds, "0123456789abcdef"] })).toBe(false);
    expect(provablyNarrows(RW, { ...FORM, serverNames: ["www.microsoft.com"] })).toBe(true);
    expect(provablyNarrows(RW, { ...FORM, serverNames: ["WWW.Microsoft.com", "LEARN.microsoft.com"] })).toBe(false);
    expect(provablyNarrows(RW, { ...FORM, dest: "cdn.example:443", endpoint: "other.example", hosts: [] })).toBe(false);
  });

  it("a typed private key never counts, and nothing narrows an inbound that was off or unkeyed", () => {
    expect(provablyNarrows(RW, { ...FORM, privateKey: RW_PRIVATE_KEY })).toBe(false);
    expect(provablyNarrows({ ...RW, enabled: false }, { ...FORM, port: "9443", shortIds: [] })).toBe(false);
    expect(provablyNarrows({ ...RW, has_private_key: false }, { ...FORM, enabled: false })).toBe(false);
  });

  it("SNI vs dest on effective values: a blank dest is www.microsoft.com:443, blank names are www.microsoft.com", () => {
    expect(sniMismatch(FORM)).toBe(false);
    expect(sniMismatch({ dest: "www.microsoft.com:443", serverNames: ["microsoft.com", "learn.microsoft.com"] })).toBe(true);
    expect(sniMismatch({ dest: "", serverNames: [] })).toBe(false);
    expect(sniMismatch({ dest: "", serverNames: ["cdn.example"] })).toBe(true);
    expect(sniMismatch({ dest: "CDN.Example:8443", serverNames: ["cdn.example"] })).toBe(false);
    expect(sniMismatch({ dest: "[2001:db8::1]:443", serverNames: ["2001:db8::1"] })).toBe(false);
    expect(sniMismatch({ dest: "no-port", serverNames: ["x.example"] })).toBe(false);
    expect(destHost({ dest: "" })).toBe("www.microsoft.com");
  });
});

describe("remote-access warnings (A1)", () => {
  it("none on a healthy, live gateway", () => {
    expect(rwWarnings(RW, FORM, false)).toEqual([]);
  });

  it("revocation pending first — hidden while this tab's own write runs — then malformed state, then SNI", () => {
    const warnings = rwWarnings({ ...RW, revocation_pending: true, state_error: "rw_port must be an integer, got 'x'" }, { dest: "www.microsoft.com:443", serverNames: ["microsoft.com"] }, false);
    expect(warnings.map((warning) => [warning.key, warning.tone])).toEqual([["revocation-pending", "bad"], ["state-error", "bad"], ["sni", "warn"]]);
    expect(warnings[0]).toEqual({ key: "revocation-pending", tone: "bad", badge: "revocation pending", title: PENDING_TITLE, text: PENDING_TEXT });
    expect(`${warnings[0]!.title} ${warnings[0]!.text}`).toBe("SECURITY WARNING: xray's stop could not be confirmed, so the revoked device may still be able to connect. The panel keeps retrying; reboot the gateway if this does not clear.");
    expect(`${warnings[1]!.title} ${warnings[1]!.text}`).toBe("Stored settings are malformed and were ignored: rw_port must be an integer, got 'x'. Save this form to overwrite them.");
    expect(`${warnings[2]!.title} ${warnings[2]!.text}`).toBe("Server name does not match dest. Reality needs the SNI to be what the dest host actually serves — a mismatch fails at handshake time with no useful error.");
    expect(rwWarnings({ ...RW, revocation_pending: true }, FORM, true)).toEqual([]);
  });

  it("at most one of: no stored key, no enabled client, not live — in that order", () => {
    const key = (rw: Rw) => rwWarnings(rw, FORM, false).map((warning) => warning.key);
    expect(key({ ...RW, has_private_key: false, clients: [], live: false })).toEqual(["no-key"]);
    expect(key({ ...RW, clients: [RW_CLIENTS[1]!], live: false })).toEqual(["no-clients"]);
    expect(key({ ...RW, live: false })).toEqual(["not-live"]);
    expect(key({ ...RW, enabled: false, has_private_key: false, clients: [], live: false })).toEqual([]);
    const [noKey, noClients, notLive] = [
      rwWarnings({ ...RW, has_private_key: false }, FORM, false)[0]!, rwWarnings({ ...RW, clients: [] }, FORM, false)[0]!, rwWarnings({ ...RW, live: false }, FORM, false)[0]!,
    ];
    expect(`${noKey.title} ${noKey.text}`).toBe("Enabled, but no private key is stored (keys are not restored from backups)");
    expect(`${noClients.title} ${noClients.text}`).toBe("Enabled with no clients — nothing is listening. xray will not start on an inbound with an empty client list, so none is emitted until you add a client.");
    expect(`${notLive.title} ${notLive.text}`).toBe("Stored, but not in the running config yet — either there is no active node to rebuild it from, or xray itself is not running. Connect a node (or bring xray back up) and the inbound comes up with it.");
    expect([noKey.tone, noClients.tone, notLive.tone]).toEqual(["warn", "warn", "warn"]);
  });
});
