import { describe, expect, it } from "vitest";
import type { Network, Status } from "../../api/client";
import { GATEWAY_NETWORK, STATUS } from "../../test/fixtures";
import {
  DISARM_CONFIRM, NO_ANSWER, RESTART_CONFIRM, appliedMessage, applyRefusedMessage, detailField, formToNetworkPatch, ip6StaticIssue, networkFormSchema,
  networkToForm, networkWarnings, poolIssue, restartConfirmNeeded, type NetworkFormValues,
} from "./networkForm";

const FORM = networkToForm(GATEWAY_NETWORK);

/** The messages the schema reports, by form field. */
function issues(patch: Partial<NetworkFormValues>): Record<string, string> {
  const result = networkFormSchema.safeParse({ ...FORM, ...patch });
  if (result.success) return {};
  return Object.fromEntries(result.error.issues.map((issue) => [issue.path.join("."), issue.message]));
}

const withSegment = (patch: Partial<Network["segment"]>): Network => ({ ...GATEWAY_NETWORK, segment: { ...GATEWAY_NETWORK.segment, ...patch } });
const withStatus = (patch: Partial<Network["status"]>): Network => ({ ...GATEWAY_NETWORK, status: { ...GATEWAY_NETWORK.status, ...patch } });

describe("network form values", () => {
  it("maps the saved network into the form, the segment's ip6 into its mode", () => {
    expect(FORM).toEqual({
      iface: "eth0.2", ip: "192.168.50.1", dhcpStart: "192.168.50.100", dhcpEnd: "192.168.50.200", clientDns: "192.168.50.1", lease: "12h",
      clientDns6: "2606:4700:4700::1111", ip6Mode: "static", ip6Static: "2001:db8:5a:2::/64", lanAccess: true, ipv6: true, killSwitch: true,
    });
    expect(networkToForm(withSegment({ ip6: "" }))).toMatchObject({ ip6Mode: "ula", ip6Static: "" });
    expect(networkToForm(withSegment({ ip6: "auto" }))).toMatchObject({ ip6Mode: "auto", ip6Static: "" });
    expect(networkToForm(withSegment({ ip6: "AUTO" }))).toMatchObject({ ip6Mode: "auto" });
  });

  it("Apply sends all eleven keys, trimmed, with segment_ip6 from the mode", () => {
    const patch = formToNetworkPatch({ ...FORM, iface: " eth0.3 ", dhcpEnd: "192.168.50.150 ", ip6Static: " 2001:db8:5a:3::/64 ", killSwitch: false });
    expect(patch).toEqual({
      segment_iface: "eth0.3", segment_ip: "192.168.50.1", segment_ip6: "2001:db8:5a:3::/64", dhcp_start: "192.168.50.100", dhcp_end: "192.168.50.150",
      dhcp_lease: "12h", client_dns: "192.168.50.1", client_dns6: "2606:4700:4700::1111", kill_switch_enabled: false, lan_access_enabled: true,
      ipv6_enabled: true,
    });
    expect(Object.keys(patch)).toHaveLength(11);
    expect(formToNetworkPatch({ ...FORM, ip6Mode: "auto" }).segment_ip6).toBe("auto");
    expect(formToNetworkPatch({ ...FORM, ip6Mode: "ula" }).segment_ip6).toBe("");
    // a mode round-trips through the gateway's own reading of it
    for (const ip6 of ["", "auto", "2001:db8:5a:2::/64"]) expect(formToNetworkPatch(networkToForm(withSegment({ ip6 }))).segment_ip6).toBe(ip6);
  });
});

describe("network form rules, in the backend's words", () => {
  it("the saved gateway is valid", () => {
    expect(issues({})).toEqual({});
  });

  it("every field but the /64 must not be blank", () => {
    expect(issues({ iface: " ", ip: "", dhcpStart: "", dhcpEnd: "", clientDns: "", lease: "", clientDns6: "" })).toEqual({
      iface: "segment_iface: must not be blank", ip: "segment_ip: must not be blank", dhcpStart: "dhcp_start: must not be blank",
      dhcpEnd: "dhcp_end: must not be blank", clientDns: "client_dns: must not be blank", lease: "dhcp_lease: must not be blank",
      clientDns6: "client_dns6: must not be blank",
    });
  });

  it("interface: a plain name of up to 15 characters", () => {
    for (const iface of ["eth0", "eth0.2", "wlan0@x", "br-lan:1", "A_b.c-d"]) expect(issues({ iface })).toEqual({});
    expect(issues({ iface: "eth 0" })).toEqual({ iface: "segment_iface: must be a plain interface name" });
    expect(issues({ iface: "eth0;drop" })).toEqual({ iface: "segment_iface: must be a plain interface name" });
    expect(issues({ iface: "a".repeat(15) })).toEqual({});
    expect(issues({ iface: "a".repeat(16) })).toEqual({ iface: "segment_iface: String should have at most 15 characters" });
  });

  it("IPv4 fields: strict dotted quads, no leading zeros, like Python's ipaddress", () => {
    for (const ip of ["10.0.0.1", "192.168.50.1", "255.255.255.255", "0.0.0.0"]) expect(issues({ ip })).toEqual({});
    for (const bad of ["192.168.50", "192.168.050.1", "256.1.1.1", "1.2.3.4.5", "a.b.c.d", "1.2.3.-4"]) {
      expect(issues({ ip: bad, dhcpStart: bad, dhcpEnd: bad, clientDns: bad })).toEqual({
        ip: "segment_ip: must be an IPv4 address", dhcpStart: "dhcp_start: must be an IPv4 address",
        dhcpEnd: "dhcp_end: must be an IPv4 address", clientDns: "client_dns: must be an IPv4 address",
      });
    }
    expect(issues({ ip: "1".repeat(16) })).toEqual({ ip: "segment_ip: String should have at most 15 characters" });
  });

  it("lease: a number with an optional unit, or infinite", () => {
    for (const lease of ["12h", "3600", "45m", "2d", "1w", "30s", "infinite", "999999999"]) expect(issues({ lease })).toEqual({});
    for (const lease of ["12 h", "12H", "1y", "forever", "1234567890", "h"]) {
      expect(issues({ lease })).toEqual({ lease: "dhcp_lease: must be a lease time like '12h', '3600', or 'infinite'" });
    }
  });

  it("client DNS (v6): an IPv6 address — checked while IPv6 is off too, as the backend does", () => {
    for (const clientDns6 of ["2606:4700:4700::1111", "::1", "fe80::", "::ffff:1.2.3.4", "1:2:3:4:5:6:7:8"]) expect(issues({ clientDns6 })).toEqual({});
    expect(issues({ clientDns6: "1.1.1.1", ipv6: false })).toEqual({ clientDns6: "client_dns6: must be an IPv6 address" });
    expect(issues({ clientDns6: "2606:4700::1111::1" })).toEqual({ clientDns6: "client_dns6: must be an IPv6 address" });
    expect(issues({ clientDns6: "2001:db8::/64" })).toEqual({ clientDns6: "client_dns6: must be an IPv6 address" });
  });

  it("static /64: a /64 prefix (host bits allowed, the server normalises), or auto; required in static mode only", () => {
    for (const ip6Static of ["2001:db8:0:2::/64", "2001:db8:0:2::1/64", "auto"]) expect(issues({ ip6Static })).toEqual({});
    expect(issues({ ip6Static: "2001:db8::/48" })).toEqual({ ip6Static: "segment_ip6: must use a /64 prefix" });
    expect(issues({ ip6Static: "2001:db8::1" })).toEqual({ ip6Static: "segment_ip6: must use a /64 prefix" });
    expect(issues({ ip6Static: "10.0.0.0/64" })).toEqual({ ip6Static: "segment_ip6: must be an IPv6 /64 or 'auto'" });
    expect(issues({ ip6Static: "2001:db8::/129" })).toEqual({ ip6Static: "segment_ip6: must be an IPv6 /64 or 'auto'" });
    expect(issues({ ip6Static: "" })).toEqual({ ip6Static: "segment_ip6: must be an IPv6 /64 or 'auto'" });
    expect(issues({ ip6Static: "", ip6Mode: "ula" })).toEqual({});
    expect(issues({ ip6Static: "garbage", ip6Mode: "auto" })).toEqual({});
    expect(ip6StaticIssue(`${"a:".repeat(30)}/64`)).toBe("segment_ip6: String should have at most 45 characters");
  });

  it("the /64 rule is reported alongside field errors elsewhere in the form", () => {
    expect(issues({ ip6Static: "2001:db8::/48", lease: "x" })).toEqual({
      ip6Static: "segment_ip6: must use a /64 prefix", lease: "dhcp_lease: must be a lease time like '12h', '3600', or 'infinite'",
    });
  });
});

describe("DHCP pool (decision 5)", () => {
  it("inside the gateway's /24 with start ≤ end; equal bounds are one address", () => {
    expect(poolIssue(FORM)).toBeNull();
    expect(poolIssue({ ...FORM, dhcpStart: "192.168.50.120", dhcpEnd: "192.168.50.120" })).toBeNull();
    expect(poolIssue({ ...FORM, dhcpEnd: "192.168.50.80" })).toBe("DHCP pool must sit inside 192.168.50.0/24 with start ≤ end");
    expect(poolIssue({ ...FORM, dhcpStart: "192.168.51.100" })).toBe("DHCP pool must sit inside 192.168.50.0/24 with start ≤ end");
    expect(poolIssue({ ...FORM, dhcpEnd: "192.168.99.200" })).toBe("DHCP pool must sit inside 192.168.50.0/24 with start ≤ end");
    expect(poolIssue({ ...FORM, ip: " 10.0.2.1 ", dhcpStart: "10.0.2.30", dhcpEnd: "10.0.2.9" })).toBe("DHCP pool must sit inside 10.0.2.0/24 with start ≤ end");
  });

  it("says nothing until the gateway IP and both bounds are addresses (their own errors come first)", () => {
    expect(poolIssue({ ...FORM, ip: "192.168.50" })).toBeNull();
    expect(poolIssue({ ...FORM, dhcpStart: "" })).toBeNull();
    expect(poolIssue({ ...FORM, dhcpEnd: "192.168.99.x" })).toBeNull();
  });
});

describe("422 details", () => {
  it("a detail that starts with a field's name belongs to that field", () => {
    expect(detailField("segment_iface: must be a plain interface name")).toBe("iface");
    expect(detailField("segment_ip: 192.168.1.5 is in the management network (192.168.1.0/24, which holds mgmt_ip 192.168.1.120) — the segment, its DHCP pool and the LAN-access NAT would overlap the network this panel is reached on. Give the segment its own /24")).toBe("ip");
    expect(detailField("segment_ip6: must use a /64 prefix")).toBe("ip6Static");
    expect(detailField("dhcp_start: must be an IPv4 address")).toBe("dhcpStart");
    expect(detailField("dhcp_end: must not be blank")).toBe("dhcpEnd");
    expect(detailField("dhcp_lease: must be a lease time like '12h', '3600', or 'infinite'")).toBe("lease");
    expect(detailField("client_dns: must be an IPv4 address")).toBe("clientDns");
    expect(detailField("client_dns6: must be an IPv6 address")).toBe("clientDns6");
    expect(detailField("body.segment_iface: String should have at most 15 characters")).toBe("iface");
  });

  it("joined cross-field refusals and anything else belong to the form", () => {
    expect(detailField("segment_iface: 'eth0' is also the management interface (mgmt_iface); segment_ip: 192.168.1.5 is in the management network")).toBeNull();
    expect(detailField("fields may be omitted but not null: segment_ip")).toBeNull();
    expect(detailField("client_dnsX: nope")).toBeNull();
  });
});

describe("Apply to host", () => {
  const connected: Status = STATUS;
  const disconnected: Status = { ...STATUS, active_node_id: null };
  const stopped: Status = { ...STATUS, running: false };

  it("asks first only when the tunnel is up and the interface, the gateway IP or IPv6 changed (decision 4)", () => {
    expect(restartConfirmNeeded(GATEWAY_NETWORK, FORM, connected)).toBe(false);
    expect(restartConfirmNeeded(GATEWAY_NETWORK, { ...FORM, dhcpEnd: "192.168.50.150", killSwitch: false, lanAccess: false }, connected)).toBe(false);
    expect(restartConfirmNeeded(GATEWAY_NETWORK, { ...FORM, iface: "eth0.3" }, connected)).toBe(true);
    expect(restartConfirmNeeded(GATEWAY_NETWORK, { ...FORM, ip: "192.168.60.1" }, connected)).toBe(true);
    expect(restartConfirmNeeded(GATEWAY_NETWORK, { ...FORM, ipv6: false }, connected)).toBe(true);
    expect(restartConfirmNeeded(GATEWAY_NETWORK, { ...FORM, iface: " eth0.2 " }, connected)).toBe(false);
    for (const status of [disconnected, stopped]) {
      expect(restartConfirmNeeded(GATEWAY_NETWORK, { ...FORM, iface: "eth0.3", ipv6: false }, status)).toBe(false);
    }
    // a status that is not known counts as connected (owner answer 6)
    expect(restartConfirmNeeded(GATEWAY_NETWORK, { ...FORM, ipv6: false }, undefined)).toBe(true);
    expect(restartConfirmNeeded(GATEWAY_NETWORK, FORM, undefined)).toBe(false);
  });

  it("its messages", () => {
    expect(RESTART_CONFIRM).toBe("Apply and restart the tunnel? Devices may drop briefly.");
    expect(DISARM_CONFIRM).toBe("Disarm the fail-closed kill-switch?\nIf the tunnel goes down, client traffic will no longer be dropped — it can leak around the tunnel instead.");
    expect(NO_ANSWER).toBe("no answer yet — the gateway may still be applying; reloading");
    expect(appliedMessage(false)).toBe("saved · network + DHCP applied to host");
    expect(appliedMessage(true)).toBe("saved · network + DHCP applied to host · tunnel restarted for IPv6");
  });

  it("a 502 kept the previous settings; one whose restore reported problems stays until dismissed", () => {
    expect(applyRefusedMessage("host provisioning failed: ip link: no such device")).toEqual({
      text: "not applied — the gateway kept its previous network settings: host provisioning failed: ip link: no such device", sticky: false,
    });
    expect(applyRefusedMessage("network apply failed; recovery: fail-closed recovery failed: nft: busy")).toEqual({
      text: "not applied, and restoring the previous state reported problems: network apply failed; recovery: fail-closed recovery failed: nft: busy",
      sticky: true,
    });
  });
});

describe("host warnings (W1)", () => {
  it("none on a healthy, confirmed gateway", () => {
    expect(networkWarnings(GATEWAY_NETWORK)).toEqual([]);
  });

  it("in order: WAN blocked, enforcement not confirmed (its error beneath), an apply warning, a foreign RA", () => {
    const warnings = networkWarnings(withStatus({
      wan_blocked: true, enforcement_status: "error", enforcement_error: "nft: Could not process rule", enforcement_warning: "LAN access chain not applied", foreign_ra: true,
    }));
    expect(warnings).toEqual([
      { key: "wan-blocked", tone: "warn", title: "WAN blocked", text: "— kill-switch holding traffic (tunnel down). No leak." },
      { key: "enforcement", tone: "bad", title: "Host enforcement is error; leak protection is not confirmed.", detail: "nft: Could not process rule" },
      { key: "apply-warning", tone: "warn", title: "Applied with a warning", text: "— LAN access chain not applied" },
      { key: "foreign-ra", tone: "bad", title: "Another router is advertising IPv6 on the client VLAN", text: "— clients will leak. Disable RA for this VLAN on your router." },
    ]);
  });

  it("each on its own; an unknown enforcement with no error has no second line; an apply warning is never bad", () => {
    expect(networkWarnings(withStatus({ wan_blocked: true })).map((w) => w.key)).toEqual(["wan-blocked"]);
    expect(networkWarnings(withStatus({ enforcement_status: "unknown" }))).toEqual([
      { key: "enforcement", tone: "bad", title: "Host enforcement is unknown; leak protection is not confirmed.", detail: undefined },
    ]);
    expect(networkWarnings(withStatus({ enforcement_warning: "LAN access chain not applied" }))[0]!.tone).toBe("warn");
    expect(networkWarnings(withStatus({ wan_blocked: null, foreign_ra: null }))).toEqual([]);
  });
});
