from pi_gw_panel.config import Settings


def test_dnsmasq_leases_default_points_under_data_dir():
    # the container's own dnsmasq writes its leasefile into the data volume now
    assert Settings.from_env({}).dnsmasq_leases == "data/dnsmasq.leases"
    assert Settings.from_env({"PI_GW_DATA_DIR": "/data"}).dnsmasq_leases == "/data/dnsmasq.leases"


def test_dnsmasq_leases_env_override():
    s = Settings.from_env({"PI_GW_DNSMASQ_LEASES": "/run/dnsmasq/custom.leases"})
    assert s.dnsmasq_leases == "/run/dnsmasq/custom.leases"
