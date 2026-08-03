"""Road-warrior inbound: VLESS + XTLS-Vision + Reality for reaching the gateway from outside.

Settings live in the k/v `settings` table (no schema migration); clients are one JSON blob.
The Reality keypair is NOT generated here — it is produced once by hand (`xray x25519`) and
pasted into the panel. That keeps `cryptography` out of the dependency list and avoids parsing
an `xray` CLI output whose labels have changed between versions.

`resolve()` is the single funnel: it returns the dict `build_config(rw_inbound=...)` wants, or
None when the feature must not emit an inbound at all.
"""
from __future__ import annotations

import ipaddress
import json
import re
import secrets
import uuid
from urllib.parse import quote

# k/v defaults. Absent key ⇒ this value; `rw_private_key`/`rw_endpoint` have no default on
# purpose — without them the feature cannot work and resolve() returns None.
DEFAULTS = {
    "rw_enabled": "0",
    "rw_port": "443",
    "rw_dest": "www.microsoft.com:443",
    "rw_server_names": "www.microsoft.com",
    "rw_short_ids": "",
    "rw_endpoint": "",
    "rw_clients": "[]",
    "rw_hosts": "{}",
    "rw_routed_nets": "",
}

_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_EMAIL = re.compile(r"^[A-Za-z0-9._-]{1,40}$")


def gen_client_id() -> str:
    return str(uuid.uuid4())


def gen_short_id() -> str:
    """8 bytes → 16 hex chars. Reality accepts 1–8 bytes; we always emit the max."""
    return secrets.token_hex(8)


def parse_csv(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def parse_nets(raw: str) -> list[str]:
    """CIDR list; dedup preserving order (the .conf emits one rule per entry)."""
    out, seen = [], set()
    for n in parse_csv(raw):
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _get(store, key: str) -> str:
    v = store.get_setting(key)
    return DEFAULTS.get(key, "") if v is None else v


# --- validation -------------------------------------------------------------------------

def validate_port(raw: str) -> int:
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"rw_port must be an integer, got {raw!r}")
    if not 1 <= port <= 65535:
        raise ValueError(f"rw_port out of range: {port}")
    return port


def validate_short_ids(ids: list[str]) -> list[str]:
    """Reality shortIds are 1–8 bytes of hex ⇒ 2–16 hex chars, even length."""
    for sid in ids:
        if len(sid) % 2 or not 2 <= len(sid) <= 16 or not re.fullmatch(r"[0-9a-fA-F]+", sid):
            raise ValueError(f"short id must be 2-16 hex chars of even length, got {sid!r}")
    return ids


def validate_hosts(hosts: dict) -> dict[str, str]:
    """`{name: ip}` for the LAN-by-name path. Names must be dotted (a bare label would
    collide with search-domain resolution) and must NOT end in `.local` — iOS/macOS answer
    that over mDNS/Bonjour and never hand it to the proxy, so it would silently never work."""
    out: dict[str, str] = {}
    for name, ip in hosts.items():
        name = str(name).strip().lower().rstrip(".")
        if not _HOSTNAME.match(name):
            raise ValueError(f"invalid host name {name!r} (need a dotted name, e.g. nas.v2pi)")
        if name.endswith(".local"):
            raise ValueError(f"{name!r}: the .local suffix is captured by mDNS on iOS/macOS "
                             "and never reaches the tunnel — use another suffix (e.g. .v2pi)")
        try:
            ipaddress.IPv4Address(str(ip).strip())
        except ValueError:
            raise ValueError(f"host {name!r} must map to an IPv4 address, got {ip!r}")
        out[name] = str(ip).strip()
    return out


def validate_nets(nets: list[str]) -> list[str]:
    for n in nets:
        try:
            ipaddress.IPv4Network(n, strict=False)
        except ValueError:
            raise ValueError(f"invalid CIDR {n!r}")
    return nets


def validate_email(email: str) -> str:
    """Client label. Also becomes the xray inbound `email` (its stats/log key), so keep it
    to a conservative charset rather than passing arbitrary user text into the config."""
    email = (email or "").strip()
    if not _EMAIL.match(email):
        raise ValueError("client name must be 1-40 chars of letters, digits, dot, dash or underscore")
    return email


# --- clients ----------------------------------------------------------------------------

def get_clients(store) -> list[dict]:
    try:
        raw = json.loads(_get(store, "rw_clients"))
    except (TypeError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    out = []
    for c in raw:
        if isinstance(c, dict) and c.get("id") and c.get("email"):
            out.append({"id": str(c["id"]), "email": str(c["email"]),
                        "enabled": bool(c.get("enabled", True))})
    return out


def set_clients(store, clients: list[dict]) -> None:
    store.set_setting("rw_clients", json.dumps(clients))


MAX_CLIENTS = 16


def add_client(store, email: str) -> dict:
    email = validate_email(email)
    clients = get_clients(store)
    if any(c["email"] == email for c in clients):
        raise ValueError(f"a client named {email!r} already exists")
    if len(clients) >= MAX_CLIENTS:
        raise ValueError(f"at most {MAX_CLIENTS} clients (remove one first)")
    client = {"id": gen_client_id(), "email": email, "enabled": True}
    set_clients(store, clients + [client])
    return client


def delete_client(store, client_id: str) -> bool:
    clients = get_clients(store)
    kept = [c for c in clients if c["id"] != client_id]
    if len(kept) == len(clients):
        return False
    set_clients(store, kept)
    return True


def set_client_enabled(store, client_id: str, enabled: bool) -> bool:
    """Suspend/resume a client without losing its uuid. The point is a lost or stolen device:
    revoke its access now, keep the identity so nothing else has to be reissued."""
    clients = get_clients(store)
    found = False
    for c in clients:
        if c["id"] == client_id:
            c["enabled"] = enabled
            found = True
    if found:
        set_clients(store, clients)
    return found


def get_hosts(store) -> dict[str, str]:
    try:
        raw = json.loads(_get(store, "rw_hosts"))
    except (TypeError, ValueError):
        return {}
    return validate_hosts(raw) if isinstance(raw, dict) else {}


def host_suffixes(hosts: dict[str, str]) -> list[str]:
    """The last label of each mapped name (`nas.v2pi` → `v2pi`). Derived rather than stored
    as its own setting so it can never drift out of sync with the host list."""
    return sorted({name.rsplit(".", 1)[-1] for name in hosts})


# --- the funnel -------------------------------------------------------------------------

def resolve(store) -> dict | None:
    """The `rw_inbound` dict for build_config, or None when no inbound may be emitted.

    None when: disabled, no private key, or no *enabled* clients — xray refuses to start on a
    vless inbound with an empty `clients` list, so an enabled-but-clientless config must not
    emit the inbound at all rather than take the whole tunnel down.
    """
    if _get(store, "rw_enabled") != "1":
        return None
    private_key = (_get(store, "rw_private_key") or "").strip()
    if not private_key:
        return None
    clients = [c for c in get_clients(store) if c["enabled"]]
    if not clients:
        return None
    return {
        "port": validate_port(_get(store, "rw_port")),
        "dest": _get(store, "rw_dest"),
        "server_names": parse_csv(_get(store, "rw_server_names")),
        "private_key": private_key,
        "short_ids": validate_short_ids(parse_csv(_get(store, "rw_short_ids"))),
        "clients": clients,
        "hosts": get_hosts(store),
    }


# --- client-facing artifacts ------------------------------------------------------------

def link(store, client: dict) -> str:
    """`vless://` share link. Shadowrocket parses these reliably, so this stays the fallback
    when the generated .conf is refused."""
    endpoint = (_get(store, "rw_endpoint") or "").strip()
    if not endpoint:
        raise ValueError("set the external endpoint (DDNS name or WAN IP) first")
    public_key = (_get(store, "rw_public_key") or "").strip()
    if not public_key:
        raise ValueError("set the Reality public key first")
    names = parse_csv(_get(store, "rw_server_names"))
    sids = parse_csv(_get(store, "rw_short_ids"))
    q = ("type=tcp&security=reality&encryption=none&flow=xtls-rprx-vision&fp=chrome"
         f"&sni={quote(names[0] if names else '')}&pbk={quote(public_key)}"
         f"&sid={quote(sids[0] if sids else '')}")
    port = validate_port(_get(store, "rw_port"))
    return f"vless://{client['id']}@{endpoint}:{port}?{q}#{quote(client['email'])}"


def _tag(email: str) -> str:
    """Shadowrocket splits [Proxy] lines on commas — keep the policy name comma-free."""
    return re.sub(r"[^A-Za-z0-9._-]", "-", email) or "v2pi"


def shadowrocket_conf(store, client: dict, nets: list[str]) -> str:
    """A Surge-style .conf for Shadowrocket (iOS + Apple-Silicon macOS).

    Two deliberate choices, both load-bearing:

    - Private ranges are NOT in `skip-proxy`. Every client ships a default "bypass LAN"
      rule, and with it `192.168.1.88` resolves to the cafe's network instead of ours.
      Reaching the LAN through the tunnel is the entire point, so the bypass list stays
      narrow: loopback plus `*.local` (mDNS, which must stay on-link).
    - No `[Host]` section. Mapping names to IPs here would make Shadowrocket resolve them
      locally and forward an IP, which puts us back on IP routing and back in collision
      range. Resolution belongs to the gateway (`dns.hosts` + the `direct-lan` outbound),
      so the client forwards the *name* and the gateway decides what it means.

    The `[Proxy]` Reality key names (`reality-public-key` / `reality-short-id`) are not in
    any official reference — they come from working third-party configs. They are the one
    line here most likely to need correcting against a real device, which is why the whole
    line is built in one place and pinned by a golden-file test.
    """
    endpoint = (_get(store, "rw_endpoint") or "").strip()
    if not endpoint:
        raise ValueError("set the external endpoint (DDNS name or WAN IP) first")
    public_key = (_get(store, "rw_public_key") or "").strip()
    if not public_key:
        raise ValueError("set the Reality public key first")
    names = parse_csv(_get(store, "rw_server_names"))
    sids = parse_csv(_get(store, "rw_short_ids"))
    port = validate_port(_get(store, "rw_port"))
    tag = _tag(client["email"])
    hosts = get_hosts(store)

    proxy = (f"{tag} = vless, {endpoint}, {port}, username={client['id']}, tls=true, "
             f"network=tcp, flow=xtls-rprx-vision, sni={names[0] if names else ''}, "
             f"fingerprint=chrome, reality-public-key={public_key}, "
             f"reality-short-id={sids[0] if sids else ''}, udp=true")

    rules = [f"DOMAIN-SUFFIX,{sfx},{tag}" for sfx in host_suffixes(hosts)]
    # no-resolve is required: without it Shadowrocket resolves the hostname first and the
    # IP-CIDR rule stops meaning "the literal address the app asked for".
    rules += [f"IP-CIDR,{net},{tag},no-resolve" for net in validate_nets(nets)]
    rules.append(f"FINAL,{tag}")

    lines = [
        f"#!name = {tag}",
        "# generated by pi-gw-panel — import into Shadowrocket",
        "",
        "[General]",
        "bypass-system = true",
        "ipv6 = false",
        "skip-proxy = 127.0.0.1, ::1, localhost, *.local",
        "",
        "[Proxy]",
        proxy,
        "",
        "[Rule]",
        *rules,
        "",
    ]
    return "\n".join(lines)
