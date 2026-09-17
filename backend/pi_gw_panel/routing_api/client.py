"""A5: ask the RUNNING xray where a destination would go.

The panel has always had a client-side tester over the staged rules, and it cannot answer for
`geoip:`/`geosite:` (the data lives on the gateway, in files of tens of megabytes) or for IPv6.
xray's own router can: `RoutingService.TestRoute` takes a routing context and returns the outbound
tag it would pick — the same decision a real connection gets, against the config that is live.

Shape notes that cost time if forgotten (verified against the pinned xray 26.3.27):

  * addresses travel as RAW BYTES, 4 or 16 of them, not as strings;
  * the request must name the network, because every ruleset this panel builds ends in a
    `tcp,udp` catch-all — leave it Unknown and the catch-all does not match;
  * `PublishResult` broadcasts the answer to the stats channel. The panel is asking a question,
    not reporting traffic, so it stays false.
"""
import ipaddress
import threading

import grpc

from pi_gw_panel.routing_api.proto import network_pb2
from pi_gw_panel.routing_api.proto import router_command_pb2 as command_pb2
from pi_gw_panel.routing_api.proto import router_command_pb2_grpc as command_pb2_grpc

_TIMEOUT_S = 4.0
NETWORKS = {"tcp": network_pb2.TCP, "udp": network_pb2.UDP}


class RouteTestUnavailable(RuntimeError):
    """xray could not answer: stopped, no api inbound, or the service is not enabled."""


def _packed(address: str) -> bytes:
    return ipaddress.ip_address(address).packed


class RoutingClient:
    """One blocking call, built like `stats.client.StatsClient`: `stub_factory` is the test seam,
    production opens an insecure channel to the loopback api inbound."""

    def __init__(self, address: str, stub_factory=None):
        self._address = address
        self._stub_factory = stub_factory or self._default_stub
        self._stub = None
        self._channel = None
        self._lock = threading.RLock()

    def _default_stub(self):
        self._channel = grpc.insecure_channel(self._address)
        return command_pb2_grpc.RoutingServiceStub(self._channel)

    def close(self) -> None:
        with self._lock:
            if self._channel is not None:
                self._channel.close()
            self._channel = None
            self._stub = None

    def test_route(self, *, domain: str = "", ip: str = "", port: int = 443,
                   network: str = "tcp", source_ip: str = "") -> str:
        """The outbound tag xray would choose. Raises RouteTestUnavailable when it cannot say."""
        context = command_pb2.RoutingContext(
            Network=NETWORKS.get(network, network_pb2.TCP),
            TargetPort=int(port),
            # The panel's own inbound tag, so a ruleset that ever scopes rules by inbound answers
            # for the segment rather than for nothing.
            InboundTag="tproxy-in",
        )
        if domain:
            context.TargetDomain = domain
        if ip:
            context.TargetIPs.append(_packed(ip))
        if source_ip:
            context.SourceIPs.append(_packed(source_ip))
        request = command_pb2.TestRouteRequest(RoutingContext=context, PublishResult=False)
        with self._lock:
            if self._stub is None:
                self._stub = self._stub_factory()
            try:
                answer = self._stub.TestRoute(request, timeout=_TIMEOUT_S)
            except grpc.RpcError as exc:
                self.close()
                detail = exc.details() if hasattr(exc, "details") else ""
                raise RouteTestUnavailable(detail or type(exc).__name__) from exc
        # An empty tag means the router matched nothing at all — with this panel's rulesets that
        # cannot happen (they end in a catch-all), so it is reported rather than shown as "direct".
        return answer.OutboundTag or ""
