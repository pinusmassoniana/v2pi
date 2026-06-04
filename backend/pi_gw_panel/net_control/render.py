from pi_gw_panel.net_control.plan import NetPlan


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


def render_nft6(plan: NetPlan) -> str:
    """IPv6 leak-guard (A2). v6 is never tproxy'd, so when the kill-switch is on we must
    still drop client→global-v6 or it routes straight to the WAN around the tunnel *and*
    around the v4 kill-switch. Link-local / ULA / loopback stay allowed. Empty (→ the v6
    table is removed) when the kill-switch is off."""
    if not plan.kill_switch:
        return ""
    return f"""\
table ip6 pi_gw_panel {{
    chain forward {{
        type filter hook forward priority filter; policy accept;
        iifname "{plan.segment_iface}" ip6 daddr != {{ ::1/128, fe80::/10, fc00::/7 }} drop
    }}
}}
"""


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
