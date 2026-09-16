import { describe, expect, it } from "vitest";
import { api } from "../api/client";
import { serverNow } from "../api/clock";
import { trafficStore } from "../api/traffic";
import {
  ALL_NODES, ALL_NODE_HEALTH, AUDIT, BACKUP_DOC, DIAGNOSTICS, FAILOVER_STATUS, GATEWAY_NETWORK, LOG_LINES, NETWORK, NODES, NODE_HEALTH, NOW_SEC, PREVIEW,
  PREVIEW_NODES, PROFILES, PROFILE_PRESETS, REFRESH_ALL, RESTORE_RESULT, ROUTING, ROUTING_PRESETS, RU_DIRECT_PRESET, RW, RW_CLIENTS, RW_PENDING,
  RW_PRIVATE_KEY, RW_PUBLIC_KEY, SETTINGS, STATUS, SUBS, TOKENS, TOKEN_CREATED,
  TRAFFIC_FRAME, TUNNEL_PROFILES, TUNNEL_ROUTING, VALID, mockApi, mockGateway, mockNodeGroups, mockSystem, mockTunnel,
} from "./fixtures";

describe("gateway fixtures", () => {
  it("mockApi answers every read the Home screens make", async () => {
    mockApi();
    await expect(api.getStatus()).resolves.toBe(STATUS);
    await expect(api.listNodes()).resolves.toBe(NODES);
    await expect(api.getNetwork()).resolves.toBe(NETWORK);
    await expect(api.listNodeHealth()).resolves.toBe(NODE_HEALTH);
    await expect(api.listSubs()).resolves.toBe(SUBS);
    await expect(api.getRouting()).resolves.toBe(ROUTING);
  });

  it("the shell's two named nodes keep their ids, so existing screens and tests still find them", () => {
    expect(NODES.slice(0, 2).map((n) => [n.id, n.name])).toEqual([[1, "nl-ams-03"], [2, "de-fra-01"]]);
    expect(new Set(NODES.map((n) => n.id)).size).toBe(NODES.length);
  });

  it("is internally consistent: one gateway, one clock, one active node", () => {
    expect(STATUS.active_node_id).toBe(1);
    expect(TRAFFIC_FRAME.active?.node_id).toBe(STATUS.active_node_id);
    expect(TRAFFIC_FRAME.ts).toBe(STATUS.server_now * 1000);
    // every health row belongs to a node, and one node has never been probed
    const ids = new Set(NODES.map((n) => n.id));
    expect(NODE_HEALTH.every((h) => ids.has(h.node_id))).toBe(true);
    expect(NODES.some((n) => !NODE_HEALTH.some((h) => h.node_id === n.id))).toBe(true);
    // events arrive oldest first, as the backend appends them, and there are more than the six Overview shows
    const ts = NETWORK.events.map((e) => e.ts);
    expect([...ts].sort((a, b) => a - b)).toEqual(ts);
    expect(NETWORK.events.length).toBeGreaterThan(6);
    expect(ts.every((t) => t <= STATUS.server_now)).toBe(true);
  });

  it("starts each test on the gateway clock and with an empty traffic store", () => {
    const previous = mockApi();
    const unsubscribe = trafficStore.subscribe(() => {});
    previous.emitTraffic(TRAFFIC_FRAME);
    unsubscribe();
    mockApi();
    expect(Math.abs(serverNow() - STATUS.server_now * 1000)).toBeLessThan(1_000);
    expect(trafficStore.getSnapshot()).toMatchObject({ live: null, disabled: false });
    expect(trafficStore.getSnapshot().samples).toHaveLength(0);
  });

  it("emitTraffic delivers a frame to the live store once something subscribes", () => {
    const api$ = mockApi();
    expect(() => api$.emitTraffic(TRAFFIC_FRAME)).toThrow(/no traffic subscriber/);
    const unsubscribe = trafficStore.subscribe(() => {});
    api$.emitTraffic(TRAFFIC_FRAME);
    expect(trafficStore.getSnapshot().live).toBe(TRAFFIC_FRAME);
    api$.emitTraffic({ disabled: true });
    expect(trafficStore.getSnapshot().disabled).toBe(true);
    unsubscribe();
  });

  it("mockApi answers the Nodes screens' reads, and mockNodeGroups serves every group", async () => {
    const api$ = mockApi();
    await expect(api.listProfiles()).resolves.toBe(PROFILES);
    await expect(api.getSettings()).resolves.toBe(SETTINGS);
    await expect(api.previewSub("https://x.example", {})).resolves.toBe(PREVIEW);
    await expect(api.previewSubNodes("https://x.example", {})).resolves.toBe(PREVIEW_NODES);
    await expect(api.refreshAllSubs()).resolves.toBe(REFRESH_ALL);
    mockNodeGroups(api$);
    await expect(api.listNodes()).resolves.toBe(ALL_NODES);
    await expect(api.listNodeHealth()).resolves.toBe(ALL_NODE_HEALTH);
  });

  it("writes answer like the gateway without reaching the network", async () => {
    mockApi();
    await expect(api.putSettings({ tunneled_fetch: false })).resolves.toMatchObject({ tunneled_fetch: false, subs_auto_switch: true });
    await expect(api.probeNode(7)).resolves.toMatchObject({ node_id: 7, last_real_ms: 69 });
    await expect(api.probeNode(99)).resolves.toMatchObject({ node_id: 99, last_real_ms: null });
    await expect(api.addNode({ name: "vps-ams-02", address: "198.51.100.77", port: 443, uuid: "u" })).resolves.toMatchObject({ id: 11, name: "vps-ams-02", subscription_id: null });
    await expect(api.updateNode(2, { note: "n" })).resolves.toMatchObject({ id: 2, name: "de-fra-01", note: "n" });
    await expect(api.addSub({ name: "big-feed", url: "https://feed.example" })).resolves.toMatchObject({ id: 4, name: "big-feed", node_count: 0 });
    await expect(api.updateSub(3, { enabled: true })).resolves.toMatchObject({ id: 3, name: "old", enabled: true });
    await expect(api.validateNode({ name: "a", address: "b", port: 1, uuid: "c" })).resolves.toEqual({ ok: true, error: "" });
  });

  it("node groups: work 6 · home 1 · old 0 · Servers 3, matching each subscription's node count", () => {
    const count = (subscriptionId: number | null) => ALL_NODES.filter((n) => n.subscription_id === subscriptionId).length;
    expect(SUBS.map((s) => [s.name, count(s.id), s.node_count])).toEqual([["work", 6, 6], ["home", 1, 1], ["old", 0, 0]]);
    expect(count(null)).toBe(3);
    expect(new Set(ALL_NODES.map((n) => n.id)).size).toBe(ALL_NODES.length);
    expect(NODES.every((n) => n.subscription_id === 1)).toBe(true);
  });

  it("covers every node state the Nodes screens draw", () => {
    const byId = new Map(ALL_NODE_HEALTH.map((h) => [h.node_id, h]));
    expect(ALL_NODES.filter((n) => n.stale).map((n) => n.name)).toEqual(["us-nyc-01"]);
    expect(ALL_NODES.filter((n) => !byId.has(n.id)).map((n) => n.name)).toEqual(["ch-zrh-02", "kz-ala-01"]);
    expect(ALL_NODE_HEALTH.filter((h) => h.last_real_ok === false).map((h) => h.node_id)).toEqual([6]);
    expect(ALL_NODE_HEALTH.filter((h) => (h.last_http_ms ?? 0) > 150).map((h) => h.node_id)).toEqual([4, 10]);
    expect(new Set(ALL_NODES.map((n) => `${n.transport}·${n.security}`))).toEqual(new Set(["vision·reality", "xhttp·reality", "xhttp·tls"]));
    expect(ALL_NODE_HEALTH.every((h) => ALL_NODES.some((n) => n.id === h.node_id))).toBe(true);
    expect(PROFILES.filter((profile) => profile.is_default).map((profile) => profile.name)).toEqual(["balanced"]);
    expect(SUBS.filter((s) => s.last_error).map((s) => s.name)).toEqual(["home"]);
    expect(SUBS.filter((s) => !s.enabled).map((s) => s.name)).toEqual(["old"]);
  });

  it("mockApi answers Tunnel's reads and writes, and mockTunnel serves its ruleset and profiles", async () => {
    const api$ = mockApi();
    await expect(api.listRoutingPresets()).resolves.toBe(ROUTING_PRESETS);
    await expect(api.routingPreset("ru-direct")).resolves.toBe(RU_DIRECT_PRESET);
    await expect(api.validateRouting({ rules: [], default_action: "proxy" })).resolves.toBe(VALID);
    await expect(api.putRouting({ rules: [{ type: "domain", value: "a.example", action: "proxy" }], default_action: "block" })).resolves.toEqual({
      rules: [{ id: 100, position: 0, type: "domain", value: "a.example", action: "proxy", enabled: true, label: "" }],
      default_action: "block", domain_strategy: "IPIfNonMatch",
    });
    await expect(api.listProfilePresets()).resolves.toBe(PROFILE_PRESETS);
    await expect(api.validateProfile({ name: "x" })).resolves.toBe(VALID);
    await expect(api.addProfile({ name: "e2e", quic: "drop" })).resolves.toMatchObject({ id: 4, name: "e2e", quic: "drop", is_default: false, is_active: false, node_count: 0 });
    await expect(api.updateProfile(2, { frag_length: "80-160" })).resolves.toMatchObject({ id: 2, name: "fragment-tls", frag_length: "80-160", is_active: true });
    await expect(api.setDefaultProfile(3)).resolves.toMatchObject({ id: 3, is_default: true });
    await expect(api.applyProfileActive(3)).resolves.toEqual({ ok: true, node_id: 1 });
    await expect(api.deleteProfile(3)).resolves.toEqual({ ok: true });
    await expect(api.getRouting()).resolves.toBe(ROUTING);
    mockTunnel(api$);
    await expect(api.getRouting()).resolves.toBe(TUNNEL_ROUTING);
    await expect(api.listProfiles()).resolves.toBe(TUNNEL_PROFILES);
  });

  it("mockApi answers Gateway's reads and writes, and mockGateway serves the gateway's network read", async () => {
    const api$ = mockApi();
    await expect(api.getRw()).resolves.toBe(RW);
    await expect(api.putNetwork({ dhcp_end: "192.168.50.150", kill_switch_enabled: false })).resolves.toMatchObject({
      segment: { ...GATEWAY_NETWORK.segment, dhcp_end: "192.168.50.150" }, kill_switch_enabled: false, lan_access_enabled: true, ipv6_enabled: true,
    });
    const body = {
      enabled: false, port: 443, dest: "", server_names: "", short_ids: "", public_key: "", endpoint: "", private_key: "", hosts: {}, routed_nets: "10.9.0.0/24",
    };
    await expect(api.putRw(body)).resolves.toMatchObject({
      enabled: false, port: 443, dest: "", server_names: "", short_ids: "", public_key: "", endpoint: "", hosts: {},
      has_private_key: true, routed_nets_override: "10.9.0.0/24", revocation: "",
    });
    await expect(api.addRwClient("e2e-phone")).resolves.toMatchObject({ clients: [...RW_CLIENTS, { email: "e2e-phone", enabled: true }] });
    const suspended = await api.setRwClientEnabled(RW_CLIENTS[0]!.id, false);
    expect(suspended.clients[0]).toEqual({ ...RW_CLIENTS[0], enabled: false });
    expect(suspended.revocation).toBe("reapplied");
    expect((await api.setRwClientEnabled(RW_CLIENTS[1]!.id, true)).revocation).toBe("");
    await expect(api.deleteRwClient(RW_CLIENTS[2]!.id)).resolves.toMatchObject({ clients: RW_CLIENTS.slice(0, 2), revocation: "reapplied" });
    await expect(api.newRwShortId()).resolves.toEqual({ short_id: "0123456789abcdef" });
    expect((await api.rwClientLink(RW_CLIENTS[0]!.id)).link).toMatch(/^vless:\/\/3f2a9c1e-.+#iphone-anna$/);
    await expect(api.rwClientConfig(RW_CLIENTS[0]!.id)).resolves.toMatchObject({ filename: "iphone-anna.conf" });
    await expect(api.getNetwork()).resolves.toBe(NETWORK);
    mockGateway(api$);
    await expect(api.getNetwork()).resolves.toBe(GATEWAY_NETWORK);
  });

  it("the gateway network: pool and leases inside the segment /24, one lease without a name and one that never expires", () => {
    const { segment, status } = GATEWAY_NETWORK;
    const net = segment.ip.split(".").slice(0, 3).join(".");
    for (const address of [segment.dhcp_start, segment.dhcp_end, ...status.clients.map((lease) => lease.ip)]) expect(address.startsWith(`${net}.`)).toBe(true);
    expect(status.clients).toHaveLength(status.dhcp_clients);
    expect(status.clients.filter((lease) => lease.hostname === "").length).toBe(1);
    expect(status.clients.filter((lease) => lease.expiry === 0).map((lease) => lease.hostname)).toEqual(["appletv"]);
    expect(status.clients.map((lease) => lease.ip)).not.toEqual([...status.clients.map((lease) => lease.ip)].sort());
    expect(new Set(GATEWAY_NETWORK.recommendations.map((rec) => rec.title)).size).toBe(6);
    expect(segment.client_dns).toBe(segment.ip);
  });

  it("remote access: well-formed keys, short ids and client ids; one suspended device; a pending twin", () => {
    for (const key of [RW_PUBLIC_KEY, RW_PRIVATE_KEY]) expect(key).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(RW.short_ids.split(",").every((id) => /^([0-9a-f]{2}){1,8}$/.test(id))).toBe(true);
    expect(new Set(RW.clients.map((client) => client.id)).size).toBe(3);
    expect(RW.clients.every((client) => /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(client.id))).toBe(true);
    expect(RW.clients.filter((client) => !client.enabled).map((client) => client.email)).toEqual(["ipad"]);
    expect(RW_PENDING).toEqual({ ...RW, revocation_pending: true });
    expect(RW.revocation_pending).toBe(false);
  });

  it("the Tunnel ruleset has one rule of every type, labels, a switched-off rule, and a preset reply with one unsaved rule", () => {
    expect(new Set(TUNNEL_ROUTING.rules.map((rule) => rule.type))).toEqual(new Set(["geoip", "geosite", "domain", "ip", "port"]));
    expect(TUNNEL_ROUTING.rules.filter((rule) => !rule.enabled).map((rule) => rule.value)).toEqual(["netflix.com"]);
    expect(TUNNEL_ROUTING.rules.filter((rule) => rule.label).length).toBeGreaterThan(1);
    expect(ROUTING_PRESETS.map((preset) => preset.name)).toEqual(["ru-direct", "block-ads", "cn-direct", "lan-direct"]);
    expect(RU_DIRECT_PRESET.rules.slice(0, TUNNEL_ROUTING.rules.length)).toEqual(TUNNEL_ROUTING.rules);
    expect(RU_DIRECT_PRESET.rules.filter((rule) => rule.id === 0).map((rule) => `${rule.type}:${rule.value}`)).toEqual(["geosite:category-ru"]);
  });

  it("mockSystem answers every System read and write, and only it does", async () => {
    const api$ = mockApi();
    // Without the System mock a screen test would reach the network — that has to be loud, not quiet.
    expect(api$).not.toHaveProperty("listAudit");
    const system$ = mockSystem(api$);
    await expect(api.getBackup()).resolves.toBe(BACKUP_DOC);
    await expect(api.restore(BACKUP_DOC)).resolves.toBe(RESTORE_RESULT);
    await expect(api.listTokens()).resolves.toBe(TOKENS);
    await expect(api.listAudit()).resolves.toBe(AUDIT);
    await expect(api.getDiagnostics()).resolves.toBe(DIAGNOSTICS);
    await expect(api.resetSettings()).resolves.toBe(SETTINGS);
    await expect(api.deleteToken(1)).resolves.toBeUndefined();
    await expect(api.createToken("home-assistant", "monitor", NOW_SEC + 30 * 86_400)).resolves.toMatchObject({
      name: "home-assistant", scope: "monitor", expires_at: NOW_SEC + 30 * 86_400, token: TOKEN_CREATED.token,
    });
    await expect(api.createToken("forever", "read")).resolves.toMatchObject({ expires_at: null });
    // Only `app` has content: xray writes neither xray-error nor xray-access, and xray-stderr — the
    // supervisor's own in-memory tail — is empty here only because this fixture models a healthy gateway.
    await expect(api.getLogs("app", 200)).resolves.toEqual({ source: "app", lines: LOG_LINES });
    for (const source of ["xray-stderr", "xray-error", "xray-access"]) {
      await expect(api.getLogs(source, 200)).resolves.toEqual({ source, lines: [] });
    }
    expect(system$.getLogs.mock.calls.map(([source]) => source)).toEqual(["app", "xray-stderr", "xray-error", "xray-access"]);
  });

  it("the System fixtures cover every state their screens draw", () => {
    // tokens: one never used, one used minutes ago, one that never expires; ordered by id, as GET /tokens is
    expect(TOKENS.map((t) => t.id)).toEqual([1, 2, 3]);
    expect(TOKENS.filter((t) => t.last_used_at === null)).toHaveLength(1);
    expect(TOKENS.filter((t) => t.expires_at === null).map((t) => t.name)).toEqual(["uptime-probe"]);
    expect(new Set(TOKENS.map((t) => t.scope))).toEqual(new Set(["monitor", "read", "readwrite"]));
    expect(TOKENS.every((t) => t.prefix.startsWith("pgwp_") && t.prefix.length === 12)).toBe(true);
    // the created token is the only place a secret exists, and it is not one of the listed rows
    expect(TOKEN_CREATED.token).toMatch(/^pgwp_[A-Za-z0-9_-]{43}$/);
    expect(TOKENS.some((t) => t.id === TOKEN_CREATED.id)).toBe(false);
    // audit: newest first, one masked remote-access path, every actor kind, 2xx / 4xx / the 413
    expect([...AUDIT].sort((a, b) => b.ts - a.ts)).toEqual(AUDIT);
    expect(AUDIT.filter((row) => row.path.startsWith("/api/rw/clients/"))).toHaveLength(1);
    expect(new Set(AUDIT.map((row) => row.actor.split(":")[0]))).toEqual(new Set(["user", "token", "anon"]));
    expect(AUDIT.map((row) => row.status)).toContain(413);
    expect(AUDIT.every((row) => ["POST", "PUT", "PATCH", "DELETE"].includes(row.method))).toBe(true);
    expect(AUDIT.every((row) => !row.path.includes("?"))).toBe(true);
    // logs: two ERROR lines and one WARNING, so the pane's tones are all exercised
    expect(LOG_LINES.filter((line) => line.includes(" ERROR "))).toHaveLength(2);
    expect(LOG_LINES.filter((line) => line.includes(" WARNING "))).toHaveLength(1);
    // a hand-taken backup carries no created_at — only the daily job and the pre-restore snapshot stamp one
    expect(BACKUP_DOC.schema_version).toBe(2);
    expect(BACKUP_DOC).not.toHaveProperty("created_at");
    // a successful restore always leaves the gateway disconnected, and names the snapshot it took
    expect(RESTORE_RESULT.runtime).toBe("disconnected");
    expect(RESTORE_RESULT.restored.rw_disabled).toBe("");
    expect(RESTORE_RESULT.pre_restore_snapshot).toMatch(/^\/app\/data\/backups\/pre-restore-/);
    // diagnostics: a healthy collector, which is what makes the warning's absence meaningful
    expect(DIAGNOSTICS.stats_last_ok_at).not.toBeNull();
    expect(DIAGNOSTICS.stats_fail_count).toBe(0);
  });

  it("the Tunnel profiles: one default, one live, one unused; every feature switched on somewhere", () => {
    expect(TUNNEL_PROFILES.filter((p) => p.is_default).map((p) => p.name)).toEqual(["balanced"]);
    expect(TUNNEL_PROFILES.filter((p) => p.is_active).map((p) => p.name)).toEqual(["fragment-tls"]);
    expect(TUNNEL_PROFILES.filter((p) => p.node_count === 0).map((p) => p.name)).toEqual(["mux-heavy"]);
    for (const flag of ["frag_enabled", "noise_enabled", "mux_enabled", "doh_enabled"] as const) {
      expect(TUNNEL_PROFILES.some((p) => p[flag])).toBe(true);
    }
    expect(new Set(TUNNEL_PROFILES.map((p) => p.quic))).toEqual(new Set(["allow", "drop", "proxy"]));
    expect(TUNNEL_PROFILES[1]!.noises).toHaveLength(2);
    expect(PROFILE_PRESETS.map((preset) => preset.name)).toEqual(["ru-hardened", "stealth-latency", "cdn-xhttp"]);
    expect(SETTINGS).toMatchObject({ health_enabled: true, health_sweep_enabled: true, failover_enabled: true });
    expect(FAILOVER_STATUS.last_failover_at).toBe(NOW_SEC - 720);
  });
});
