import ipaddress
import json
import logging
import os
import socket
import subprocess
import time
from pi_gw_panel.config import Settings
from pi_gw_panel.health.snapshot import active_health
from pi_gw_panel.net_control.linux import _run
from pi_gw_panel.net_control.plan import NetPlan
from pi_gw_panel.xray_config.validate import config_digest

_log = logging.getLogger("pi_gw_panel")


def _run_text(cmd: list[str]) -> str:
    """Stdout of one host command, bounded by the same limits as every other shell-out.

    These `ip` calls run on request paths (`/api/network`, `/api/ready`) rather than under the
    apply-lock, so a hung one wedges the worker thread serving that request instead of the whole
    process — but "only one worker at a time" is still unbounded, and readiness is polled. There
    is one runner with one set of limits (`linux.command_timeout`); a second, unbounded one here
    is how the class stayed open after it was closed for `nft`/`ip`/`iptables`. A timeout arrives
    as `CalledProcessError(124, …)`, which is exactly what both callers below already catch.
    """
    return _run(cmd).stdout


def iface_addresses(iface: str, run=_run_text) -> set[str]:
    """Return normalized CIDRs currently assigned to one interface."""
    try:
        text = run(["ip", "-o", "addr", "show", "dev", iface])
    except (subprocess.CalledProcessError, OSError):
        return set()
    found: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        for family in ("inet", "inet6"):
            if family not in parts:
                continue
            pos = parts.index(family)
            if pos + 1 >= len(parts):
                continue
            try:
                found.add(str(ipaddress.ip_interface(parts[pos + 1])))
            except ValueError:
                pass
    return found


def _normalized_address(addr: str) -> str | None:
    try:
        return str(ipaddress.ip_interface(addr))
    except ValueError:
        return None


def _kernel_owned(addr: str) -> bool:
    """Addresses the kernel maintains on any up interface (link-local / loopback / multicast).
    They are never panel-installed, so they can't be drift."""
    try:
        ip = ipaddress.ip_interface(addr).ip
    except ValueError:
        return True
    return ip.is_link_local or ip.is_loopback or ip.is_multicast


def address_drift(expected: set[str], actual: set[str]) -> set[str]:
    """Addresses live on the panel-managed segment interface that the panel did not put there.

    A subset test ("everything we expect is present") cannot see these, so an address stranded
    by a partially-applied reconcile stays invisible to readiness forever. Exact-compare instead:
    while the panel manages the segment it is the only writer of routable addresses on it, so
    an extra one means the recorded ownership and the kernel have diverged.
    """
    return {addr for addr in actual if addr not in expected and not _kernel_owned(addr)}


def _segment_address_detail(iface: str, expected: list[str | None], actual: set[str],
                            drift: set[str]) -> str:
    """Why `segment_addresses` is false, in one line — the same facts the drift log gets.

    Readiness answered with a bare boolean, so the *reason* it computed here was thrown away
    and reached the operator only through the server log: `false` could not be told apart
    into "an address the panel installed is gone" and "an address nobody recorded is
    stranded on the segment", which are different repairs.
    """
    parts: list[str] = []
    if drift:
        parts.append("unexpected " + ", ".join(sorted(drift)))
    missing = sorted(value for value in expected if value is not None and value not in actual)
    if missing:
        parts.append("missing " + ", ".join(missing))
    if any(value is None for value in expected):
        parts.append("no valid managed address is recorded")
    return f"{iface or '?'}: " + ("; ".join(parts) if parts else "address check failed")


# --- what the live xray LOADED, against what the config file now says -----------------
#
# Single-slot memo of the on-disk config's digest, keyed by the file's identity: reading and
# hashing config.json on every `/api/status` would be a re-read every 3s per open dashboard tab
# (`subscribeStatus(3000)`), for a file that changes only when an apply/revocation rewrites it.
# The key is (inode, mtime_ns, size): every panel write goes through tempfile + `os.replace`
# (`ConfigManager._write_atomic`), so the inode changes on each one, and a hand-edit over SSH moves
# mtime_ns and usually size too. Concurrent readers may both recompute — the slot is replaced
# with one immutable tuple, never mutated in place — which is wasted work, never a wrong answer.
_disk_digest_memo: tuple[str, tuple[int, int, int], str | None] | None = None


def disk_config_digest(path: str) -> str | None:
    """`config_digest` of the config file as it is on disk NOW, or None if it cannot be read.

    None means "cannot tell", and callers must keep it distinct from a digest: a truncated or
    deleted config is not evidence that the running process is serving the right thing.
    """
    global _disk_digest_memo
    if not path:
        return None
    try:
        info = os.stat(path)
    except OSError:
        return None
    key = (info.st_ino, info.st_mtime_ns, info.st_size)
    memo = _disk_digest_memo
    if memo is not None and memo[0] == path and memo[1] == key:
        return memo[2]
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        digest = None
    else:
        digest = config_digest(cfg) if isinstance(cfg, dict) else None
    _disk_digest_memo = (path, key, digest)
    return digest


def config_drift(supervisor, config_path: str = "") -> tuple[str, str]:
    """Is the running xray serving the config that is on disk? → (verdict, one-line detail).

    "drift"   — proven divergence: the process loaded one config and the file now hashes to
                another. Nothing reloaded it, so a credential the operator revoked minutes ago
                is still being admitted; this is the state the whole comparison exists for.
    "ok"      — both digests are known and equal.
    "unknown" — either digest is missing: nothing has been started yet (the normal state at
                boot), the config could not be parsed when it was loaded, or it cannot be read
                now. Deliberately NOT folded into "ok": an absent digest is not evidence of a
                match, and reporting it as one is exactly the silence this replaces.

    The comparison is `config_digest`-to-`config_digest` on both sides — the supervisor records
    the loaded one with that same function — so it is stable under reformatting and comparable
    to the digests used for apply provenance.
    """
    try:
        loaded = supervisor.status().get("loaded_config_digest")
    except Exception:
        return "unknown", ""
    path = config_path or getattr(supervisor, "config_path", "") or ""
    on_disk = disk_config_digest(path)
    if not loaded or not on_disk:
        return "unknown", ""
    if loaded == on_disk:
        return "ok", ""
    return "drift", (
        f"{path} was rewritten after the running xray loaded it "
        f"(loaded {loaded[:12]}, on disk {on_disk[:12]}) — the live process is still serving "
        "the older config; restart or reload xray to apply it")


def readiness_checks(state, address_reader=iface_addresses,
                     details: dict[str, str] | None = None) -> dict[str, bool]:
    """Truthful gateway readiness, stricter than process liveness.

    A migration may commit only when every layer needed to carry client traffic through the
    intended active tunnel is confirmed. Failures and unavailable host probes are fail-closed.

    `details`, when given, is filled with a one-line explanation per failed check that has
    one. It is strictly additive: the returned booleans keep their exact names and meaning,
    because the migration script commits on them.
    """
    store = state.store
    provision_result = getattr(state, "provision_result", None)
    provisioning = bool(provision_result is not None and getattr(provision_result, "ok", False))

    manage_segment = (store.get_setting("manage_segment") or "1") == "1"
    iface = store.get_setting("managed_segment_iface") or ""
    try:
        actual = address_reader(iface) if manage_segment and iface else set()
    except Exception:
        actual = set()
    actual = {normalized for value in actual if (normalized := _normalized_address(value))}
    expected = [_normalized_address(store.get_setting("managed_segment_addr4") or "")]
    if (store.get_setting("ipv6_enabled") or "0") == "1":
        expected.append(_normalized_address(store.get_setting("managed_segment_addr6") or ""))
    if not manage_segment:
        # Running on a host-provisioned segment is a supported mode, not a failure: there are no
        # panel-owned addresses to confirm. Reporting it as failed pins /api/ready at 503 for
        # good and makes the migration script roll a perfectly healthy cutover back.
        segment_addresses = True
    else:
        complete = all(value is not None and value in actual for value in expected)
        drift = address_drift({v for v in expected if v is not None}, actual)
        if drift:
            _log.warning("segment address drift on %s: unexpected %s", iface, sorted(drift))
        segment_addresses = bool(expected and complete and not drift)
        if details is not None and not segment_addresses:
            details["segment_addresses"] = _segment_address_detail(
                iface, expected, actual, drift)

    dns = getattr(state, "dnsmasq", None)
    try:
        dnsmasq = bool(
            (store.get_setting("manage_dnsmasq") or "1") == "1"
            and dns is not None and dns.status().get("running"))
    except Exception:
        dnsmasq = False

    net = state.net
    enforcement = bool(
        getattr(net, "enforcement_status", "unknown") == "ok"
        and getattr(net, "wan_blocked", None) is False)

    active_value = store.get_setting("active_node_id")
    try:
        active_id = int(active_value) if active_value else None
    except (TypeError, ValueError):
        active_id = None
    try:
        active_node = active_id is not None and store.get_node(active_id) is not None
    except Exception:
        active_node = False

    try:
        xray = bool(state.supervisor.status().get("running"))
    except Exception:
        xray = False
    # A process that is up is not the same as a process that is serving the current config: a
    # rewrite nobody reloaded (hand-edit, restored backup, an apply whose reload threw) leaves
    # `xray: true` while the old config — and the credential it still admits — stays live.
    # Only a PROVEN divergence fails: "unknown" is the normal state before anything has been
    # started, and failing closed on it would pin /api/ready at 503 on a healthy boot and make
    # the migration script roll a good cutover back (the same reasoning as `manage_segment=0`
    # above). Unknown is still reported as unknown on /api/status — it is never called a match.
    # No try here on purpose: `config_drift` answers "unknown" for every failure it can meet
    # (a supervisor that raises, an unreadable/unparseable file), so it cannot raise out.
    drift, drift_detail = config_drift(state.supervisor)
    xray_config = drift != "drift"
    if details is not None and not xray_config:
        details["xray_config"] = drift_detail
    try:
        health = active_health(store)
    except Exception:
        health = None
    tunnel = bool(
        health is not None and health.get("real_ok") is True and health.get("stale") is False)
    return {
        "provisioning": provisioning,
        "segment_addresses": segment_addresses,
        "dnsmasq": dnsmasq,
        "enforcement": enforcement,
        "active_node": active_node,
        "xray": xray,
        "xray_config": xray_config,
        "tunnel": tunnel,
    }


def _iface_mac(iface: str, sysfs: str = "/sys/class/net") -> str | None:
    try:
        with open(os.path.join(sysfs, iface, "address")) as f:
            return f.read().strip().lower()
    except OSError:
        return None


def foreign_ra(iface: str, run=_run_text, own_mac=None) -> bool | None:
    """Detect *another* router advertising on the client segment (the leak we hit with the
    Keenetic): an IPv6 neighbor on `iface` flagged `router` whose link-layer address is NOT the
    segment's own. The Pi advertises RA itself, and on a hairpinning L2 the kernel can list the
    Pi's own RA back as a `router` neighbor — that's not foreign, so we exclude our own MAC.
    None when we can't tell (dev / `ip` absent). `run`/`own_mac` are injectable seams for tests."""
    try:
        text = run(["ip", "-6", "neigh", "show", "dev", iface])
    except (subprocess.CalledProcessError, OSError):
        return None
    own = (own_mac if own_mac is not None else _iface_mac(iface)) or ""
    own = own.lower()
    for line in text.splitlines():
        parts = line.split()
        if "router" not in parts[2:]:
            continue
        mac = parts[parts.index("lladdr") + 1].lower() if "lladdr" in parts else ""
        if own and mac == own:        # our own RA reflected back (hairpin) — not foreign
            continue
        return True
    return False


def segment_prefix6(iface: str, proc_path: str = "/proc/net/if_inet6", read=None) -> str | None:
    """The segment iface's GLOBAL IPv6 address/prefix as the host sees it — used by the
    DHCPv6-PD `auto` mode to *observe* the prefix delegated to the segment (the PD client +
    RA run on the host, the panel just reads the result). Parses `/proc/net/if_inet6`
    (`<32hex-addr> <ifidx> <plen-hex> <scope-hex> <flags> <name>`; scope 00 = global). Returns
    e.g. `2001:db8:0:2::1/64`, or None when there's no global v6 (dev / not delegated yet).
    `read` is an injectable seam for tests."""
    try:
        text = read() if read is not None else open(proc_path).read()
    except OSError:
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 6 or parts[5] != iface or parts[3] != "00":   # match iface + global scope
            continue
        try:
            if int(parts[4], 16) & 0x01:      # F: skip IFA_F_TEMPORARY (privacy) addresses
                continue
            addr = ipaddress.IPv6Address(int(parts[0], 16))
            return f"{addr.compressed}/{int(parts[2], 16)}"
        except ValueError:
            continue
    return None


def uplink_up(host: str = "1.1.1.1", port: int = 443, timeout: float = 1.5,
              connect=socket.create_connection) -> bool:
    """Direct TCP reachability of the WAN/uplink (the Pi's Home leg), bypassing the tunnel
    (C1). Lets the UI tell a bad node (tunnel red, uplink green) from a dead internet
    (both red). Only invoked on the real Pi backend — dev/CI report 'unknown'."""
    try:
        connect((host, port), timeout).close()
        return True
    except OSError:
        return False


def segment_up(iface: str, sysfs: str = "/sys/class/net") -> bool | None:
    """Read ``<sysfs>/<iface>/operstate``. True/False on up/down; None when the
    path is absent (dev/macOS — no Linux sysfs)."""
    try:
        with open(os.path.join(sysfs, iface, "operstate")) as f:
            return f.read().strip() == "up"
    except OSError:
        return None


def dhcp_leases(leases_path: str, now: float | None = None) -> list[dict]:
    """Parse the dnsmasq leases file → unexpired leases ``[{ip, mac, hostname, expiry}]``
    (P5). Each line is ``<expiry_epoch> <mac> <ip> <hostname> <client-id>``; expiry 0 means
    no expiry. Expired leases are dropped (audit F4). Empty/absent file → []."""
    now = time.time() if now is None else now
    out: list[dict] = []
    try:
        with open(leases_path) as f:
            for line in f:
                parts = line.split()
                if len(parts) < 4:
                    continue
                try:
                    expiry = int(parts[0])
                except ValueError:
                    continue
                if expiry != 0 and expiry < now:
                    continue                                   # lease has expired
                host = parts[3] if parts[3] != "*" else ""
                out.append({"ip": parts[2], "mac": parts[1], "hostname": host, "expiry": expiry})
    except OSError:
        return []
    return out


def _tunnel(store) -> dict:
    """Active node's tunnel egress + freshness via the shared health snapshot (F3)."""
    a = active_health(store)
    if a is None:
        return {"real_ok": None, "latency_ms": None, "egress_ip": None, "checked_at": None}
    return {"real_ok": a["real_ok"], "latency_ms": a["latency_ms"],
            "egress_ip": a["egress_ip"], "checked_at": a["checked_at"]}


def last_net_warning(store) -> str:
    """The last network apply's non-fatal warning, as recorded by the controller.

    A `NetResult.warning` (e.g. "LAN access chain not applied") used to have no reader at
    all: an apply that installed enforcement but silently failed to place the LAN-access
    rules looked exactly like a clean success everywhere except the server log. The
    controller parks the last apply's warning on the (long-lived, in-memory) store next to
    the rest of its enforcement snapshot — nothing is persisted, so a fresh process starts
    with no warning and the very first apply sets the truthful value.
    """
    return getattr(store, "last_net_warning", "") or ""


def network_status(store, settings: Settings, *, sysfs: str = "/sys/class/net",
                   leases_path: str | None = None, uplink_check=lambda: None) -> dict:
    """Live gateway status: segment link, uplink, DHCP clients (+ list), tunnel egress.
    Real checks are Pi-only; dev returns unknown/0 gracefully (paths/probes injected in
    tests). `uplink_check` defaults to 'unknown' so a bare call never touches the network —
    the route passes the real probe only on the Linux backend (C1)."""
    iface = store.get_setting("segment_iface") or settings.segment_iface
    clients = dhcp_leases(leases_path or settings.dnsmasq_leases)
    return {
        "segment_up": segment_up(iface, sysfs=sysfs),
        "uplink": uplink_check(),
        "dhcp_clients": len(clients),
        "clients": clients,
        "tunnel": _tunnel(store),
        # A partially-applied network is a success WITH a warning; it must reach the operator
        # without reading as a failed apply (see `enforcement_error` for that).
        "enforcement_warning": last_net_warning(store),
    }


def router_recommendations(settings: Settings | NetPlan, ipv6_enabled: bool = False,
                           segment_ip6: str = "") -> list[dict]:
    """Static, config-derived guidance for the one box the panel never touches —
    the router. The live-status panel verifies the result visually. When the IPv6 tunnel is
    on, append the v6 prefix/RA guidance (the panel tunnels v6 but RA is host/router-managed)."""
    # Accept the effective store-resolved NetPlan as well as immutable Settings for callers/tests.
    iface = settings.segment_iface
    vlan = iface.split(".")[-1] if "." in iface else "?"
    recs = [
        {"title": f"Create VLAN {vlan}",
         "detail": f"Add VLAN {vlan} on the router and tag the client switch port to it "
                   f"(the Pi's client leg is {iface})."},
        {"title": f"Disable the router's DHCP on VLAN {vlan}",
         "detail": f"The Pi serves DHCP + DNS on this segment ({settings.dhcp_start}–{settings.dhcp_end}); "
                   f"two DHCP servers on one VLAN conflict."},
        {"title": "Give the Pi's Home leg internet",
         "detail": f"The Pi reaches the tunnel through its Home leg {settings.mgmt_iface} "
                   f"({settings.mgmt_ip}); keep that port on your normal LAN with internet access."},
    ]
    if ipv6_enabled:
        if segment_ip6.strip().lower() == "auto":
            first = {"title": "Enable DHCPv6-PD on the router",
                     "detail": f"`auto` mode: the panel runs a DHCPv6-PD client (dhclient -6 -P) on "
                               f"{settings.mgmt_iface} and requests a prefix — enable prefix "
                               f"delegation on the router; the panel shows it once it lands."}
        else:
            prefix = segment_ip6 or "your /64"
            first = {"title": f"Delegate an IPv6 /64 to VLAN {vlan}",
                     "detail": f"Route a v6 /64 to this segment — DHCPv6-PD on the router, or a "
                               f"static route of {prefix} to the Pi's Home leg {settings.mgmt_iface}. "
                               f"(Set the prefix to `auto` to read it from a host PD client instead.)"}
        recs += [
            first,
            {"title": f"Disable the router's IPv6 / Router Advertisement on VLAN {vlan}",
             "detail": "The Pi advertises IPv6 (RA) on this segment itself; a second router "
                       "advertising its ISP prefix here makes clients leak around the tunnel "
                       "(they'd pick the router's prefix, not the Pi's)."},
            {"title": "Use a node with IPv6 egress",
             "detail": "v6 traffic exits via the active node; pick one with working IPv6 or v6-only "
                       "sites will fail (no leak — they just won't connect)."},
        ]
    return recs
