import platform
import pytest
from pi_gw_panel.config import Settings
from pi_gw_panel.net_control.plan import NetPlan
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.net_control.factory import select_backend


def test_dryrun_records_rendered_and_applies_nothing():
    be = DryRunBackend()
    res = be.apply_tproxy(NetPlan.from_settings(Settings()))
    assert res.ok is True
    assert "tproxy ip to :52345" in res.rendered
    assert be.applied == [res.rendered]          # recorded, not executed
    assert be.teardown().ok is True
    assert be.applied == []                       # teardown clears


def test_dryrun_records_combined_nft_and_dnsmasq():
    be = DryRunBackend()
    res = be.apply_tproxy(NetPlan.from_settings(Settings()))
    assert len(be.applied) == 1                                            # one combined entry
    assert "tproxy ip to :52345" in res.rendered                          # nft half
    assert "dhcp-range=192.168.10.30,192.168.10.200,12h" in res.rendered  # dnsmasq half
    assert "dhcp-option=3,192.168.10.2" in res.rendered


def test_dryrun_combined_render_includes_killswitch_drop():
    plan = NetPlan.from_settings(Settings())
    plan.kill_switch = True
    res = DryRunBackend().apply_tproxy(plan)
    assert "chain forward" in res.rendered and " drop" in res.rendered


def test_factory_forces_dryrun_via_env(monkeypatch):
    monkeypatch.setenv("PI_GW_NET_BACKEND", "dryrun")
    assert type(select_backend(Settings())).__name__ == "DryRunBackend"


def test_factory_refuses_linux_explicit(monkeypatch):
    monkeypatch.setenv("PI_GW_NET_BACKEND", "linux")
    with pytest.raises(NotImplementedError):
        select_backend(Settings())


@pytest.mark.skipif(platform.system() != "Linux", reason="Linux-only implicit guard")
def test_factory_refuses_linux_implicit(monkeypatch):
    monkeypatch.delenv("PI_GW_NET_BACKEND", raising=False)
    with pytest.raises(NotImplementedError):
        select_backend(Settings())
