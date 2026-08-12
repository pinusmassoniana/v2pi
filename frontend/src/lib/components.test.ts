// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import App from "../App.svelte";
import ConfirmModal from "./ConfirmModal.svelte";
import Dashboard from "./Dashboard.svelte";
import Health from "./Health.svelte";
import Login from "./Login.svelte";
import NetworkScreen from "./Network.svelte";
import Nodes from "./Nodes.svelte";
import Operations from "./Operations.svelte";
import RoadWarrior from "./RoadWarrior.svelte";
import Routing from "./Routing.svelte";
import SettingsScreen from "./Settings.svelte";
import Setup from "./Setup.svelte";
import Subscriptions from "./Subscriptions.svelte";
import Tuning from "./Tuning.svelte";
import { api, ApiError, type Network, type Node, type Rw, type Settings, type Status, type Subscription, type TuningProfile } from "./api";
import { pollStatusOnce, resetStatus } from "./status.svelte";

const mounted: object[] = [];

const STATUS: Status = {
  running: true, pid: 7, active_node_id: 1, xray_state: "working", active_since: 1_700_000_000,
  last_failover_at: null, prev_active_node_id: null, server_now: 1_700_000_100,
};

function network(): Network {
  return {
    segment: {
      iface: "eth0.2", ip: "192.168.10.2", ip6: "", dhcp_start: "192.168.10.30",
      dhcp_end: "192.168.10.200", dhcp_lease: "12h", client_dns: "1.1.1.1", client_dns6: "",
    },
    kill_switch_enabled: true, lan_access_enabled: true, ipv6_enabled: false,
    status: {
      segment_up: true, uplink: true, uplink6: null, dhcp_clients: 0, clients: [], wan_blocked: false,
      tunnel: { real_ok: true, latency_ms: 20, egress_ip: "203.0.113.8", checked_at: new Date().toISOString() },
      ipv6_prefix: null, foreign_ra: false, ipv6_prefix_source: null, enforcement_warning: "",
    },
    recommendations: [], events: [],
  };
}

function rw(over: Partial<Rw> = {}): Rw {
  return {
    enabled: false, port: 443, dest: "www.microsoft.com:443", server_names: "www.microsoft.com",
    short_ids: "ab12cd34", public_key: "PUB", endpoint: "home.example.org",
    has_private_key: false, state_error: "", hosts: {}, routed_nets: ["192.168.10.0/24"],
    routed_nets_override: "", clients: [], live: false, revocation: "", ...over,
  };
}

function node(id: number, name: string): Node {
  return {
    id, name, address: `${name}.example.org`, port: 443, uuid: `uuid-${id}`, transport: "vision",
    network: "tcp", security: "reality", sni: "", public_key: "", short_id: "", fingerprint: "chrome",
    path: "", host: "", mode: "", alpn: "", note: "", subscription_id: null, stale: false,
    tuning_profile_id: null,
  };
}

function subscription(id: number, name: string, over: Partial<Subscription> = {}): Subscription {
  return {
    id, name, url: `https://example.org/${name}`, injection: {}, interval_sec: 0, enabled: true,
    default_profile_id: null, last_fetched: null, last_status: null, last_path: null, last_error: null,
    up_bytes: null, down_bytes: null, total_bytes: null, expire_at: null, node_count: 0, ...over,
  };
}

function profile(id: number, name: string, over: Partial<TuningProfile> = {}): TuningProfile {
  return {
    id, name, fingerprint: "chrome", frag_enabled: false, frag_packets: "tlshello", frag_length: "100-200",
    frag_interval: "10-20", mux_enabled: false, doh_enabled: true, doh_url: "", quic: "allow",
    noise_enabled: false, noises: [], xhttp_padding: "", xmux_max_concurrency: "", xmux_max_connections: "",
    mux_concurrency: "", xudp_proxy_udp443: "", alpn: "", tls_min: "", tls_max: "",
    is_default: false, is_active: false, node_count: 0, ...over,
  };
}

function settings(): Settings {
  return {
    tunneled_fetch: true, subs_auto_switch: true, routing_default_action: "proxy", health_enabled: true,
    health_sweep_enabled: true, health_active_interval: 60,
    health_interval: 1800, health_hysteresis: 3, health_probe_url: "https://example.com",
    failover_enabled: true, failover_cooldown: 120, stats_enabled: true,
    stats_api_port: 10085, traffic_sample_ms: 1000, dns_intercept: true,
    session_timeout_min: 30, auto_backup_enabled: true,
  };
}

function setupApi() {
  vi.spyOn(api, "getSetup").mockResolvedValue({ needs_setup: false });
  vi.spyOn(api, "getStatus").mockResolvedValue(STATUS);
  vi.spyOn(api, "listNodes").mockResolvedValue([]);
  vi.spyOn(api, "listNodeHealth").mockResolvedValue([]);
  vi.spyOn(api, "listProfiles").mockResolvedValue([]);
  vi.spyOn(api, "listProfilePresets").mockResolvedValue([]);
  vi.spyOn(api, "listRoutingPresets").mockResolvedValue([]);
  vi.spyOn(api, "listSubs").mockResolvedValue([]);
  vi.spyOn(api, "getNetwork").mockImplementation(async () => network());
  vi.spyOn(api, "getRw").mockImplementation(async () => rw());
  vi.spyOn(api, "getSettings").mockImplementation(async () => settings());
  vi.spyOn(api, "getRouting").mockResolvedValue({ rules: [], default_action: "proxy", domain_strategy: "IPIfNonMatch" });
  vi.spyOn(api, "getTrafficHistory").mockResolvedValue({ samples: [], interval_ms: 1000 });
  vi.spyOn(api, "listTokens").mockResolvedValue([]);
  vi.spyOn(api, "connectTraffic").mockReturnValue({ close: vi.fn(), resume: vi.fn() });
}

async function flush() {
  await Promise.resolve();
  await tick();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await tick();
}

function setValue(input: HTMLInputElement, value: string) {
  input.value = value;
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

beforeEach(() => {
  document.body.innerHTML = "";
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
  Object.defineProperty(document, "hidden", { configurable: true, value: false });
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  vi.stubGlobal("ResizeObserver", ResizeObserverStub);
  resetStatus();
  setupApi();
});

afterEach(async () => {
  while (mounted.length) await unmount(mounted.pop()!);
  resetStatus();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

describe("mounted frontend regressions", () => {
  it("keeps the Health throughput window selected in the child graph", async () => {
    mounted.push(mount(Health, { target: document.body }));
    await flush();
    const oneHour = [...document.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
      .find((button) => button.textContent?.trim() === "1h")!;
    oneHour.click();
    await flush();
    expect(oneHour.getAttribute("aria-selected")).toBe("true");
  });

  it("marks charts and latency stale when the active health frame is stale", async () => {
    let traffic!: (message: any) => void;
    vi.mocked(api.connectTraffic).mockImplementation((onMessage) => {
      traffic = onMessage;
      return { close: vi.fn(), resume: vi.fn() };
    });
    mounted.push(mount(Health, { target: document.body }));
    await flush();
    traffic({
      ts: Date.now(), outbounds: { proxy: { up_bps: 1, down_bps: 2 } }, totals: { up: 1, down: 2 },
      active: {
        node_id: 1, real_ok: true, stale: true, latency_ms: 8, egress_ip: "203.0.113.8",
        egress_ip6: null, egress_cc: "NL", egress_cc6: null, checked_at: new Date(0).toISOString(), lat_history: [8, 9],
      },
    });
    await tick();
    expect(document.body.textContent).toContain("stale");
    expect(document.querySelector('[role="img"]')?.getAttribute("aria-label")).toContain("tunnel health stale");
  });

  it("disables every Network switch while an immutable apply is pending", async () => {
    let resolve!: (value: Network) => void;
    vi.spyOn(api, "putNetwork").mockReturnValue(new Promise((done) => { resolve = done; }));
    mounted.push(mount(NetworkScreen, { target: document.body }));
    await flush();
    const toggle = document.querySelector<HTMLButtonElement>('[role="switch"][aria-label="lan-access"]')!;
    toggle.click();
    await tick();
    document.querySelector<HTMLButtonElement>("button.btn-primary")!.click();
    await tick();
    for (const control of document.querySelectorAll<HTMLButtonElement>('[role="switch"]')) {
      expect(control.disabled).toBe(true);
    }
    resolve(network());
  });

  it("shows a partially-applied network as a warning, not as a failure", async () => {
    // The apply succeeded and enforcement is up; only the LAN-access rules are missing. That
    // state used to be invisible outside the server log.
    vi.mocked(api.getNetwork).mockImplementation(async () => {
      const n = network();
      n.status.enforcement_status = "ok";
      n.status.enforcement_warning =
        "LAN access chain not applied: iptables: No chain/target/match by that name.";
      return n;
    });
    mounted.push(mount(NetworkScreen, { target: document.body }));
    await flush();
    expect(document.body.textContent).toContain("LAN access chain not applied");
    expect(document.querySelector(".warn-row.soft")).not.toBeNull();
    // …and it is not dressed up as a hard failure (no error-toned row anywhere).
    expect(document.querySelector(".sdot.bad")).toBeNull();
    expect(document.body.textContent).not.toContain("leak protection is not confirmed");
  });

  it("renders no network warning row when the last apply was clean", async () => {
    mounted.push(mount(NetworkScreen, { target: document.body }));
    await flush();
    expect(document.querySelector(".warn-row")).toBeNull();
    expect(document.body.textContent).not.toContain("Applied with a warning");
  });

  it("cannot arm the remote-access inbound before a private key is stored", async () => {
    vi.spyOn(api, "getRw").mockImplementation(async () => rw({ has_private_key: false }));
    mounted.push(mount(RoadWarrior, { target: document.body }));
    await flush();
    // Arming without a key would produce a config xray rejects — the control stays locked
    // rather than letting the operator find out via a failed apply.
    expect(document.querySelector<HTMLButtonElement>('[role="switch"][aria-label="rw-enabled"]')!.disabled).toBe(true);
  });

  it("says out loud that an enabled-but-clientless inbound is not listening", async () => {
    vi.spyOn(api, "getRw").mockImplementation(async () =>
      rw({ enabled: true, has_private_key: true, clients: [] }));
    mounted.push(mount(RoadWarrior, { target: document.body }));
    await flush();
    // No inbound is emitted at all in this state (xray won't start on an empty client list),
    // so "enabled" must not read as "listening".
    expect(document.body.textContent).toContain("nothing is listening");
  });

  it("warns that stored remote-access settings are not live without an active node", async () => {
    vi.spyOn(api, "getRw").mockImplementation(async () =>
      rw({ enabled: true, has_private_key: true, live: false,
           clients: [{ id: "cid", email: "iphone", enabled: true }] }));
    mounted.push(mount(RoadWarrior, { target: document.body }));
    await flush();
    expect(document.body.textContent).toContain("not in the running config yet");
  });

  it("trusts the server's live flag over a cleared active_node_id (disconnect keeps the inbound up)", async () => {
    // disconnect() deliberately clears active_node_id while the road-warrior inbound can keep
    // running — the banner must follow rw.live, not re-derive liveness from active_node_id.
    vi.spyOn(api, "getStatus").mockResolvedValue({ ...STATUS, active_node_id: null });
    await pollStatusOnce();
    vi.spyOn(api, "getRw").mockImplementation(async () =>
      rw({ enabled: true, has_private_key: true, live: true,
           clients: [{ id: "cid", email: "iphone", enabled: true }] }));
    mounted.push(mount(RoadWarrior, { target: document.body }));
    await flush();
    expect(document.body.textContent).not.toContain("not in the running config yet");
  });

  it("refetches remote-access state when xray's running state flips with the same active node (FIX-K)", async () => {
    // A crash/restart that leaves active_node_id unchanged must still trigger a refetch — the
    // old cue watched active_node_id alone, so the banner could keep showing a stale `live`
    // value indefinitely with the truth having changed underneath.
    await pollStatusOnce();   // baseline: STATUS has running: true, active_node_id: 1
    const getRw = vi.spyOn(api, "getRw").mockImplementation(async () =>
      rw({ enabled: true, has_private_key: true, live: true,
           clients: [{ id: "cid", email: "iphone", enabled: true }] }));
    mounted.push(mount(RoadWarrior, { target: document.body }));
    await flush();
    expect(getRw).toHaveBeenCalledTimes(1);
    vi.spyOn(api, "getStatus").mockResolvedValue({ ...STATUS, running: false });   // same node, xray down
    await pollStatusOnce();
    await flush();
    expect(getRw).toHaveBeenCalledTimes(2);
  });

  it("refuses to save a half-filled host row instead of dropping it silently", async () => {
    const put = vi.spyOn(api, "putRw");
    vi.spyOn(api, "getRw").mockImplementation(async () =>
      rw({ has_private_key: true, hosts: { "nas.v2pi": "192.168.1.88" } }));
    mounted.push(mount(RoadWarrior, { target: document.body }));
    await flush();
    // clear the IP of the existing row, leaving a name with no address
    const ipInput = [...document.querySelectorAll<HTMLInputElement>(".host-row input")][1];
    ipInput.value = "";
    ipInput.dispatchEvent(new Event("input", { bubbles: true }));
    await tick();
    document.querySelector<HTMLButtonElement>("button.btn-primary")!.click();
    await flush();
    expect(put).not.toHaveBeenCalled();
    expect(document.body.textContent).toContain("needs both a name and an IP");
  });

  it("warns when the Reality server name does not match dest", async () => {
    vi.spyOn(api, "getRw").mockImplementation(async () =>
      rw({ has_private_key: true, dest: "www.microsoft.com:443", server_names: "example.org" }));
    mounted.push(mount(RoadWarrior, { target: document.body }));
    await flush();
    expect(document.body.textContent).toContain("does not match");
  });

  it("does not warn when the server name matches dest", async () => {
    vi.spyOn(api, "getRw").mockImplementation(async () =>
      rw({ has_private_key: true, dest: "www.microsoft.com:443", server_names: "www.microsoft.com" }));
    mounted.push(mount(RoadWarrior, { target: document.body }));
    await flush();
    expect(document.body.textContent).not.toContain("does not match");
  });

  it("surfaces malformed stored settings instead of rendering them as truth", async () => {
    vi.spyOn(api, "getRw").mockImplementation(async () =>
      rw({ state_error: "rw_port must be an integer, got 'not-a-number'" }));
    mounted.push(mount(RoadWarrior, { target: document.body }));
    await flush();
    expect(document.body.textContent).toContain("malformed and were ignored");
  });

  it("suspends a client without removing it", async () => {
    const patch = vi.spyOn(api, "setRwClientEnabled").mockImplementation(async () =>
      rw({ has_private_key: true, clients: [{ id: "cid", email: "iphone", enabled: false }] }));
    vi.spyOn(api, "getRw").mockImplementation(async () =>
      rw({ has_private_key: true, clients: [{ id: "cid", email: "iphone", enabled: true }] }));
    mounted.push(mount(RoadWarrior, { target: document.body }));
    await flush();
    [...document.querySelectorAll<HTMLButtonElement>(".client-acts button")]
      .find((b) => b.textContent === "Suspend")!.click();
    await flush();
    expect(patch).toHaveBeenCalledWith("cid", false);
    expect(document.body.textContent).toContain("suspended");
  });

  it("exposes and can disable unattended subscription-driven node switching", async () => {
    const put = vi.spyOn(api, "putSettings").mockImplementation(async () => settings());
    mounted.push(mount(SettingsScreen, { target: document.body }));
    await flush();
    const toggle = document.querySelector<HTMLButtonElement>('[role="switch"][aria-label="subscription auto-switch"]')!;
    expect(toggle.getAttribute("aria-checked")).toBe("true");   // server default is on — must be visible, not hidden
    expect(document.body.textContent).toContain("replace the active node on its own, with no operator action");
    toggle.click();
    await tick();
    document.querySelector<HTMLFormElement>("form")!.requestSubmit();
    await flush();
    const sent = put.mock.calls[0][0] as any;
    expect(sent.subs_auto_switch).toBe(false);
  });

  it("lets the all-server sweep be turned off while the active-server check stays on", async () => {
    const put = vi.spyOn(api, "putSettings").mockImplementation(async () => settings());
    mounted.push(mount(SettingsScreen, { target: document.body }));
    await flush();
    document.querySelector<HTMLButtonElement>('[role="switch"][aria-label="health sweep"]')!.click();
    await tick();
    document.querySelector<HTMLFormElement>("form")!.requestSubmit();
    await flush();
    const sent = put.mock.calls[0][0] as any;
    expect(sent.health_sweep_enabled).toBe(false);
    expect(sent.health_enabled).toBe(true);      // failover keeps its input
  });

  it("disables both cadence fields when the health master switch is off", async () => {
    vi.spyOn(api, "getSettings").mockImplementation(async () => ({ ...settings(), health_enabled: false }));
    mounted.push(mount(SettingsScreen, { target: document.body }));
    await flush();
    const nums = [...document.querySelectorAll<HTMLInputElement>(".field.sub input")];
    expect(nums.length).toBe(2);
    expect(nums.every((i) => i.disabled)).toBe(true);
  });

  it("rejects an active-check cadence below the server floor before sending it", async () => {
    const put = vi.spyOn(api, "putSettings");
    vi.spyOn(api, "getSettings").mockImplementation(async () => ({ ...settings(), health_active_interval: 5 }));
    mounted.push(mount(SettingsScreen, { target: document.body }));
    await flush();
    document.querySelector<HTMLFormElement>("form")!.requestSubmit();
    await flush();
    expect(put).not.toHaveBeenCalled();
    expect(document.body.textContent).toContain("≥ 10 s");
  });

  it("keeps API-token creation single-flight", async () => {
    let resolve!: (value: any) => void;
    const create = vi.spyOn(api, "createToken").mockReturnValue(new Promise((done) => { resolve = done; }));
    mounted.push(mount(SettingsScreen, { target: document.body }));
    await flush();
    const name = document.querySelector<HTMLInputElement>('input[placeholder^="token name"]')!;
    setValue(name, "monitor");
    await tick();
    const form = name.closest("form")!;
    form.dispatchEvent(new SubmitEvent("submit", { bubbles: true, cancelable: true }));
    form.dispatchEvent(new SubmitEvent("submit", { bubbles: true, cancelable: true }));
    expect(create).toHaveBeenCalledTimes(1);
    resolve({ id: 1, name: "monitor", scope: "monitor", prefix: "pgwp_x", token: "secret", created_at: 1, last_used_at: null });
  });

  it("distinguishes an audit load failure and can retry to a genuine empty state", async () => {
    const listAudit = vi.spyOn(api, "listAudit")
      .mockRejectedValueOnce(new ApiError(500, "audit unavailable"))
      .mockResolvedValueOnce([]);
    mounted.push(mount(SettingsScreen, { target: document.body }));
    await flush();
    [...document.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.trim() === "Show")!.click();
    await flush();
    expect(document.body.textContent).toContain("audit unavailable");
    expect(document.body.textContent).not.toContain("No recorded changes yet.");
    [...document.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.trim() === "Retry / refresh")!.click();
    await flush();
    expect(document.body.textContent).not.toContain("audit unavailable");
    expect(document.body.textContent).toContain("No recorded changes yet.");
    expect(listAudit).toHaveBeenCalledTimes(2);
  });

  it("cancels navigation away from staged Network changes", async () => {
    mounted.push(mount(App, { target: document.body }));
    await flush();
    [...document.querySelectorAll<HTMLButtonElement>(".nav-item")].find((b) => b.textContent?.includes("Network"))!.click();
    await flush();
    document.querySelector<HTMLButtonElement>('[role="switch"][aria-label="lan-access"]')!.click();
    await tick();
    [...document.querySelectorAll<HTMLButtonElement>(".nav-item")].find((b) => b.textContent?.includes("Overview"))!.click();
    await flush();
    expect(document.querySelector(".page-title")?.textContent).toContain("Network");
    expect(document.body.textContent).toContain("Discard staged changes");
    [...document.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.trim() === "Cancel")!.click();
    await flush();
    expect(document.querySelector(".page-title")?.textContent).toContain("Network");
    [...document.querySelectorAll<HTMLButtonElement>(".nav-item")].find((b) => b.textContent?.includes("Overview"))!.click();
    await flush();
    [...document.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.trim() === "Confirm")!.click();
    await flush();
    expect(document.querySelector(".page-title")?.textContent).toContain("Overview");
  });

  it("stages every routing preset field without applying it", async () => {
    vi.mocked(api.listRoutingPresets).mockResolvedValue([{ name: "strict", title: "Strict" }]);
    vi.spyOn(api, "routingPreset").mockResolvedValue({
      rules: [{ id: 4, position: 0, type: "domain", value: "blocked.example", action: "block", enabled: true, label: "preset" }],
      default_action: "direct",
      domain_strategy: "AsIs",
    });
    const save = vi.spyOn(api, "putRouting");
    mounted.push(mount(Routing, { target: document.body }));
    await flush();
    const preset = document.querySelector<HTMLSelectElement>('select[aria-label="Import routing preset"]')!;
    preset.value = "strict";
    preset.dispatchEvent(new Event("change", { bubbles: true }));
    await flush();
    const labelled = [...document.querySelectorAll<HTMLLabelElement>("label.inline")];
    const defaultSelect = labelled.find((label) => label.textContent?.includes("Default"))!.querySelector("select")!;
    const domainSelect = labelled.find((label) => label.textContent?.includes("Domain strategy"))!.querySelector("select")!;
    expect(document.querySelector<HTMLInputElement>('input[aria-label="Rule 1 value"]')?.value).toBe("blocked.example");
    expect(defaultSelect.value).toBe("direct");
    expect(domainSelect.value).toBe("AsIs");
    expect(document.body.textContent).toContain("STAGED");
    expect(save).not.toHaveBeenCalled();
  });

  it("keeps password reveal keyboard-focusable and announces login errors", async () => {
    vi.spyOn(api, "login").mockRejectedValue(new ApiError(401, "bad password"));
    mounted.push(mount(Login, { target: document.body, props: { onLogin: vi.fn() } }));
    await flush();
    const username = document.querySelector<HTMLInputElement>('#username')!;
    const password = document.querySelector<HTMLInputElement>('#password')!;
    setValue(username, "admin"); setValue(password, "wrong");
    document.querySelector("form")!.dispatchEvent(new SubmitEvent("submit", { bubbles: true, cancelable: true }));
    await flush();
    expect(document.querySelector<HTMLButtonElement>(".pw-toggle")!.tabIndex).toBeGreaterThanOrEqual(0);
    expect(document.querySelector('[role="alert"]')?.textContent).toContain("bad password");
  });

  it("requires and forwards the production bootstrap proof", async () => {
    const setup = vi.spyOn(api, "setup").mockResolvedValue({ ok: true });
    vi.spyOn(api, "ensureCsrf").mockResolvedValue("csrf");
    mounted.push(mount(Setup, { target: document.body, props: { bootstrapRequired: true, onDone: vi.fn() } }));
    await flush();
    setValue(document.querySelector<HTMLInputElement>('input[name="bootstrap-token"]')!, "proof");
    setValue(document.querySelector<HTMLInputElement>('#username')!, "admin");
    setValue(document.querySelector<HTMLInputElement>('#new-password')!, "password1");
    setValue(document.querySelector<HTMLInputElement>('#confirm-password')!, "password1");
    document.querySelector("form")!.dispatchEvent(new SubmitEvent("submit", { bubbles: true, cancelable: true }));
    await flush();
    expect(setup).toHaveBeenCalledWith("admin", "password1", "proof");
  });

  it("cannot double-fire a node reorder while one is in flight (F9-1)", async () => {
    vi.spyOn(api, "listNodes").mockResolvedValue([node(1, "n1"), node(2, "n2")]);
    let resolveReorder!: () => void;
    const reorder = vi.spyOn(api, "reorderNodes")
      .mockReturnValue(new Promise((resolve) => { resolveReorder = () => resolve(undefined); }));
    mounted.push(mount(Nodes, { target: document.body }));
    await flush();
    const down = document.querySelector<HTMLButtonElement>('[aria-label="move down"]')!;
    down.click();
    await tick();
    expect(reorder).toHaveBeenCalledTimes(1);
    expect(down.disabled).toBe(true);   // busy guard, mirrors applyingId/probingId elsewhere in this file
    down.click();                       // must be a no-op — the first reorder hasn't resolved yet
    await tick();
    expect(reorder).toHaveBeenCalledTimes(1);
    resolveReorder();
    await flush();
  });

  it("warns before discarding a dirty Add-node modal instead of closing silently (F9-3)", async () => {
    mounted.push(mount(Nodes, { target: document.body }));
    mounted.push(mount(ConfirmModal, { target: document.body }));
    await flush();
    [...document.querySelectorAll<HTMLButtonElement>("button")]
      .find((b) => b.textContent?.trim() === "+ Add server")!.click();
    await tick();
    const name = document.querySelector<HTMLInputElement>('input[aria-label="Node name"]')!;
    setValue(name, "staged-node");
    await tick();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await flush();
    expect(document.body.textContent).toContain("Discard unsaved changes?");
    // Cancel: the dirty form must still be there — nothing silently discarded.
    [...document.querySelectorAll<HTMLButtonElement>("button")]
      .find((b) => b.textContent?.trim() === "Cancel")!.click();
    await flush();
    expect(document.querySelector('input[aria-label="Node name"]')).not.toBeNull();
    // Confirm: now it actually closes.
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await flush();
    [...document.querySelectorAll<HTMLButtonElement>("button")]
      .find((b) => b.textContent?.trim() === "Confirm")!.click();
    await flush();
    expect(document.querySelector('input[aria-label="Node name"]')).toBeNull();
  });

  it("asks for confirmation before disarming the kill-switch (F9-5)", async () => {
    mounted.push(mount(NetworkScreen, { target: document.body }));
    mounted.push(mount(ConfirmModal, { target: document.body }));
    await flush();
    const toggle = document.querySelector<HTMLButtonElement>('[role="switch"][aria-label="kill-switch"]')!;
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    toggle.click();
    await flush();
    expect(document.body.textContent).toContain("Disarm the fail-closed kill-switch");
    expect(toggle.getAttribute("aria-checked")).toBe("true");   // unchanged until confirmed
    [...document.querySelectorAll<HTMLButtonElement>("button")]
      .find((b) => b.textContent?.trim() === "Confirm")!.click();
    await flush();
    expect(toggle.getAttribute("aria-checked")).toBe("false");
  });

  it("surfaces a stopped road-warrior revocation instead of leaving it invisible", async () => {
    vi.spyOn(api, "getRw").mockImplementation(async () =>
      rw({ has_private_key: true, clients: [{ id: "cid", email: "iphone", enabled: true }] }));
    vi.spyOn(api, "deleteRwClient").mockImplementation(async () =>
      rw({ has_private_key: true, clients: [], revocation: "stopped" }));
    mounted.push(mount(RoadWarrior, { target: document.body }));
    mounted.push(mount(ConfirmModal, { target: document.body }));
    await flush();
    [...document.querySelectorAll<HTMLButtonElement>(".client-acts button")]
      .find((b) => b.textContent === "Remove")!.click();
    await flush();
    [...document.querySelectorAll<HTMLButtonElement>("button")]
      .find((b) => b.textContent?.trim() === "Confirm")!.click();
    await flush();
    expect(document.body.textContent).toContain("xray stopped");
  });

  // A completed-but-not-reloaded revocation ("cleaned") and a revocation that found nothing to
  // cut ("not-live") used to share one backend value and therefore one line of copy — the one
  // that says nothing was cut. An operator who has just lost a phone reads that as "the panel
  // did not do it". The two must read as opposites.
  async function removeOnlyClient(revocation: Rw["revocation"]) {
    vi.spyOn(api, "getRw").mockImplementation(async () =>
      rw({ has_private_key: true, clients: [{ id: "cid", email: "iphone", enabled: true }] }));
    vi.spyOn(api, "deleteRwClient").mockImplementation(async () =>
      rw({ has_private_key: true, clients: [], revocation }));
    mounted.push(mount(RoadWarrior, { target: document.body }));
    mounted.push(mount(ConfirmModal, { target: document.body }));
    await flush();
    [...document.querySelectorAll<HTMLButtonElement>(".client-acts button")]
      .find((b) => b.textContent === "Remove")!.click();
    await flush();
    [...document.querySelectorAll<HTMLButtonElement>("button")]
      .find((b) => b.textContent?.trim() === "Confirm")!.click();
    await flush();
    return document.body.textContent ?? "";
  }

  it("says a cleaned revocation was written to the stored config, not that nothing was cut", async () => {
    const text = await removeOnlyClient("cleaned");
    expect(text).toContain("cannot come back on the next start");
    expect(text).not.toContain("no live access to cut");
  });

  it("still says nothing was cut when nothing was actually serving the inbound", async () => {
    const text = await removeOnlyClient("not-live");
    expect(text).toContain("no live access to cut");
    expect(text).not.toContain("cannot come back on the next start");
  });

  // A stop that FAILED used to come back as "stopped", so the operator who had just removed a
  // lost phone read "xray stopped — remote access is down for everyone" about a process that
  // survived SIGKILL and is still serving that phone's credential. It is the one outcome on this
  // screen that has to read as a warning rather than a status line.
  it("warns that a revoked device may still be live when xray could not be stopped", async () => {
    const text = await removeOnlyClient("stop-failed");
    expect(text).toContain("SECURITY WARNING");
    expect(text).toContain("may still be able to connect");
    // "stop-failed" also covers stop() raising, where the panel never learns what happened to
    // the process — a specific "survived SIGKILL" diagnosis is not established for that case.
    expect(text).toContain("could not be confirmed");
    expect(text).not.toContain("SIGKILL");
    expect(text).not.toContain("remote access is down for everyone");
    const alert = document.querySelector(".msg")!;
    expect(alert.classList.contains("err")).toBe(true);   // never presented as a success
    expect(alert.getAttribute("role")).toBe("alert");
  });

  it("warns the same way when a narrowing save could not stop xray", async () => {
    vi.spyOn(api, "getRw").mockImplementation(async () =>
      rw({ enabled: true, has_private_key: true,
           clients: [{ id: "cid", email: "iphone", enabled: true }] }));
    vi.spyOn(api, "putRw").mockImplementation(async () =>
      rw({ enabled: true, has_private_key: true, clients: [], revocation: "stop-failed" }));
    mounted.push(mount(RoadWarrior, { target: document.body }));
    await flush();
    document.querySelector<HTMLButtonElement>("button.btn-primary")!.click();
    await flush();
    expect(document.body.textContent).toContain("SECURITY WARNING");
    expect(document.body.textContent).toContain("may still be live");
    expect(document.body.textContent).toContain("could not be confirmed");
    expect(document.body.textContent).not.toContain("SIGKILL");
    expect(document.querySelector(".msg")!.classList.contains("err")).toBe(true);
  });

  it("distinguishes the same two outcomes on a save that narrowed access", async () => {
    // The save path has its own copy (a narrowing save removes no named client, so "without it"
    // has no antecedent) — and therefore its own way of conflating the two outcomes.
    vi.spyOn(api, "getRw").mockImplementation(async () =>
      rw({ enabled: true, has_private_key: true,
           clients: [{ id: "cid", email: "iphone", enabled: true }] }));
    vi.spyOn(api, "putRw").mockImplementation(async () =>
      rw({ enabled: true, has_private_key: true, clients: [], revocation: "cleaned" }));
    mounted.push(mount(RoadWarrior, { target: document.body }));
    await flush();
    document.querySelector<HTMLButtonElement>("button.btn-primary")!.click();
    await flush();
    expect(document.body.textContent).toContain("cannot come back on the next start");
    expect(document.body.textContent).not.toContain("nothing live to narrow");
  });

  it("does not delete a node until the confirm dialog is confirmed (T2)", async () => {
    vi.spyOn(api, "listNodes").mockResolvedValue([node(1, "n1")]);
    const del = vi.spyOn(api, "deleteNode").mockResolvedValue(undefined as any);
    mounted.push(mount(Nodes, { target: document.body }));
    mounted.push(mount(ConfirmModal, { target: document.body }));
    await flush();
    document.querySelector<HTMLButtonElement>('[aria-label="Delete node"]')!.click();
    await tick();
    expect(document.body.textContent).toContain("Delete server");
    expect(del).not.toHaveBeenCalled();
    [...document.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.trim() === "Cancel")!.click();
    await flush();
    expect(del).not.toHaveBeenCalled();
    expect(document.querySelector('[aria-label="Delete node"]')).not.toBeNull();   // row still there — nothing silently dropped
    document.querySelector<HTMLButtonElement>('[aria-label="Delete node"]')!.click();
    await tick();
    [...document.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.trim() === "Confirm")!.click();
    await flush();
    expect(del).toHaveBeenCalledWith(1);
  });

  it("does not bulk-delete nodes until the confirm dialog is confirmed (T2)", async () => {
    vi.spyOn(api, "listNodes").mockResolvedValue([node(1, "n1"), node(2, "n2")]);
    const del = vi.spyOn(api, "deleteNode").mockResolvedValue(undefined as any);
    mounted.push(mount(Nodes, { target: document.body }));
    mounted.push(mount(ConfirmModal, { target: document.body }));
    await flush();
    document.querySelector<HTMLInputElement>('input[aria-label="select all"]')!.click();
    await tick();
    const bulkDeleteBtn = () => [...document.querySelectorAll<HTMLButtonElement>(".bulk button")].find((b) => b.textContent?.trim() === "Delete")!;
    bulkDeleteBtn().click();
    await tick();
    expect(document.body.textContent).toContain("Delete 2 server(s)?");
    expect(del).not.toHaveBeenCalled();
    [...document.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.trim() === "Cancel")!.click();
    await flush();
    expect(del).not.toHaveBeenCalled();
    expect(document.body.textContent).toContain("2 selected");   // selection preserved, nothing silently dropped
    bulkDeleteBtn().click();
    await tick();
    [...document.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.trim() === "Confirm")!.click();
    await flush();
    expect(del).toHaveBeenCalledTimes(2);
    expect(del).toHaveBeenCalledWith(1);
    expect(del).toHaveBeenCalledWith(2);
  });

  it("does not restore a backup until the confirm dialog is confirmed (T3)", async () => {
    const restore = vi.spyOn(api, "restore").mockResolvedValue({ ok: true, restored: { nodes: 1, profiles: 0 } });
    mounted.push(mount(Operations, { target: document.body }));
    mounted.push(mount(ConfirmModal, { target: document.body }));
    await flush();
    const input = document.querySelector<HTMLInputElement>('input[type="file"]')!;
    const doc = { schema_version: 1, nodes: [] };
    function selectBackup() {
      const file = new File([JSON.stringify(doc)], "backup.json", { type: "application/json" });
      Object.defineProperty(input, "files", { value: [file], configurable: true });
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }
    selectBackup();
    await flush();
    expect(document.body.textContent).toContain("Restore replaces all nodes");
    expect(restore).not.toHaveBeenCalled();
    [...document.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.trim() === "Cancel")!.click();
    await flush();
    expect(restore).not.toHaveBeenCalled();
    selectBackup();
    await flush();
    [...document.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.trim() === "Confirm")!.click();
    await flush();
    expect(restore).toHaveBeenCalledWith(doc);
  });

  it("does not reset settings to defaults until the confirm dialog is confirmed (T3)", async () => {
    const reset = vi.spyOn(api, "resetSettings").mockResolvedValue(settings());
    mounted.push(mount(Operations, { target: document.body }));
    mounted.push(mount(ConfirmModal, { target: document.body }));
    await flush();
    const resetBtn = document.querySelector<HTMLButtonElement>(".danger .btn-danger")!;
    resetBtn.click();
    await tick();
    expect(document.body.textContent).toContain("Reset all panel settings");
    expect(reset).not.toHaveBeenCalled();
    [...document.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.trim() === "Cancel")!.click();
    await flush();
    expect(reset).not.toHaveBeenCalled();
    resetBtn.click();
    await tick();
    [...document.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.trim() === "Confirm")!.click();
    await flush();
    expect(reset).toHaveBeenCalledTimes(1);
  });

  it("mounts the Overview dashboard and renders live status without throwing (T4)", async () => {
    // NOTE: api.rollback() (Dashboard.svelte's config-rollback handler, confirmDialog-gated) has
    // no caller anywhere in this file or in ConnFlow.svelte — its "Rollback" button was removed by
    // commit a34e293 ("Overview rebuilt to dense NOC layout") while the handler was left behind.
    // The gate can't be exercised via user interaction because there is no control left to click;
    // this is flagged separately rather than faked with a synthetic invocation.
    vi.spyOn(api, "listNodes").mockResolvedValue([node(1, "n1")]);
    mounted.push(mount(Dashboard, { target: document.body }));
    await flush();
    expect(document.body.textContent).toContain("Live Traffic");
    expect(document.body.textContent).toContain("Upstream Health");
    expect(document.body.textContent).toContain("n1");
  });

  it("reports a config the running xray never loaded, beside the process state", async () => {
    // `running: true` is not "serving the config on disk": a rewrite nobody reloaded (a
    // revocation whose reload threw, a hand-edit, a restored backup) keeps the old config —
    // and the client it still admits — live. The strip said RUNNING and nothing else.
    vi.spyOn(api, "getStatus").mockResolvedValue({ ...STATUS, config_drift: "drift" });
    mounted.push(mount(Dashboard, { target: document.body }));
    await flush();
    expect(document.body.textContent).toContain("STALE CONFIG");
    expect(document.body.textContent).toContain("Restart Xray");
    expect(document.querySelector('[role="alert"]')).not.toBeNull();
    // still honest about the process itself — it IS running
    expect(document.body.textContent).toContain("RUNNING");
  });

  it("does not tell the operator which configuration is the older one", async () => {
    // Two unequal digests prove the live process and the file DIFFER; they prove nothing about
    // which came first. Restore an older backup over config.json and the older side is the file,
    // so "Xray is running on an older configuration" is wrong exactly there — and it sends the
    // operator hunting for a change nobody made instead of restarting onto the file.
    vi.spyOn(api, "getStatus").mockResolvedValue({ ...STATUS, config_drift: "drift" });
    mounted.push(mount(Dashboard, { target: document.body }));
    await flush();
    const banner = document.querySelector('[role="alert"]')!;
    expect(banner.textContent).toContain("different from the configuration on disk");
    expect(document.body.textContent ?? "").not.toMatch(/older|newer|out of date/i);
    expect(document.querySelector('[title*="different from the one this process loaded"]'))
      .not.toBeNull();       // the STALE CONFIG cell tooltip says it the same way
  });

  it("stays quiet on a boot that has not started xray yet (config_drift unknown)", async () => {
    // Unknown is the normal state before the first start, and on any backend too old to answer.
    // Rendering it as a problem would make every healthy boot look like a broken gateway.
    for (const drift of ["unknown", "ok", undefined] as const) {
      document.body.innerHTML = "";
      resetStatus();
      setupApi();
      vi.spyOn(api, "getStatus").mockResolvedValue({ ...STATUS, config_drift: drift });
      const app = mount(Dashboard, { target: document.body });
      await flush();
      expect(document.body.textContent, `config_drift=${drift}`).not.toContain("STALE CONFIG");
      expect(document.querySelector('[role="alert"]'), `config_drift=${drift}`).toBeNull();
      unmount(app);
    }
  });

  it("does not delete a subscription until the confirm dialog is confirmed (F13-4)", async () => {
    vi.spyOn(api, "listSubs").mockResolvedValue([subscription(9, "sub1", { node_count: 3 })]);
    const del = vi.spyOn(api, "deleteSub").mockResolvedValue(undefined as any);
    mounted.push(mount(Subscriptions, { target: document.body }));
    mounted.push(mount(ConfirmModal, { target: document.body }));
    await flush();
    document.querySelector<HTMLButtonElement>('[aria-label="Delete subscription"]')!.click();
    await tick();
    expect(document.body.textContent).toContain("Delete subscription");
    expect(del).not.toHaveBeenCalled();
    [...document.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.trim() === "Cancel")!.click();
    await flush();
    expect(del).not.toHaveBeenCalled();
    document.querySelector<HTMLButtonElement>('[aria-label="Delete subscription"]')!.click();
    await tick();
    [...document.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.trim() === "Confirm")!.click();
    await flush();
    expect(del).toHaveBeenCalledWith(9);
  });

  it("does not delete a tuning profile until the confirm dialog is confirmed (F13-4)", async () => {
    vi.spyOn(api, "listProfiles").mockResolvedValue([profile(3, "p1", { node_count: 2 })]);
    const del = vi.spyOn(api, "deleteProfile").mockResolvedValue(undefined as any);
    mounted.push(mount(Tuning, { target: document.body }));
    mounted.push(mount(ConfirmModal, { target: document.body }));
    await flush();
    document.querySelector<HTMLButtonElement>('[aria-label="Delete profile"]')!.click();
    await tick();
    expect(document.body.textContent).toContain("Delete profile");
    expect(del).not.toHaveBeenCalled();
    [...document.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.trim() === "Cancel")!.click();
    await flush();
    expect(del).not.toHaveBeenCalled();
    document.querySelector<HTMLButtonElement>('[aria-label="Delete profile"]')!.click();
    await tick();
    [...document.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.trim() === "Confirm")!.click();
    await flush();
    expect(del).toHaveBeenCalledWith(3);
  });

  it("does not discard unsaved tuning-profile edits until the confirm dialog is confirmed (F13-4)", async () => {
    vi.spyOn(api, "listProfiles").mockResolvedValue([profile(1, "p1"), profile(2, "p2")]);
    mounted.push(mount(Tuning, { target: document.body }));
    mounted.push(mount(ConfirmModal, { target: document.body }));
    await flush();
    const editButtons = () => [...document.querySelectorAll<HTMLButtonElement>('[aria-label="Edit profile"]')];
    editButtons()[0].click();
    await tick();
    const nameInput = document.querySelector<HTMLInputElement>('input[placeholder="name"]')!;
    expect(nameInput.value).toBe("p1");
    setValue(nameInput, "p1-edited");
    await tick();
    editButtons()[1].click();   // switch to p2 while p1's edit is unsaved
    await tick();
    expect(document.body.textContent).toContain("Discard unsaved profile changes?");
    expect(nameInput.value).toBe("p1-edited");   // not yet overwritten
    [...document.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.trim() === "Cancel")!.click();
    await flush();
    expect(document.querySelector<HTMLInputElement>('input[placeholder="name"]')!.value).toBe("p1-edited");   // still unsaved, still there
    editButtons()[1].click();
    await tick();
    [...document.querySelectorAll<HTMLButtonElement>("button")].find((b) => b.textContent?.trim() === "Confirm")!.click();
    await flush();
    expect(document.querySelector<HTMLInputElement>('input[placeholder="name"]')!.value).toBe("p2");
  });

  it("shares one errText from api.ts — no local copies pasted across screens", () => {
    // Vite's raw-source glob, not node:fs — this project has no @types/node dependency.
    const sources = import.meta.glob("./*.svelte", { query: "?raw", import: "default", eager: true }) as Record<string, string>;
    for (const [path, src] of Object.entries(sources)) {
      expect(src, `${path} must not redefine errText locally`).not.toMatch(/function\s+errText\s*\(/);
    }
    const shouldImport = [
      "./Nodes.svelte", "./Subscriptions.svelte", "./Tuning.svelte", "./Routing.svelte", "./Settings.svelte",
      "./Operations.svelte", "./Network.svelte", "./Dashboard.svelte",
    ];
    for (const path of shouldImport) {
      const src = sources[path];
      expect(src, `${path} must import errText from ./api`).toMatch(/import\s*\{[^}]*\berrText\b[^}]*\}\s*from\s*"\.\/api"/);
    }
  });
});
