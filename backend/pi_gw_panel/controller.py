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


@dataclass
class _EmergencyDenyScope:
    """The only thing `provision`'s emergency-deny helpers read off a state: the net backend.

    The release has to happen where the enforcement outcome is RECORDED, and that funnel is handed
    a backend rather than an `AppState` — `apply_net(settings, net, store)` has no state to pass and
    neither does `stop_net`. So the one attribute those helpers touch is handed over on its own.
    """
    net: object


def _release_emergency_deny(net) -> str:
    """Drop the interface-independent emergency deny, now that host enforcement is CONFIRMED.

    "" when nothing is recorded as standing in for the panel's own enforcement any more; otherwise
    the sentence saying why every forwarded packet may still be being dropped.

    UNCONDITIONAL on a confirmed enforcement, and never attempted without one. The deny exists for
    the state in which the panel cannot say which interfaces to name (see
    `provision.install_emergency_forward_deny`), it names none itself, and so nothing narrows it and
    nothing drains it: the only thing that can end it is enforcement the host has taken. It is also
    not gated on the in-memory note, because the note is in this process and the table is in the
    kernel — a panel that installed the deny and was restarted arrives with nothing recorded and a
    table that would drop forwarded traffic for ever, which is why `release_emergency_forward_deny`
    reads "no such table" as its success answer.

    THE NOTE, NOT THE RETURN VALUE, IS THE AUTHORITY on whether anything is still holding the
    forward path. A release that reports success while something is still recorded as in force must
    not be able to produce a healthy-looking gateway, so both are consulted and either one is enough
    to withhold "ok". A raise is treated the same way: an undropped deny is a silent blackhole, so
    "we could not find out" is reported, never assumed away.
    """
    from pi_gw_panel.net_control import provision
    try:
        why = provision.release_emergency_forward_deny(_EmergencyDenyScope(net))
    except Exception as exc:
        why = ("the segment enforcement is installed, but the emergency deny on forwarded traffic "
               "could not be removed, so segment clients may still have no network: "
               + (str(exc) or exc.__class__.__name__))
        logger.exception("%s", why)
    return why or provision.enforcement_fallback_note(net)


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

    IT IS ALSO THE ONE PLACE THE EMERGENCY DENY IS RELEASED, because it is the one place a
    confirmed host enforcement is recognised — every apply, every guard and every teardown reaches
    it, from `apply_net`, `stop_net` and `sync_net_plan` alike. The release used to live only in the
    tail of `provision.host_provision`, so a recovery that runs no provisioning pass — a failover
    tick, a reapply, a subscription refresh — installed correct enforcement over a deny that stayed
    in force: every forwarded packet dropped indefinitely, reported as `enforcement_status="ok"`,
    with nothing that would ever come back to it. Doing it per-caller instead was the same defect
    one level up: it covers the callers somebody remembered.

    A RELEASE THAT DID NOT TAKE TURNS THE SUCCESS INTO A FAILURE, and that is deliberate. The
    enforcement is on the host, but the gateway carries nothing — the same fact `host_provision`
    already refuses to report as an applied configuration. Swallowing it is the reported-healthy
    blackhole this exists to end, so it goes back to the caller through the channel every one of
    them already handles (`apply_node` rolls back and fails closed, the routes answer 502) and into
    the snapshot as `error` plus the reason. `enforcement_status` therefore cannot say "ok" while
    anything is holding the forward path, which is also what makes `/api/ready` honest: its
    `enforcement` check is that status, and the note is already in its `details`.

    NOTHING SHORT OF CONFIRMED RELEASES IT. A failed or refused enforcement (`_refuse_enforcement`,
    a backend that cannot guard, a render that raised) arrives here with `ok=False` and is recorded
    without the deny being touched — releasing on an unproven render would reopen the exact hole the
    deny plugs.
    """
    if result.ok:
        held = _release_emergency_deny(net)
        if held:
            # `rendered` is kept (it is what went on the host); `warning` is not — it is a
            # success-only channel, and this is now a failure that reports through `error`.
            result = NetResult(ok=False, rendered=result.rendered, error=held)
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


class EnforcementCoverUnknown(RuntimeError):
    """The panel cannot say which interfaces may still be carrying the segment.

    Raised by `_enforcement_plan` INSTEAD of returning a plan, because there is no partial plan
    worth having. A ruleset is not edited, it is REPLACED — the linux backend deletes its table and
    recreates it from the render, and every other backend behaves the same way — so a render made
    from the cover sources that happened to answer does not "install what it knows": it takes the
    kill-switch drop and the tproxy redirect OFF an interface the unreadable source may have been
    naming, while that interface is up carrying a segment address. That is the direct-WAN bypass
    the whole segment protocol exists to prevent, and it needs no failed provisioning pass to reach
    it — one record that will not answer at render time is enough.

    So `apply_net` and `stop_net` turn this into a recorded failure and DO NOT enter the backend.
    Not applying is the only way to leave the live ruleset alone; applying a best guess is not a
    weaker version of it, it is the defect. Covering an interface that is already gone costs
    nothing (nft matches `iifname` by name), which is why the safe direction is always "keep what
    is there".
    """


def _refuse_enforcement(net, reason: str, store=None) -> NetResult:
    """Report a render that may not be installed, having installed nothing.

    Same shape as every other failure on this path — a `NetResult` plus the enforcement snapshot —
    so no caller has to learn a new one, and none of them can mistake it for a success. The
    backend is never called, so whatever ruleset is on the host stays exactly as it is.
    """
    logger.error("%s", reason)
    return _record_enforcement(net, NetResult(ok=False, error=reason),
                               wan_blocked=None, store=store)


def _enforcement_plan(settings: Settings, store) -> NetPlan:
    """The plan to ENFORCE from the store: its own, scoped to every interface that may still be
    carrying the segment.

    The store names exactly one segment interface, and there are states in which the host has two.
    A move that failed leaves the old interface up and addressed with the ownership record already
    pointing at the candidate; a rolled-back change leaves a candidate interface the undo may not
    delete still up and carrying the address the pass put there. Both outlast the pass that made
    them, so a ruleset rendered here from the segment interface alone would leave one of them
    outside the kill-switch drop and the tproxy redirect — the direct-WAN bypass, for as long as
    the interface lasts. `provision.enforcement_cover` is the panel's record of what may be up, and
    every render on this path consumes it; the interfaces in it leave only when the host proves
    them gone. This is enforcement only: dnsmasq, readiness and the API still describe the one
    interface the configuration names.

    A cover source that will not answer means there is NO PLAN TO RENDER, and this raises
    `EnforcementCoverUnknown` rather than returning the names that did answer. Installing those was
    the defect: a ruleset is replaced, never edited, so a render short by one interface uninstalls
    that interface's kill-switch drop and tproxy redirect while it may still be up carrying the
    segment — reachable with no provisioning pass anywhere near it, by one store read failing
    underneath a `sync_net`. The pass that NARROWS already refuses on the same fact (see
    `provision.Cover`); the pass and the render were never entitled to different answers, and the
    render is the one that runs on every apply, every disconnect and every boot.

    Imported inside the call for the same reason `provision` imports this module inside its own:
    the two reach into each other and neither may import the other at module scope.
    """
    from pi_gw_panel.net_control import provision
    plan = NetPlan.from_store(store, settings)
    cover = provision.enforcement_cover(store, plan.segment_iface)
    if not cover.known:
        raise EnforcementCoverUnknown(
            "the interfaces that may still be carrying the segment could not be established, so "
            "the enforcement already on the host was left exactly as it is: " + cover.why())
    return provision.covering_plan(plan, cover.names) if cover.names else plan


def apply_net(settings: Settings, net, store=None) -> NetResult:
    """Render+apply the Pi net plan. With a store, editable fields (segment/DHCP/DNS)
    and the kill-switch come from the settings k/v (falling back to config); without
    one it's the pure-config plan. Reused by apply_node and PUT /api/network.

    A plan the panel cannot promise covers every interface that may be carrying the segment is not
    applied AT ALL — the tproxy ruleset that is already on the host stays, and this returns failure
    (see `EnforcementCoverUnknown`). Every caller of this treats a failed `NetResult` as a failed
    apply already: `apply_node` rolls the config back and fails closed, the route rollback guards
    and reports, `sync_net`'s callers answer 502.
    """
    if store is None:
        plan = NetPlan.from_settings(settings)
    else:
        try:
            plan = _enforcement_plan(settings, store)
        except EnforcementCoverUnknown as exc:
            return _refuse_enforcement(net, str(exc), store)
    return _call_net(net, "apply_tproxy", plan, wan_blocked=False, store=store)


def _kill_switch_on(store) -> bool:
    return store is not None and (store.get_setting("kill_switch_enabled") or "1") == "1"


def stop_net(settings: Settings, net, store=None) -> NetResult:
    """Tear down the tunnel's net rules when the tunnel is intentionally stopped
    (disconnect / xray-stop / boot-before-tunnel).

    Kill-switch ON  → install the fail-closed leak-guard (keep dropping client→WAN, v4+v6)
    instead of a full teardown — otherwise a "fail closed" kill-switch would *leak* the
    moment you stop (audit A1). Kill-switch OFF → full teardown (clients fall back direct).

    The guard is a ruleset like any other, so it is subject to the same refusal: a cover the panel
    cannot complete means the guard is NOT installed and this returns failure, leaving whatever is
    on the host in place. A teardown renders no plan and is unaffected — it names no interface, and
    with the kill-switch off falling back to direct IS the configured intent.

    A CONFIRMED TEARDOWN RELEASES THE EMERGENCY DENY, for the same reason it cannot be refused. The
    deny stands in for enforcement the panel could not render, and the hazard it plugs is a render
    that names one interface fewer than the host has — a teardown names none, so there is no
    interface for it to be short of and no bypass to reopen by ending it. What is left if it stays is
    a bare forward drop that nothing narrows and nothing drains, holding the segment at no network at
    all, against a configuration whose whole content is "let these clients out directly". That is the
    panel enforcing a policy nobody asked for, indefinitely; the operator asked for direct and the
    cost of honouring it is bounded by their own choice. The release still happens only once the
    backend CONFIRMS the teardown — `_record_enforcement` is reached with `ok` — never on the
    intention to attempt one.
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
            plan = _enforcement_plan(settings, store)
        except EnforcementCoverUnknown as exc:
            # Not a render that failed — one that may not be INSTALLED. Handled before the general
            # case only so the operator is told which of the two happened; the outcome is the same
            # either way, and it is the only safe one: nothing goes on the host.
            return _refuse_enforcement(net, str(exc), store)
        except Exception as exc:
            return _record_enforcement(
                net, NetResult(ok=False, error=f"could not render the net plan: {exc}"),
                wan_blocked=None, store=store)
        return _call_net(net, "apply_guard", plan, wan_blocked=True, store=store)
    return _call_net(net, "teardown", wan_blocked=False, store=store)


def _enforcement_mode(state) -> str:
    """Which enforcement the current runtime state calls for: tproxy, guard, or teardown.

    One reading of that state, shared by the two callers that act on it: `sync_net`, which renders
    the plan from the store, and `sync_net_plan`, which is handed one. They must not be able to
    drift — a segment retarget applies a transitional ruleset through the second and is narrowed
    back through the first, and the two deciding differently would swap the rules out for a
    different KIND of ruleset halfway through the move.
    """
    running = state.supervisor.status().get("running")
    if running and state.store.get_setting("active_node_id"):
        return "tproxy"
    return "guard" if _kill_switch_on(state.store) else "teardown"


def sync_net(state) -> NetResult:
    """Apply the net rules that match the CURRENT tunnel state — used by PUT /network so a
    segment/kill-switch edit takes effect immediately without black-holing. Tunnel up (xray
    running + an active node) → full tproxy; otherwise → stop_net (leak-guard if the
    kill-switch is on, else teardown). Avoids installing a tproxy-to-dead-port when there's
    no live tunnel (A1).

    THIS is where the incomplete cover was reproduced, with no provisioning pass in sight: a
    segment edit, or anything else that syncs, rendered from a store whose survivor record would
    not answer and replaced a ruleset covering two interfaces with one covering the configured one.
    Both branches now refuse instead (see `EnforcementCoverUnknown`), so a sync that cannot see the
    whole picture changes nothing and reports — the caller answers 502 and the live ruleset, which
    was rendered when the picture WAS complete, keeps standing."""
    if _enforcement_mode(state) == "tproxy":
        return apply_net(state.settings, state.net, state.store)
    return stop_net(state.settings, state.net, state.store)


def sync_net_plan(state, plan: NetPlan) -> NetResult:
    """`sync_net` for a plan the caller has already rendered, and the ONE reason that exists.

    Enforcement is scoped by interface name, and the store names exactly one segment interface —
    so while the segment is being moved from one to another, the plan the store renders cannot
    describe the host: for the length of that move both interfaces are live, and a ruleset naming
    either one alone leaves the other outside the kill-switch drop and the tproxy redirect. The
    pass performing the move is the only thing that knows what it is about to do (see
    `net_control.provision.host_provision`); it builds the transitional plan and applies it
    through here, before it raises anything, and narrows back through here once the old link is
    gone. The mode decision, the failure wrapping and the enforcement snapshot are the same as
    `sync_net`'s, deliberately: this is where the plan comes from, and nothing else.

    What a FINISHED pass leaves behind is a different question and is not answered here: an
    interface a failed move or a rolled-back change left live outlasts the pass, so it is recorded
    and picked up by `_enforcement_plan` on every store-derived render. The plan handed in here has
    already been scoped by its caller and is applied exactly as given.
    """
    mode = _enforcement_mode(state)
    if mode == "tproxy":
        return _call_net(state.net, "apply_tproxy", plan, wan_blocked=False, store=state.store)
    if mode == "guard":
        if not hasattr(state.net, "apply_guard"):
            return _record_enforcement(
                state.net, NetResult(ok=False, error="network backend cannot install fail-closed guard"),
                wan_blocked=None, store=state.store)
        return _call_net(state.net, "apply_guard", plan, wan_blocked=True, store=state.store)
    return _call_net(state.net, "teardown", wan_blocked=False, store=state.store)


def _disconnected_guard_plan(state) -> NetPlan | None:
    """The enforcement a disconnect will install, rendered while the tunnel is still up.

    `None` when the kill-switch is off, and that is not a missing answer: that disconnect is a full
    teardown, which names no interface, renders no plan, and so has nothing in it that can refuse.
    Every other answer is a plan the caller must KEEP and hand to `sync_net_plan` exactly as given.

    THE RENDER IS THE PART THAT CAN FAIL, which is the whole reason this exists as a step of its own.
    A store-derived render refuses when the enforcement cover cannot be completed
    (`EnforcementCoverUnknown`) and raises outright on stored net settings that will not parse — and
    both of those are transient-persistence-fault shaped. Asking the question where the ruleset is
    installed puts them after the runtime has been taken down, where the answer "there is no plan to
    install" is a gateway outage instead of a refusal. Asked here, with the tunnel the operator has
    still up, the same answer costs nothing.

    Re-deriving the plan at the install point would put a second copy of that failure back at exactly
    the point this moves it away from, so callers apply the retained plan and do not render again.
    """
    if not _kill_switch_on(state.store):
        return None
    return _enforcement_plan(state.settings, state.store)


def boot_guard(state) -> NetResult:
    """Close the boot leak window (A1): if the kill-switch is on, install the leak-guard
    BEFORE the tunnel is (re)applied, so a reboot never leaks client→WAN while xray starts.
    No-op when the kill-switch is off. Best-effort — never blocks boot.

    Boot is not a clean slate: nft rules live in the kernel, so a panel or container restart
    arrives with the previous, correctly-covering ruleset still installed, and a guard rendered
    here from an incomplete cover would narrow exactly that. So this refuses on the same fact as
    every other store-derived render and returns the failure, which boot logs and carries in the
    enforcement snapshot rather than treating as a guard that went on."""
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
                # A guard that cannot be rendered over every interface that may be carrying the
                # segment is reported, not approximated: this apply's own net step has already
                # failed, so the ruleset on the host is the one the PREVIOUS apply installed —
                # rendered when the cover was complete, and pointing at an xray this recovery has
                # just stopped. Keeping it black-holes clients; replacing it with a short guard
                # would release one of them to the WAN.
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


# The candidate keys, split by where each one lands once the document is in. Every reader resolves
# these as `store.get_setting(k) or <fallback>`, and `import_state` DELETEs the whole allowlisted
# settings snapshot before it writes — so a key the document omits does NOT keep the override the
# box is running now; it falls all the way through to that fallback.
#   segment_iface/segment_ip/segment_ip6 — `NetPlan._EDITABLE`: `store.get_setting(k) or
#     getattr(settings, k)`, i.e. the attribute of the same name on the running `Settings`.
#   ipv6_enabled/manage_segment — k/v flags read as `store.get_setting(k) or "0"` / `... or "1"`,
#     and those literals are what `SETTINGS_DEFAULTS` holds.
_CANDIDATE_FROM_SETTINGS = ("segment_iface", "segment_ip", "segment_ip6")
_CANDIDATE_FROM_DEFAULTS = ("ipv6_enabled", "manage_segment")
_CANDIDATE_NET_KEYS = _CANDIDATE_FROM_SETTINGS + _CANDIDATE_FROM_DEFAULTS


def _as_imported(value) -> str:
    """A document value exactly as `import_state` will write it into the settings table.

    Booleans have to go through the same `True → '1'` mapping the importer uses, or a document
    saying `ipv6_enabled: false` would read back as blank here and be projected as "omitted" —
    silently adopting the default instead of the "off" it actually asked for.
    """
    if value is None:
        return ""
    return ("1" if value else "0") if isinstance(value, bool) else str(value)


def _restore_candidate_data(state, validated) -> dict:
    """The net values this box will RUN once the document is in — projected, not quoted.

    `segment_iface` is in the restorable allowlist, so a restore can retarget the segment exactly
    like `PUT /api/network` does — and its `host_provision` runs inside the same DB transaction,
    so a failure afterwards leaves the same orphan on the same host with the same nothing looking
    at it. Same candidate ledger, then, and the same undo.

    Which makes *what* the ledger names the whole point, and quoting the document is not it. A
    backup is routinely sparse — `export_state` only carries the settings rows that exist, so a box
    that never overrode the segment exports no `segment_iface` at all — while a restore is not
    sparse at all: it deletes the allowlisted settings and every reader falls back to the runtime
    default. Handing the helper only the keys the document mentions let it fill the rest from
    `NetPlan.from_store`, which is the state we are LEAVING. With a segment override in place and a
    document that omits it, the ledger recorded the old interface while `host_provision` went and
    created the default one: the interface that actually got made was named nowhere, so a crash or
    a rollback left it on the host, outside the nft guard and invisible to every later pass.

    So every key is resolved here, through the same fallback chain the import itself uses, and
    handed over complete — the helper's own `or plan.<field>` fallbacks then never fire.
    """
    document = getattr(validated, "settings", None) or {}

    def projected(key: str, fallback) -> str:
        # `or`, deliberately: a document that stores an empty string is not setting the value —
        # every reader treats a blank setting as absent and falls through to the same default.
        return _as_imported(document.get(key)).strip() or str(fallback or "")

    data = {key: projected(key, getattr(state.settings, key, ""))
            for key in _CANDIDATE_FROM_SETTINGS}
    data.update({key: projected(key, SETTINGS_DEFAULTS.get(key, ""))
                 for key in _CANDIDATE_FROM_DEFAULTS})
    return data


# THE PREFLIGHT CANDIDATE RECORD DESCRIBES AN INTENTION, NOT SOMETHING THE PANEL HAS DONE.
#
# It is written before the restore has run a single host command — that is the point of it being
# first — and its two readers need opposite answers from exactly that fact:
#
#   * The enforcement cover reads it as a COVERAGE source (`provision.pending_candidate_ifaces`), and
#     naming an interface in a ruleset costs nothing: nft matches `iifname` by name, so a rule for a
#     device that is not there simply never matches. Covering an interface the restore only might
#     create is the safe direction, and it stays.
#   * The undo reads it as a licence to REMOVE host state — `provision.resume_pending_provision_undo`
#     at the next boot, and the in-process rollback — and there an intention licenses nothing. Acting
#     on it deletes a link this restore never created, and an `ip link delete` against an interface
#     the operator, netplan or NetworkManager put there is the segment on a live gateway.
#
# The exits between the record and the host pass are where the difference bites: none of them ran a
# provisioning pass, all of them try to settle the record, and the clear is best effort by contract —
# so a store that quietly writes nothing leaves the record behind and boot is authorised to undo a
# candidate that was never created.
#
# So the record is written UNARMED first and ARMED only once the restore is committed to running the
# pass. Unarmed is not a flag the readers must learn: it is a record whose delete preconditions
# cannot be met, so the existing readers already do the right thing with it. `provision`'s undo will
# not issue a link delete unless the record says the candidate is a VLAN *and* carries the
# `LINK_ABSENT` probe answer that proves the pass created it, and it reports and records nothing for
# addresses the record does not name. `armed` is carried on top of that so the intent is legible in
# the stored value instead of being inferable from which fields are missing.
def _unarmed_candidate(candidate: dict) -> dict:
    """`candidate` as it may be recorded BEFORE the restore has touched the host.

    Keeps the interface, so the enforcement cover goes on naming it. Drops everything the undo would
    need in order to justify removing anything: the addresses it would report as orphans and write
    into the surviving-candidate ledger, the VLAN fact, and the prior-probe answer that is the only
    licence to delete a link. Either of the last two alone blocks the delete and both are dropped —
    over-caution is free in this direction and an interface is not.

    Empty in, empty out: an empty candidate already means "the pass puts nothing anywhere", and it is
    never recorded at all.
    """
    from pi_gw_panel.net_control import provision
    if not candidate:
        return {}
    return {"iface": candidate.get("iface") or "",
            "addr4": "",
            "addr6": "",
            "vlan": False,
            "link_state": provision.LINK_UNKNOWN,
            "armed": False}


def _settle_preflight_candidate(state, after: str) -> None:
    """Settle the unarmed preflight record on an exit that ran no host provisioning, and SAY when it
    did not settle.

    `clear_provision_candidate` reports rather than raises, deliberately — nothing on a recovery path
    may turn one failure into another — and its result was being dropped, so a store that accepted the
    write and kept nothing left the record in place with nothing said about it. What that now costs is
    bounded by the record being unarmed: its interface stays named in every later ruleset, which is
    the safe direction and drains on its own, and no undo may act on it. So this reports and does not
    raise: the exit that calls it has already decided its own answer and this may not change it.
    """
    from pi_gw_panel.net_control import provision
    why = provision.clear_provision_candidate(state.store)
    if why:
        logger.error("the restore's unarmed provisioning record could not be settled after %s; the "
                     "enforcement keeps covering the interface it names and no undo may act on it: "
                     "%s", after, why)


def restore_backup(state, document) -> RestoreResult:
    """Restore validated intent and leave runtime explicitly disconnected + enforced.

    Two phases, in this order: everything that can REFUSE is proven while the gateway the operator
    has is still running, and only then is that gateway taken down. The lockout guard, the recovery
    snapshot, the candidate record and the render of the disconnected guard are all in the first
    phase; the disconnect, that guard's INSTALL, the import and the host pass are in the second. A
    refusal therefore costs nothing — same as `PUT /api/network`.
    """
    from pi_gw_panel import backup as backup_mod
    from pi_gw_panel.net_control import provision

    # Pure preflight, before anything is stopped or written — and checked against THIS panel's own
    # `Settings`, not a second reading of the process environment. `create_app(settings, state=...)`
    # builds a panel from a `Settings` that need not have come from the env at all, so the
    # environment is not a reliable copy of the management leg the operator is actually reached on;
    # a lockout guard comparing against the wrong `mgmt_iface` accepts the collision it exists to
    # refuse. `state.settings` IS the one every other consumer on this path uses (`stop_net`,
    # `NetPlan.from_store`, `host_provision`), so the guard now judges the same configuration the
    # restore is about to reconfigure.
    validated = backup_mod.validate_document(document, state.settings)
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
        # PROVEN BEFORE ANY RUNTIME IS TOUCHED, and that ordering is the whole point.
        #
        # `PUT /api/network` refuses before it mutates the host: the validation and the candidate
        # record both come first, so a refusal leaves the gateway exactly as it was. This path did
        # the same two things in the opposite order — it stopped xray and installed the disconnected
        # guard, and only then recorded what the pass was about to put on the host. A store that
        # would not take that record therefore imported nothing, applied nothing, changed nothing
        # the operator can see afterwards, and left a previously working gateway DISCONNECTED. The
        # write is transient by nature (a locked database, a full disk), which is exactly the kind of
        # failure that must not cost a working tunnel.
        #
        # Nothing here touches the host: the candidate is rendered from stored settings plus one
        # read-only probe of the target link, and the record is a settings write. So it can be proven
        # first, and the disconnect below now happens only once the restore is committed to running.
        #
        # It is recorded OUTSIDE the transaction for the same reason `PUT /api/network` does it: a
        # record that rolls back with the transaction it exists to clean up after would never be
        # readable when it is needed, and a crash in between would strand the candidate interface.
        try:
            candidate = provision.provision_candidate(
                state, _restore_candidate_data(state, validated))
        except Exception as exc:
            # REFUSE, AND NEVER `{}`. An empty candidate is this path's way of saying "the pass will
            # put nothing anywhere" — a dry-run backend, or `manage_segment` projected off — and it
            # is read that way everywhere below: no record is written, no undo is attempted, and no
            # exit settles anything. A render that FAILED is the opposite claim. The pass may still
            # create a link and address it, and with the failure spent as "there is nothing to
            # record" the restore went on to disconnect and provision with no undo record at all, so
            # a rolled-back transaction — or a crash — left that interface outside the restored
            # enforcement cover, invisible to every later pass and outside the nft guard. The two
            # facts demand opposite behaviour, so they may not share a value.
            #
            # It cannot be narrowed to "only when a candidate is required" from out here either,
            # because that is precisely what the failed render was going to answer. It does not need
            # to be: `provision_candidate` returns `{}` before it can raise unless the backend
            # carries the host seam (`_run`), so reaching here IS the host branch — the one case in
            # which Linux segment management may require a candidate.
            #
            # Refusing costs nothing, which is the whole reason this sits where it does: xray is
            # still serving the node it was serving, no host command has run, and nothing has been
            # written. The operator gets the broken setting reported instead of a disconnected
            # gateway. Hand-edited or restored net settings reaching `int()`/`ip_network()` are
            # exactly what makes the render raise, and they are also what the pass below would fail
            # on a moment later — with the runtime already down.
            return RestoreResult(
                ok=False, snapshot=snapshot,
                error=f"could not establish what the restore would put on the host, so nothing was "
                      f"applied: {exc}")
        # UNARMED, because nothing below this line has touched the host yet and every exit before the
        # pass tries to settle it again — see `_unarmed_candidate` for why a record that survives a
        # failed settle must not be one a later boot may act on.
        preflight = _unarmed_candidate(candidate)
        try:
            provision.record_provision_candidate(state.store, preflight)
        except Exception as exc:
            # A candidate that cannot be PROVEN recorded is a pass with no recovery story, so the
            # restore does not start one — and now nothing has been stopped either. The tunnel is
            # still up on the configuration the operator had.
            return RestoreResult(ok=False, snapshot=snapshot,
                                 error=f"could not record what the restore is about to put on the "
                                       f"host, so nothing was applied: {exc}")
        # THE DISCONNECTED GUARD IS RENDERED HERE AND APPLIED BELOW — one render, two places.
        #
        # It is a store-derived render like every other, so it can refuse: a cover source that will
        # not answer means there is NO plan to install (see `EnforcementCoverUnknown`), and stored net
        # settings that will not parse make the render raise outright. Both were being asked AFTER
        # `supervisor.stop()`, where "there is no plan to install" is no longer a refusal but a
        # gateway outage — xray down, the tproxy ruleset the previous apply installed still on the
        # host pointing at it, clients black-holed, and nothing imported to show for it. A transient
        # persistence fault must not be able to buy that.
        #
        # Asked here it is the same question with the operator's tunnel still up, so it joins the
        # other first-phase refusals and costs nothing. The plan is then RETAINED and applied
        # verbatim: re-deriving it after the disconnect would put the same failure back at the same
        # point. `sync_net_plan` applies a plan it is handed and, with xray stopped, decides the very
        # mode `stop_net` would have — the fail-closed guard with the kill-switch on, a teardown
        # without it, which renders no plan and is the `None` case.
        #
        # AFTER the candidate record, deliberately. That record is one of the cover's sources
        # (`pending_candidate_ifaces`), so a plan rendered before it landed would name one interface
        # fewer than the plan this path installs today — and dropping a name from a ruleset that is
        # replaced rather than edited is the one direction that is never safe.
        try:
            guard_plan = _disconnected_guard_plan(state)
        except Exception as exc:
            # Nothing has been stopped, so this is still free. The candidate record is settled for
            # the same reason as the exits below: no pass ran, so there is nothing to reclaim.
            if preflight:
                _settle_preflight_candidate(state, "a guard that could not be rendered")
            return RestoreResult(
                ok=False, snapshot=snapshot,
                error=f"could not enforce disconnected state, so the gateway was left as it was: "
                      f"{exc}")
        # From here the runtime IS touched, so every remaining exit before the transaction settles
        # the record above: no pass ran, so there is no candidate for a later boot to reclaim, and
        # leaving one behind would keep an interface named in every ruleset. Settling is best effort
        # and cannot be made otherwise, which is why the record it tries to settle is UNARMED: what a
        # silent no-op leaves behind is an interface the enforcement keeps covering, and nothing
        # `resume_pending_provision_undo` may remove.
        state.supervisor.stop()
        if state.supervisor.status().get("running"):
            if preflight:
                _settle_preflight_candidate(state, "a stop that did not take")
            return RestoreResult(ok=False, snapshot=snapshot,
                                 error="xray did not stop before restore")
        initial_guard = (stop_net(state.settings, state.net, state.store) if guard_plan is None
                         else sync_net_plan(state, guard_plan))
        if not initial_guard.ok:
            if preflight:
                _settle_preflight_candidate(state, "a guard the host would not take")
            return RestoreResult(
                ok=False, snapshot=snapshot,
                error=f"could not enforce disconnected state: {initial_guard.error}")
        # ARMED HERE, and this is the line between an intention and a fact. Every exit above ran no
        # host provisioning whatsoever, so the record they may leave behind must authorise no undo;
        # below, `host_provision` can create the candidate link and address it, and from then on this
        # record is the only thing that can reclaim it after a rollback or a crash.
        #
        # OUTSIDE the transaction, for the reason the preflight write is: a record that rolled back
        # with the transaction it exists to clean up after would never be readable when it is needed.
        try:
            provision.record_provision_candidate(state.store, candidate)
        except Exception as exc:
            # Same rule as the preflight write, at the only other point it can be applied: a candidate
            # that cannot be PROVEN recorded is a pass with no recovery story, so the restore does not
            # start one. Nothing is imported and the host still has the segment it had — the gateway
            # is disconnected and says so, which is what every failure in this phase looks like.
            if preflight:
                _settle_preflight_candidate(state, "a record that could not be armed")
            return RestoreResult(
                ok=False, snapshot=snapshot,
                error=f"could not arm the record of what the restore is about to put on the host, so "
                      f"no host provisioning was started: {exc}")
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
