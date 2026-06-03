import { describe, it, expect } from "vitest";
import { networkView } from "./network";
import type { Network } from "./api";

function sample(over: Partial<Network["status"]> = {}): Network {
  return {
    segment: { iface: "eth0.2", ip: "192.168.10.2", dhcp_start: "192.168.10.30",
               dhcp_end: "192.168.10.200", dhcp_lease: "12h", client_dns: "1.1.1.1" },
    kill_switch_enabled: false,
    status: { segment_up: null, dhcp_clients: 0,
              tunnel: { real_ok: null, latency_ms: null, egress_ip: null }, ...over },
    recommendations: [],
  };
}

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
                                   tunnel: { real_ok: true, latency_ms: 42, egress_ip: "9.9.9.9" } }));
    expect(v.segment).toEqual({ tone: "ok", label: "up" });
    expect(v.dhcp_clients).toBe(3);
    expect(v.tunnel).toEqual({ tone: "ok", egress: "9.9.9.9", latency: "42 ms" });
  });

  it("maps a down segment + failing tunnel to bad tones", () => {
    const v = networkView(sample({ segment_up: false,
                                   tunnel: { real_ok: false, latency_ms: null, egress_ip: null } }));
    expect(v.segment.tone).toBe("bad");
    expect(v.segment.label).toBe("down");
    expect(v.tunnel.tone).toBe("bad");
  });
});
