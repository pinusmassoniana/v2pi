"""Thin gRPC client to xray's StatsService (Wave 3a).

`stub_factory` is injectable so tests exercise the request-build + response-parse
logic with a fake stub (no grpc channel / no running xray). Production builds an
insecure channel to the local api inbound (127.0.0.1:<stats_api_port>)."""
import grpc
from pi_gw_panel.stats.proto import command_pb2, command_pb2_grpc

_QUERY_TIMEOUT_S = 4.0   # bound the blocking unary call so a hung xray can't pin an executor worker forever


class StatsClient:
    def __init__(self, address: str, stub_factory=None):
        self._address = address
        self._stub_factory = stub_factory or self._default_stub
        self._stub = None
        self._channel = None                                   # kept so close() can release the transport (default stub)

    def _default_stub(self):
        self._channel = grpc.insecure_channel(self._address)
        return command_pb2_grpc.StatsServiceStub(self._channel)

    def _ensure_stub(self):
        if self._stub is None:
            self._stub = self._stub_factory()
        return self._stub

    def query(self, pattern: str = "", reset: bool = False) -> dict[str, int]:
        """QueryStats(pattern, reset) → {stat_name: value}. With the default empty
        pattern xray returns every counter; pass e.g. 'outbound>>>' to scope it. A
        hung/failed xray read returns {} (a gap for that tick) instead of blocking or raising."""
        req = command_pb2.QueryStatsRequest(pattern=pattern, reset=reset)
        stub = self._ensure_stub()
        try:
            try:
                resp = stub.QueryStats(req, timeout=_QUERY_TIMEOUT_S)   # deadline-bounded so a stuck call returns
            except TypeError:
                resp = stub.QueryStats(req)                             # injected test stub without a timeout param
        except grpc.RpcError:
            self.close()                                        # drop the (maybe dead) channel so the next call rebuilds it
            return {}
        return {s.name: s.value for s in resp.stat}

    def close(self) -> None:
        """Release the cached gRPC channel + its background transport (call at app/recorder shutdown)."""
        if self._channel is not None:
            self._channel.close()
            self._channel = None
        self._stub = None
