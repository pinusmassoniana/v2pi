// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import App from "../App.svelte";
import Health from "./Health.svelte";
import Login from "./Login.svelte";
import NetworkScreen from "./Network.svelte";
import Routing from "./Routing.svelte";
import SettingsScreen from "./Settings.svelte";
import Setup from "./Setup.svelte";
import { api, ApiError, type Network, type Settings, type Status } from "./api";
import { resetStatus } from "./status.svelte";

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
      ipv6_prefix: null, foreign_ra: false, ipv6_prefix_source: null,
    },
    recommendations: [], events: [],
  };
}

function settings(): Settings {
  return {
    tunneled_fetch: true, routing_default_action: "proxy", health_enabled: true,
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
});
