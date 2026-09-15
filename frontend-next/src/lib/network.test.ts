import { describe, it, expect } from "vitest";
import { NETWORK } from "../test/fixtures";
import { ipv6Source, killSwitchState, networkView } from "./network";
import type { Network } from "../api/client";

function sample(over: Partial<Network["status"]> = {}): Network {
  return {
    segment: { iface: "eth0.2", ip: "192.168.10.2", ip6: "", dhcp_start: "192.168.10.30",
               dhcp_end: "192.168.10.200", dhcp_lease: "12h", client_dns: "1.1.1.1", client_dns6: "" },
    kill_switch_enabled: false, lan_access_enabled: true, ipv6_enabled: false,
    status: { segment_up: null, uplink: null, uplink6: null, dhcp_clients: 0, clients: [],
              tunnel: { real_ok: null, latency_ms: null, egress_ip: null, checked_at: null },
              wan_blocked: false, enforcement_error: "", ipv6_prefix: null, foreign_ra: null,
              ipv6_prefix_source: null, ...over },
    recommendations: [],
    events: [],
  };
}

const withStatus = (patch: Partial<Network["status"]>): Network => ({ ...NETWORK, status: { ...NETWORK.status, ...patch } });

describe("kill-switch and IPv6 source, shared by Home and Gateway", () => {
  it("kill-switch: disabled is open, armed only on confirmed enforcement, unknown otherwise", () => {
    expect(killSwitchState(NETWORK)).toEqual({ label: "ARMED", tone: "ok" });
    expect(killSwitchState({ ...NETWORK, kill_switch_enabled: false })).toEqual({ label: "OPEN", tone: "bad" });
    expect(killSwitchState(withStatus({ enforcement_status: "error" }))).toEqual({ label: "UNKNOWN", tone: "neutral" });
    expect(killSwitchState(withStatus({ enforcement_status: "unknown" })).label).toBe("UNKNOWN");
    expect(killSwitchState(withStatus({ enforcement_status: undefined })).label).toBe("UNKNOWN");
    expect(killSwitchState(undefined).label).toBe("UNKNOWN");
  });

  it("IPv6 source", () => {
    expect(ipv6Source(NETWORK)).toBe("static");
    expect(ipv6Source(withStatus({ ipv6_prefix_source: "pd" }))).toBe("pd");
    expect(ipv6Source(withStatus({ ipv6_prefix_source: null }))).toBe("on");
    expect(ipv6Source({ ...NETWORK, ipv6_enabled: false })).toBe("off");
  });
});

describe("networkView", () => {
  it("maps unknown (dev) status to unknown tones and em-dashes", () => {
    const v = networkView(sample());
    expect(v.segment).toEqual({ tone: "unknown", label: "unknown" });
    expect(v.tunnel.tone).toBe("unknown");
    expect(v.tunnel.egress).toBe("—");
    expect(v.tunnel.latency).toBe("—");
  });

  it("maps a live up segment + healthy tunnel to ok tones", () => {
    const v = networkView(sample({ segment_up: true, dhcp_clients: 3,
                                   tunnel: { real_ok: true, latency_ms: 42, egress_ip: "9.9.9.9", checked_at: null } }));
    expect(v.segment).toEqual({ tone: "ok", label: "up" });
    expect(v.dhcp_clients).toBe(3);
    expect(v.tunnel).toEqual({ tone: "ok", egress: "9.9.9.9", latency: "42 ms" });
  });

  it("maps a down segment + failing tunnel to bad tones", () => {
    const v = networkView(sample({ segment_up: false,
                                   tunnel: { real_ok: false, latency_ms: null, egress_ip: null, checked_at: null } }));
    expect(v.segment.tone).toBe("bad");
    expect(v.segment.label).toBe("down");
    expect(v.tunnel.tone).toBe("bad");
  });

  it("flags a foreign router advertising v6 on the segment", () => {
    expect(networkView(sample()).foreign_ra).toBe(false);          // absent/unknown -> not flagged
    expect(networkView(sample({ foreign_ra: true })).foreign_ra).toBe(true);
  });
});
