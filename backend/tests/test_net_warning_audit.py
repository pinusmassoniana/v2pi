"""A network apply that partially failed must reach the operator, not only the server log.

`NetResult.warning` is produced by the LAN-access step of every apply and used to have no
reader anywhere — not in the controller, not in the API schema, not in the UI. An apply that
installed enforcement but silently failed to place the segment→home-LAN rules therefore looked
exactly like a clean success from the panel. These tests pin the whole channel, end to end,
through the real LinuxBackend (its iptables step is the thing that warns).
"""
import json
import subprocess

from conftest import _login
from fastapi.testclient import TestClient

from pi_gw_panel.app import create_app
from pi_gw_panel.controller import apply_net, stop_net
from pi_gw_panel.net_control import netcheck
from pi_gw_panel.net_control.linux import LinuxBackend
from pi_gw_panel.state import build_state

_RULES = json.dumps([{"priority": 100, "src": "all", "fwmark": "0x40", "table": "100"}])
_ROUTES = json.dumps([{"type": "local", "dst": "default", "dev": "lo", "scope": "host"}])
_LAN_INSERT = ["iptables", "-I", "DOCKER-USER"]


class _Run:
    """LinuxBackend's injectable subprocess seam: every command succeeds (with the read-back
    JSON `_verify_tproxy` requires) except the LAN-access insert while `lan_fails` is set —
    exactly the shape of a host with no DOCKER-USER chain."""

    def __init__(self, lan_fails: bool = True, nft_fails: bool = False):
        self.lan_fails = lan_fails
        self.nft_fails = nft_fails

    def __call__(self, cmd, input=None):
        if self.nft_fails and cmd[:2] == ["nft", "-f"]:
            raise subprocess.CalledProcessError(1, cmd, stderr="nft: syntax error")
        if self.lan_fails and cmd[:3] == _LAN_INSERT:
            raise subprocess.CalledProcessError(
                1, cmd, stderr="iptables: No chain/target/match by that name.")
        stdout = ""
        if cmd[-2:] == ["rule", "show"]:
            stdout = _RULES
        elif cmd[-4:] == ["route", "show", "table", "100"]:
            stdout = _ROUTES
        return subprocess.CompletedProcess(cmd, 0, stdout, "")


def _linux_client(settings, stub_xray, monkeypatch, run):
    """Real app + real LinuxBackend over the fake runner. The uplink probe is stubbed because
    /api/network probes it for real on a Linux backend, and tests never touch the network."""
    monkeypatch.setattr(netcheck, "uplink_up", lambda *args, **kwargs: None)
    settings.xray_bin = stub_xray
    state = build_state(settings, net=LinuxBackend(settings, run=run,
                                                   write_proc=lambda path, value: True))
    client = TestClient(create_app(settings, state=state))
    _login(client)
    return client, state


def test_a_failed_lan_access_step_reaches_the_api_and_still_reports_a_successful_apply(
        settings, stub_xray, monkeypatch):
    client, state = _linux_client(settings, stub_xray, monkeypatch, _Run())

    result = apply_net(settings, state.net, state.store)
    assert result.ok is True                              # LAN access is secondary, never fatal
    assert "LAN access chain not applied" in result.warning

    status = client.get("/api/network").json()["status"]
    # The warning is visible…
    assert "LAN access chain not applied" in status["enforcement_warning"]
    # …and did NOT turn a working, enforced network into a reported failure.
    assert status["enforcement_status"] == "ok"
    assert status["enforcement_error"] == ""
    assert status["wan_blocked"] is False


def test_a_clean_apply_reports_no_warning_at_all(settings, stub_xray, monkeypatch):
    client, state = _linux_client(settings, stub_xray, monkeypatch, _Run(lan_fails=False))
    assert apply_net(settings, state.net, state.store).ok is True
    assert client.get("/api/network").json()["status"]["enforcement_warning"] == ""


def test_the_warning_describes_the_last_apply_and_is_not_sticky(
        settings, stub_xray, monkeypatch):
    # A warning that outlived the condition would be worse than none: the operator fixes the
    # host, re-applies, and the panel keeps accusing it.
    run = _Run()
    client, state = _linux_client(settings, stub_xray, monkeypatch, run)
    apply_net(settings, state.net, state.store)
    assert client.get("/api/network").json()["status"]["enforcement_warning"] != ""

    run.lan_fails = False
    apply_net(settings, state.net, state.store)
    assert client.get("/api/network").json()["status"]["enforcement_warning"] == ""


def test_a_failed_apply_leaves_no_warning_behind(settings, stub_xray, monkeypatch):
    # A failure reports through enforcement_error. Keeping the previous apply's warning next
    # to it would attribute a stale, unrelated cause to the failure.
    run = _Run()
    client, state = _linux_client(settings, stub_xray, monkeypatch, run)
    apply_net(settings, state.net, state.store)
    assert client.get("/api/network").json()["status"]["enforcement_warning"] != ""

    run.nft_fails = True
    assert apply_net(settings, state.net, state.store).ok is False
    status = client.get("/api/network").json()["status"]
    assert status["enforcement_status"] == "error"
    assert status["enforcement_warning"] == ""


def test_stopping_the_network_clears_a_previous_apply_warning(
        settings, stub_xray, monkeypatch):
    run = _Run()
    client, state = _linux_client(settings, stub_xray, monkeypatch, run)
    apply_net(settings, state.net, state.store)
    assert client.get("/api/network").json()["status"]["enforcement_warning"] != ""

    run.lan_fails = False
    assert stop_net(settings, state.net, state.store).ok is True
    assert client.get("/api/network").json()["status"]["enforcement_warning"] == ""


def test_network_status_reports_no_warning_before_any_apply(settings, stub_xray, monkeypatch):
    client, _state = _linux_client(settings, stub_xray, monkeypatch, _Run())
    # A fresh process has applied nothing yet: the field exists and is empty, never absent.
    assert client.get("/api/network").json()["status"]["enforcement_warning"] == ""
