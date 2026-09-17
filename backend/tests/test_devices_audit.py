"""A4 + B1: pinned devices — the dnsmasq lines, the routing override, the nft counters and the
per-minute accounting.

Everything here touches the live segment's configuration, so the tests that matter are the ones
about a bad row: a MAC `dnsmasq --test` would happily accept, a reservation restored onto a
gateway serving a different segment, and counters that reset under the sampler's feet.
"""
import pytest

from pi_gw_panel.devices import DeviceSampler, deltas
from pi_gw_panel.models import Reservation, RoutingRule
from pi_gw_panel.net_control.plan import NetPlan
from pi_gw_panel.net_control.render import (applied_reservations, counter_names, render_dnsmasq,
                                            render_nft, reservation_issue, reservation_value_issue)
from pi_gw_panel.xray_config.routing import rules_to_xray, validate_rule

BASE = dict(tproxy_port=52345, fwmark=1, egress_mark=2, table=100, segment_iface="eth0.2",
            segment_ip="192.168.10.1", dhcp_start="192.168.10.50", dhcp_end="192.168.10.200",
            dhcp_lease="12h", client_dns="1.1.1.1", mgmt_ip="192.168.1.120")
TV = ("aa:bb:cc:dd:ee:ff", "192.168.10.42", "tv")
CONSOLE = ("aa:bb:cc:dd:ee:01", "192.168.10.43", "console")


def plan(**over) -> NetPlan:
    return NetPlan(**{**BASE, **over})


# --- what may be pinned ---

@pytest.mark.parametrize("mac,ip,name,expected", [
    ("aa:bb:cc:dd:ee:ff", "192.168.10.42", "tv", None),
    ("AA:BB:CC:DD:EE:FF", "192.168.10.42", "tv", None),                  # case is normalised
    ("nonsense", "192.168.10.42", "tv", "MAC must look like"),
    ("aa:bb:cc:dd:ee:ff", "not-an-ip", "tv", "not an IPv4 address"),
    ("aa:bb:cc:dd:ee:ff", "fd00::1", "tv", "IPv4 only"),
    ("aa:bb:cc:dd:ee:ff", "192.168.10.42", "living room", "letters, digits and hyphens"),
    ("aa:bb:cc:dd:ee:ff", "192.168.10.42", "tv\ndhcp-script=/x.sh", "letters, digits and hyphens"),
])
def test_the_shape_checks_are_ours_because_dnsmasq_accepts_nonsense(mac, ip, name, expected):
    """`dnsmasq --test` passes `dhcp-host=nonsense,...` — it reads the token as a client id — so
    nothing downstream would catch a malformed MAC. Verified against the real binary."""
    problem = reservation_value_issue(mac, ip, name)
    assert (problem is None) if expected is None else (expected in problem)


def test_a_reservation_must_belong_to_this_segment_and_not_be_the_gateway():
    assert reservation_issue("aa:bb:cc:dd:ee:ff", "192.168.11.9", "", plan()) is not None
    assert "gateway's own address" in reservation_issue("aa:bb:cc:dd:ee:ff", "192.168.10.1", "", plan())
    assert reservation_issue("aa:bb:cc:dd:ee:ff", "192.168.10.42", "", plan()) is None


# --- what the host ends up with ---

def test_pinned_devices_render_one_dhcp_host_line_each():
    text = render_dnsmasq(plan(reservations=(TV, ("aa:bb:cc:dd:ee:01", "192.168.10.43", ""))))
    assert "dhcp-host=aa:bb:cc:dd:ee:ff,192.168.10.42,tv" in text
    assert "dhcp-host=aa:bb:cc:dd:ee:01,192.168.10.43\n" in text          # no name, no trailing comma
    assert render_dnsmasq(plan()) == render_dnsmasq(plan(reservations=()))


def test_a_row_this_segment_cannot_serve_is_skipped_not_raised():
    """A restore from a gateway that served 192.168.20.0/24 must not take DHCP down with it."""
    foreign = ("aa:bb:cc:dd:ee:02", "192.168.20.9", "old")
    text = render_dnsmasq(plan(reservations=(TV, foreign)))
    assert "192.168.20.9" not in text and "192.168.10.42" in text
    assert applied_reservations(plan(reservations=(foreign,))) == []


def test_each_pinned_device_gets_a_counter_pair_in_chains_of_its_own():
    with_devices = render_nft(plan(reservations=(TV, CONSOLE)))
    up, down = counter_names("192.168.10.42")
    assert f"counter {up} {{ }}" in with_devices and f"counter {down} {{ }}" in with_devices
    # Accounting lives in its own chains, carries no verdict, and excludes LAN traffic on both
    # sides — what a device does with the LAN is not internet traffic.
    acct_up = with_devices.split("chain acct_up")[1].split("\n    }")[0]
    acct_down = with_devices.split("chain acct_down")[1].split("\n    }")[0]
    assert f"ip saddr 192.168.10.42 counter name {up}" in acct_up
    assert f"ip daddr 192.168.10.42 counter name {down}" in acct_down
    assert acct_up.index("192.168.0.0/16 } return") < acct_up.index("counter name")
    assert acct_down.index("192.168.0.0/16 } return") < acct_down.index("counter name")
    # The enforcement chain is untouched: accounting may never decide where a packet goes.
    prerouting = with_devices.split("chain prerouting")[1].split("\n    }")[0]
    assert "counter" not in prerouting
    # And counting survives a stopped tunnel, which is when "what is still talking?" gets asked.
    stopped = render_nft(plan(reservations=(TV,)), tunnel_up=False)
    assert f"counter name {up}" in stopped and f"counter name {down}" in stopped
    # Pin nothing and the ruleset is byte-for-byte what it was before this feature existed.
    assert render_nft(plan()) == render_nft(plan(reservations=()))
    assert render_nft(plan(), tunnel_up=False) == render_nft(plan(reservations=()), tunnel_up=False)


def test_a_device_rule_matches_the_source_and_refuses_a_v6_address():
    rule = RoutingRule(id=None, position=0, type="device", value="192.168.10.42", action="direct")
    assert rules_to_xray([rule], "proxy")[1] == {
        "type": "field", "outboundTag": "direct", "source": ["192.168.10.42"]}
    assert validate_rule(rule) is None
    # A client's v6 address is SLAAC/privacy — it rotates, so a rule on it would quietly stop.
    assert "IPv4" in validate_rule(
        RoutingRule(id=None, position=0, type="device", value="fd00::5", action="direct"))


# --- the counters, over time ---

def test_a_counter_reset_is_read_as_the_whole_value_not_a_negative():
    """Every net apply reloads the table, which zeroes the counters. Subtracting would report a
    huge negative; the reading after a reset IS what has moved since it."""
    previous = {"dev_up_x": (10, 5_000), "dev_down_x": (10, 9_000)}
    assert deltas(previous, {"dev_up_x": (12, 6_500), "dev_down_x": (12, 9_000)}) == {"dev_up_x": 1_500}
    assert deltas(previous, {"dev_up_x": (1, 400)}) == {"dev_up_x": 400}        # reset
    assert deltas({}, {"dev_up_x": (1, 700)}) == {"dev_up_x": 700}              # first reading


class _Store:
    def __init__(self, reservations):
        self._reservations = reservations
        self.rows: list[tuple] = []

    def list_reservations(self):
        return self._reservations

    def add_device_minute(self, ip, ts_min, up, down):
        self.rows.append((ip, ts_min, up, down))


class _State:
    def __init__(self, store, counters):
        self.store = store
        self.net = type("Net", (), {"read_counters": staticmethod(lambda: counters)})()


def test_the_sampler_writes_one_row_per_device_that_moved():
    reservations = [Reservation(id=1, mac=TV[0], ip=TV[1], name="tv"),
                    Reservation(id=2, mac=CONSOLE[0], ip=CONSOLE[1], name="console")]
    up_tv, down_tv = counter_names(TV[1])
    store = _Store(reservations)
    state = _State(store, {up_tv: (5, 1_000), down_tv: (7, 4_000)})
    sampler = DeviceSampler(state)

    assert sampler.sample_once(now=600) == 1                 # only the TV moved
    assert store.rows == [(TV[1], 10, 1_000, 4_000)]

    # A second reading with the same totals is no traffic at all, not the same bytes again.
    assert sampler.sample_once(now=660) == 0
    assert len(store.rows) == 1


def test_a_gateway_with_no_counter_support_samples_nothing():
    state = _State(_Store([]), {})
    state.net = object()                                     # a backend without read_counters
    assert DeviceSampler(state).sample_once() == 0


def test_usage_and_retention(tmp_path):
    from pi_gw_panel.db import connect, init_schema
    from pi_gw_panel.nodes.store import NodeStore
    conn = connect(str(tmp_path / "t.db"), check_same_thread=False)
    init_schema(conn)
    store = NodeStore(conn)

    store.add_device_minute("192.168.10.42", 1_000, 100, 900)
    store.add_device_minute("192.168.10.42", 1_000, 50, 50)      # same minute: additive
    store.add_device_minute("192.168.10.43", 1_001, 10, 10)
    assert store.device_usage(since_min=0) == [
        {"ip": "192.168.10.42", "up_bytes": 150, "down_bytes": 950},
        {"ip": "192.168.10.43", "up_bytes": 10, "down_bytes": 10},
    ]
    assert store.device_usage(since_min=1_001) == [{"ip": "192.168.10.43", "up_bytes": 10, "down_bytes": 10}]
    assert store.device_minutes("192.168.10.42", 0) == [{"ts_min": 1_000, "up_bytes": 150, "down_bytes": 950}]

    # A lone far-future sample (a clock step, an RTC that came back wrong) must not wipe the
    # history: the prune floor is anchored to the newest sample that ALREADY survived.
    store.add_device_minute("192.168.10.42", 10_000_000, 1, 1)
    assert len(store.device_usage(since_min=0)) == 2
    # A series that genuinely advances past the window does prune.
    store.add_device_minute("192.168.10.42", 10_000_001, 2, 2)
    assert store.device_usage(since_min=0) == [{"ip": "192.168.10.42", "up_bytes": 3, "down_bytes": 3}]
