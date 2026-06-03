"""Thin gRPC client to xray's StatsService (Wave 3a).

`stub_factory` is injectable so tests exercise the request-build + response-parse
logic with a fake stub (no grpc channel / no running xray). Production builds an
insecure channel to the local api inbound (127.0.0.1:<stats_api_port>)."""
import grpc
from pi_gw_panel.stats.proto import command_pb2, command_pb2_grpc


class StatsClient:
    def __init__(self, address: str, stub_factory=None):
        self._address = address
        self._stub_factory = stub_factory or self._default_stub
        self._stub = None

    def _default_stub(self):
        channel = grpc.insecure_channel(self._address)
        return command_pb2_grpc.StatsServiceStub(channel)

    def _ensure_stub(self):
        if self._stub is None:
            self._stub = self._stub_factory()
        return self._stub

    def query(self, pattern: str = "", reset: bool = False) -> dict[str, int]:
        """QueryStats(pattern, reset) → {stat_name: value}. With the default empty
        pattern xray returns every counter; pass e.g. 'outbound>>>' to scope it."""
        req = command_pb2.QueryStatsRequest(pattern=pattern, reset=reset)
        resp = self._ensure_stub().QueryStats(req)
        return {s.name: s.value for s in resp.stat}
