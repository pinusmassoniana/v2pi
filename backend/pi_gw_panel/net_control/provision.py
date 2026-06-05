"""Self-provisioning gateway: bring the whole host gateway up from settings, idempotently.

Gated on the linux backend (a net backend carrying a `_run` seam) + the `manage_segment`
setting. Every side-effect goes through an injectable seam — `run` for shell-outs,
`write_proc`/`write_file` for /proc + conf files — so the command/file emission is unit-tested
with no root or Pi. The real apply reuses the LinuxBackend runner so it shares logging."""
import ipaddress
import logging
import secrets
import subprocess

from pi_gw_panel.net_control.plan import NetPlan
from pi_gw_panel.net_control.render import render_dnsmasq

_log = logging.getLogger("pi_gw_panel")

NM_CONF_PATH = "/etc/NetworkManager/conf.d/99-v2pi.conf"


def _run(cmd: list[str], input: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=input, capture_output=True, text=True, check=True)


def _write_proc(path: str, value: str) -> None:
    try:
        with open(path, "w") as f:
            f.write(value)
    except OSError:
        pass


def _write_file(path: str, text: str) -> None:
    try:
        with open(path, "w") as f:
            f.write(text)
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
    return f"{net.network_address + 1}/{net.prefixlen}"


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


def ensure_segment_iface(plan: NetPlan, run=_run, link_exists=None) -> None:
    """Create the segment VLAN if absent, (re)assign its v4 (and v6 when enabled) address, bring
    it up. `ip ... replace` + the existence check make every step idempotent."""
    link_exists = link_exists or (lambda i: _link_exists(i, run))
    seg = plan.segment_iface
    parent, vid = parse_vlan(seg)
    if vid is not None and not link_exists(seg):
        run(["ip", "link", "add", "link", parent, "name", seg, "type", "vlan", "id", str(vid)])
    run(["ip", "addr", "replace", f"{plan.segment_ip}/24", "dev", seg])
    if plan.ipv6_enabled:
        ha6 = host_addr6(plan.segment_ip6)
        if ha6:
            run(["ip", "-6", "addr", "replace", ha6, "dev", seg])
    run(["ip", "link", "set", seg, "up"])


def ensure_nm_unmanaged(seg: str, run=_run, write_file=_write_file, nm_active=None) -> None:
    """Tell NetworkManager to leave the segment alone (so it doesn't fight our addressing).
    Writes the drop-in unconditionally (honored whenever NM (re)starts); reloads NM live via
    nsenter into pid 1 only when NM is actually running."""
    write_file(NM_CONF_PATH, f"[keyfile]\nunmanaged-devices=interface-name:{seg}\n")
    nm_active = nm_active or (lambda: _nm_active(run))
    if nm_active():
        try:
            run(["nsenter", "-t", "1", "-m", "-n", "--", "nmcli", "general", "reload"])
        except (subprocess.CalledProcessError, OSError):
            pass


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


# --- orchestrator --------------------------------------------------------------

def _is_linux_backend(net) -> bool:
    """The real host backend carries the `_run` shell-out seam; DryRun (dev/CI) does not."""
    return hasattr(net, "_run")


def host_provision(state) -> None:
    """Idempotent host gateway bring-up. Gated on the linux backend + `manage_segment`.
    Never raises out — a provisioning failure is logged, not fatal to boot. Re-entrant under
    the controller apply-lock so it can't interleave with a tunnel apply."""
    store, settings = state.store, state.settings
    if not _is_linux_backend(state.net) or (store.get_setting("manage_segment") or "1") != "1":
        return
    from pi_gw_panel.controller import apply_lock
    run = getattr(state.net, "_run", _run)
    with apply_lock:
        try:
            ensure_sysctls(settings)
            ensure_segment_prefix6(store, settings)
            plan = NetPlan.from_store(store, settings)
            ensure_segment_iface(plan, run=run)
            ensure_nm_unmanaged(plan.segment_iface, run=run)
            pd = getattr(state, "pd_client", None)
            if pd is not None and (store.get_setting("segment_ip6") or "").strip().lower() == "auto":
                pd.start()       # Phase D: acquire a delegated prefix on the uplink
            dnsmasq = getattr(state, "dnsmasq", None)
            if dnsmasq is not None and (store.get_setting("manage_dnsmasq") or "1") == "1":
                dnsmasq.apply(render_dnsmasq(plan))
        except Exception as exc:    # never crash boot on a provisioning hiccup
            _log.warning("host_provision failed: %s", exc)
