import ipaddress
import logging
import re

from pi_gw_panel.net_control.plan import NetPlan, net24

logger = logging.getLogger(__name__)

# What an interface name may look like before it is interpolated into a rule. The same shape the
# API boundary enforces on `segment_iface` (`config._NET_IFACE_RE`), applied here to the EXTRA
# names, which do not come from a request: they are read back out of the panel's own records of
# what it put — or may have put — on the host (the ownership, link, stale-address and surviving-
# candidate ledgers), so a hand-edited database or an imported backup document is the one way
# something else could arrive.
# A value that cannot be an interface name is not one — Linux allows neither whitespace nor
# quotes and stops at 15 characters — so dropping it costs no coverage of anything real, while
# interpolating it would let a stored newline write nft rules of its own choosing.
_IFACE_RE = re.compile(r"[A-Za-z0-9._@:-]{1,15}")


def _segment_ifaces(plan: NetPlan) -> list[str]:
    """Every interface the segment rules must cover: the configured one, then any being replaced.

    Ordered and de-duplicated, and the configured interface is always first so the ordinary
    one-interface render is byte-identical to what it has always been.
    """
    names = [plan.segment_iface]
    for name in plan.extra_ifaces:
        name = (name or "").strip()
        if name and name not in names and _IFACE_RE.fullmatch(name):
            names.append(name)
    return names


def _iif(plan: NetPlan) -> str:
    """The `iifname` match scoping a rule to the segment — one name, or a set of them.

    nft matches an anonymous set of interface names exactly as it matches one, so a transitional
    ruleset covering both sides of a segment move is the same rules with the same meaning, and
    not a second ruleset that has to be kept in step with this one.
    """
    names = _segment_ifaces(plan)
    if len(names) == 1:
        return f'iifname "{names[0]}"'
    return "iifname { " + ", ".join(f'"{name}"' for name in names) + " }"


def _seg_prefix6(plan: NetPlan) -> str | None:
    """The segment's own /64 as a normalized nft token, for the 'leave local v6 alone' bypass
    (the v6 analog of RFC-1918). None for a blank / `auto` / invalid value — then intra-segment
    v6 isn't carved out (documented limitation for `auto` until the prefix is observed)."""
    s = (plan.segment_ip6 or "").strip()
    if not s or s.lower() == "auto":
        return None
    try:
        return ipaddress.ip_network(s, strict=False).with_prefixlen
    except ValueError:
        return None


def _local6(plan: NetPlan) -> str:
    """nft v6 'local / don't-tunnel' set: loopback, link-local, ULA, multicast, and (when known)
    the segment's own prefix."""
    nets = ["::1/128", "fe80::/10", "fc00::/7", "ff00::/8"]
    seg = _seg_prefix6(plan)
    if seg:
        nets.append(seg)
    return "{ " + ", ".join(nets) + " }"


_PRIVATE_SET = "127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16"


def counter_names(ip: str) -> tuple[str, str]:
    """B1: the nft counter pair for one pinned device, derived from its address so the names are
    stable across renders (a counter keyed by row id would move when a row is deleted)."""
    key = ip.replace(".", "_")
    return f"dev_up_{key}", f"dev_down_{key}"


def _accounting(plan: NetPlan) -> tuple[str, str]:
    """B1, for the pinned devices only (owner decision): the counter declarations and the two
    chains that feed them.

    They are chains of their OWN, at the same hooks as the enforcement rules but carrying no
    verdict, for two reasons. The tproxy chain stays byte-for-byte what it was — accounting must
    not be able to change where a packet goes — and counting keeps working while the tunnel is
    stopped, which is exactly when an operator wonders what is still talking.

    Private destinations (upload) and private sources (download) are excluded, so what a device
    does with the LAN is not reported as internet traffic.

    With no usable reservations both are empty and the ruleset is the old one, byte for byte.
    """
    usable = applied_reservations(plan)
    if not usable:
        return "", ""
    declarations, uploads, downloads = [], [], []
    for _mac, ip, _name in usable:
        up, down = counter_names(ip)
        declarations.append(f"    counter {up} {{ }}\n    counter {down} {{ }}\n")
        uploads.append(f"        ip saddr {ip} counter name {up}\n")
        downloads.append(f"        ip daddr {ip} counter name {down}\n")
    chains = (
        "    chain acct_up {\n"
        "        type filter hook prerouting priority mangle; policy accept;\n"
        f"        ip daddr {{ {_PRIVATE_SET} }} return\n"
        + "".join(uploads) +
        "    }\n"
        "    chain acct_down {\n"
        "        type filter hook postrouting priority mangle; policy accept;\n"
        f"        ip saddr {{ {_PRIVATE_SET} }} return\n"
        + "".join(downloads) +
        "    }\n")
    return "".join(declarations), chains


def render_nft(plan: NetPlan, tunnel_up: bool = True) -> str:
    # Mark client TCP/UDP *arriving on the segment iface* with fwmark and tproxy to the
    # xray dokodemo port. The iifname scope keeps it to segment clients only — host-
    # forwarded traffic (Docker bridge, other interfaces) stays direct (otherwise a
    # `docker build` etc. gets tunneled and breaks). Skip packets already carrying the
    # xray egress mark (anti-loop), and bypass loopback + RFC-1918 so local/LAN stays direct.
    #
    # DHCP carve-out: a client's DHCPDISCOVER is a broadcast to 255.255.255.255, which the
    # RFC-1918 daddr return does NOT cover — so without this explicit bypass it falls through
    # to tproxy and xray swallows it, and new segment clients never get a lease (only unicast
    # renewals to the gateway survive). Excluding udp 67/68 lets DHCP reach the local dnsmasq.
    # It is scoped to the segment iface AND to a DHCP destination (the segment /24 or the
    # limited broadcast): an unscoped `udp dport { 67, 68 } return` sitting above the tproxy
    # rule would let ANY client datagram from/to those ports skip the tunnel entirely — the
    # packet leaves un-NAT'd, but a home router happily NATs it onward, so it is a real leak.
    #
    # Kill-switch (fail-closed): when on, add a forward-chain drop for client-segment
    # traffic headed to a non-private destination. Correctly-tunneled client packets
    # are tproxy'd to local xray (never forwarded) so they're unaffected; only leaked
    # client→WAN traffic is dropped. Byte-identical to the off path when disabled.
    #
    # `tunnel_up=False` renders the FAIL-CLOSED guard: the forward drop only, with no
    # prerouting/tproxy — used when the tunnel is intentionally stopped but the kill-switch
    # must keep blocking client→WAN (A1). With the kill-switch off this is an empty table.
    #
    # Every rule below is scoped by `_iif`, which is the configured segment interface alone except
    # while the segment is being MOVED between interfaces, when it names both — see `_iif`.
    iif = _iif(plan)
    counters, accounting = _accounting(plan)
    forward = ""
    if plan.kill_switch:
        forward = f"""\
    chain forward {{
        type filter hook forward priority filter; policy accept;
        {iif} ip daddr != {{ 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 }} drop
    }}
"""
    seg_net, lan = net24(plan.segment_ip), net24(plan.mgmt_ip)
    dhcp_dst = f"{seg_net}, 255.255.255.255" if seg_net else "255.255.255.255"
    prerouting = ""
    if tunnel_up:
        prerouting = f"""\
    chain prerouting {{
        type filter hook prerouting priority mangle; policy accept;
        meta mark 0x{plan.egress_mark:x} return
        ip daddr {{ 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 }} return
        {iif} ip daddr {{ {dhcp_dst} }} udp dport {{ 67, 68 }} return
        {iif} meta l4proto {{ tcp, udp }} meta mark set 0x{plan.fwmark:x} tproxy ip to :{plan.tproxy_port} accept
    }}
"""
    # LAN access (independent of tunnel state): SNAT segment→home-LAN so replies return via the
    # mgmt leg (the router/Proxmox have no route back to the segment). Scoped to the LAN cidr +
    # mgmt iface so it can NEVER NAT internet-bound traffic — that stays tproxy'd through the
    # tunnel. The forward-accept that lets these packets past Docker's `FORWARD policy=drop` is
    # added by LinuxBackend in DOCKER-USER (a base chain's drop can't be overridden from here).
    postrouting = ""
    if plan.lan_access and seg_net and lan:
        postrouting = f"""\
    chain postrouting {{
        type nat hook postrouting priority srcnat; policy accept;
        ip saddr {seg_net} ip daddr {lan} oifname "{plan.mgmt_iface}" masquerade
    }}
"""
    # The download counters ride along even while the tunnel is stopped: a device talking to the
    # LAN or to a direct destination is still traffic the operator asked to see.
    return f"""\
table ip pi_gw_panel {{
{counters}{prerouting}{forward}{postrouting}{accounting}}}
"""


def render_nft6(plan: NetPlan, tunnel_up: bool = True) -> str:
    """IPv6 `ip6` table, mode-aware:

    - **IPv6 tunnel on + tunnel up** → tproxy v6 TCP/UDP to xray (port6), PLUS a forward-drop
      backstop. tproxy only catches tcp/udp, so without the drop, ICMPv6 (ping/traceroute) and
      any other non-tcp/udp v6 to a global dest would forward DIRECT — a leak around the tunnel
      (audit A). tproxy'd traffic is delivered locally and never traverses `forward`, so the
      drop only catches the leak-prone remainder. The `_local6` set (loopback/link-local/ULA/
      multicast + the segment's own /64) stays direct, so NDP/RA and intra-segment v6 work; the
      DHCPv6 (546/547) carve-out keeps address assignment working.
    - **tunnel up, IPv6 tunnel off** → forward-drop only. The v4 tunnel being up means the
      operator wants client traffic tunneled; a client holding a leftover v6 default route via
      the gateway (the RA-lifetime window right after disabling v6) would otherwise forward
      DIRECT around the up tunnel (audit B3). Fail-open stays a tunnel-DOWN property only.
      (A foreign-RA L2 bypass never traverses this gateway — that case is detection-only.)
    - **tunnel down + kill-switch on** → fail-closed drop of client→global-v6 (the v1.8 leak-guard).
    - **tunnel down + kill-switch off** → empty (the v6 table is removed; clients fall back direct)."""
    local6, iif = _local6(plan), _iif(plan)
    drop = (f"        {iif} ip6 daddr != {local6} drop\n")
    forward = (f"    chain forward {{\n"
               f"        type filter hook forward priority filter; policy accept;\n"
               f"{drop}"
               f"    }}\n")
    if plan.ipv6_enabled and tunnel_up:
        return f"""\
table ip6 pi_gw_panel {{
    chain prerouting {{
        type filter hook prerouting priority mangle; policy accept;
        meta mark 0x{plan.egress_mark:x} return
        ip6 daddr {local6} return
        udp dport {{ 546, 547 }} return
        {iif} meta l4proto {{ tcp, udp }} meta mark set 0x{plan.fwmark:x} tproxy ip6 to :{plan.tproxy_port6} accept
    }}
{forward}}}
"""
    if plan.kill_switch or tunnel_up:
        return f"""\
table ip6 pi_gw_panel {{
{forward}}}
"""
    return ""


_DNSMASQ_FIELDS = ("segment_iface", "dnsmasq_leases", "dhcp_start", "dhcp_end", "dhcp_lease",
                   "segment_ip", "client_dns", "client_dns6")


def _no_line_break(plan: NetPlan) -> None:
    """Refuse to render a dnsmasq config out of a value carrying CR/LF.

    In this file format a newline is not data, it is the start of the next DIRECTIVE — and the
    file is handed to a dnsmasq the panel supervises as root, so `12h\\ndhcp-script=/data/x.sh`
    is remote code execution on the next lease event, in a config `dnsmasq --test` calls valid.
    Every write path validates these values; this is the backstop for the one that gets added
    later and forgets to.
    """
    for field in _DNSMASQ_FIELDS:
        value = str(getattr(plan, field, "") or "")
        if "\n" in value or "\r" in value:
            raise ValueError(f"{field} must not contain a line break")


# A4: what a reservation may carry into the dnsmasq config. `dnsmasq --test` accepts nonsense in
# the MAC position (it reads it as a client id), so these are the only check there is.
_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,30}[A-Za-z0-9])?$")


def reservation_value_issue(mac: str, ip: str, name: str) -> str | None:
    """Shape alone: what a reservation must look like whatever segment it lands in. A restored
    document is checked against this, because the box it is restored onto may serve a different
    segment than the one the backup was taken from."""
    if not _MAC_RE.match((mac or "").strip().lower()):
        return "MAC must look like aa:bb:cc:dd:ee:ff"
    if name and not _HOSTNAME_RE.match(name):
        return "name may use letters, digits and hyphens only (up to 32 characters)"
    try:
        address = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return f"{ip!r} is not an IPv4 address"
    if address.version != 4:
        return "a reservation is IPv4 only"
    return None


def reservation_issue(mac: str, ip: str, name: str, plan: NetPlan) -> str | None:
    """Why this reservation cannot be applied to THIS segment, or None. Shared by the API (which
    refuses) and the renderer (which skips, so one unusable row cannot take the segment down)."""
    shape = reservation_value_issue(mac, ip, name)
    if shape:
        return shape
    address = ipaddress.ip_address(ip.strip())
    segment = net24(plan.segment_ip)
    if not segment or address not in ipaddress.ip_network(segment):
        return f"{ip} is outside the segment {segment or '(unset)'}"
    if plan.segment_ip and str(address) == plan.segment_ip.strip():
        return "that is the gateway's own address"
    return None


def applied_reservations(plan: NetPlan) -> list[tuple[str, str, str]]:
    """The reservations this plan can actually render. A row that does not fit the current
    segment — a restore from a gateway that served another one — is left out and logged rather
    than raising: the segment's DHCP and ruleset are worth more than that row."""
    out = []
    for mac, ip, name in plan.reservations:
        problem = reservation_issue(mac, ip, name, plan)
        if problem:
            logger.warning("skipping reservation %s %s: %s", mac, ip, problem)
            continue
        out.append((mac, ip, name))
    return out


def render_dnsmasq(plan: NetPlan) -> str:
    _no_line_break(plan)
    # dnsmasq is the segment's DHCP (v4) + RA (v6) server — the panel's own supervised child
    # (router DHCP/RA is off on the VLAN). It hands clients the Pi as gateway and a public DNS
    # the tproxy rule above intercepts and carries through the tunnel (no RU-resolver leak).
    # When the v6 tunnel is on, `enable-ra` + `constructor:<seg>` advertises whatever /64 sits
    # on the segment iface (the ULA or the PD GUA); `ra-stateless` = SLAAC addresses + DHCPv6
    # DNS only (no stateful v6 leases).
    base = f"""\
interface={plan.segment_iface}
bind-interfaces
dhcp-leasefile={plan.dnsmasq_leases}
dhcp-range={plan.dhcp_start},{plan.dhcp_end},{plan.dhcp_lease}
dhcp-option=3,{plan.segment_ip}
dhcp-option=6,{plan.client_dns}
"""
    for mac, ip, name in applied_reservations(plan):
        base += f"dhcp-host={mac},{ip}" + (f",{name}\n" if name else "\n")
    if plan.ipv6_enabled:
        base += f"""\
enable-ra
dhcp-range=::,constructor:{plan.segment_iface},ra-stateless,64,{plan.dhcp_lease}
dhcp-option=option6:dns-server,[{plan.client_dns6}]
"""
    return base
