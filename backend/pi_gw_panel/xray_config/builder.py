import logging

from pi_gw_panel.config import Settings
from pi_gw_panel.models import Node, TuningProfile
from pi_gw_panel.xray_config.routing import rules_to_xray

logger = logging.getLogger(__name__)

# Destinations a client-facing inbound may never be allowed to reach. The gateway runs
# unauthenticated services on loopback — xray's own gRPC api (127.0.0.1:stats_api_port) and the
# sub-fetch http proxy (127.0.0.1:local_proxy_port) — and `direct` is a plain freedom outbound,
# so without an explicit block the gateway happily serves them: `rw-in` sniffs with
# ``routeOnly``, which lets a remote client name `127.0.0.1` outright, and `tproxy-in` can be
# steered there by a sniffed domain that resolves to loopback (DNS rebinding).
#
# "The gateway itself" has more than one spelling, and the block must name every one of them:
# on Linux a connect() to the unspecified address is delivered to a local listener, so
# `0.0.0.0:10808` reaches the very sub-fetch proxy that `127.0.0.1:10808` is blocked from, and
# `[::]` does the same for the v6 inbound. Blocking only the canonical loopback prefix left that
# alias falling through `geoip:private → direct`. The whole 0.0.0.0/8 ("this network") goes with
# it: no address in it is a routable destination, so blocking it costs nothing.
#
# NOT listed: the IPv4-mapped v6 spellings (`::ffff:127.0.0.1`). A `::ffff:…/104` literal is not
# safely expressible as a routing CIDR here, and the blunt `::ffff:0:0/96` would blackhole every
# IPv4 destination that reaches the dual-stack `tproxy-in6` listener in mapped form. Their match
# relies on xray folding a mapped address back to its IPv4 form, where `127.0.0.0/8` covers it.
LOOPBACK_NETS = [
    "0.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16",
    "::/128", "::1/128", "fe80::/10",
]

RW_TAG = "rw-in"
# The LAN-by-name outbound. Emitted ONLY by the remote-access feature and named by exactly one
# routing rule, which is what lets a revocation identify both as its own and take them away.
DIRECT_LAN_TAG = "direct-lan"


def rw_inbound_block(rw_inbound: dict) -> dict:
    """The `rw-in` inbound, alone. THE definition — every credential the remote-access feature
    hands out (client uuids, the Reality private key, short ids, server names, the port) lives
    in this one object and nowhere else in the config.

    Factored out of build_config because a revocation must be able to replace exactly this
    object in a config it did not build (see `_rw_sanitized_config`): removing a credential
    needs no node, but it does need the field list to stay in one place. A field added here
    reaches the sanitizer for free; a second hand-written copy would silently keep serving
    whatever the copy forgot.
    """
    return {
        "tag": RW_TAG,
        "listen": "0.0.0.0",
        "port": rw_inbound["port"],
        "protocol": "vless",
        "settings": {
            "clients": [{"id": c["id"], "flow": "xtls-rprx-vision", "email": c["email"]}
                        for c in rw_inbound["clients"]],
            "decryption": "none",
        },
        "streamSettings": {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "show": False,
                "dest": rw_inbound["dest"],
                "xver": 0,
                "serverNames": rw_inbound["server_names"],
                "privateKey": rw_inbound["private_key"],
                "shortIds": rw_inbound["short_ids"],
            },
        },
        # routeOnly: route on the sniffed domain but keep dialing the address the client
        # chose. Without it xray discards the client's own target.
        "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"],
                     "routeOnly": True},
    }


def rw_grants(block: dict | None) -> frozenset[str]:
    """Everything an `rw-in` block GRANTS, as a set of namespaced tokens.

    The point is set arithmetic on two blocks: `live - expected` is empty exactly when the
    config on disk hands out nothing the store does not currently hand out. Fewer clients on
    disk than the store grants is not excess (it is merely stale in the safe direction), so a
    subset test — not equality — is the honest question to ask before serving a file.

    Namespaced because the values share a space: a short id is hex and so is nothing else here,
    but a server name and a dest host are both host names, and an unprefixed union would let one
    field's value vouch for another's. Lives next to `rw_inbound_block` and reads the same
    fields, so a credential added to the block is a credential this notices — the whole reason
    the block is one function.

    A block of the wrong shape yields a token that can never be in an expected set, so an
    unreadable grant reads as excess: the fail-safe direction.
    """
    if not isinstance(block, dict):
        return frozenset()
    out = {f"port:{block.get('port')!r}"}
    settings = block.get("settings")
    clients = settings.get("clients") if isinstance(settings, dict) else None
    if isinstance(clients, list):
        for c in clients:
            out.add(f"client:{(c.get('id') if isinstance(c, dict) else c)!r}")
    else:
        out.add(f"clients:{clients!r}")
    stream = block.get("streamSettings")
    reality = stream.get("realitySettings") if isinstance(stream, dict) else None
    reality = reality if isinstance(reality, dict) else {}
    out.add(f"privateKey:{reality.get('privateKey')!r}")
    out.add(f"dest:{reality.get('dest')!r}")
    for key, ns in (("shortIds", "shortId"), ("serverNames", "serverName")):
        vals = reality.get(key)
        if isinstance(vals, list):
            out.update(f"{ns}:{v!r}" for v in vals)
        else:
            out.add(f"{key}:{vals!r}")
    return frozenset(out)


def rw_lan_outbound(settings: Settings) -> dict:
    """The LAN-by-name outbound. A SECOND freedom outbound: only `UseIP` resolves through xray's
    own DNS (where `dns.hosts` lives). The global `direct` stays AsIs on purpose — flipping it
    would push every RU domain through DoH and wreck RU CDN geolocation.

    Factored out beside `rw_inbound_block` and for the same reason: it belongs to the
    remote-access feature, so a revocation has to be able to take it away from a config it did
    not build (see `_rw_reconcile_lan`), and a second hand-written copy would drift.
    """
    return {"tag": DIRECT_LAN_TAG, "protocol": "freedom",
            "settings": {"domainStrategy": "UseIP"},
            "streamSettings": {"sockopt": {"mark": settings.egress_mark}}}


def rw_lan_rule(hosts: dict) -> dict:
    """One `full:` (exact-name) rule for every mapped host.

    NOT a `domain:<last label>` suffix rule derived from the names: xray's `domain:x` matches
    `x` and every subdomain of it, so a single mapped `nas.example.com` would emit `domain:com`
    and send EVERY .com destination out the plain `direct-lan` freedom outbound — first match
    wins, and this rule sits ahead of the catch-all.

    It is not scoped to `rw-in` either, so it applies to tproxy traffic as well; that is
    deliberate (a LAN name should resolve the same from inside), and it is exactly why a
    revocation that removes the inbound must remove this too — otherwise turning remote access
    off leaves mapped names routed out an untunnelled freedom outbound for every LAN client.
    """
    return {"type": "field",
            "domain": [f"full:{name}" for name in sorted(hosts)],
            "outboundTag": DIRECT_LAN_TAG}


def build_config(node: Node, settings: Settings, profile: TuningProfile | None = None,
                 routing=None, tunneled_fetch: bool = False, stats: dict | None = None,
                 dns_intercept: bool = False, domain_strategy: str = "IPIfNonMatch",
                 ipv6_tproxy: bool = False, profile_explicit: bool = False,
                 rw_inbound: dict | None = None) -> dict:
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
    - ``rw_inbound`` (see :mod:`pi_gw_panel.rw_inbound`) adds the road-warrior
      VLESS+Vision+Reality inbound, plus the optional resolve-LAN-names-on-the-gateway
      path. It needs NO routing changes: ``geoip:private → direct`` already carries a
      remote client into the LAN, and the catch-all already carries it out the tunnel.
    """
    # The node's own fingerprint is server-specific (it comes from the subscription, and some
    # reality servers reject other uTLS fingerprints — e.g. one that fails on `chrome`). The
    # auto-applied DEFAULT profile must NOT clobber it; only an EXPLICITLY-assigned profile
    # (profile_explicit) overrides the node's fingerprint. Other profile knobs still apply.
    fingerprint = profile.fingerprint if (profile is not None and profile_explicit) else node.fingerprint
    doh_on = profile.doh_enabled if profile is not None else True
    doh_url = profile.doh_url if profile is not None and profile.doh_url else settings.doh_url
    # gateway DNS interception needs a tunnelled resolver present even if a profile disabled DoH
    if dns_intercept or doh_on:
        # Fail closed on a resolver that is not actually encrypted. `validate_profile` rejects a
        # plaintext URL at the API boundary, but that only guards NEW writes: a profile stored
        # before the rule existed, restored from an older backup, or hand-edited in the DB still
        # arrives here — and this branch presents whatever it carries as the one encrypted
        # resolver, in plaintext, for every client domain the gateway resolves. A config that
        # cannot be built is a visible failure; a silent downgrade to cleartext is not.
        if not doh_url.startswith("https://"):
            raise ValueError(
                f"DoH resolver URL must use https:// (got {doh_url!r}); a plaintext resolver "
                "would expose every client's domains and is never installed as dns.servers")
        # DoH only — no `localhost` fallback. With `IPIfNonMatch` xray resolves nearly every
        # destination domain itself, so a plaintext fallback server hands the full domain history
        # of every client to the host/ISP resolver in cleartext. One encrypted resolver that can
        # fail is the right trade against a silent leak that cannot be noticed.
        dns_servers = [{"address": doh_url}]
    else:
        # DoH explicitly disabled ⇒ xray resolves through the host resolver in cleartext. That is
        # a leak, so it is never silent: it lands in the backend log the panel exposes.
        logger.warning(
            "tuning profile %r has DoH disabled: xray will resolve destination domains through "
            "the host resolver in cleartext (no encrypted resolver is configured)",
            getattr(profile, "name", "?"))
        dns_servers = ["localhost"]

    # proxy outbound: user + transport/security-aware streamSettings.
    #   tcp+reality+vision (legacy) ── realitySettings + user.flow
    #   xhttp+tls            ──────── xhttpSettings{path,host,mode} + tlsSettings{sni,alpn}
    user: dict = {"id": node.uuid, "encryption": "none"}
    if node.flow:                         # Vision flow only; XHTTP nodes carry none
        user["flow"] = node.flow
    network = node.network or "tcp"
    security = node.security or "reality"
    # Fail closed: Node.normalize() allow-lists this, so an unknown value means something
    # bypassed normalization — never render it into streamSettings (`none` = plaintext VLESS).
    if security not in ("reality", "tls"):
        raise ValueError(f"unsupported node security '{security}' (expected reality or tls)")
    stream: dict = {"network": network, "security": security,
                    "sockopt": {"mark": settings.egress_mark}}
    if security == "reality":
        stream["realitySettings"] = {"serverName": node.sni, "fingerprint": fingerprint,
                                     "publicKey": node.public_key, "shortId": node.short_id}
    else:
        tls: dict = {"serverName": node.sni, "fingerprint": fingerprint}
        # alpn is also a subscription-carried node field — same rule as fingerprint: only an
        # explicitly-assigned profile overrides it; the default profile keeps the node's own.
        alpn = profile.alpn if (profile is not None and profile_explicit and profile.alpn) else node.alpn
        if alpn:
            tls["alpn"] = [a.strip() for a in alpn.split(",") if a.strip()]
        if profile is not None and profile.tls_min:
            tls["minVersion"] = profile.tls_min
        if profile is not None and profile.tls_max:
            tls["maxVersion"] = profile.tls_max
        stream["tlsSettings"] = tls
    if network == "xhttp":
        xs: dict = {k: getattr(node, k) for k in ("path", "host", "mode") if getattr(node, k)}
        if profile is not None:
            extra: dict = {}
            if profile.xhttp_padding:
                extra["xPaddingBytes"] = profile.xhttp_padding
            xmux: dict = {}
            if profile.xmux_max_concurrency:
                xmux["maxConcurrency"] = profile.xmux_max_concurrency
            if profile.xmux_max_connections:
                xmux["maxConnections"] = profile.xmux_max_connections
            if xmux:
                extra["xmux"] = xmux
            if extra:
                xs["extra"] = extra
        stream["xhttpSettings"] = xs

    cfg = {
        # Keep access logging off by default. With no error path Xray writes diagnostics to
        # stderr, which the supervisor retains as a bounded, redacted tail.
        "log": {"loglevel": "warning", "access": "none"},
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
            "domainStrategy": domain_strategy,
            "rules": [
                {"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"},
                {"type": "field", "network": "tcp,udp", "outboundTag": "proxy"},
            ],
        },
    }

    proxy_out = cfg["outbounds"][0]

    # IPv6 tproxy: a second dokodemo inbound listening on :: at tproxy_port6, fed by the nft
    # `ip6` tproxy rule. Separate from the v4 inbound to avoid IPV6_V6ONLY tproxy edge-cases.
    # The catch-all routing rule already sends it out `proxy`; the exit node dials the v6 dest.
    if ipv6_tproxy:
        cfg["inbounds"].append({
            "tag": "tproxy-in6",
            "protocol": "dokodemo-door",
            "listen": "::",
            "port": settings.tproxy_port6,
            "settings": {"network": "tcp,udp", "followRedirect": True},
            "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
            "streamSettings": {"sockopt": {"tproxy": "tproxy", "mark": settings.egress_mark}},
        })

    if profile is not None:
        # mux is invalid with XTLS Vision — only emit it for non-Vision (xhttp) outbounds (TC1).
        if not node.flow:
            mux: dict = {"enabled": bool(profile.mux_enabled)}
            if profile.mux_enabled and profile.mux_concurrency.strip():
                mux["concurrency"] = int(profile.mux_concurrency)
            if profile.mux_enabled and profile.xudp_proxy_udp443:
                mux["xudpProxyUDP443"] = profile.xudp_proxy_udp443
            proxy_out["mux"] = mux

        # Fragment + UDP noise: a freedom outbound the `proxy` dials through (dialerProxy).
        if profile.frag_enabled or (profile.noise_enabled and profile.noises):
            fset: dict = {}
            if profile.frag_enabled:
                fset["fragment"] = {"packets": profile.frag_packets,
                                    "length": profile.frag_length,
                                    "interval": profile.frag_interval}
            if profile.noise_enabled and profile.noises:
                fset["noises"] = profile.noises
            cfg["outbounds"].insert(1, {
                "tag": "fragment", "protocol": "freedom", "settings": fset,
                "streamSettings": {"sockopt": {"mark": settings.egress_mark}},
            })
            proxy_out["streamSettings"]["sockopt"]["dialerProxy"] = "fragment"

    # Ordered routing rules replace the Wave-0 catch-all (private→direct kept first,
    # the configurable default-action catch-all last). routing == (rules, default_action).
    if routing is not None:
        rules, default_action = routing
        cfg["routing"]["rules"] = rules_to_xray(rules, default_action)

    # QUIC, layered on top of whatever routing produced: drop→block, proxy→proxy,
    # allow→no rule. Inserted immediately after private→direct, i.e. BEFORE every user rule:
    # first match wins, so behind a user rule (say `geosite:google → proxy`) the knob silently
    # stops applying and QUIC still reaches a node that may not relay it — the exact blackhole
    # the knob exists to prevent.
    if profile is not None and profile.quic in ("drop", "proxy"):
        tag = "block" if profile.quic == "drop" else "proxy"
        rlist = cfg["routing"]["rules"]
        after_private = next((i + 1 for i, r in enumerate(rlist)
                              if r.get("ip") == ["geoip:private"] and r.get("outboundTag") == "direct"),
                             0)
        rlist.insert(after_private,
                     {"type": "field", "protocol": ["quic"], "outboundTag": tag})

    # Gateway DNS (toggle): resolve segment clients' DNS inside xray over DoH/TCP through the
    # tunnel instead of proxying their raw UDP — so it works even on nodes that don't relay UDP
    # (the "no internet, but the node works for TCP" case). Route :53 from the tproxy inbound to
    # a dns outbound, which resolves via the DoH server already in the dns block.
    if dns_intercept:
        cfg["outbounds"].append({"protocol": "dns", "tag": "dns-out"})
        in_tags = ["tproxy-in", "tproxy-in6"] if ipv6_tproxy else ["tproxy-in"]
        cfg["routing"]["rules"].insert(0, {
            "type": "field", "inboundTag": in_tags, "port": 53, "outboundTag": "dns-out"})

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

    # Road-warrior inbound, appended LAST so no existing inbound's index shifts. `flow` is
    # only valid on tcp+reality, and mux is never set on an inbound — don't mirror the
    # outbound's mux logic above. rw_inbound is None whenever the client list is empty:
    # xray refuses to start on a vless inbound with no clients, and taking the whole tunnel
    # down because a remote-access client was deleted would be a self-inflicted outage.
    if rw_inbound is not None:
        cfg["inbounds"].append(rw_inbound_block(rw_inbound))

        # Reach LAN hosts by name instead of by address. A remote client that routes
        # `192.168.1.0/24` into the tunnel collides with every cafe running the same prefix;
        # a name cannot collide. The gateway owns the mapping, so the client forwards the
        # name and never has to resolve it.
        hosts = rw_inbound.get("hosts") or {}
        if hosts:
            cfg["dns"]["hosts"] = dict(hosts)
            cfg["outbounds"].append(rw_lan_outbound(settings))
            rlist = cfg["routing"]["rules"]
            rlist.insert(len(rlist) - 1, rw_lan_rule(hosts))

    # FIRST rule, inserted last so nothing can end up ahead of it: client-facing inbounds may
    # never reach the gateway's loopback services (see LOOPBACK_NETS). It has to precede
    # `geoip:private → direct`, which would otherwise hand the request to plain `freedom`.
    client_facing = ["tproxy-in"]
    if ipv6_tproxy:
        client_facing.append("tproxy-in6")
    if rw_inbound is not None:
        client_facing.append(RW_TAG)
    cfg["routing"]["rules"].insert(0, {
        "type": "field",
        "inboundTag": client_facing,
        "ip": list(LOOPBACK_NETS),
        "outboundTag": "block",
    })

    return cfg
