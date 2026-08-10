"""Strict, versioned backup/restore for configuration-defining state.

Validation and reference checks finish before the writer opens a destructive transaction.
Authentication, API tokens, live selection, health, accounting, and host-observation keys are
never accepted from a backup.
"""

import ipaddress
import json
import os
import tempfile
import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from pi_gw_panel import rw_inbound as rw
from pi_gw_panel.config import (SETTINGS_DEFAULTS, validate_net_settings,
                                validate_setting_values)
from pi_gw_panel.models import Node, RoutingRule, Subscription, TuningProfile
from pi_gw_panel.nodes.store import _NODE_COLS, _node_values, _PROFILE_COLS, _profile_values

BACKUP_SCHEMA = 2
MAX_NODES = 5000
MAX_SUBSCRIPTIONS = 256
MAX_PROFILES = 256
MAX_RULES = 256
# Pre-restore snapshots are a safety net taken on every restore, not a bounded daily job, so
# without a retention cap a burst of restores can grow this directory without limit.
_PRE_RESTORE_RETAIN = 10

# Complete config intent stored in SQLite. Explicit tuple prevents a hostile restore from
# smuggling auth/session/transient keys simply because runtime code adds a new setting later.
#
# The road-warrior *shape* keys are carried so a restore onto a fresh host brings the inbound
# back instead of silently dropping it. The two keys that ARE the remote-access credentials are
# deliberately ABSENT — see _SETTINGS_NEVER_BACKED_UP below.
_SETTINGS_KEYS = tuple(dict.fromkeys((*SETTINGS_DEFAULTS,
    "segment_iface", "segment_ip", "dhcp_start", "dhcp_end", "dhcp_lease",
    "client_dns", "kill_switch_enabled", "lan_access_enabled", "default_profile_id",
    "ula_prefix6",
    "rw_enabled", "rw_port", "rw_dest", "rw_server_names", "rw_short_ids",
    "rw_public_key", "rw_endpoint", "rw_hosts", "rw_routed_nets")))
_SETTINGS_SET = frozenset(_SETTINGS_KEYS)
# Neither exported nor written by a restore — asserted by a test, not just by omission above.
# Because these keys are not in the DELETE list either, a restore leaves whatever the gateway
# currently holds exactly as it is; the document's copy, if any, is ignored.
#
# `rw_private_key`: a backup document is downloaded into the browser and handed around, and the
# Reality private key is the one secret that lets anything impersonate this gateway's inbound.
# It is re-entered by hand after a restore.
#
# `rw_clients`: the client uuids ARE the credentials the inbound authenticates on, so the same
# argument applies to the document leaving the box — and a second, worse one applies to it coming
# back. Restoring the roster reinstates every client the operator has revoked since the backup
# was taken: a lost phone cut off on Tuesday is live again the moment a Monday backup is
# restored, under the same Reality keypair and short ids, with no trace in the roster that it
# ever went away. Revocation has to be one-way, so the roster is never taken from a document —
# the live one (which already has the revocation applied) simply survives the restore.
_SETTINGS_NEVER_BACKED_UP = frozenset({"rw_private_key", "rw_clients"})

_NODE_DUMP = ("id",) + _NODE_COLS
_PROFILE_DUMP = ("id",) + _PROFILE_COLS + ("noises",)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BackupNoise(_Strict):
    type: Literal["rand", "str", "base64", "hex"] = "rand"
    packet: str = Field(default="50-150", max_length=256)
    delay: str = Field(default="10-16", max_length=64)


class BackupProfile(_Strict):
    id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=512)
    fingerprint: str = Field(default="chrome", max_length=32)
    frag_enabled: bool = False
    frag_packets: str = Field(default="tlshello", max_length=64)
    frag_length: str = Field(default="100-200", max_length=64)
    frag_interval: str = Field(default="10-20", max_length=64)
    mux_enabled: bool = False
    doh_enabled: bool = True
    doh_url: str = Field(default="", max_length=2048)
    quic: Literal["allow", "drop", "proxy"] = "allow"
    noise_enabled: bool = False
    noises: list[BackupNoise] = Field(default_factory=list, max_length=32)
    xhttp_padding: str = Field(default="", max_length=64)
    xmux_max_concurrency: str = Field(default="", max_length=64)
    xmux_max_connections: str = Field(default="", max_length=64)
    mux_concurrency: str = Field(default="", max_length=64)
    xudp_proxy_udp443: str = Field(default="", max_length=32)
    alpn: str = Field(default="", max_length=128)
    tls_min: str = Field(default="", max_length=32)
    tls_max: str = Field(default="", max_length=32)


class BackupSubscription(_Strict):
    id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=512)
    url: str = Field(min_length=1, max_length=2048)
    injection: dict[str, Any] = Field(default_factory=dict)
    interval_sec: int = Field(default=0, ge=0, le=31_536_000)
    enabled: bool = True
    default_profile_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def bounded_injection(self):
        stack = [(self.injection, 0)]
        items = 0
        while stack:
            value, depth = stack.pop()
            if depth > 8:
                raise ValueError("subscription injection nesting is too deep")
            items += 1
            if items > 2048:
                raise ValueError("subscription injection has too many items")
            if isinstance(value, dict):
                for key, child in value.items():
                    if len(str(key)) > 512:
                        raise ValueError("subscription injection key is too long")
                    stack.append((child, depth + 1))
            elif isinstance(value, list):
                stack.extend((child, depth + 1) for child in value)
            elif isinstance(value, str) and len(value) > 8192:
                raise ValueError("subscription injection value is too long")
        if len(json.dumps(self.injection, separators=(",", ":"))) > 65_536:
            raise ValueError("subscription injection is too large")
        return self


class BackupNode(_Strict):
    id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=512)
    address: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    uuid: str = Field(min_length=1, max_length=512)
    transport: Literal["vision", "xhttp"] = "vision"
    sni: str = Field(default="", max_length=253)
    public_key: str = Field(default="", max_length=512)
    short_id: str = Field(default="", max_length=512)
    fingerprint: str = Field(default="chrome", max_length=32)
    flow: str = Field(default="xtls-rprx-vision", max_length=64)
    network: str = Field(default="tcp", max_length=32)
    security: str = Field(default="reality", max_length=32)
    path: str = Field(default="", max_length=512)
    host: str = Field(default="", max_length=253)
    mode: str = Field(default="", max_length=64)
    alpn: str = Field(default="", max_length=128)
    note: str = Field(default="", max_length=512)
    subscription_id: int | None = Field(default=None, gt=0)
    stale: bool = False
    tuning_profile_id: int | None = Field(default=None, gt=0)
    position: int = Field(default=0, ge=0, le=MAX_NODES)


class BackupRule(_Strict):
    type: Literal["geoip", "geosite", "domain", "ip", "port"]
    value: str = Field(min_length=1, max_length=512)
    action: Literal["direct", "proxy", "block"]
    enabled: bool = True
    label: str = Field(default="", max_length=512)


class BackupRouting(_Strict):
    rules: list[BackupRule] = Field(default_factory=list, max_length=MAX_RULES)
    default_action: Literal["direct", "proxy", "block"] = "proxy"


class BackupDocument(_Strict):
    schema_version: Literal[1, 2]
    created_at: int | None = Field(default=None, ge=0)
    nodes: list[BackupNode] = Field(default_factory=list, max_length=MAX_NODES)
    subscriptions: list[BackupSubscription] = Field(
        default_factory=list, max_length=MAX_SUBSCRIPTIONS)
    profiles: list[BackupProfile] = Field(min_length=1, max_length=MAX_PROFILES)
    routing: BackupRouting
    settings: dict[str, str | int | bool] = Field(default_factory=dict, max_length=64)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_empty_profiles(cls, value):
        """Schema 1 allowed an empty profile list; restore it with a safe default.

        Schema 2 deliberately keeps the stricter non-empty contract.
        """
        if (isinstance(value, dict) and value.get("schema_version") == 1
                and value.get("profiles") == []):
            value = dict(value)
            value["profiles"] = [{"id": 1, "name": "default"}]
            settings = dict(value.get("settings") or {})
            settings.setdefault("default_profile_id", "1")
            value["settings"] = settings
        return value

    @model_validator(mode="after")
    def validate_references(self):
        def unique(values, label):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {label} id")

        profile_ids = [p.id for p in self.profiles]
        subscription_ids = [s.id for s in self.subscriptions]
        node_ids = [n.id for n in self.nodes]
        unique(profile_ids, "profile")
        unique(subscription_ids, "subscription")
        unique(node_ids, "node")
        profiles, subscriptions = set(profile_ids), set(subscription_ids)
        for sub in self.subscriptions:
            if sub.default_profile_id is not None and sub.default_profile_id not in profiles:
                raise ValueError(f"subscription {sub.id} references missing profile")
        identities = set()
        for node in self.nodes:
            if node.subscription_id is not None and node.subscription_id not in subscriptions:
                raise ValueError(f"node {node.id} references missing subscription")
            if node.tuning_profile_id is not None and node.tuning_profile_id not in profiles:
                raise ValueError(f"node {node.id} references missing profile")
            identity = (node.subscription_id, node.address, node.port, node.uuid,
                        node.path, node.sni, node.short_id)
            if identity in identities:
                raise ValueError("duplicate node identity")
            identities.add(identity)
        # Documents written before `rw_clients` became non-restorable carry it, and a recovery is
        # the worst possible moment to refuse a file outright over a key we are going to ignore
        # anyway. Tolerated here, dropped in import_state — never written.
        unknown = set(self.settings) - _SETTINGS_SET - _SETTINGS_NEVER_BACKED_UP
        if unknown:
            raise ValueError(f"unsupported setting keys: {', '.join(sorted(unknown))}")
        if any(len(str(value)) > 2048 for value in self.settings.values()):
            raise ValueError("setting value is too long")
        # Type/range-check the numeric and closed-choice settings with the SAME table the
        # interactive PUT /api/settings uses. Restore used to skip it entirely, so a document
        # carrying health_interval="not-a-number" was accepted and then made GET /api/settings
        # raise for good — an outage only fixable over SSH.
        validate_setting_values(self.settings)
        self._reconcile_default_action()
        default_id = self.settings.get("default_profile_id")
        if default_id not in (None, ""):
            try:
                parsed_default = int(default_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("default_profile_id must be an integer") from exc
            if parsed_default not in profiles:
                raise ValueError("default_profile_id references missing profile")
        _validate_network_settings(self.settings)
        return self

    def _reconcile_default_action(self) -> None:
        """Make `routing.default_action` the single source of truth for the default action.

        A document carries it twice — as the Literal-typed routing field and as a raw string in
        the settings map — because export writes both. The importer's settings loop runs after
        the routing insert, so the *unchecked* copy silently won: `settings` could put any string
        into the xray config the routing field exists to keep to direct/proxy/block. An omitted
        routing field adopts the settings value (old documents); a genuine disagreement is a
        hand-edited document and is refused rather than resolved by write order.
        """
        raw = self.settings.get("routing_default_action")
        if raw is None:
            return
        raw = str(raw)
        if "default_action" not in self.routing.model_fields_set:
            self.routing.default_action = raw     # already checked by validate_setting_values
        elif raw != self.routing.default_action:
            raise ValueError("settings.routing_default_action conflicts with "
                             "routing.default_action")


def _validate_network_settings(settings: dict) -> None:
    """Segment/DHCP/DNS + road-warrior values, checked with the very validators the interactive
    routes use.

    These land in the settings table and from there in the dnsmasq config the panel supervises
    **as root** (`dhcp_lease` and `client_dns6` are interpolated verbatim), and in the client
    artifacts the road-warrior screen generates. A second, laxer copy of the rules is exactly how
    restore came to accept `dhcp_lease="12h\\ndhcp-script=/data/x.sh"` — a config `--test` calls
    valid, which runs an arbitrary script as root on the next lease event — while
    PUT /api/network rejected the very same string.
    """
    validate_net_settings(settings)
    values = {}
    for key in ("segment_ip", "dhcp_start", "dhcp_end"):
        raw = str(settings.get(key) or "").strip()
        if raw:
            values[key] = ipaddress.IPv4Address(raw)   # shape already proven above
    if len(values) == 3:
        network = ipaddress.ip_network(f"{values['segment_ip']}/24", strict=False)
        if values["dhcp_start"] not in network or values["dhcp_end"] not in network:
            raise ValueError("DHCP range must be inside the segment subnet")
        if int(values["dhcp_start"]) > int(values["dhcp_end"]):
            raise ValueError("DHCP range start must not exceed end")
    _validate_rw_settings(settings)


def _validate_rw_settings(settings: dict) -> None:
    """Road-warrior settings, through rw_inbound's own validators.

    A backup used to be able to carry ANY string under 2048 chars into `realitySettings`, the
    generated `.conf` and the `vless://` link. `rw_clients` has no check here because it is not
    restorable at all (see _SETTINGS_NEVER_BACKED_UP) — a document's copy never reaches the
    store, so there is nothing here to validate.
    """
    def value(key: str) -> str:
        return str(settings.get(key) or "").strip()

    if value("rw_port"):
        rw.validate_port(value("rw_port"))
    if value("rw_dest"):
        rw.validate_dest(value("rw_dest"))
    if value("rw_server_names"):
        rw.validate_server_names(rw.parse_csv(value("rw_server_names")))
    if value("rw_short_ids"):
        rw.validate_short_ids(rw.parse_csv(value("rw_short_ids")))
    if value("rw_public_key"):
        rw.validate_key(value("rw_public_key"), "the Reality public key")
    if value("rw_endpoint"):
        rw.validate_endpoint(value("rw_endpoint"))
    if value("rw_routed_nets"):
        rw.validate_nets(rw.parse_nets(value("rw_routed_nets")))
    if value("rw_hosts"):
        try:
            hosts = json.loads(value("rw_hosts"))
        except ValueError as exc:
            raise ValueError("rw_hosts must be a JSON object of name → IPv4") from exc
        if not isinstance(hosts, dict):
            raise ValueError("rw_hosts must be a JSON object of name → IPv4")
        rw.validate_hosts(hosts)


def _node_dict(node: Node) -> dict:
    return {column: getattr(node, column) for column in _NODE_DUMP}


def _profile_dict(profile: TuningProfile) -> dict:
    return {column: getattr(profile, column) for column in _PROFILE_DUMP}


def export_state(store) -> dict:
    """Materialize one coherent SQLite snapshot, then return detached plain data."""
    with store.transaction():
        setting_rows = store._conn.execute(
            f"SELECT key,value FROM settings WHERE key IN ({','.join('?' for _ in _SETTINGS_KEYS)})",
            _SETTINGS_KEYS).fetchall()
        settings = {row["key"]: row["value"] for row in setting_rows}
        nodes = [_node_dict(node) for node in store.list_nodes()]
        subscriptions = [
            {"id": sub.id, "name": sub.name, "url": sub.url, "injection": sub.injection,
             "interval_sec": sub.interval_sec, "enabled": sub.enabled,
             "default_profile_id": sub.default_profile_id}
            for sub in store.list_subscriptions()]
        profiles = [_profile_dict(profile) for profile in store.list_profiles()]
        rules = [
            {"type": rule.type, "value": rule.value, "action": rule.action,
             "enabled": rule.enabled, "label": rule.label}
            for rule in store.get_routing()]
        default_action = store.get_setting("routing_default_action") or "proxy"
    return {
        "schema_version": BACKUP_SCHEMA,
        "nodes": nodes,
        "subscriptions": subscriptions,
        "profiles": profiles,
        "routing": {"rules": rules, "default_action": default_action},
        "settings": settings,
    }


def backups_dir(settings) -> str:
    """`data_dir/backups`, created 0700 — the files inside carry subscription URLs."""
    path = os.path.join(settings.data_dir, "backups")
    # mode= on the create, not only the chmod after it: between the two the directory stood
    # open at whatever the process umask allowed, and the first backup can land in that window.
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def write_document(doc: dict, path: str) -> str:
    """Write one backup document atomically at 0600, fsyncing the file and its directory.

    A crash or a full disk mid-write must not leave a truncated file behind: the pruner would
    keep it (it is the newest) while deleting the older good generations.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".backup-", suffix=".tmp", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(doc, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        tmp = ""
        dir_fd = os.open(directory, os.O_RDONLY)   # persist the rename itself
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
    return path


def _reserve_snapshot_path(directory: str, now: int) -> str:
    """Claim a filename for a pre-restore snapshot that cannot collide with a concurrent one
    (audit FIX-E-4): a second-precision timestamp alone let two restores inside the same second
    resolve to the identical path, and `write_document`'s `os.replace` would then silently
    overwrite the first recovery copy with the second. O_CREAT|O_EXCL makes the claim itself
    atomic; the UUID suffix makes a genuine collision practically impossible in the first place.
    """
    for _ in range(8):
        candidate = os.path.join(directory, f"pre-restore-{now}-{uuid.uuid4().hex}.json")
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate
    raise OSError("could not allocate a unique pre-restore snapshot filename")


def _prune_pre_restore_snapshots(directory: str, keep: int = _PRE_RESTORE_RETAIN) -> None:
    """Keep only the `keep` most recent pre-restore snapshots; best-effort — a failure here must
    not undo the snapshot write that just succeeded."""
    try:
        names = [f for f in os.listdir(directory) if f.startswith("pre-restore-") and f.endswith(".json")]
    except OSError:
        return
    if len(names) <= keep:
        return
    try:
        paths = sorted((os.path.join(directory, f) for f in names), key=os.path.getmtime)
    except OSError:
        return
    for stale in paths[:-keep]:
        try:
            os.unlink(stale)
        except OSError:
            pass


def write_pre_restore_snapshot(state, now: int | None = None) -> str:
    """Dump the state a restore is about to destroy, and return the path.

    Restore is a whole-state replace with no undo, and daily auto-backup is off by default — so
    without this the only copy of the configuration being overwritten is whatever the operator
    happened to export by hand. Validated like any other backup, so the snapshot is known to be
    restorable at the moment it is taken rather than at the moment it is needed.
    """
    now = int(time.time()) if now is None else now
    doc = export_state(state.store)
    doc["created_at"] = now
    validate_document(doc)
    directory = backups_dir(state.settings)
    path = _reserve_snapshot_path(directory, now)
    try:
        write_document(doc, path)
    except Exception:
        # `_reserve_snapshot_path` claims `path` itself as an empty 0-byte placeholder so two
        # concurrent restores can never collide on one filename; `write_document`'s `os.replace`
        # normally fills it in. If serialization/fsync on the temp file fails *before* that
        # replace, the reservation is still the empty file it started as — left behind, it counts
        # toward the pruner's retention cap by mtime like any other snapshot and can displace an
        # older *valid* one (audit FIX-J-4). Only remove it while still empty: once `os.replace`
        # has landed the real content, a later failure (e.g. the directory fsync) must not delete
        # a snapshot that already holds good data.
        try:
            if os.path.getsize(path) == 0:
                os.unlink(path)
        except OSError:
            pass
        raise
    _prune_pre_restore_snapshots(directory)
    return path


def validate_document(doc: dict | BackupDocument) -> BackupDocument:
    if isinstance(doc, BackupDocument):
        return doc
    try:
        return BackupDocument.model_validate(doc)
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        location = ".".join(str(part) for part in first["loc"])
        raise ValueError(f"{location}: {first['msg']}") from exc


def _profile_from(profile: BackupProfile) -> TuningProfile:
    values = profile.model_dump()
    values["noises"] = [noise.model_dump() for noise in profile.noises]
    return TuningProfile(**values)


def _node_from(node: BackupNode) -> Node:
    return Node(**node.model_dump())


def import_state(store, doc: dict | BackupDocument) -> dict:
    """Preflight fully, then replace the validated snapshot in one short transaction."""
    validated = validate_document(doc)
    profiles = [_profile_from(profile) for profile in validated.profiles]
    subscriptions = [Subscription(**sub.model_dump()) for sub in validated.subscriptions]
    nodes = [_node_from(node) for node in validated.nodes]
    rules = [RoutingRule(id=None, position=index, **rule.model_dump())
             for index, rule in enumerate(validated.routing.rules)]
    # The never-backed-up keys are dropped here as well as omitted from _SETTINGS_KEYS: the
    # allowlist stops export and the DELETE below from touching them, this stops a hand-made or
    # pre-existing document from writing one back. Both halves are load-bearing.
    settings = {
        key: ("1" if value else "0") if isinstance(value, bool) else str(value)
        for key, value in validated.settings.items()
        if key not in _SETTINGS_NEVER_BACKED_UP
    }
    # The routing block owns the default action (see _reconcile_default_action); dropping the
    # settings copy here means the write order below can never decide it again.
    settings.pop("routing_default_action", None)

    conn = store._conn
    with store.transaction():
        # Read before the settings DELETE below wipes the allowlisted keys. The private half of
        # the Reality pair is never in a backup, so these two decide both whether a restored
        # public key may be stored at all and whether `rw_enabled` can honestly be honoured.
        local_private = (store.get_setting("rw_private_key") or "").strip()
        local_public = (store.get_setting("rw_public_key") or "").strip()
        conn.execute("DELETE FROM node_health")
        conn.execute("DELETE FROM nodes")
        conn.execute("DELETE FROM subscriptions")
        conn.execute("DELETE FROM routing_rules")
        conn.execute("DELETE FROM tuning_profiles")
        # Replace the entire allowlisted settings snapshot. Omitted keys intentionally fall back
        # to current code defaults; unrelated auth/transient keys remain untouched.
        conn.execute(
            f"DELETE FROM settings WHERE key IN ({','.join('?' for _ in _SETTINGS_KEYS)})",
            _SETTINGS_KEYS)
        for profile in profiles:
            columns = ("id",) + _PROFILE_COLS + ("noises_json",)
            values = (profile.id, *_profile_values(profile), json.dumps(profile.noises))
            conn.execute(
                f"INSERT INTO tuning_profiles ({', '.join(columns)}) "
                f"VALUES ({', '.join(['?'] * len(columns))})", values)
        for sub in subscriptions:
            conn.execute(
                "INSERT INTO subscriptions (id,name,url,injection_json,interval_sec,enabled,"
                "default_profile_id) VALUES (?,?,?,?,?,?,?)",
                (sub.id, sub.name, sub.url, json.dumps(sub.injection), sub.interval_sec,
                 int(sub.enabled), sub.default_profile_id))
        for node in nodes:
            columns = ("id",) + _NODE_COLS
            values = (node.id, *_node_values(node))
            conn.execute(
                f"INSERT INTO nodes ({', '.join(columns)}) "
                f"VALUES ({', '.join(['?'] * len(columns))})", values)
        for rule in rules:
            conn.execute(
                "INSERT INTO routing_rules (position,type,value,action,enabled,label) "
                "VALUES (?,?,?,?,?,?)",
                (rule.position, rule.type, rule.value, rule.action,
                 int(rule.enabled), rule.label))
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('routing_default_action',?)",
            (validated.routing.default_action,))
        # The private half never travels in a document, so the one this box holds SURVIVES the
        # restore. Any public half arriving in a document is therefore only storable if it is
        # provably that private key's partner; anything else leaves the two halves of the stored
        # pair belonging to two different gateways.
        #
        # The only proof available here is the public key this box already holds: the pair is made
        # by hand with `xray x25519` and pasted in, so the stored public half IS this box's record
        # of what the private half pairs with (deriving it would need a crypto dependency the
        # panel deliberately does not carry — see rw_inbound's module docstring). Hence:
        #   - equal to the local public key ⇒ verified, and storing it changes nothing;
        #   - different from the local public key ⇒ refused, the local one is kept;
        #   - no local public key to check it against, but a private key survives ⇒ unverifiable,
        #     so it is not stored AT ALL and the operator pastes the matching half by hand;
        #   - no local key material whatsoever (a fresh host) ⇒ nothing survives that it could
        #     contradict, and carrying the public half is the reason a backup holds one: adopt it.
        #
        # None of that consults `rw_enabled`. The document's flag is precisely the value a stale
        # or hostile file controls, and it says nothing about whether the pair checks out: gating
        # the check on it let a document with `rw_enabled=0` overwrite the local public key with a
        # foreign one and leave the split pair sitting there, armed for whenever the operator next
        # turns the inbound on — from a screen that would show them the wrong key to hand out.
        restored_public = settings.get("rw_public_key", "")
        pair_verified = restored_public == local_public
        no_local_keypair = not local_private and not local_public
        if not pair_verified and not no_local_keypair:
            if local_public:
                settings["rw_public_key"] = local_public   # survives the DELETE above untouched
            else:
                settings.pop("rw_public_key", None)        # unverifiable ⇒ never written

        # Remote access cannot come back on a key this box does not hold. Restoring `rw_enabled=1`
        # without the matching private half leaves /api/rw reporting the inbound as enabled while
        # rw_inbound.resolve() returns None and nothing is actually served — a split state the API
        # itself refuses to create. A public key that belongs to a DIFFERENT pair than the private
        # key that survived the restore is the same lie with a working-looking inbound behind it;
        # the document's key is not stored either way, but the claim that remote access is on must
        # still be refused rather than carried over onto the surviving pair.
        #
        # No local public key is the same case, not an exemption from it: an inbound enabled with
        # nothing to check the document's half against is enabled on an unverified pair. PUT
        # /api/rw will not enable the inbound without a stored public key, so this state is never
        # one the panel itself produced; it fails closed and the operator re-enables after pasting
        # the matching half.
        rw_disabled = ""
        if settings.get("rw_enabled") == "1":
            if not local_private:
                rw_disabled = "no local Reality private key"
            elif not local_public:
                rw_disabled = "no local Reality public key to check the restored one against"
            elif not pair_verified:
                rw_disabled = "the restored public key does not match the local private key"
            if rw_disabled:
                settings["rw_enabled"] = "0"
        for key, value in settings.items():
            conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    return {
        "nodes": len(nodes), "subscriptions": len(subscriptions),
        "profiles": len(profiles), "routing_rules": len(rules),
        "rw_disabled": rw_disabled,
    }
