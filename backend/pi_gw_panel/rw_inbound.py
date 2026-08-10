"""Road-warrior inbound: VLESS + XTLS-Vision + Reality for reaching the gateway from outside.

Settings live in the k/v `settings` table (no schema migration); clients are one JSON blob.
The Reality keypair is NOT generated here — it is produced once by hand (`xray x25519`) and
pasted into the panel. That keeps `cryptography` out of the dependency list and avoids parsing
an `xray` CLI output whose labels have changed between versions.

`resolve()` is the single funnel: it returns the dict `build_config(rw_inbound=...)` wants, or
None when the feature must not emit an inbound at all.
"""
from __future__ import annotations

import base64
import binascii
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
# Any DNS name, case-insensitive, single label allowed (SNI / DDNS endpoint). Deliberately
# excludes every character that carries meaning in the artifacts we generate — comma (splits a
# Shadowrocket `[Proxy]` line into fields), CR/LF (starts a new directive, e.g. an injected
# `[Rule]` / `FINAL,DIRECT` that would send the phone straight past the VPN), space, `#`, `?`, `&`.
_ANY_HOSTNAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$")
# x25519 keys as `xray x25519` prints them: 32 raw bytes ⇒ 43 base64 chars (std or url alphabet),
# padding optional. Shape alone is not enough — the decoded length is checked too.
_B64_KEY = re.compile(r"^[A-Za-z0-9+/_-]{43}=?$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

_MAX_DNS_NAME = 253
# A stored setting may not exceed 2048 chars or the panel's own backup becomes unrestorable
# (backup validates every value against that cap). `rw_hosts` is the widest blob we write:
# 32 entries × ("name": "255.255.255.255" ⇒ len(name) + 15 + 6) + separators. At 40 that is
# 32*61 + 64 = 2016 — under the cap with room to spare. Raising either bound needs this redone.
MAX_HOSTS = 32
MAX_HOST_NAME = 40


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


def validate_hostname(raw: str, what: str) -> str:
    """A DNS name that is safe to interpolate into the generated artifacts. `what` names the
    field so the message points at the box the operator has to fix."""
    name = (raw or "").strip()
    if not name:
        raise ValueError(f"{what} must not be empty")
    if len(name) > _MAX_DNS_NAME:
        raise ValueError(f"{what} is longer than {_MAX_DNS_NAME} characters")
    if not _ANY_HOSTNAME.match(name) or any(len(l) > 63 for l in name.split(".")):
        raise ValueError(f"{what} must be a host name (letters, digits, dashes and dots), "
                         f"got {name!r}")
    return name


def validate_server_names(names: list[str]) -> list[str]:
    """Reality `serverNames`. Each is echoed into `realitySettings` AND into the client's
    `sni=` field, so an unvalidated one lands in both the xray config and the .conf."""
    if not names:
        raise ValueError("at least one server name is required")
    return [validate_hostname(n, "server name") for n in names]


def validate_dest(raw: str) -> str:
    """Reality `dest` — always `host:port`. Never format-checked before, so a value like
    'this is not host:port' reached realitySettings verbatim and made `xray -test` fail on
    every later apply: one bad setting could keep the whole tunnel down."""
    dest = (raw or "").strip()
    host, sep, port = dest.rpartition(":")
    if not sep or not host or not port:
        raise ValueError(f"rw_dest must be host:port (e.g. www.microsoft.com:443), got {dest!r}")
    if host.startswith("[") and host.endswith("]"):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            raise ValueError(f"rw_dest has an invalid IPv6 literal, got {dest!r}")
    else:
        validate_hostname(host, "the rw_dest host")
    try:
        parsed = int(port)
    except ValueError:
        raise ValueError(f"rw_dest port must be an integer, got {port!r}")
    if not 1 <= parsed <= 65535:
        raise ValueError(f"rw_dest port out of range: {parsed}")
    return f"{host}:{parsed}"


def validate_key(raw: str, what: str) -> str:
    """A Reality x25519 key. The VALUE is never echoed in the error — `what` is one of these
    keys the private half, and an error message travels back to the browser and into logs."""
    key = (raw or "").strip()
    if not _B64_KEY.match(key):
        raise ValueError(f"{what} must be a base64 x25519 key — 43 characters, exactly as "
                         "`xray x25519` prints it")
    try:
        decoded = base64.urlsafe_b64decode(
            key.rstrip("=").replace("+", "-").replace("/", "_") + "=")
    except (ValueError, binascii.Error):
        raise ValueError(f"{what} is not valid base64")
    if len(decoded) != 32:
        raise ValueError(f"{what} must decode to 32 bytes, got {len(decoded)}")
    return key


def validate_endpoint(raw: str) -> str:
    """The externally reachable address clients dial. Interpolated into both the .conf and the
    `vless://` link, so a bare IPv6 (which would need brackets there) is refused along with
    everything else that isn't a plain name or address."""
    endpoint = (raw or "").strip()
    if not endpoint:
        raise ValueError("the external endpoint must not be empty")
    if endpoint.startswith("[") and endpoint.endswith("]"):
        try:
            ipaddress.IPv6Address(endpoint[1:-1])
        except ValueError:
            raise ValueError(f"the external endpoint has an invalid IPv6 literal: {endpoint!r}")
        return endpoint
    try:
        ipaddress.IPv4Address(endpoint)
        return endpoint
    except ValueError:
        pass
    return validate_hostname(endpoint, "the external endpoint")


def validate_short_ids(ids: list[str]) -> list[str]:
    """Reality shortIds are 1–8 bytes of hex ⇒ 2–16 hex chars, even length."""
    for sid in ids:
        if len(sid) % 2 or not 2 <= len(sid) <= 16 or not re.fullmatch(r"[0-9a-fA-F]+", sid):
            raise ValueError(f"short id must be 2-16 hex chars of even length, got {sid!r}")
    return ids


def validate_hosts(hosts: dict) -> dict[str, str]:
    """`{name: ip}` for the LAN-by-name path. Names must be dotted (a bare label would collide
    with search-domain resolution) and must NOT end in `.local` — iOS/macOS answer that over
    mDNS/Bonjour and never hand it to the proxy, so such a mapping would silently never work.

    A name under a REAL public suffix (`nas.example.com`, `nas.corp.ru`) is accepted: that is
    split-horizon DNS, which an operator who owns the domain is entitled to want. It used to be
    refused by a hand-kept TLD list, which rejected every ccTLD (i.e. every operator outside the
    gTLD space) while still allowing the many delegated gTLDs the list never heard of — a guard
    both over-strict and porous. What the list was really protecting against was a *suffix*
    routing rule spilling onto unrelated traffic, and that is now structural: the builder emits
    one exact `full:<name>` rule per mapping, so a mapped name can only ever capture itself.
    The residual effect is scoped and deliberate — the gateway answers that one exact name with
    a LAN address for the clients it resolves for, and removing the entry undoes it.

    Both bounds are load-bearing, not tidiness: the serialized map is one k/v setting, and a
    backup refuses any setting over 2048 chars. Bounding only the entry COUNT let 32 long names
    write a blob the panel's own `GET /api/backup` could no longer restore."""
    if len(hosts) > MAX_HOSTS:
        raise ValueError(f"at most {MAX_HOSTS} host mappings, got {len(hosts)}")
    out: dict[str, str] = {}
    for name, ip in hosts.items():
        name = str(name).strip().lower().rstrip(".")
        if len(name) > MAX_HOST_NAME:
            raise ValueError(f"host name {name!r} is longer than {MAX_HOST_NAME} characters")
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
    """Every client we would put in the config or in a generated profile.

    Entries whose id/name don't have the shape `add_client` produces are DROPPED rather than
    repaired: a hand-edited DB can put anything here, and both fields are interpolated into the
    `[Proxy]` line and the `vless://` link. Dropping errs toward less access, never more.
    (A backup can no longer put anything here — the roster is never restored from a document.)
    """
    try:
        raw = json.loads(_get(store, "rw_clients"))
    except (TypeError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    out = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        cid, email = str(c.get("id") or ""), str(c.get("email") or "")
        if _UUID.match(cid) and _EMAIL.match(email):
            out.append({"id": cid, "email": email, "enabled": bool(c.get("enabled", True))})
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

    Every value is re-validated here even though the API validates on write: the settings are
    also reachable through a restore and through a hand-edited DB, and a malformed `dest` or
    `private_key` reaching `realitySettings` verbatim makes `xray -test` fail on EVERY later
    apply. A raise here is caught upstream and degrades to "feature off" — one bad remote-access
    value must never be able to hold the tunnel down.
    """
    if _get(store, "rw_enabled") != "1":
        return None
    private_key = (_get(store, "rw_private_key") or "").strip()
    if not private_key:
        return None
    clients = [c for c in get_clients(store) if c["enabled"]]
    if not clients:
        return None
    # An empty stored value means "never set" (a partial PUT writes ""), so fall back to the
    # documented default instead of emitting an empty dest/serverNames xray would choke on.
    dest = _get(store, "rw_dest").strip() or DEFAULTS["rw_dest"]
    names = parse_csv(_get(store, "rw_server_names")) or parse_csv(DEFAULTS["rw_server_names"])
    return {
        "port": validate_port(_get(store, "rw_port")),
        "dest": validate_dest(dest),
        "server_names": validate_server_names(names),
        "private_key": validate_key(private_key, "the Reality private key"),
        "short_ids": validate_short_ids(parse_csv(_get(store, "rw_short_ids"))),
        "clients": clients,
        "hosts": get_hosts(store),
    }


# --- client-facing artifacts ------------------------------------------------------------

def _artifact_fields(store) -> tuple[str, str, list[str], list[str], int]:
    """Everything both artifacts interpolate, validated once, in one place.

    Neither output is JSON — the .conf is a line/comma-delimited format and the link is a URI —
    so a value carrying a newline or a comma does not get escaped, it becomes STRUCTURE. A
    newline in the endpoint could close `[Proxy]` and open a `[Rule]` block with `FINAL,DIRECT`:
    an imported profile that looks right and routes nothing through the VPN. These settings are
    restorable from a backup, so validating them on write is not enough — check them here too.
    """
    endpoint = (_get(store, "rw_endpoint") or "").strip()
    if not endpoint:
        raise ValueError("set the external endpoint (DDNS name or WAN IP) first")
    public_key = (_get(store, "rw_public_key") or "").strip()
    if not public_key:
        raise ValueError("set the Reality public key first")
    # Same "" ⇒ default fallback resolve() uses, so the SNI the client is told to send is the
    # one the inbound actually presents.
    names = parse_csv(_get(store, "rw_server_names")) or parse_csv(DEFAULTS["rw_server_names"])
    return (validate_endpoint(endpoint),
            validate_key(public_key, "the Reality public key"),
            validate_server_names(names),
            validate_short_ids(parse_csv(_get(store, "rw_short_ids"))),
            validate_port(_get(store, "rw_port")))


def _artifact_client(client: dict) -> tuple[str, str]:
    """The uuid and label are interpolated too. get_clients() already drops malformed entries;
    this is the second lock, for any caller that hands us a client from somewhere else."""
    cid, email = str(client.get("id") or ""), str(client.get("email") or "")
    if not _UUID.match(cid):
        raise ValueError("client id is malformed — remove and re-add the client")
    return cid, validate_email(email)


def link(store, client: dict) -> str:
    """`vless://` share link. Shadowrocket parses these reliably, so this stays the fallback
    when the generated .conf is refused."""
    endpoint, public_key, names, sids, port = _artifact_fields(store)
    cid, email = _artifact_client(client)
    q = ("type=tcp&security=reality&encryption=none&flow=xtls-rprx-vision&fp=chrome"
         f"&sni={quote(names[0])}&pbk={quote(public_key)}"
         f"&sid={quote(sids[0] if sids else '')}")
    return f"vless://{cid}@{endpoint}:{port}?{q}#{quote(email)}"


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
    endpoint, public_key, names, sids, port = _artifact_fields(store)
    cid, email = _artifact_client(client)
    tag = _tag(email)
    hosts = get_hosts(store)

    proxy = (f"{tag} = vless, {endpoint}, {port}, username={cid}, tls=true, "
             f"network=tcp, flow=xtls-rprx-vision, sni={names[0]}, "
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
