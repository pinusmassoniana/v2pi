import { describe, it, expect, beforeEach, vi } from "vitest";
import { api } from "./api";

function mockFetch() {
  const calls: any[] = [];
  const f = vi.fn(async (url: string, opts: any = {}) => {
    calls.push({ url, opts });
    if (url.endsWith("/api/login")) return jsonRes({ ok: true });
    if (url.endsWith("/api/csrf")) return jsonRes({ csrf: "tok-123" });
    if (url.endsWith("/api/status")) return jsonRes({ running: true, pid: 7, active_node_id: 2 });
    if (url.endsWith("/api/nodes") && opts.method === "POST") return jsonRes({ id: 5, name: "n", address: "a", port: 1, transport: "vision" });
    if (url.endsWith("/api/nodes")) return jsonRes([{ id: 5, name: "n", address: "a", port: 1, transport: "vision" }]);
    if (url.match(/\/api\/nodes\/\d+\/apply/)) return jsonRes({ ok: true });
    if (url.endsWith("/api/subs/preview")) return jsonRes({ method: "GET", url: "u", headers: { "x-hwid": "m" }, query: {} });
    if (url.endsWith("/api/subs") && opts.method === "POST") return jsonRes(subFixture());
    if (url.endsWith("/api/subs")) return jsonRes([subFixture()]);
    if (url.endsWith("/api/settings") && opts.method === "PUT") return jsonRes(settingsFixture(true));
    if (url.endsWith("/api/settings")) return jsonRes(settingsFixture(false));
    if (url.endsWith("/api/profiles/default")) return jsonRes({ ...profileFixture(), is_default: true });
    if (url.match(/\/api\/profiles\/\d+$/) && opts.method === "PATCH") return jsonRes({ ...profileFixture(), name: "renamed" });
    if (url.match(/\/api\/profiles\/\d+$/) && opts.method === "DELETE") return jsonRes({ ok: true });
    if (url.endsWith("/api/profiles") && opts.method === "POST") return jsonRes(profileFixture());
    if (url.endsWith("/api/profiles")) return jsonRes([profileFixture()]);
    if (url.endsWith("/api/routing/preset/ru-direct")) return jsonRes(routingFixture(true));
    if (url.endsWith("/api/routing") && opts.method === "PUT") return jsonRes(routingFixture(true));
    if (url.endsWith("/api/routing")) return jsonRes(routingFixture(false));
    if (url.endsWith("/api/node-health")) return jsonRes([nodeHealthFixture()]);
    if (url.endsWith("/api/network") && opts.method === "PUT") return jsonRes(networkFixture(true));
    if (url.endsWith("/api/network")) return jsonRes(networkFixture(false));
    if (url.endsWith("/api/setup") && opts.method === "POST") return jsonRes({ ok: true });
    if (url.endsWith("/api/setup")) return jsonRes({ needs_setup: false });
    if (url.endsWith("/api/password")) return jsonRes({ ok: true });
    if (url.endsWith("/api/backup")) return jsonRes(backupFixture());
    if (url.endsWith("/api/restore")) return jsonRes({ ok: true, restored: { nodes: 1 } });
    if (url.includes("/api/logs")) return jsonRes({ source: "xray-error", lines: ["a", "b"] });
    return jsonRes({ ok: true });
  });
  (globalThis as any).fetch = f;
  return { f, calls };
}
function backupFixture() {
  return { schema_version: 1, nodes: [], subscriptions: [], profiles: [],
           routing: { rules: [], default_action: "proxy" }, settings: {} };
}
function subFixture() {
  return { id: 9, name: "s", url: "u", injection: {}, interval_sec: 0, last_fetched: null, last_status: null, last_path: null, node_count: 0 };
}
function settingsFixture(changed: boolean) {
  return { tunneled_fetch: true, routing_default_action: changed ? "direct" : "proxy",
           health_enabled: true, health_interval: changed ? 15 : 30, health_hysteresis: 3,
           health_probe_url: "https://api.ipify.org?format=json", failover_enabled: true, failover_cooldown: 120,
           stats_enabled: true, stats_api_port: 10085, traffic_sample_ms: 1000 };
}
function profileFixture() {
  return { id: 3, name: "p", fingerprint: "chrome", frag_enabled: false, frag_packets: "tlshello",
           frag_length: "100-200", frag_interval: "10-20", mux_enabled: false, doh_enabled: true,
           doh_url: "", quic: "allow", is_default: false };
}
function routingFixture(custom: boolean) {
  return { rules: custom ? [{ id: 1, position: 0, type: "geoip", value: "ru", action: "direct" }] : [],
           default_action: custom ? "direct" : "proxy" };
}
function nodeHealthFixture() {
  return { node_id: 5, last_tcp_ok: true, last_tcp_ms: 10, last_real_ok: true, last_real_ms: 20,
           egress_ip: "9.9.9.9", checked_at: "t", fail_count: 0 };
}
function networkFixture(changed: boolean) {
  return {
    segment: { iface: "eth0.2", ip: "192.168.10.2", dhcp_start: "192.168.10.30",
               dhcp_end: changed ? "192.168.10.250" : "192.168.10.200", dhcp_lease: "12h", client_dns: "1.1.1.1" },
    kill_switch_enabled: changed, lan_access_enabled: true,
    status: { segment_up: null, dhcp_clients: 0, tunnel: { real_ok: null, latency_ms: null, egress_ip: null } },
    recommendations: [{ title: "Create VLAN 2", detail: "tag the client port to eth0.2" }],
  };
}
function jsonRes(body: any, status = 200) {
  return { ok: status < 400, status, json: async () => body } as Response;
}

beforeEach(() => { api._reset(); });

describe("api client", () => {
  it("login then csrf caches the token", async () => {
    mockFetch();
    await api.login("admin", "pw");
    const tok = await api.ensureCsrf();
    expect(tok).toBe("tok-123");
  });

  it("sends credentials and X-CSRF-Token on mutations", async () => {
    const { calls } = mockFetch();
    await api.login("admin", "pw");
    await api.ensureCsrf();
    await api.addNode({ name: "n", address: "a", port: 1, uuid: "u" });
    const post = calls.find((c) => c.url.endsWith("/api/nodes") && c.opts.method === "POST");
    expect(post.opts.credentials).toBe("include");
    expect(post.opts.headers["X-CSRF-Token"]).toBe("tok-123");
  });

  it("getStatus returns typed status", async () => {
    mockFetch();
    await api.login("admin", "pw");
    const st = await api.getStatus();
    expect(st).toEqual({ running: true, pid: 7, active_node_id: 2 });
  });

  it("throws ApiError on non-ok", async () => {
    (globalThis as any).fetch = vi.fn(async () => jsonRes({ detail: "bad password" }, 401));
    await expect(api.login("admin", "x")).rejects.toThrow("bad password");
  });

  it("subs: list + add carry the contract and CSRF", async () => {
    const { calls } = mockFetch();
    await api.login("admin", "pw");
    await api.ensureCsrf();
    const subs = await api.listSubs();
    expect(subs[0].node_count).toBe(0);
    await api.addSub({ name: "s", url: "u" });
    const post = calls.find((c) => c.url.endsWith("/api/subs") && c.opts.method === "POST");
    expect(post.opts.headers["X-CSRF-Token"]).toBe("tok-123");
  });

  it("node update/delete use PATCH/DELETE with CSRF", async () => {
    const { calls } = mockFetch();
    await api.login("admin", "pw");
    await api.ensureCsrf();
    await api.updateNode(5, { name: "x" });
    await api.deleteNode(5);
    expect(calls.find((c) => c.url.endsWith("/api/nodes/5") && c.opts.method === "PATCH")).toBeTruthy();
    const del = calls.find((c) => c.url.endsWith("/api/nodes/5") && c.opts.method === "DELETE");
    expect(del.opts.headers["X-CSRF-Token"]).toBe("tok-123");
  });

  it("putSettings sends PUT and previewSub posts", async () => {
    const { calls } = mockFetch();
    await api.login("admin", "pw");
    await api.ensureCsrf();
    const s = await api.putSettings({ health_interval: 15 });
    expect(s.health_interval).toBe(15);
    const prev = await api.previewSub("u", { headers: { "x-hwid": "{machine_id}" } });
    expect(prev.method).toBe("GET");
    expect(calls.find((c) => c.url.endsWith("/api/settings") && c.opts.method === "PUT")).toBeTruthy();
  });

  it("profiles: list/add/update/delete/setDefault carry the contract + CSRF", async () => {
    const { calls } = mockFetch();
    await api.login("admin", "pw");
    await api.ensureCsrf();
    expect((await api.listProfiles())[0].name).toBe("p");
    await api.addProfile({ name: "x", quic: "drop" });
    expect((await api.updateProfile(3, { name: "renamed" })).name).toBe("renamed");
    expect((await api.setDefaultProfile(3)).is_default).toBe(true);
    await api.deleteProfile(3);
    const post = calls.find((c) => c.url.endsWith("/api/profiles") && c.opts.method === "POST");
    expect(post.opts.headers["X-CSRF-Token"]).toBe("tok-123");
    expect(calls.find((c) => c.url.endsWith("/api/profiles/default") && c.opts.method === "PUT")).toBeTruthy();
  });

  it("routing: get/put/preset carry the contract + CSRF", async () => {
    const { calls } = mockFetch();
    await api.login("admin", "pw");
    await api.ensureCsrf();
    expect((await api.getRouting()).default_action).toBe("proxy");
    const r = await api.putRouting({ rules: [{ type: "geoip", value: "ru", action: "direct" }], default_action: "direct" });
    expect(r.default_action).toBe("direct");
    expect((await api.routingPreset("ru-direct")).rules.length).toBeGreaterThan(0);
    expect(calls.find((c) => c.url.endsWith("/api/routing") && c.opts.method === "PUT")).toBeTruthy();
  });

  it("listNodeHealth returns typed rows", async () => {
    mockFetch();
    const h = await api.listNodeHealth();
    expect(h[0].egress_ip).toBe("9.9.9.9");
    expect(h[0].last_real_ok).toBe(true);
  });

  it("network: get + put(partial) carry the contract + CSRF", async () => {
    const { calls } = mockFetch();
    await api.login("admin", "pw");
    await api.ensureCsrf();
    const net = await api.getNetwork();
    expect(net.segment.iface).toBe("eth0.2");
    expect(net.kill_switch_enabled).toBe(false);
    expect(net.lan_access_enabled).toBe(true);
    expect(net.status.tunnel.egress_ip).toBeNull();
    expect(net.recommendations[0].title).toBe("Create VLAN 2");
    const updated = await api.putNetwork({ dhcp_end: "192.168.10.250", kill_switch_enabled: true });
    expect(updated.segment.dhcp_end).toBe("192.168.10.250");
    expect(updated.kill_switch_enabled).toBe(true);
    const put = calls.find((c) => c.url.endsWith("/api/network") && c.opts.method === "PUT");
    expect(put.opts.headers["X-CSRF-Token"]).toBe("tok-123");
    expect(JSON.parse(put.opts.body)).toEqual({ dhcp_end: "192.168.10.250", kill_switch_enabled: true });
  });

  it("setup: getSetup reads status, setup posts username+password", async () => {
    const { calls } = mockFetch();
    expect((await api.getSetup()).needs_setup).toBe(false);
    await api.setup("admin", "s3cret");
    const post = calls.find((c) => c.url.endsWith("/api/setup") && c.opts.method === "POST");
    expect(JSON.parse(post.opts.body)).toEqual({ username: "admin", password: "s3cret" });
  });

  it("changePassword posts current+new with CSRF", async () => {
    const { calls } = mockFetch();
    await api.login("admin", "pw");
    await api.ensureCsrf();
    await api.changePassword("old", "new");
    const post = calls.find((c) => c.url.endsWith("/api/password"));
    expect(post.opts.headers["X-CSRF-Token"]).toBe("tok-123");
    expect(JSON.parse(post.opts.body)).toEqual({ current_password: "old", new_password: "new" });
  });

  it("backup/restore: get + post with CSRF", async () => {
    const { calls } = mockFetch();
    await api.login("admin", "pw");
    await api.ensureCsrf();
    const doc = await api.getBackup();
    expect(doc.schema_version).toBe(1);
    const r = await api.restore(doc);
    expect(r.restored.nodes).toBe(1);
    const post = calls.find((c) => c.url.endsWith("/api/restore"));
    expect(post.opts.headers["X-CSRF-Token"]).toBe("tok-123");
  });

  it("getLogs passes source + lines", async () => {
    const { calls } = mockFetch();
    const out = await api.getLogs("xray-error", 50);
    expect(out.lines).toEqual(["a", "b"]);
    expect(calls.find((c) => c.url.includes("source=xray-error") && c.url.includes("lines=50"))).toBeTruthy();
  });

  it("openTraffic builds a ws and forwards parsed frames", () => {
    let inst: any;
    class FakeWS { onmessage: any = null; constructor(public url: string) { inst = this; } close() {} }
    (globalThis as any).WebSocket = FakeWS;
    const got: any[] = [];
    const ws = api.openTraffic((m) => got.push(m));
    expect(ws.url.startsWith("ws://") && ws.url.endsWith("/api/ws/traffic")).toBe(true);
    inst.onmessage({ data: JSON.stringify({ ts: 1, outbounds: { proxy: { up_bps: 8, down_bps: 16 } }, active: null }) });
    expect(got[0].outbounds.proxy.up_bps).toBe(8);
  });
});
