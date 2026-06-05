from pi_gw_panel.net_control import pd_client


def test_derive_segment_prefix_from_delegation():
    assert pd_client.derive_segment_prefix("2001:db8:ab00::/56", 2) == "2001:db8:ab00:2::/64"
    assert pd_client.derive_segment_prefix("2001:db8:ab00::/56", 0) == "2001:db8:ab00::/64"


def test_derive_segment_prefix_rejects_too_small():
    # a /64 delegation can't be subnetted to a distinct /64 for vlan != 0
    assert pd_client.derive_segment_prefix("2001:db8:0:5::/64", 2) is None
    # but vlan 0 reuses the whole /64
    assert pd_client.derive_segment_prefix("2001:db8:0:5::/64", 0) == "2001:db8:0:5::/64"


def test_derive_segment_prefix_none_on_garbage():
    assert pd_client.derive_segment_prefix("not-a-prefix", 2) is None


class FakeProc:
    def poll(self):
        return None

    def terminate(self):
        self.t = True

    def wait(self, timeout=None):
        pass

    def kill(self):
        pass

    @property
    def pid(self):
        return 99


def test_pd_client_starts_dhclient_on_mgmt_iface():
    spawned = []
    cl = pd_client.PdClient("eth0", "/tmp/pd.sh",
                            popen=lambda cmd: spawned.append(cmd) or FakeProc())
    cl.start()
    assert spawned and spawned[0][0] == "dhclient"
    assert "eth0" in spawned[0]
    assert "-P" in spawned[0] and "-6" in spawned[0]   # DHCPv6 prefix delegation (IA_PD)


def test_pd_client_start_is_idempotent():
    spawned = []
    cl = pd_client.PdClient("eth0", "/tmp/pd.sh",
                            popen=lambda cmd: spawned.append(cmd) or FakeProc())
    cl.start()
    cl.start()
    assert len(spawned) == 1
