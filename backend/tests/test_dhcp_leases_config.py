from pi_gw_panel.config import Settings


def test_dnsmasq_leases_default_points_at_pi_gw_leasefile():
    # must match the host's pi-gw-dhcp.service `dhcp-leasefile=/var/lib/misc/pi-gw.leases`
    assert Settings.from_env({}).dnsmasq_leases == "/var/lib/misc/pi-gw.leases"


def test_dnsmasq_leases_env_override():
    s = Settings.from_env({"PI_GW_DNSMASQ_LEASES": "/run/dnsmasq/custom.leases"})
    assert s.dnsmasq_leases == "/run/dnsmasq/custom.leases"
