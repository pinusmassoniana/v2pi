import ipaddress
from pi_gw_panel.net_control.plan import NetPlan


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
    #
    # Kill-switch (fail-closed): when on, add a forward-chain drop for client-segment
    # traffic headed to a non-private destination. Correctly-tunneled client packets
    # are tproxy'd to local xray (never forwarded) so they're unaffected; only leaked
    # client→WAN traffic is dropped. Byte-identical to the off path when disabled.
    #
    # `tunnel_up=False` renders the FAIL-CLOSED guard: the forward drop only, with no
    # prerouting/tproxy — used when the tunnel is intentionally stopped but the kill-switch
    # must keep blocking client→WAN (A1). With the kill-switch off this is an empty table.
    forward = ""
    if plan.kill_switch:
        forward = f"""\
    chain forward {{
        type filter hook forward priority filter; policy accept;
        iifname "{plan.segment_iface}" ip daddr != {{ 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 }} drop
    }}
"""
    prerouting = ""
    if tunnel_up:
        prerouting = f"""\
    chain prerouting {{
        type filter hook prerouting priority mangle; policy accept;
        meta mark 0x{plan.egress_mark:x} return
        ip daddr {{ 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 }} return
        udp dport {{ 67, 68 }} return
        iifname "{plan.segment_iface}" meta l4proto {{ tcp, udp }} meta mark set 0x{plan.fwmark:x} tproxy ip to :{plan.tproxy_port} accept
    }}
"""
    return f"""\
table ip pi_gw_panel {{
{prerouting}{forward}}}
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
    - **otherwise + kill-switch on** → fail-closed drop of client→global-v6 (the v1.8 leak-guard).
    - **otherwise + kill-switch off** → empty (the v6 table is removed)."""
    local6 = _local6(plan)
    drop = (f'        iifname "{plan.segment_iface}" ip6 daddr != {local6} drop\n')
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
        iifname "{plan.segment_iface}" meta l4proto {{ tcp, udp }} meta mark set 0x{plan.fwmark:x} tproxy ip6 to :{plan.tproxy_port6} accept
    }}
{forward}}}
"""
    if plan.kill_switch:
        return f"""\
table ip6 pi_gw_panel {{
{forward}}}
"""
    return ""


def render_dnsmasq(plan: NetPlan) -> str:
    # dnsmasq is the segment's DHCP server (router DHCP is off on VLAN2). It hands
    # clients the Pi as gateway and a public DNS that the tproxy rule above
    # intercepts and carries through the tunnel (no RU-resolver leak).
    return f"""\
interface={plan.segment_iface}
bind-interfaces
dhcp-range={plan.dhcp_start},{plan.dhcp_end},{plan.dhcp_lease}
dhcp-option=3,{plan.segment_ip}
dhcp-option=6,{plan.client_dns}
"""
