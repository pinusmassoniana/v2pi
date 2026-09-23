"""A5: "where would this go?", asked of the running xray.

The client-side tester the panel already had cannot answer for geoip/geosite (the data is on the
gateway) or for IPv6. This asks the router itself. The shape of the request is the whole risk —
addresses travel as raw bytes and the network has to be named — so that is what these pin, along
with every way the answer can be unavailable.
"""
import grpc
import pytest

from conftest import _client, _login
from pi_gw_panel.routing_api.client import NETWORKS, RoutingClient, RouteTestUnavailable
from pi_gw_panel.routing_api.proto import network_pb2


class _Stub:
    """Stands in for the gRPC stub: records the request, answers with a tag."""

    def __init__(self, tag="proxy", raises=None):
        self.tag = tag
        self.raises = raises
        self.seen = []

    def TestRoute(self, request, timeout=None):
        self.seen.append((request, timeout))
        if self.raises is not None:
            raise self.raises
        return type("Answer", (), {"OutboundTag": self.tag})()


class _RpcError(grpc.RpcError):
    def details(self):
        return "UNIMPLEMENTED: unknown service"


def test_a_domain_test_names_the_network_because_every_ruleset_ends_in_a_catch_all():
    stub = _Stub("block")
    client = RoutingClient("127.0.0.1:0", stub_factory=lambda: stub)

    assert client.test_route(domain="ads.example.com", port=443) == "block"

    request, timeout = stub.seen[0]
    context = request.RoutingContext
    assert context.TargetDomain == "ads.example.com" and context.TargetPort == 443
    assert context.Network == network_pb2.TCP           # not Unknown: the catch-all is tcp,udp
    assert context.InboundTag == "tproxy-in"
    assert request.PublishResult is False               # a question, not traffic to report
    assert timeout and timeout <= 5


def test_addresses_travel_as_raw_bytes_v4_and_v6():
    stub = _Stub("direct")
    client = RoutingClient("127.0.0.1:0", stub_factory=lambda: stub)

    client.test_route(ip="192.168.1.5", port=80, network="udp", source_ip="192.168.10.42")
    context = stub.seen[0][0].RoutingContext
    assert context.TargetIPs == [b"\xc0\xa8\x01\x05"]           # 4 bytes, not "192.168.1.5"
    assert context.SourceIPs == [b"\xc0\xa8\x0a\x2a"]
    assert context.Network == network_pb2.UDP

    client.test_route(ip="2606:4700:4700::1111", port=443)
    assert len(stub.seen[1][0].RoutingContext.TargetIPs[0]) == 16


def test_the_network_names_are_the_ones_the_api_accepts():
    assert set(NETWORKS) == {"tcp", "udp"}


def test_an_rpc_failure_is_typed_and_drops_the_channel():
    stub = _Stub(raises=_RpcError())
    client = RoutingClient("127.0.0.1:0", stub_factory=lambda: stub)
    with pytest.raises(RouteTestUnavailable, match="UNIMPLEMENTED"):
        client.test_route(domain="x.example")
    assert client._stub is None            # the next call rebuilds rather than reusing a dead one


def test_a_bad_address_is_a_value_error_not_a_call():
    stub = _Stub()
    client = RoutingClient("127.0.0.1:0", stub_factory=lambda: stub)
    with pytest.raises(ValueError):
        client.test_route(ip="not-an-ip")
    assert stub.seen == []


# --- over the API ---

def _ask(c, headers, **body):
    return c.post("/api/routing/test", json={"destination": "example.com", **body}, headers=headers)


def test_the_route_test_says_why_it_cannot_answer(settings, stub_xray):
    """Every one of these is a normal state, not an error page: the answer says which."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}

    body = _ask(c, h).json()                       # xray is not running in this fixture
    assert body["ok"] is False and "not running" in body["error"]

    assert _ask(c, h, destination="   ").json()["error"].startswith("type a host")


def test_the_answer_carries_what_was_asked(settings, stub_xray, monkeypatch):
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    state = c.app.state.app_state
    monkeypatch.setattr(state.supervisor, "status", lambda: {"running": True})
    asked = {}

    def fake(**kwargs):
        asked.update(kwargs)
        return "proxy"

    monkeypatch.setattr(state.routing_client, "test_route", fake)

    body = _ask(c, h, destination="[2606:4700:4700::1111]:8443", network="udp",
                source_ip="192.168.10.42").json()

    assert body == {"ok": True, "outbound": "proxy", "host": "2606:4700:4700::1111", "port": 8443,
                    "network": "udp", "source_ip": "192.168.10.42", "error": ""}
    assert asked == {"ip": "2606:4700:4700::1111", "port": 8443, "network": "udp",
                     "source_ip": "192.168.10.42"}


@pytest.mark.parametrize("destination,host,port", [
    ("example.com", "example.com", 443),
    ("example.com:8080", "example.com", 8080),
    ("1.2.3.4", "1.2.3.4", 443),
    ("1.2.3.4:53", "1.2.3.4", 53),
    ("2606:4700:4700::1111", "2606:4700:4700::1111", 443),      # bare v6: the colons are the address
    ("[2606:4700:4700::1111]:443", "2606:4700:4700::1111", 443),
    # A port that is not 1-65535 is refused (None), never answered for as 443 or clamped.
    ("example.com:not-a-port", "example.com", None),
    ("example.com:70000", "example.com", None),
    ("example.com:0", "example.com", None),
])
def test_destinations_are_split_the_way_the_field_is_typed(destination, host, port):
    from pi_gw_panel.api.routes import _split_destination
    assert _split_destination(destination) == (host, port)


def test_the_test_needs_the_stats_api(settings, stub_xray, monkeypatch):
    """The routing service listens on the stats api inbound, so switching that off takes the live
    test with it — and the panel says so rather than reporting a failed RPC."""
    c = _client(settings, stub_xray)
    h = {"X-CSRF-Token": _login(c)}
    state = c.app.state.app_state
    monkeypatch.setattr(state.supervisor, "status", lambda: {"running": True})
    state.store.set_setting("stats_enabled", "0")

    body = _ask(c, h).json()
    assert body["ok"] is False and "stats API" in body["error"]


def test_the_route_test_is_a_write_so_it_carries_csrf(settings, stub_xray):
    c = _client(settings, stub_xray)
    _login(c)
    assert c.post("/api/routing/test", json={"destination": "example.com"}).status_code == 403
