import logging
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
logger = logging.getLogger(__name__)


def _tunneled_fetch(store) -> bool:
    """The `tunneled_fetch` setting (default on) — gates the local http sub-fetch inbound."""
    return (store.get_setting("tunneled_fetch") or "1") == "1"


def _dns_intercept(store) -> bool:
    """The `dns_intercept` setting (default OFF) — gateway resolves segment DNS over DoH."""
    return (store.get_setting("dns_intercept") or "0") == "1"


def _ipv6_enabled(store) -> bool:
    """The `ipv6_enabled` setting (default OFF) — tunnel segment client IPv6 through xray."""
    return (store.get_setting("ipv6_enabled") or "0") == "1"


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


@dataclass
class RestoreResult:
    ok: bool
    summary: dict | None = None
    error: str = ""


def _record_enforcement(net, result: NetResult, *, wan_blocked: bool | None) -> NetResult:
    """Keep the last *confirmed* host-enforcement outcome on the backend instance.

    The API must not infer that a guard exists merely because the corresponding setting is on.
    Backends are long-lived AppState resources, so this tiny status snapshot naturally survives
    across requests without persisting host-specific/transient truth in SQLite.
    """
    net.enforcement_status = "ok" if result.ok else "error"
    net.wan_blocked = wan_blocked if result.ok else None
    net.enforcement_error = "" if result.ok else (result.error or "network operation failed")
    return result


def _call_net(net, method: str, *args, wan_blocked: bool | None) -> NetResult:
    try:
        result = getattr(net, method)(*args)
    except Exception as exc:
        result = NetResult(ok=False, error=f"{method} raised: {exc}")
    if not isinstance(result, NetResult):
        result = NetResult(ok=False, error=f"{method} returned no NetResult")
    return _record_enforcement(net, result, wan_blocked=wan_blocked)


def _require_net(result: NetResult, action: str) -> None:
    if not result.ok:
        raise RuntimeError(f"{action}: {result.error or 'network backend reported failure'}")


def apply_net(settings: Settings, net, store=None) -> NetResult:
    """Render+apply the Pi net plan. With a store, editable fields (segment/DHCP/DNS)
    and the kill-switch come from the settings k/v (falling back to config); without
    one it's the pure-config plan. Reused by apply_node and PUT /api/network."""
    plan = NetPlan.from_store(store, settings) if store is not None else NetPlan.from_settings(settings)
    return _call_net(net, "apply_tproxy", plan, wan_blocked=False)


def _kill_switch_on(store) -> bool:
    return store is not None and (store.get_setting("kill_switch_enabled") or "1") == "1"


def stop_net(settings: Settings, net, store=None) -> NetResult:
    """Tear down the tunnel's net rules when the tunnel is intentionally stopped
    (disconnect / xray-stop / boot-before-tunnel).

    Kill-switch ON  → install the fail-closed leak-guard (keep dropping client→WAN, v4+v6)
    instead of a full teardown — otherwise a "fail closed" kill-switch would *leak* the
    moment you stop (audit A1). Kill-switch OFF → full teardown (clients fall back direct).
    """
    if _kill_switch_on(store):
        if not hasattr(net, "apply_guard"):
            return _record_enforcement(
                net, NetResult(ok=False, error="network backend cannot install fail-closed guard"),
                wan_blocked=None)
        return _call_net(net, "apply_guard", NetPlan.from_store(store, settings),
                         wan_blocked=True)
    return _call_net(net, "teardown", wan_blocked=False)


def sync_net(state) -> NetResult:
    """Apply the net rules that match the CURRENT tunnel state — used by PUT /network so a
    segment/kill-switch edit takes effect immediately without black-holing. Tunnel up (xray
    running + an active node) → full tproxy; otherwise → stop_net (leak-guard if the
    kill-switch is on, else teardown). Avoids installing a tproxy-to-dead-port when there's
    no live tunnel (A1)."""
    running = state.supervisor.status().get("running")
    if running and state.store.get_setting("active_node_id"):
        return apply_net(state.settings, state.net, state.store)
    return stop_net(state.settings, state.net, state.store)


def boot_guard(state) -> NetResult:
    """Close the boot leak window (A1): if the kill-switch is on, install the leak-guard
    BEFORE the tunnel is (re)applied, so a reboot never leaks client→WAN while xray starts.
    No-op when the kill-switch is off. Best-effort — never blocks boot."""
    if not _kill_switch_on(state.store):
        return NetResult(ok=True)
    return stop_net(state.settings, state.net, state.store)


def build_node_config(node: Node, settings: Settings, store=None) -> dict:
    """Render the xray config for `node` exactly as apply would — tuning profile + ordered
    routing + tunneled-fetch + stats + dns from the store (when given). Shared by apply_node
    and the pre-flight validate route (NN10). With no store this is the Wave-0 path."""
    if store is not None:
        profile = resolve_profile(store, node)
        # Only an EXPLICITLY-assigned profile overrides the node's own fingerprint; the default
        # fallback profile must not (resolve_profile returns the default when unassigned/dangling).
        explicit = profile is not None and profile.id == node.tuning_profile_id
        routing = _resolve_routing(store)
        tunneled = _tunneled_fetch(store)
        stats = _resolve_stats(store)
        dns_intercept = _dns_intercept(store)
        ipv6 = _ipv6_enabled(store)
        domain_strategy = store.get_setting("routing_domain_strategy") or "IPIfNonMatch"
    else:
        profile, routing, tunneled, stats, dns_intercept, ipv6 = None, None, False, None, False, False
        explicit = False
        domain_strategy = "IPIfNonMatch"
    return build_config(node, settings, profile=profile, routing=routing,
                        tunneled_fetch=tunneled, stats=stats, dns_intercept=dns_intercept,
                        domain_strategy=domain_strategy, ipv6_tproxy=ipv6, profile_explicit=explicit)


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
        previous_id_raw = store.get_setting("active_node_id") if store is not None else None
        try:
            previous_id = int(previous_id_raw) if previous_id_raw else None
        except (TypeError, ValueError):
            previous_id = None
        previous_node = store.get_node(previous_id) if store is not None and previous_id else None

        cfg = build_node_config(node, settings, store)
        mgr = ConfigManager(settings, xray_bin=xray_bin)
        try:
            ok, out = mgr.apply(cfg)
        except Exception as exc:
            return ApplyResult(ok=False, error=f"config apply failed: {exc}")
        if not ok:
            return ApplyResult(ok=False, error=out)
        try:
            # reload() now reports whether xray actually came up — a config can pass `-test` yet
            # the live process still die at boot (port bound, cap drop, tproxy/nft state). Treat a
            # non-ready reload as a failure so we roll back instead of blackholing all client traffic.
            if not supervisor.reload():
                raise RuntimeError("xray did not come up on the new config")
            _require_net(apply_net(settings, net, store), "network apply failed")
        except Exception as exc:
            recovery: list[str] = []
            restored = False
            try:
                rolled_back = mgr.rollback()
            except Exception as rollback_exc:
                rolled_back = False
                recovery.append(f"config rollback raised: {rollback_exc}")
            if not rolled_back:
                recovery.append("config rollback unavailable")

            # A valid prior active node means the rolled-back config describes a tunnel we can
            # restore. Both the process readiness and host rules are authoritative contracts.
            if previous_node is not None and rolled_back:
                try:
                    if not supervisor.reload():
                        recovery.append("previous xray did not become ready")
                    else:
                        previous_net = apply_net(settings, net, store)
                        if previous_net.ok:
                            restored = True
                        else:
                            recovery.append(
                                f"previous network restore failed: {previous_net.error or 'unknown error'}")
                except Exception as restore_exc:
                    recovery.append(f"previous runtime restore raised: {restore_exc}")

            if not restored:
                # No verified prior tunnel remains. Stop any uncertain candidate process, then
                # install the kill-switch-aware guard. Never call raw teardown on this path.
                try:
                    supervisor.stop()
                except Exception as stop_exc:
                    recovery.append(f"xray stop raised: {stop_exc}")
                guard = stop_net(settings, net, store)
                if not guard.ok:
                    recovery.append(f"fail-closed recovery failed: {guard.error or 'unknown error'}")

            suffix = f"; recovery: {'; '.join(recovery)}" if recovery else ""
            return ApplyResult(ok=False, error=f"apply failed after validation: {exc}{suffix}")
        if store is not None and node.id is not None:
            with store.transaction():
                # A same-node reapply (boot/profile/routing/settings) must preserve the actual
                # rollback target rather than replacing it with the current node itself.
                if previous_id != node.id:
                    store.set_setting(
                        "prev_active_node_id", previous_id_raw if previous_id_raw is not None else "")
                store.set_setting("active_node_id", str(node.id))
                store.set_setting("active_since", str(int(time.time())))   # uptime anchor (P3)
                # NF4: snapshot the lifetime data-used baseline so the Dashboard can show "this
                # session" (since (re)connect) = lifetime − baseline, beside the lifetime total.
                store.set_setting("session_base_up", store.get_setting("data_used_up") or "0")
                store.set_setting("session_base_down", store.get_setting("data_used_down") or "0")
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
        node = None
    if node is None:
        # Reconcile stale persisted intent immediately: otherwise status/sync_net continue to
        # treat a non-existent node as active. Keep the gateway closed while recording why.
        with apply_lock:
            with state.store.transaction():
                state.store.set_setting("active_node_id", "")
                state.store.set_setting("active_since", "")
                from pi_gw_panel.net_control import events as conn_events
                conn_events.record(state.store, "stale-active", f"cleared missing active node {aid}")
            guard = stop_net(state.settings, state.net, state.store)
        if not guard.ok:
            return ApplyResult(ok=False, error=f"stale active cleanup failed: {guard.error}")
        return None
    try:
        return apply_node(node, state.settings, state.supervisor, state.net,
                          store=state.store, xray_bin=state.xray_bin)
    except Exception as exc:  # never let boot crash on a bad saved node/config
        return ApplyResult(ok=False, error=f"boot reapply failed: {exc}")


def restore_backup(state, document) -> RestoreResult:
    """Restore validated intent and leave runtime explicitly disconnected + enforced."""
    from pi_gw_panel import backup as backup_mod
    from pi_gw_panel.net_control.provision import host_provision

    validated = backup_mod.validate_document(document)  # pure preflight before stopping anything
    with apply_lock:
        stats_client = getattr(state, "stats_client", None)
        previous_stats_address = (
            stats_client.status().get("address") if stats_client is not None else None)
        state.supervisor.stop()
        if state.supervisor.status().get("running"):
            return RestoreResult(ok=False, error="xray did not stop before restore")
        initial_guard = stop_net(state.settings, state.net, state.store)
        if not initial_guard.ok:
            return RestoreResult(
                ok=False, error=f"could not enforce disconnected state: {initial_guard.error}")
        try:
            with state.store.transaction():
                summary = backup_mod.import_state(state.store, validated)
                state.store.set_setting("active_node_id", "")
                state.store.set_setting("prev_active_node_id", "")
                state.store.set_setting("active_since", "")
                provisioned = host_provision(state)
                if getattr(provisioned, "ok", True) is False:
                    raise RuntimeError(provisioned.error or "restored host provisioning failed")
                guard = stop_net(state.settings, state.net, state.store)
                _require_net(guard, "restored fail-closed state failed")
                if stats_client is not None:
                    port = (state.store.get_setting("stats_api_port")
                            or SETTINGS_DEFAULTS["stats_api_port"])
                    stats_client.reconfigure(f"127.0.0.1:{int(port)}")
        except Exception as exc:
            # Candidate DB rows rolled back. The process remains stopped; reconcile that fact in
            # persisted state and re-assert the previous host/guard intent before returning 502.
            recovery: list[str] = []
            with state.store.transaction():
                state.store.set_setting("active_node_id", "")
                state.store.set_setting("prev_active_node_id", "")
                state.store.set_setting("active_since", "")
            try:
                previous_host = host_provision(state)
                if getattr(previous_host, "ok", True) is False:
                    recovery.append(previous_host.error or "previous host restore failed")
            except Exception as recovery_exc:
                recovery.append(f"previous host restore raised: {recovery_exc}")
            previous_guard = stop_net(state.settings, state.net, state.store)
            if not previous_guard.ok:
                recovery.append(previous_guard.error or "previous guard restore failed")
            if stats_client is not None and previous_stats_address:
                try:
                    stats_client.reconfigure(previous_stats_address)
                except Exception as recovery_exc:
                    recovery.append(f"stats client restore raised: {recovery_exc}")
            suffix = f"; recovery: {'; '.join(recovery)}" if recovery else ""
            return RestoreResult(ok=False, error=f"restore apply failed: {exc}{suffix}")
        return RestoreResult(ok=True, summary=summary)
