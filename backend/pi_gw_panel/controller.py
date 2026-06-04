import threading
import time
from dataclasses import dataclass
from pi_gw_panel.config import Settings, SETTINGS_DEFAULTS
from pi_gw_panel.models import Node
from pi_gw_panel.xray_config.builder import build_config
from pi_gw_panel.xray_config.tuning import resolve_profile
from pi_gw_panel.xray_config.validate import ConfigManager
from pi_gw_panel.xray_supervisor.supervisor import XraySupervisor
from pi_gw_panel.net_control.plan import NetPlan, NetResult

# Serializes everything that mutates the live xray + net state (config write, supervisor
# reload, tproxy apply/teardown). Re-entrant so a route that already holds it can call
# apply_node. Without it a manual Connect and the failover tick (separate threads) can
# interleave supervisor stop/start and config writes (NR1).
apply_lock = threading.RLock()


def _tunneled_fetch(store) -> bool:
    """The `tunneled_fetch` setting (default on) — gates the local http sub-fetch inbound."""
    return (store.get_setting("tunneled_fetch") or "1") == "1"


def _dns_intercept(store) -> bool:
    """The `dns_intercept` setting (default OFF) — gateway resolves segment DNS over DoH."""
    return (store.get_setting("dns_intercept") or "0") == "1"


def _resolve_routing(store) -> tuple:
    """Ordered routing rules + the configurable default action (default 'proxy').
    Empty rules + 'proxy' default reproduce the Wave-0 routing exactly."""
    return store.get_routing(), (store.get_setting("routing_default_action") or "proxy")


def _resolve_stats(store) -> dict | None:
    """xray StatsService config when `stats_enabled` (default on), else None
    (→ build_config emits no stats block)."""
    if (store.get_setting("stats_enabled") or SETTINGS_DEFAULTS["stats_enabled"]) != "1":
        return None
    port = store.get_setting("stats_api_port") or SETTINGS_DEFAULTS["stats_api_port"]
    return {"api_port": int(port)}


@dataclass
class ApplyResult:
    ok: bool
    error: str = ""


def apply_net(settings: Settings, net, store=None) -> NetResult:
    """Render+apply the Pi net plan. With a store, editable fields (segment/DHCP/DNS)
    and the kill-switch come from the settings k/v (falling back to config); without
    one it's the pure-config plan. Reused by apply_node and PUT /api/network."""
    plan = NetPlan.from_store(store, settings) if store is not None else NetPlan.from_settings(settings)
    return net.apply_tproxy(plan)


def build_node_config(node: Node, settings: Settings, store=None) -> dict:
    """Render the xray config for `node` exactly as apply would — tuning profile + ordered
    routing + tunneled-fetch + stats + dns from the store (when given). Shared by apply_node
    and the pre-flight validate route (NN10). With no store this is the Wave-0 path."""
    if store is not None:
        profile = resolve_profile(store, node)
        routing = _resolve_routing(store)
        tunneled = _tunneled_fetch(store)
        stats = _resolve_stats(store)
        dns_intercept = _dns_intercept(store)
        domain_strategy = store.get_setting("routing_domain_strategy") or "IPIfNonMatch"
    else:
        profile, routing, tunneled, stats, dns_intercept = None, None, False, None, False
        domain_strategy = "IPIfNonMatch"
    return build_config(node, settings, profile=profile, routing=routing,
                        tunneled_fetch=tunneled, stats=stats, dns_intercept=dns_intercept,
                        domain_strategy=domain_strategy)


def apply_node(node: Node, settings: Settings, supervisor: XraySupervisor,
               net, store=None, xray_bin: str | None = None) -> ApplyResult:
    """Backbone: build -> validate(+snapshot) -> reload xray -> apply net.

    Serialized by `apply_lock` so a manual Connect and the failover tick can't interleave.
    On validation failure nothing is mutated (last-good preserved). If applying a
    *validated* config fails downstream (xray reload or net), roll the live config
    back to last-good, tear down the net ruleset, and report — never leave a
    half-applied state. On success, persist the active node id (if a store given).
    """
    with apply_lock:
        cfg = build_node_config(node, settings, store)
        mgr = ConfigManager(settings, xray_bin=xray_bin)
        ok, out = mgr.apply(cfg)
        if not ok:
            return ApplyResult(ok=False, error=out)
        try:
            supervisor.reload()
            apply_net(settings, net, store)   # honors editable net overrides + kill-switch
        except Exception as exc:
            mgr.rollback()
            try:
                net.teardown()
            except Exception:
                pass
            return ApplyResult(ok=False, error=f"apply failed after validation: {exc}")
        if store is not None and node.id is not None:
            # record the node that was active before this apply, so /rollback can revert to it
            prev = store.get_setting("active_node_id")
            store.set_setting("prev_active_node_id", prev if prev is not None else "")
            store.set_setting("active_node_id", str(node.id))
            store.set_setting("active_since", str(int(time.time())))   # uptime anchor (P3)
        return ApplyResult(ok=True)


def reapply_active_node(state) -> ApplyResult | None:
    """Boot/restart persistence: re-apply the saved active node (rebuild+validate → start
    xray → apply net) on startup, so a reboot or container restart restores the tunnel with
    no manual Connect. Returns the ApplyResult, or None when there is no (valid) saved active
    node. Never raises — a failure is reported in the result, not by crashing boot."""
    aid = state.store.get_setting("active_node_id")
    if not aid:
        return None
    try:
        node = state.store.get_node(int(aid))
    except (TypeError, ValueError):
        return None
    if node is None:
        return None  # saved node vanished (e.g. a sub resync removed it) — skip
    try:
        return apply_node(node, state.settings, state.supervisor, state.net,
                          store=state.store, xray_bin=state.xray_bin)
    except Exception as exc:  # never let boot crash on a bad saved node/config
        return ApplyResult(ok=False, error=f"boot reapply failed: {exc}")
