"""Strict, versioned backup/restore for configuration-defining state.

Validation and reference checks finish before the writer opens a destructive transaction.
Authentication, API tokens, live selection, health, accounting, and host-observation keys are
never accepted from a backup.
"""

import ipaddress
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from pi_gw_panel.config import SETTINGS_DEFAULTS
from pi_gw_panel.models import Node, RoutingRule, Subscription, TuningProfile
from pi_gw_panel.nodes.store import _NODE_COLS, _node_values, _PROFILE_COLS, _profile_values

BACKUP_SCHEMA = 2
MAX_NODES = 5000
MAX_SUBSCRIPTIONS = 256
MAX_PROFILES = 256
MAX_RULES = 256

# Complete config intent stored in SQLite. Explicit tuple prevents a hostile restore from
# smuggling auth/session/transient keys simply because runtime code adds a new setting later.
_SETTINGS_KEYS = tuple(dict.fromkeys((*SETTINGS_DEFAULTS,
    "segment_iface", "segment_ip", "dhcp_start", "dhcp_end", "dhcp_lease",
    "client_dns", "kill_switch_enabled", "lan_access_enabled", "default_profile_id",
    "ula_prefix6")))
_SETTINGS_SET = frozenset(_SETTINGS_KEYS)

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
        unknown = set(self.settings) - _SETTINGS_SET
        if unknown:
            raise ValueError(f"unsupported setting keys: {', '.join(sorted(unknown))}")
        if any(len(str(value)) > 2048 for value in self.settings.values()):
            raise ValueError("setting value is too long")
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


def _validate_network_settings(settings: dict) -> None:
    iface = str(settings.get("segment_iface", "eth0.2"))
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", iface):
        raise ValueError("invalid segment_iface")
    values = {}
    for key in ("segment_ip", "dhcp_start", "dhcp_end", "client_dns"):
        if key not in settings:
            continue
        try:
            values[key] = ipaddress.IPv4Address(str(settings[key]))
        except ValueError as exc:
            raise ValueError(f"{key} must be an IPv4 address") from exc
    if {"segment_ip", "dhcp_start", "dhcp_end"} <= values.keys():
        network = ipaddress.ip_network(f"{values['segment_ip']}/24", strict=False)
        if values["dhcp_start"] not in network or values["dhcp_end"] not in network:
            raise ValueError("DHCP range must be inside the segment subnet")
        if int(values["dhcp_start"]) > int(values["dhcp_end"]):
            raise ValueError("DHCP range start must not exceed end")


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
    settings = {
        key: ("1" if value else "0") if isinstance(value, bool) else str(value)
        for key, value in validated.settings.items()
    }

    conn = store._conn
    with store.transaction():
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
        for key, value in settings.items():
            conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    return {
        "nodes": len(nodes), "subscriptions": len(subscriptions),
        "profiles": len(profiles), "routing_rules": len(rules),
    }
