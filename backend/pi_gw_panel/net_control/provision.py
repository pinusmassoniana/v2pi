"""Self-provisioning gateway: bring the whole host gateway up from settings, idempotently.

Gated on the linux backend (a net backend carrying a `_run` seam) + the `manage_segment`
setting. Every side-effect goes through an injectable seam — `run` for shell-outs,
`write_proc`/`write_file` for /proc + conf files — so the command/file emission is unit-tested
with no root or Pi. The default runner/proc-writer ARE the LinuxBackend ones (imported, not
re-declared) so both paths keep one contract; the real apply passes the backend's own seam."""
import ipaddress
import logging
import os
import secrets
import subprocess

from pi_gw_panel.net_control.linux import _run, _write_proc
from pi_gw_panel.net_control.plan import NetPlan, NetResult
from pi_gw_panel.net_control.render import render_dnsmasq

_log = logging.getLogger("pi_gw_panel")

NM_CONF_PATH = "/etc/NetworkManager/conf.d/99-v2pi.conf"


def _write_file(path: str, text: str) -> None:
    try:
        with open(path, "w") as f:
            f.write(text)
    except OSError:
        pass


def _remove_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# --- pure helpers --------------------------------------------------------------

def parse_vlan(iface: str) -> tuple[str, int | None]:
    """`eth0.2` -> (`eth0`, 2); a dotless iface -> (iface, None) (no VLAN to create)."""
    if "." in iface:
        parent, vid = iface.rsplit(".", 1)
        try:
            return parent, int(vid)
        except ValueError:
            return iface, None
    return iface, None


def host_addr6(segment_ip6: str) -> str | None:
    """The gateway's own v6 address in the segment /64 = first host address (`<prefix>::1/64`).
    None for blank / `auto` / invalid."""
    s = (segment_ip6 or "").strip()
    if not s or s.lower() == "auto":
        return None
    try:
        net = ipaddress.ip_network(s, strict=False)
    except ValueError:
        return None
    if net.version != 6 or net.prefixlen != 64:
        return None
    return f"{net.network_address + 1}/64"


def generate_ula_prefix(vlan_id: int, rand=secrets.token_bytes) -> str:
    """A stable, install-unique ULA /64: `fd` + 40 random bits (global ID) + 16-bit subnet =
    the VLAN id. Persisted by the caller so it survives reboots."""
    gid = rand(5)
    b = bytes([0xFD]) + gid + bytes([(vlan_id >> 8) & 0xFF, vlan_id & 0xFF]) + bytes(8)
    net = ipaddress.ip_network((ipaddress.IPv6Address(b), 64), strict=False)
    return net.with_prefixlen


# --- host bring-up steps -------------------------------------------------------

def _link_exists(iface: str, run=_run) -> bool:
    try:
        run(["ip", "link", "show", iface])
        return True
    except subprocess.CalledProcessError:
        return False


def _nm_active(run=_run) -> bool:
    """True if a NetworkManager is running on the host (so a reload is meaningful)."""
    try:
        run(["nsenter", "-t", "1", "-m", "-n", "--",
             "systemctl", "is-active", "--quiet", "NetworkManager"])
        return True
    except (subprocess.CalledProcessError, OSError):
        return False


def ensure_sysctls(settings, write_proc=_write_proc) -> None:
    """Forwarding (v4 + v6) and accept_ra=2 on the uplink (so the Pi keeps its own v6 default
    route even with forwarding on). Best-effort; the privileged container has writable
    /proc/sys."""
    write_proc("/proc/sys/net/ipv4/ip_forward", "1")
    write_proc("/proc/sys/net/ipv6/conf/all/forwarding", "1")
    write_proc(f"/proc/sys/net/ipv6/conf/{settings.mgmt_iface}/accept_ra", "2")


def ensure_segment_link(store, plan: NetPlan, run=_run, link_exists=None) -> None:
    """Create the configured VLAN when needed and bring the segment link up.

    A VLAN the panel creates is recorded as panel-owned BEFORE the kernel call, so disabling
    `manage_segment` can delete exactly the link this panel added (and never a pre-existing
    one) even if the process dies between the record and the creation.
    """
    link_exists = link_exists or (lambda i: _link_exists(i, run))
    seg = plan.segment_iface
    parent, vid = parse_vlan(seg)
    if vid is not None and not link_exists(seg):
        store.set_setting("managed_segment_link", seg)
        run(["ip", "link", "add", "link", parent, "name", seg,
             "type", "vlan", "id", str(vid)])
    run(["ip", "link", "set", seg, "up"])


def clear_managed_link(store, run=_run) -> None:
    """Delete only a VLAN link this panel created, and forget it."""
    link = store.get_setting("managed_segment_link") or ""
    if not link:
        return
    try:
        run(["ip", "link", "delete", link])
    except (subprocess.CalledProcessError, OSError):
        # Already gone (reboot / manual cleanup). Ownership metadata still needs clearing.
        pass
    store.set_setting("managed_segment_link", "")


def _delete_owned(addr: str, iface: str, *, ipv6: bool, run=_run) -> None:
    """Best-effort removal of one address previously recorded as panel-owned."""
    if not addr or not iface:
        return
    cmd = ["ip"] + (["-6"] if ipv6 else []) + ["addr", "del", addr, "dev", iface]
    try:
        run(cmd)
    except subprocess.CalledProcessError:
        # The kernel may already have lost the address across a reboot. Ownership metadata
        # still needs clearing; never broaden this into an address flush.
        pass


STALE_KEY = "managed_segment_stale"


def _parse_stale(store) -> list[tuple[str, str]]:
    """`(iface, addr)` pairs recorded as panel-owned but not yet removed from the kernel."""
    out: list[tuple[str, str]] = []
    for line in (store.get_setting(STALE_KEY) or "").splitlines():
        parts = line.split()
        if len(parts) == 2:
            out.append((parts[0], parts[1]))
    return out


def _record_ownership(store, iface: str, addr4: str, addr6: str,
                      stale: list[tuple[str, str]]) -> None:
    store.set_setting("managed_segment_iface", iface)
    store.set_setting("managed_segment_addr4", addr4)
    store.set_setting("managed_segment_addr6", addr6)
    store.set_setting(STALE_KEY, "\n".join(f"{i} {a}" for i, a in stale))


def reconcile_segment_addresses(store, plan: NetPlan, run=_run) -> None:
    """Atomically replace the desired addresses, then delete only addresses the panel owns.

    Ownership is recorded BEFORE the kernel is touched, and an address that is being replaced
    stays recorded (on the stale list) until its `ip addr del` has actually run. That ordering
    is what keeps the panel's record a superset of what it put on the host: a failure — or a
    caller whose surrounding DB transaction rolls back mid-apply — can then still find every
    address to remove, instead of leaving an orphan no later pass would ever delete.

    The ownership keys make config changes and IPv6 disablement safe on hosts which also carry
    unrelated addresses on the segment interface: no wildcard/flush operation is ever used.
    """
    old_iface = store.get_setting("managed_segment_iface") or plan.segment_iface
    old4 = store.get_setting("managed_segment_addr4") or ""
    old6 = store.get_setting("managed_segment_addr6") or ""
    new_iface = plan.segment_iface
    new4 = f"{plan.segment_ip}/24"
    new6 = host_addr6(plan.segment_ip6) if plan.ipv6_enabled else None

    stale = _parse_stale(store)
    if old4 and (old4 != new4 or old_iface != new_iface):
        stale.append((old_iface, old4))
    if old6 and (old6 != new6 or old_iface != new_iface):
        stale.append((old_iface, old6))

    _record_ownership(store, new_iface, new4, new6 or "", stale)

    run(["ip", "addr", "replace", new4, "dev", new_iface])
    if new6:
        run(["ip", "-6", "addr", "replace", new6, "dev", new_iface])

    for iface, addr in stale:
        _delete_owned(addr, iface, ipv6=":" in addr, run=run)
    _record_ownership(store, new_iface, new4, new6 or "", [])


def clear_managed_addresses(store, run=_run) -> None:
    """Remove only addresses previously installed by this panel and clear ownership state."""
    iface = store.get_setting("managed_segment_iface") or ""
    for stale_iface, addr in _parse_stale(store):
        _delete_owned(addr, stale_iface, ipv6=":" in addr, run=run)
    _delete_owned(store.get_setting("managed_segment_addr4") or "", iface,
                  ipv6=False, run=run)
    _delete_owned(store.get_setting("managed_segment_addr6") or "", iface,
                  ipv6=True, run=run)
    _record_ownership(store, "", "", "", [])


def _nm_reload(run, nm_active) -> None:
    """Reload NetworkManager live via nsenter into pid 1, but only when it is running."""
    nm_active = nm_active or (lambda: _nm_active(run))
    if nm_active():
        try:
            run(["nsenter", "-t", "1", "-m", "-n", "--", "nmcli", "general", "reload"])
        except (subprocess.CalledProcessError, OSError):
            pass


def ensure_nm_unmanaged(seg: str, run=_run, write_file=_write_file, nm_active=None) -> None:
    """Tell NetworkManager to leave the segment alone (so it doesn't fight our addressing).
    Writes the drop-in unconditionally (honored whenever NM (re)starts); reloads NM live via
    nsenter into pid 1 only when NM is actually running."""
    write_file(NM_CONF_PATH, f"[keyfile]\nunmanaged-devices=interface-name:{seg}\n")
    _nm_reload(run, nm_active)


def remove_nm_unmanaged(run=_run, remove_file=None, nm_active=None) -> None:
    """Hand the segment back to NetworkManager when the panel stops managing it.

    Without this the drop-in outlives the panel's ownership and NM refuses to manage the
    interface forever, so an operator who turns `manage_segment` off is left with a segment
    nobody configures.
    """
    (remove_file or _remove_file)(NM_CONF_PATH)
    _nm_reload(run, nm_active)


def ensure_segment_prefix6(store, settings, rand=secrets.token_bytes) -> str:
    """Resolve the segment v6 prefix for the current mode and return it:
    static CIDR -> unchanged; `auto` -> unchanged (the PD client owns it, Phase D);
    blank + v6 on -> generate a stable ULA, persist it, return it."""
    cur = (store.get_setting("segment_ip6") or settings.segment_ip6 or "").strip()
    v6_on = (store.get_setting("ipv6_enabled") or "0") == "1"
    if cur or not v6_on:
        return cur
    _, vid = parse_vlan(store.get_setting("segment_iface") or settings.segment_iface)
    ula = generate_ula_prefix(vid if vid is not None else 0, rand=rand)
    store.set_setting("segment_ip6", ula)
    _log.info("generated stable ULA prefix for the segment: %s", ula)
    return ula


def effective_segment_prefix6(store, settings, rand=secrets.token_bytes) -> str:
    """Return the /64 to install without mutating the configured ``auto`` intent.

    Auto mode prefers a currently delegated /64 and otherwise uses a persistent ULA fallback,
    so client IPv6 remains deterministic while the upstream PD lease is absent or renewing.
    """
    if (store.get_setting("ipv6_enabled") or "0") != "1":
        return ""
    intent = (store.get_setting("segment_ip6") or settings.segment_ip6 or "").strip()
    if intent.lower() != "auto":
        return ensure_segment_prefix6(store, settings, rand=rand)
    delegated = (store.get_setting("pd_segment_prefix6") or "").strip()
    if host_addr6(delegated):
        return delegated
    ula = (store.get_setting("ula_prefix6") or "").strip()
    if not host_addr6(ula):
        _, vid = parse_vlan(store.get_setting("segment_iface") or settings.segment_iface)
        ula = generate_ula_prefix(vid if vid is not None else 0, rand=rand)
        store.set_setting("ula_prefix6", ula)
        _log.info("generated stable ULA fallback for DHCPv6-PD: %s", ula)
    return ula


# --- orchestrator --------------------------------------------------------------

def _is_linux_backend(net) -> bool:
    """The real host backend carries the `_run` shell-out seam; DryRun (dev/CI) does not."""
    return hasattr(net, "_run")


def _set_result(state, result: NetResult) -> NetResult:
    state.provision_result = result
    return result


def _pd_callback(state, run):
    """Build the callback used by PdClient's prefix-file watcher."""
    def changed(delegated: str | None) -> None:
        from pi_gw_panel.controller import apply_lock
        from pi_gw_panel.net_control.pd_client import derive_segment_prefix
        with apply_lock:
            store, settings = state.store, state.settings
            # Ignore a late hook notification after auto mode (or segment management as a
            # whole) has been disabled — otherwise this re-adds the addresses and restarts
            # the dnsmasq that the disable path just tore down.
            if ((store.get_setting("manage_segment") or "1") != "1"
                    or (store.get_setting("ipv6_enabled") or "0") != "1"
                    or (store.get_setting("segment_ip6") or "").strip().lower() != "auto"):
                return
            _, vid = parse_vlan(store.get_setting("segment_iface") or settings.segment_iface)
            selected = derive_segment_prefix(delegated, vid or 0) if delegated else None
            if delegated and selected is None:
                _log.warning("ignoring unusable delegated IPv6 prefix: %s", delegated)
                return
            try:
                store.set_setting("pd_segment_prefix6", selected or "")
                plan = NetPlan.from_store(store, settings)
                plan.segment_ip6 = effective_segment_prefix6(store, settings)
                reconcile_segment_addresses(store, plan, run=run)
                dnsmasq = getattr(state, "dnsmasq", None)
                if dnsmasq is not None and (store.get_setting("manage_dnsmasq") or "1") == "1":
                    dnsmasq.apply(render_dnsmasq(plan))
                _set_result(state, NetResult(ok=True))
            except Exception as exc:
                _set_result(state, NetResult(ok=False, error=f"PD prefix apply failed: {exc}"))
                raise
    return changed


def _stop_pd(pd) -> None:
    """Stop the PD client and discard its last delegation. Never raises."""
    try:
        pd.stop()
        clear_state = getattr(pd, "clear_state", None)
        if clear_state is not None:
            clear_state()
    except Exception as exc:
        _log.warning("stopping the DHCPv6-PD client failed: %s", exc)


def host_provision(state) -> NetResult:
    """Idempotent host gateway bring-up. Gated on the linux backend + `manage_segment`.
    Never raises out — a provisioning failure is logged, not fatal to boot. Re-entrant under
    the controller apply-lock so it can't interleave with a tunnel apply."""
    store, settings = state.store, state.settings
    if not _is_linux_backend(state.net):
        return _set_result(state, NetResult(ok=True))
    from pi_gw_panel.controller import apply_lock
    run = getattr(state.net, "_run", _run)
    pd = getattr(state, "pd_client", None)
    stop_pd = False
    with apply_lock:
        try:
            dnsmasq = getattr(state, "dnsmasq", None)
            if (store.get_setting("manage_segment") or "1") != "1":
                stop_pd = True
                store.set_setting("pd_segment_prefix6", "")
                if dnsmasq is not None:
                    dnsmasq.stop()
                clear_managed_addresses(store, run=run)
                clear_managed_link(store, run=run)
                remove_nm_unmanaged(run=run)
                result = _set_result(state, NetResult(ok=True))
            else:
                ensure_sysctls(settings)
                ensure_segment_prefix6(store, settings)
                plan = NetPlan.from_store(store, settings)
                plan.segment_ip6 = effective_segment_prefix6(store, settings)
                ensure_segment_link(store, plan, run=run)
                reconcile_segment_addresses(store, plan, run=run)
                ensure_nm_unmanaged(plan.segment_iface, run=run)
                auto_pd = (plan.ipv6_enabled
                           and (store.get_setting("segment_ip6") or "").strip().lower() == "auto")
                if pd is not None:
                    if auto_pd:
                        set_callback = getattr(pd, "set_callback", None)
                        if set_callback is not None:
                            set_callback(_pd_callback(state, run))
                        pd.start()
                    else:
                        stop_pd = True
                        store.set_setting("pd_segment_prefix6", "")
                if dnsmasq is not None and (store.get_setting("manage_dnsmasq") or "1") == "1":
                    dnsmasq.apply(render_dnsmasq(plan))
                elif dnsmasq is not None:
                    dnsmasq.stop()
                result = _set_result(state, NetResult(ok=True))
        except Exception as exc:    # never crash boot on a provisioning hiccup
            _log.warning("host_provision failed: %s", exc)
            result = _set_result(state, NetResult(ok=False, error=str(exc)))
    # Outside the apply-lock on purpose: the PD watcher takes that same lock inside its
    # callback, so joining its thread while holding it would block for the whole join
    # timeout. The store state that makes a late callback a no-op is already committed above.
    if stop_pd and pd is not None:
        _stop_pd(pd)
    return result
