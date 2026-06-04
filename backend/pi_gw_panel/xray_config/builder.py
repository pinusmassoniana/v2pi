from pi_gw_panel.config import Settings
from pi_gw_panel.models import Node, TuningProfile
from pi_gw_panel.xray_config.routing import rules_to_xray


def build_config(node: Node, settings: Settings, profile: TuningProfile | None = None,
                 routing=None, tunneled_fetch: bool = False, stats: dict | None = None,
                 dns_intercept: bool = False) -> dict:
    """Build xray config.json.

    With ``profile=None, routing=None, tunneled_fetch=False, stats=None`` this is
    byte-identical to the Wave-0 config (tproxy in, vless+reality+vision out, DoH dns,
    private→direct + catch-all→proxy routing). The extras are purely additive:

    - ``profile`` (a TuningProfile) drives the realitySettings ``fingerprint``, TLS
      fragmentation, mux on/off, the DoH resolver (url + enable), and a QUIC routing
      rule (``drop``→block / ``proxy``→proxy / ``allow``→none).
    - ``routing`` (ordered RoutingRule list + a default action) replaces the Wave-0
      catch-all via :func:`apply_routing` (Task 4).
    - ``tunneled_fetch`` gates the 127.0.0.1 http inbound used to fetch subscriptions
      through the tunnel (port = ``settings.local_proxy_port``).
    - ``stats`` (``{"api_port": int}``) enables xray's StatsService: per-outbound
      traffic counters + an api dokodemo inbound on ``127.0.0.1`` + a first routing
      rule dispatching that inbound to the api (Wave 3a traffic graph).
    """
    fingerprint = profile.fingerprint if profile is not None else node.fingerprint
    doh_on = profile.doh_enabled if profile is not None else True
    doh_url = profile.doh_url if profile is not None and profile.doh_url else settings.doh_url
    # gateway DNS interception needs a tunnelled resolver present even if a profile disabled DoH
    dns_servers = ([{"address": doh_url}] if (doh_on or dns_intercept) else []) + ["localhost"]

    # proxy outbound: user + transport/security-aware streamSettings.
    #   tcp+reality+vision (legacy) ── realitySettings + user.flow
    #   xhttp+tls            ──────── xhttpSettings{path,host,mode} + tlsSettings{sni,alpn}
    user: dict = {"id": node.uuid, "encryption": "none"}
    if node.flow:                         # Vision flow only; XHTTP nodes carry none
        user["flow"] = node.flow
    network = node.network or "tcp"
    security = node.security or "reality"
    stream: dict = {"network": network, "security": security,
                    "sockopt": {"mark": settings.egress_mark}}
    if security == "reality":
        stream["realitySettings"] = {"serverName": node.sni, "fingerprint": fingerprint,
                                     "publicKey": node.public_key, "shortId": node.short_id}
    else:
        tls: dict = {"serverName": node.sni, "fingerprint": fingerprint}
        if node.alpn:
            tls["alpn"] = [a.strip() for a in node.alpn.split(",") if a.strip()]
        stream["tlsSettings"] = tls
    if network == "xhttp":
        stream["xhttpSettings"] = {k: getattr(node, k) for k in ("path", "host", "mode")
                                   if getattr(node, k)}

    cfg = {
        "log": {"loglevel": "warning",
                "error": settings.xray_error_log, "access": settings.xray_access_log},
        "dns": {"servers": dns_servers},
        "inbounds": [
            {
                "tag": "tproxy-in",
                "protocol": "dokodemo-door",
                "port": settings.tproxy_port,
                "settings": {"network": "tcp,udp", "followRedirect": True},
                "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
                "streamSettings": {"sockopt": {"tproxy": "tproxy", "mark": settings.egress_mark}},
            }
        ],
        "outbounds": [
            {
                "tag": "proxy",
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {"address": node.address, "port": node.port, "users": [user]}
                    ]
                },
                "streamSettings": stream,
            },
            {"tag": "direct", "protocol": "freedom", "settings": {},
             "streamSettings": {"sockopt": {"mark": settings.egress_mark}}},
            {"tag": "block", "protocol": "blackhole", "settings": {}},
        ],
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "rules": [
                {"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"},
                {"type": "field", "network": "tcp,udp", "outboundTag": "proxy"},
            ],
        },
    }

    proxy_out = cfg["outbounds"][0]

    if profile is not None:
        # mux on/off — emitted whenever a profile is resolved (Wave-0 omits it).
        proxy_out["mux"] = {"enabled": bool(profile.mux_enabled)}

        # TLS fragmentation: a fragment freedom outbound that `proxy` dials through.
        if profile.frag_enabled:
            cfg["outbounds"].insert(1, {
                "tag": "fragment",
                "protocol": "freedom",
                "settings": {"fragment": {
                    "packets": profile.frag_packets,
                    "length": profile.frag_length,
                    "interval": profile.frag_interval,
                }},
                "streamSettings": {"sockopt": {"mark": settings.egress_mark}},
            })
            proxy_out["streamSettings"]["sockopt"]["dialerProxy"] = "fragment"

    # Ordered routing rules replace the Wave-0 catch-all (private→direct kept first,
    # the configurable default-action catch-all last). routing == (rules, default_action).
    if routing is not None:
        rules, default_action = routing
        cfg["routing"]["rules"] = rules_to_xray(rules, default_action)

    # QUIC, layered on top of whatever routing produced: drop→block, proxy→proxy,
    # allow→no rule. Inserted before the catch-all so the catch-all stays last.
    if profile is not None and profile.quic in ("drop", "proxy"):
        tag = "block" if profile.quic == "drop" else "proxy"
        rlist = cfg["routing"]["rules"]
        rlist.insert(len(rlist) - 1,
                     {"type": "field", "protocol": ["quic"], "outboundTag": tag})

    # Gateway DNS (toggle): resolve segment clients' DNS inside xray over DoH/TCP through the
    # tunnel instead of proxying their raw UDP — so it works even on nodes that don't relay UDP
    # (the "no internet, but the node works for TCP" case). Route :53 from the tproxy inbound to
    # a dns outbound, which resolves via the DoH server already in the dns block.
    if dns_intercept:
        cfg["outbounds"].append({"protocol": "dns", "tag": "dns-out"})
        cfg["routing"]["rules"].insert(0, {
            "type": "field", "inboundTag": ["tproxy-in"], "port": 53, "outboundTag": "dns-out"})

    # xray StatsService (Wave 3a): per-outbound traffic counters + a local api inbound,
    # with its dispatch rule prepended first. Gated — stats=None keeps the config Wave-0.
    if stats is not None:
        cfg["stats"] = {}
        cfg["policy"] = {"system": {"statsOutboundUplink": True, "statsOutboundDownlink": True}}
        cfg["api"] = {"tag": "api", "services": ["StatsService"]}
        cfg["inbounds"].append({
            "tag": "api",
            "protocol": "dokodemo-door",
            "listen": "127.0.0.1",
            "port": stats["api_port"],
            "settings": {"address": "127.0.0.1"},
        })
        cfg["routing"]["rules"].insert(0, {"type": "field", "inboundTag": ["api"], "outboundTag": "api"})

    # Gated local http proxy inbound so the backend can fetch subscriptions through the tunnel.
    if tunneled_fetch:
        cfg["inbounds"].append({
            "tag": "sub-fetch",
            "protocol": "http",
            "listen": "127.0.0.1",
            "port": settings.local_proxy_port,
            "settings": {},
        })

    return cfg
