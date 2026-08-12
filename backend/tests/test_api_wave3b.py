import subprocess

from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.net_control.plan import NetResult
from conftest import _client, _login


def test_get_network_shape(settings, stub_xray):
    c = _client(settings, stub_xray)
    _login(c)
    body = c.get("/api/network").json()
    assert body["segment"] == {"iface": "eth0.2", "ip": "192.168.10.2", "ip6": "",
                               "dhcp_start": "192.168.10.30", "dhcp_end": "192.168.10.200",
                               "dhcp_lease": "12h", "client_dns": "1.1.1.1",
                               "client_dns6": "2606:4700:4700::1111"}
    assert body["kill_switch_enabled"] is True
    assert body["ipv6_enabled"] is False
    assert body["status"]["segment_up"] is None             # dev: no Linux sysfs
    assert body["status"]["dhcp_clients"] == 0
    assert set(body["status"]["tunnel"]) == {"real_ok", "latency_ms", "egress_ip", "checked_at"}
    assert body["status"]["clients"] == []
    assert len(body["recommendations"]) >= 1


def test_put_network_requires_csrf(settings, stub_xray):
    c = _client(settings, stub_xray)
    _login(c)
    assert c.put("/api/network", json={"dhcp_end": "192.168.10.250"}).status_code == 403


def test_put_network_updates_field_and_flips_killswitch(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    body = c.put("/api/network",
                 json={"dhcp_end": "192.168.10.250", "kill_switch_enabled": True}, headers=h).json()
    assert body["segment"]["dhcp_end"] == "192.168.10.250"   # field persisted
    assert body["kill_switch_enabled"] is True
    # A1: kill-switch ON with no tunnel up installs the fail-closed leak-guard (forward drop,
    # NO tproxy pointed at a dead xray port), not the full tproxy render.
    applied = c.app.state.app_state.net.applied[-1]
    assert "chain forward" in applied and " drop" in applied
    assert "tproxy ip to" not in applied


def test_put_network_rejects_empty_and_unknown(settings, stub_xray):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    assert c.put("/api/network", json={"segment_iface": ""}, headers=h).status_code == 422
    assert c.put("/api/network", json={"bogus": "x"}, headers=h).status_code == 422


class _FailGuardNet(DryRunBackend):
    def apply_guard(self, plan):
        return NetResult(ok=False, error="nft denied")


def test_put_network_rolls_back_intent_when_apply_fails(settings, stub_xray):
    settings.xray_bin = stub_xray
    state = build_state(settings, net=_FailGuardNet())
    c = TestClient(create_app(settings, state=state))
    h = {"X-CSRF-Token": _login(c)}
    before = state.store.get_setting("dhcp_end")
    r = c.put("/api/network", json={"dhcp_end": "192.168.10.250"}, headers=h)
    assert r.status_code == 502
    assert state.store.get_setting("dhcp_end") == before


class _FailGuardLinuxNet(_FailGuardNet):
    """The failing-guard backend plus the `_run` seam that makes host provisioning take its
    linux path — so the commands it would issue against the host are recorded, not executed."""

    def __init__(self):
        super().__init__()
        self.cmds: list[list[str]] = []
        self.links = {"eth0", "eth0.2"}          # what already exists on the host

    def _run(self, cmd, **kw):
        self.cmds.append(list(cmd))
        if cmd[:3] == ["ip", "link", "show"] and cmd[3] not in self.links:
            # Speaks like iproute2 does: absence is an explicit not-found on stderr. The panel
            # reads that text (and only that text) as proof the device is not there — a bare
            # non-zero exit is "could not tell", which licenses no deletion at all.
            raise subprocess.CalledProcessError(
                1, cmd, stderr=f'Device "{cmd[3]}" does not exist.')
        if cmd[:3] == ["ip", "link", "add"]:
            self.links.add(cmd[cmd.index("name") + 1])
        if cmd[:3] == ["ip", "link", "delete"]:
            self.links.discard(cmd[3])
        return ""


def test_a_failed_interface_change_takes_its_candidate_host_state_with_it(settings, stub_xray):
    """Host provisioning runs INSIDE the DB transaction, so a later failure rolls its ownership
    metadata back while the interface and address it created stay on the host. Both the recovery
    pass and the readiness check then read the restored metadata — which names the OLD interface,
    so a candidate one is invisible to the panel and outside the nft guard scoped to the old
    interface. The candidate has to be recorded outside the transaction and undone explicitly."""
    settings.xray_bin = stub_xray
    state = build_state(settings, net=_FailGuardLinuxNet())
    state.dnsmasq = state.pd_client = None      # keep this about the address/link ownership
    c = TestClient(create_app(settings, state=state))
    h = {"X-CSRF-Token": _login(c)}

    r = c.put("/api/network", json={"segment_iface": "eth0.9"}, headers=h)
    assert r.status_code == 502
    assert c.get("/api/network").json()["segment"]["iface"] == "eth0.2"   # intent rolled back
    assert ["ip", "link", "add", "link", "eth0", "name", "eth0.9",
            "type", "vlan", "id", "9"] in state.net.cmds               # it really was created
    assert ["ip", "link", "delete", "eth0.9"] in state.net.cmds, \
        "the candidate interface was left on the host with nothing recording it"
    assert "eth0.9" not in state.net.links
    assert "eth0.9" in r.json()["detail"]                              # reported, not silent
    assert state.store.get_setting("pending_provision_undo") == ""     # nothing left pending
    # the interface we went back to keeps its address
    assert ["ip", "addr", "replace", "192.168.10.2/24", "dev", "eth0.2"] in state.net.cmds
    assert not any(cmd[:3] == ["ip", "addr", "del"] and cmd[-1] == "eth0.2"
                   and cmd[3] == "192.168.10.2/24" for cmd in state.net.cmds[-4:])
