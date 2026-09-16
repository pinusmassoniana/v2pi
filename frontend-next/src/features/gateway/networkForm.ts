// Gateway › Network: the segment form's values, what Apply to host sends, the backend's field rules and messages, the
// DHCP pool check, the restart question, the result messages and the host warnings. Pure and unit-tested.
import { z } from "zod";
import type { Network, NetworkPatch, Status } from "../../api/client";
import { ipv4Number, ipv6PrefixLength, isIPv6Address } from "../../lib/ip";

export const IP6_MODES = ["static", "auto", "ula"] as const;
export type Ip6Mode = (typeof IP6_MODES)[number];

export interface NetworkFormValues {
  iface: string;
  ip: string;
  dhcpStart: string;
  dhcpEnd: string;
  clientDns: string;
  lease: string;
  clientDns6: string;
  ip6Mode: Ip6Mode;
  /** The static /64; only sent in static mode. */
  ip6Static: string;
  lanAccess: boolean;
  ipv6: boolean;
  killSwitch: boolean;
}

export type NetworkField = keyof NetworkFormValues;

/** The saved network as form values. The segment's ip6 is "" for ULA, "auto" for DHCPv6-PD, or a static /64. */
export function networkToForm(network: Network): NetworkFormValues {
  const { segment } = network;
  const ip6 = segment.ip6.trim();
  const ip6Mode: Ip6Mode = ip6 === "" ? "ula" : ip6.toLowerCase() === "auto" ? "auto" : "static";
  return {
    iface: segment.iface, ip: segment.ip, dhcpStart: segment.dhcp_start, dhcpEnd: segment.dhcp_end, clientDns: segment.client_dns,
    lease: segment.dhcp_lease, clientDns6: segment.client_dns6, ip6Mode, ip6Static: ip6Mode === "static" ? ip6 : "",
    lanAccess: network.lan_access_enabled, ipv6: network.ipv6_enabled, killSwitch: network.kill_switch_enabled,
  };
}

/** Apply to host sends the whole form, trimmed — every editable key, as the Svelte panel did — never a partial patch. */
export function formToNetworkPatch(form: NetworkFormValues): Required<NetworkPatch> {
  return {
    segment_iface: form.iface.trim(),
    segment_ip: form.ip.trim(),
    segment_ip6: form.ip6Mode === "auto" ? "auto" : form.ip6Mode === "ula" ? "" : form.ip6Static.trim(),
    dhcp_start: form.dhcpStart.trim(),
    dhcp_end: form.dhcpEnd.trim(),
    dhcp_lease: form.lease.trim(),
    client_dns: form.clientDns.trim(),
    client_dns6: form.clientDns6.trim(),
    kill_switch_enabled: form.killSwitch,
    lan_access_enabled: form.lanAccess,
    ipv6_enabled: form.ipv6,
  };
}

/** The backend's name for each form field: its messages start with it. */
export const SERVER_FIELDS = {
  iface: "segment_iface", ip: "segment_ip", ip6Static: "segment_ip6", dhcpStart: "dhcp_start", dhcpEnd: "dhcp_end", lease: "dhcp_lease",
  clientDns: "client_dns", clientDns6: "client_dns6",
} as const satisfies Partial<Record<NetworkField, string>>;

const IFACE = /^[A-Za-z0-9._@:-]{1,15}$/;
const LEASE = /^(?:infinite|\d{1,9}[smhdw]?)$/;

/** A required segment value, checked in the backend's order: blank, too long (the schema), then its shape. */
function required(field: string, max: number, shape: (value: string) => string | null) {
  return z.string().superRefine((raw, ctx) => {
    const value = raw.trim();
    const message = value === ""
      ? `${field}: must not be blank`
      : value.length > max
        ? `${field}: String should have at most ${max} characters`
        : shape(value);
    if (message) ctx.addIssue({ code: "custom", message });
  });
}

const ipv4 = (field: string) => required(field, 15, (value) => (ipv4Number(value) === null ? `${field}: must be an IPv4 address` : null));

/** The static /64, as config.py `_v6_prefix64` checks it; "auto" is accepted there too. */
export function ip6StaticIssue(raw: string): string | null {
  const value = raw.trim();
  if (value.toLowerCase() === "auto") return null;
  if (value.length > 45) return "segment_ip6: String should have at most 45 characters";
  const prefix = value === "" ? null : ipv6PrefixLength(value);
  if (prefix === null) return "segment_ip6: must be an IPv6 /64 or 'auto'";
  return prefix === 64 ? null : "segment_ip6: must use a /64 prefix";
}

/**
 * The backend's checks (routes.py `_validate_net_fields`, config.py `validate_net_settings`, schemas.py `NetworkIn`) in
 * its own words. The IPv6 fields are checked even while IPv6 is off: the backend checks them regardless.
 */
export const networkFormSchema = z.object({
  iface: required("segment_iface", 15, (value) => (IFACE.test(value) ? null : "segment_iface: must be a plain interface name")),
  ip: ipv4("segment_ip"),
  dhcpStart: ipv4("dhcp_start"),
  dhcpEnd: ipv4("dhcp_end"),
  clientDns: required("client_dns", 45, (value) => (ipv4Number(value) === null ? "client_dns: must be an IPv4 address" : null)),
  lease: required("dhcp_lease", 32, (value) => (LEASE.test(value) ? null : "dhcp_lease: must be a lease time like '12h', '3600', or 'infinite'")),
  clientDns6: required("client_dns6", 45, (value) => (isIPv6Address(value) ? null : "client_dns6: must be an IPv6 address")),
  ip6Mode: z.enum(IP6_MODES),
  ip6Static: z.string(),
  lanAccess: z.boolean(),
  ipv6: z.boolean(),
  killSwitch: z.boolean(),
}).superRefine((form, ctx) => {
  // Blank in static mode would be sent as "" — which the backend reads as ULA — so static needs a value.
  if (form.ip6Mode !== "static") return;
  const message = ip6StaticIssue(form.ip6Static);
  if (message) ctx.addIssue({ code: "custom", path: ["ip6Static"], message });
});

/**
 * Decision 5: the DHCP pool must sit inside the gateway's /24 with start ≤ end. The backend never checks it, so this
 * blocks Apply like a field error. Null until the gateway IP and both bounds are valid IPv4 addresses.
 */
export function poolIssue(form: Pick<NetworkFormValues, "ip" | "dhcpStart" | "dhcpEnd">): string | null {
  const [ip, start, end] = [form.ip, form.dhcpStart, form.dhcpEnd].map((value) => ipv4Number(value.trim()));
  if (ip == null || start == null || end == null) return null;
  const net = Math.floor(ip / 256);
  if (Math.floor(start / 256) === net && Math.floor(end / 256) === net && start <= end) return null;
  return `DHCP pool must sit inside ${form.ip.trim().split(".").slice(0, 3).join(".")}.0/24 with start ≤ end`;
}

/**
 * The form field a 422 detail belongs to: a detail starting with "‹field›: " (a schema message may carry pydantic's
 * "body." in front). Several refusals joined by "; " — the cross-field checks — belong to the form, as does anything else.
 */
export function detailField(detail: string): NetworkField | null {
  const text = detail.replace(/^body\./, "");
  if (text.includes("; ")) return null;
  for (const [field, server] of Object.entries(SERVER_FIELDS) as [NetworkField, string][]) {
    if (text.startsWith(`${server}: `)) return field;
  }
  return null;
}

/** Whether the tunnel is up for Apply's purposes: running with an active node — or not known, which counts as up. */
export function connectedForApply(status: Status | undefined): boolean {
  return status === undefined || (status.running && status.active_node_id !== null);
}

/**
 * Decision 4: Apply asks first only when the tunnel is up (or its state unknown) and the apply restarts it or re-leases
 * every client — the segment interface or gateway IP changed, or IPv6 was flipped.
 */
export function restartConfirmNeeded(saved: Network, form: NetworkFormValues, status: Status | undefined): boolean {
  if (!connectedForApply(status)) return false;
  return form.iface.trim() !== saved.segment.iface || form.ip.trim() !== saved.segment.ip || form.ipv6 !== saved.ipv6_enabled;
}

export const RESTART_CONFIRM = "Apply and restart the tunnel? Devices may drop briefly.";

export const DISARM_CONFIRM =
  "Disarm the fail-closed kill-switch?\nIf the tunnel goes down, client traffic will no longer be dropped — it can leak around the tunnel instead.";

export const NO_ANSWER = "no answer yet — the gateway may still be applying; reloading";

/** Apply's success. "· tunnel restarted for IPv6" only when IPv6 was flipped while the tunnel ran. */
export function appliedMessage(ipv6Restarted: boolean): string {
  return `saved · network + DHCP applied to host${ipv6Restarted ? " · tunnel restarted for IPv6" : ""}`;
}

/**
 * A 502 from Apply: the store rolled back and the host was restored from the previous settings. When restoring reported
 * problems too ("; recovery: …") the gateway's state is not known, so that message stays until it is dismissed.
 */
export function applyRefusedMessage(detail: string): { text: string; sticky: boolean } {
  if (detail.includes("recovery:")) return { text: `not applied, and restoring the previous state reported problems: ${detail}`, sticky: true };
  return { text: `not applied — the gateway kept its previous network settings: ${detail}`, sticky: false };
}

export interface NetworkWarning {
  key: string;
  tone: "warn" | "bad";
  title: string;
  text?: string;
  /** A second line under the sentence. */
  detail?: string;
}

/** W1: what the host reports, in this order — WAN blocked, enforcement not confirmed, an apply warning, a foreign RA. */
export function networkWarnings(network: Network): NetworkWarning[] {
  const { status } = network;
  const warnings: NetworkWarning[] = [];
  if (status.wan_blocked === true) {
    warnings.push({ key: "wan-blocked", tone: "warn", title: "WAN blocked", text: "— kill-switch holding traffic (tunnel down). No leak." });
  }
  const enforcement = status.enforcement_status ?? "unknown";
  if (enforcement !== "ok") {
    warnings.push({
      key: "enforcement", tone: "bad", title: `Host enforcement is ${enforcement}; leak protection is not confirmed.`,
      detail: status.enforcement_error || undefined,
    });
  }
  if (status.enforcement_warning) {
    warnings.push({ key: "apply-warning", tone: "warn", title: "Applied with a warning", text: `— ${status.enforcement_warning}` });
  }
  if (status.foreign_ra === true) {
    warnings.push({
      key: "foreign-ra", tone: "bad", title: "Another router is advertising IPv6 on the client VLAN",
      text: "— clients will leak. Disable RA for this VLAN on your router.",
    });
  }
  return warnings;
}

/** The kill-switch caption under its pill, per saved state (contract W6). */
export const KILL_SWITCH_CAPTION = {
  ARMED: "fail-closed · traffic dropped if no healthy upstream",
  OPEN: "⚠ clients may leak around the tunnel",
  UNKNOWN: "configured, but host enforcement is not confirmed",
} as const;
