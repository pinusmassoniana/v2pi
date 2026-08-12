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


def _rw_inbound(store) -> dict | None:
    """The road-warrior inbound (default OFF). Values are validated at the API boundary, so a
    raise here means hand-edited/restored state — degrade to "feature off" instead of blocking
    every apply: a malformed remote-access setting must not be able to keep the tunnel down."""
    from pi_gw_panel import rw_inbound as rw
    try:
        return rw.resolve(store)
    except ValueError as exc:
        logger.warning("road-warrior inbound disabled — invalid stored settings: %s", exc)
        return None


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
    snapshot: str = ""     # the pre-restore copy of the state this replaced


def _record_enforcement(net, result: NetResult, *, wan_blocked: bool | None,
                        store=None) -> NetResult:
    """Keep the last *confirmed* host-enforcement outcome on the backend instance.

    The API must not infer that a guard exists merely because the corresponding setting is on.
    Backends are long-lived AppState resources, so this tiny status snapshot naturally survives
    across requests without persisting host-specific/transient truth in SQLite.

    `result.warning` is the same snapshot for the parts of an apply that are secondary and so
    only warn (LAN access). It had no reader, so a network that came up with its LAN-access
    rules missing was indistinguishable from a clean one outside the server log. It goes on
    the store — the one long-lived object `netcheck.network_status` is handed — and is always
    overwritten, never appended: it describes the LAST apply, and a failed apply reports
    through `enforcement_error` instead, so it must not leave a stale warning behind.
    """
    net.enforcement_status = "ok" if result.ok else "error"
    net.wan_blocked = wan_blocked if result.ok else None
    net.enforcement_error = "" if result.ok else (result.error or "network operation failed")
    if store is not None:
        store.last_net_warning = result.warning if result.ok else ""
    return result


def _call_net(net, method: str, *args, wan_blocked: bool | None, store=None) -> NetResult:
    try:
        result = getattr(net, method)(*args)
    except Exception as exc:
        result = NetResult(ok=False, error=f"{method} raised: {exc}")
    if not isinstance(result, NetResult):
        result = NetResult(ok=False, error=f"{method} returned no NetResult")
    return _record_enforcement(net, result, wan_blocked=wan_blocked, store=store)


def _require_net(result: NetResult, action: str) -> None:
    if not result.ok:
        raise RuntimeError(f"{action}: {result.error or 'network backend reported failure'}")


def apply_net(settings: Settings, net, store=None) -> NetResult:
    """Render+apply the Pi net plan. With a store, editable fields (segment/DHCP/DNS)
    and the kill-switch come from the settings k/v (falling back to config); without
    one it's the pure-config plan. Reused by apply_node and PUT /api/network."""
    plan = NetPlan.from_store(store, settings) if store is not None else NetPlan.from_settings(settings)
    return _call_net(net, "apply_tproxy", plan, wan_blocked=False, store=store)


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
                wan_blocked=None, store=store)
        # Rendering the plan is INSIDE the guard, not an argument expression. `_call_net` only
        # wraps the backend call, so `NetPlan.from_store(...)` evaluated as an argument raised
        # straight out of stop_net — past every caller, all of which treat this as a function
        # that reports failure by returning a NetResult. A revocation calls it after the
        # credential has already been taken out of the store and the config, inside one DB
        # transaction, so a raise here rolled the revocation itself back: the operator got an
        # error, the device stayed granted, and xray was left in whatever state the stop had
        # reached. Malformed stored net settings (hand-edited DB, foreign backup) are exactly
        # what can make the render raise, and they must not be able to veto a revocation.
        try:
            plan = NetPlan.from_store(store, settings)
        except Exception as exc:
            return _record_enforcement(
                net, NetResult(ok=False, error=f"could not render the net plan: {exc}"),
                wan_blocked=None, store=store)
        return _call_net(net, "apply_guard", plan, wan_blocked=True, store=store)
    return _call_net(net, "teardown", wan_blocked=False, store=store)


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
        rw = _rw_inbound(store)
        domain_strategy = store.get_setting("routing_domain_strategy") or "IPIfNonMatch"
    else:
        profile, routing, tunneled, stats, dns_intercept, ipv6 = None, None, False, None, False, False
        explicit = False
        rw = None
        domain_strategy = "IPIfNonMatch"
    return build_config(node, settings, profile=profile, routing=routing,
                        tunneled_fetch=tunneled, stats=stats, dns_intercept=dns_intercept,
                        domain_strategy=domain_strategy, ipv6_tproxy=ipv6, profile_explicit=explicit,
                        rw_inbound=rw)


# The durable record that a committed narrowing of remote access has not yet reached the running
# xray. The API layer sets it inside the mutation's own transaction; what CLEARS it is whatever can
# prove the runtime caught up, and that is why the key and the clear live down here rather than
# beside the setter: `apply_node` below is one such proof, and the only place it exists while the
# lock that makes acting on it safe is still held.
RW_PENDING_KEY = "rw_reconcile_pending"


def rw_reconcile_is_pending(store) -> bool:
    """Whether a committed narrowing is still waiting for its runtime half.

    A store read that FAILS answers False — "do not act now", not "nothing to do". The marker is
    durable, so nothing is lost by waiting for the next tick, whereas treating an unreadable
    store as pending would run a full config rebuild on every pass while the database is broken.
    """
    try:
        return bool(store.get_setting(RW_PENDING_KEY))
    except Exception:
        logger.warning("could not read the pending-revocation marker", exc_info=True)
        return False


def rw_clear_reconcile_pending(store) -> bool:
    """Drop the marker and report whether it is now PROVABLY gone.

    Guarded: failing to clear it costs one redundant reconcile later, while raising here would
    turn a COMPLETED revocation into a 500.

    The ANSWER is what the background recovery reports as "done", and it may not be inferred from
    the write. A `set_setting` that raises, or one that returns having written nothing, leaves the
    marker set and the next tick repeats the whole reconciliation — so a recovery that called
    itself finished on the outcome alone reset its own backoff and its episode flag and went round
    again every 20 s, reloading the live tunnel each time. The key is therefore read back, and
    a read that fails is not proof of anything: the only thing that clears this is seeing it gone.
    """
    try:
        store.set_setting(RW_PENDING_KEY, "")
    except Exception:
        logger.warning("could not clear the pending-revocation marker; the reconcile will be "
                       "retried", exc_info=True)
        return False
    try:
        return not store.get_setting(RW_PENDING_KEY)
    except Exception:
        logger.warning("could not confirm the pending-revocation marker was cleared; the "
                       "reconcile will be retried", exc_info=True)
        return False


def _rw_complete_pending(store) -> None:
    """Finish a pending revocation that THIS apply has just made true — under `apply_lock`.

    A full apply that returns ok rebuilt the config from the store and reloaded xray onto it,
    confirming readiness, so the running process is provably serving something the store produced
    — the whole of what the marker was waiting for. Leaving it set is not free: the next liveness
    tick reloads a healthy tunnel, and the recovery drops any rollback target it cannot vouch for,
    so a marker nobody clears keeps taking the operator's undo away.

    THE PROOF IS THE CONFIG AND THE RELOAD, and nothing else in the apply is load-bearing for it.
    An apply that returns ok can still carry a warning about a secondary step (LAN access — see
    `_record_enforcement`); that step can only ever grant LESS reachability, never re-list a
    credential the rebuilt config no longer names, so it does not bear on whether the revocation
    reached the running process. A step that would — a config that did not validate, a reload that
    did not come up ready, a net apply that failed outright — makes the apply itself fail, and a
    failed apply never gets here.

    IT BELONGS HERE, AND INSIDE THIS LOCK. Wrapping the CALLERS instead — one wrapper per apply
    site — was wrong twice over:

      * It ran after `apply_node` had released `apply_lock`. A revocation can commit a NEWER
        marker in that gap and then fail with `stop-failed`, and the older apply's wrapper erased
        that marker afterwards — so nothing would ever come back to finish a revocation whose
        credential was still being served. Every revocation holds `apply_lock` across both its
        store half and its runtime half, so a clear taken while the lock is held can only ever
        belong to a marker whose revocation has already finished.
      * It only covered the apply sites somebody remembered to wrap. The boot reapply, a
        subscription refresh and the failover tick reach exactly the same proven success and were
        not among them, so a successful apply from any of those left the marker set — a needless
        incident report and reload, and a fresh rollback target destroyed by the retry.

    FAILS CLOSED in every direction. The marker survives unless it is read back absent: a write
    that raises, one that quietly wrote nothing, and a read that cannot answer all leave it, and
    the recovery finishes the episode later. Guarded, because clearing a marker may never be the
    thing that turns a successful apply into a failure — the apply happened either way.
    """
    if store is None:
        return
    try:
        if rw_reconcile_is_pending(store) and rw_clear_reconcile_pending(store):
            logger.warning("a pending remote-access revocation was completed by a full apply")
    except Exception:
        logger.warning("could not clear the pending-revocation marker after an apply",
                       exc_info=True)


def apply_node(node: Node, settings: Settings, supervisor: XraySupervisor,
               net, store=None, xray_bin: str | None = None,
               irreversible: bool = False) -> ApplyResult:
    """Backbone: build -> validate(+snapshot) -> reload xray -> apply net.

    Serialized by `apply_lock` so a manual Connect and the failover tick can't interleave.
    On validation failure nothing is mutated (last-good preserved). If applying a
    *validated* config fails downstream (xray reload or net), roll the live config
    back to last-good, tear down the net ruleset, and report — never leave a
    half-applied state. On success, persist the active node id (if a store given).

    `irreversible` is for an apply whose effect `POST /rollback` may NEVER undo — a
    remote-access revocation, and so far nothing else. It writes through
    `ConfigManager.apply_irreversible`, which publishes no rollback target at all, instead of
    `apply()`, which files the config it REPLACES as the undo. That difference is the whole
    point: a revocation applied through the ordinary path necessarily snapshots the
    credential-bearing config and marks the pairing valid, leaving `/rollback` one button from
    reinstating the lost device until something else invalidates the marker — and a sweep that
    can only run afterwards is a guard with a window, not a guard.

    OFF by default, and it must stay that way: publishing the undo target is the feature for
    every other caller (Connect, boot reapply, a subscription refresh, the failover tick), and
    an ordinary apply that stopped doing it would take the operator's undo away.
    """
    with apply_lock:
        previous_id_raw = store.get_setting("active_node_id") if store is not None else None
        try:
            previous_id = int(previous_id_raw) if previous_id_raw else None
        except (TypeError, ValueError):
            previous_id = None
        previous_node = store.get_node(previous_id) if store is not None and previous_id else None

        mgr = ConfigManager(settings, xray_bin=xray_bin)
        try:
            # Build inside the guard too. Rendering reads stored values that are only validated
            # at the API boundary, so hand-edited or restored state can raise here (e.g. a
            # non-numeric `stats_api_port` reaching `int()`); outside the try that surfaced as a
            # 500 from every caller instead of a reported ApplyResult. Nothing is mutated yet,
            # so returning is safe — there is no runtime to roll back.
            cfg = build_node_config(node, settings, store)
            ok, out = mgr.apply_irreversible(cfg) if irreversible else mgr.apply(cfg)
        except Exception as exc:
            return ApplyResult(ok=False, error=f"config apply failed: {exc}")
        if not ok:
            return ApplyResult(ok=False, error=out)
        if store is not None and node.id is not None:
            # Crash-forward intent marker. From here on the LIVE config on disk already
            # describes `node`, but `active_node_id` still names the old one, and the window
            # spans an xray reload plus a full net apply. A panel crash inside it used to make
            # boot reapply silently UNDO a completed failover — it would re-apply the node we
            # had just failed away from, because that is what the store still said.
            store.set_setting("pending_active_node_id", str(node.id))
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
            rolled_back = False
            if irreversible:
                # Restoring the config this apply replaced is the one thing an irreversible
                # apply exists to prevent: that config still grants what was just revoked, and
                # the restore below would reload — on an xray this recovery may have stopped,
                # start — a process to serve it. There is nothing promotable to restore either
                # (apply_irreversible invalidated the pairing before writing), so this says so
                # rather than proving it again, and the fail-closed branch below takes over.
                recovery.append("config rollback withheld: this apply may not be undone")
            else:
                try:
                    rolled_back = mgr.rollback()
                except Exception as rollback_exc:
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
                    # False = the child outlived SIGKILL and is still up on the candidate
                    # config. Recorded, not retried: the fail-closed net guard below is what
                    # keeps clients off it, and starting anything on top of a process we could
                    # not stop only adds a second xray to the same port.
                    if supervisor.stop() is False:
                        recovery.append("xray could not be stopped (it survived SIGKILL)")
                except Exception as stop_exc:
                    recovery.append(f"xray stop raised: {stop_exc}")
                guard = stop_net(settings, net, store)
                if not guard.ok:
                    recovery.append(f"fail-closed recovery failed: {guard.error or 'unknown error'}")

            if store is not None:
                # The switch did not happen; there is no forward to converge to.
                store.set_setting("pending_active_node_id", "")
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
                store.set_setting("pending_active_node_id", "")   # intent and fact agree again
                store.set_setting("active_since", str(int(time.time())))   # uptime anchor (P3)
                # NF4: snapshot the lifetime data-used baseline so the Dashboard can show "this
                # session" (since (re)connect) = lifetime − baseline, beside the lifetime total.
                store.set_setting("session_base_up", store.get_setting("data_used_up") or "0")
                store.set_setting("session_base_down", store.get_setting("data_used_down") or "0")
        # Still holding `apply_lock`, and deliberately: this is the one thing here whose
        # correctness depends on no revocation being able to interleave (see
        # `_rw_complete_pending`). Outside the bookkeeping transaction, though — a raise inside it
        # marks the whole nested unit rollback-only and would discard the active-node writes above.
        _rw_complete_pending(store)
        return ApplyResult(ok=True)


def _pending_or_active(state) -> str | None:
    """Which node boot should re-establish: the interrupted switch if there was one.

    `apply_node` writes `pending_active_node_id` before touching the runtime and clears it once
    `active_node_id` matches. Finding one still set means we died mid-switch — the live config,
    and quite possibly the running xray and the host rules, already belong to the pending node,
    so converging FORWARD onto it is what makes boot agree with the world. Without this a crash
    during auto-failover reverts the panel to the very node the failover fled."""
    store = state.store
    active = store.get_setting("active_node_id")
    pending = store.get_setting("pending_active_node_id")
    if not pending or pending == active:
        return active or None
    try:
        node = store.get_node(int(pending))
    except (TypeError, ValueError):
        node = None
    if node is None:
        store.set_setting("pending_active_node_id", "")
        return active or None
    from pi_gw_panel.net_control import events as conn_events
    conn_events.record(store, "failover",
                       f"resumed node switch to {pending} interrupted by shutdown")
    logger.warning("boot: resuming interrupted switch to node %s (store still says %s)",
                   pending, active or "none")
    return pending


def reapply_active_node(state, irreversible: bool = False) -> ApplyResult | None:
    """Boot/restart persistence: re-apply the saved active node (rebuild+validate → start
    xray → apply net) on startup, so a reboot or container restart restores the tunnel with
    no manual Connect. Returns the ApplyResult, or None when there is no (valid) saved active
    node. Never raises — a failure is reported in the result, not by crashing boot.

    `irreversible` is forwarded to `apply_node` verbatim: the same rebuild is how a revocation
    reaches the config when a node is connected, and that one may leave no rollback target
    behind. Off by default — boot and every ordinary reapply keep publishing the undo."""
    aid = _pending_or_active(state)
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
                state.store.set_setting("pending_active_node_id", "")
                state.store.set_setting("active_since", "")
                from pi_gw_panel.net_control import events as conn_events
                conn_events.record(state.store, "stale-active", f"cleared missing active node {aid}")
            guard = stop_net(state.settings, state.net, state.store)
        if not guard.ok:
            return ApplyResult(ok=False, error=f"stale active cleanup failed: {guard.error}")
        return None
    try:
        return apply_node(node, state.settings, state.supervisor, state.net,
                          store=state.store, xray_bin=state.xray_bin,
                          irreversible=irreversible)
    except Exception as exc:  # never let boot crash on a bad saved node/config
        return ApplyResult(ok=False, error=f"boot reapply failed: {exc}")


_CANDIDATE_NET_KEYS = ("segment_iface", "segment_ip", "segment_ip6", "ipv6_enabled",
                       "manage_segment")


def _restore_candidate_data(validated) -> dict:
    """The net values a restore document is about to import, as the candidate helper reads them.

    `segment_iface` is in the restorable allowlist, so a restore can retarget the segment exactly
    like `PUT /api/network` does — and its `host_provision` runs inside the same DB transaction,
    so a failure afterwards leaves the same orphan on the same host with the same nothing looking
    at it. Same candidate ledger, then, and the same undo.
    """
    settings = getattr(validated, "settings", None) or {}
    return {key: str(settings[key]) for key in _CANDIDATE_NET_KEYS if key in settings}


def restore_backup(state, document) -> RestoreResult:
    """Restore validated intent and leave runtime explicitly disconnected + enforced."""
    from pi_gw_panel import backup as backup_mod
    from pi_gw_panel.net_control import provision

    validated = backup_mod.validate_document(document)  # pure preflight before stopping anything
    with apply_lock:
        # Snapshot AND restore are one serialized operation (audit FIX-E-2): taking the snapshot
        # before the lock let a concurrent mutation (e.g. a manual Connect) commit in the gap and
        # then get erased by the restore below without ever appearing in the recovery copy — the
        # safety net had a hole exactly when it was needed. Held inside the same `with apply_lock`
        # as the destructive work, no other mutator can land between "what we're about to replace"
        # and "replacing it".
        try:            # keep a copy of what this is about to replace, before anything is touched
            snapshot = backup_mod.write_pre_restore_snapshot(state)
        except (OSError, ValueError) as exc:
            return RestoreResult(ok=False, error=f"could not snapshot the current state: {exc}")
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
        # Recorded OUTSIDE the transaction below, for the same reason `PUT /api/network` does it:
        # a record that rolls back with the transaction it exists to clean up after would never be
        # readable when it is needed, and a crash in between would strand the candidate interface.
        try:
            candidate = provision.provision_candidate(state, _restore_candidate_data(validated))
        except Exception as exc:
            # Rendering the candidate reads the CURRENT stored net settings, which are only
            # validated at the API boundary and so can raise on hand-edited state. That must not
            # abort a restore which has already stopped the tunnel and installed the guard: the
            # pass below reports the same broken settings through its own result, which the
            # recovery here knows how to handle. No candidate simply means no orphan to reclaim.
            logger.warning("could not record the restore's provisioning candidate: %s", exc)
            candidate = {}
        provision.record_provision_candidate(state.store, candidate)
        installed: dict = {}
        try:
            with state.store.transaction():
                summary = backup_mod.import_state(state.store, validated)
                state.store.set_setting("active_node_id", "")
                state.store.set_setting("prev_active_node_id", "")
                state.store.set_setting("pending_active_node_id", "")
                state.store.set_setting("active_since", "")
                provisioned = provision.host_provision(state)
                # Read back before anything can roll it away: this is what the pass actually put
                # on the host, including a v6 prefix only it could resolve.
                installed = provision.managed_host_state(state.store)
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
                state.store.set_setting("pending_active_node_id", "")
                state.store.set_setting("active_since", "")
            try:
                previous_host = provision.host_provision(state)
                if getattr(previous_host, "ok", True) is False:
                    recovery.append(previous_host.error or "previous host restore failed")
            except Exception as recovery_exc:
                recovery.append(f"previous host restore raised: {recovery_exc}")
            # That pass reconciles the interface we went BACK to. A restored document may have
            # retargeted the segment, and whatever the candidate pass put somewhere else is then
            # invisible to every later pass and outside the nft guard — so name it and remove it.
            undone = provision.undo_provision_candidate(state, candidate, installed)
            recovery.extend(undone.actions)
            if candidate and not undone.unresolved:
                provision.clear_provision_candidate(state.store)
            previous_guard = stop_net(state.settings, state.net, state.store)
            if not previous_guard.ok:
                recovery.append(previous_guard.error or "previous guard restore failed")
            if stats_client is not None and previous_stats_address:
                try:
                    stats_client.reconfigure(previous_stats_address)
                except Exception as recovery_exc:
                    recovery.append(f"stats client restore raised: {recovery_exc}")
            suffix = f"; recovery: {'; '.join(recovery)}" if recovery else ""
            return RestoreResult(ok=False, error=f"restore apply failed: {exc}{suffix}",
                                 snapshot=snapshot)
        if candidate:                   # committed, so nothing is left pointing at an undo
            provision.clear_provision_candidate(state.store)
        return RestoreResult(ok=True, summary=summary, snapshot=snapshot)
