"""Thin gRPC client to xray's StatsService (Wave 3a).

`stub_factory` is injectable so tests exercise the request-build + response-parse
logic with a fake stub (no grpc channel / no running xray). Production builds an
insecure channel to the local api inbound (127.0.0.1:<stats_api_port>)."""
import threading
import time

import grpc
from pi_gw_panel.stats.proto import command_pb2, command_pb2_grpc

_QUERY_TIMEOUT_S = 4.0   # bound the blocking unary call so a hung xray can't pin an executor worker forever


class StatsUnavailable(RuntimeError):
    """Typed gap: the previous counters remain valid, but this tick has no fresh proof."""


class StatsClient:
    def __init__(self, address: str, stub_factory=None, clock=time.time):
        self._address = address
        self._stub_factory = stub_factory or self._default_stub
        self._stub = None
        self._channel = None                                   # kept so close() can release the transport (default stub)
        self._clock = clock
        self._lock = threading.RLock()
        self.last_ok_at: float | None = None
        self.last_error = ""
        self.fail_count = 0

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
        successful empty response returns {}; an unavailable RPC raises StatsUnavailable so
        callers preserve the previous baseline and mark a telemetry gap."""
        req = command_pb2.QueryStatsRequest(pattern=pattern, reset=reset)
        with self._lock:
            stub = self._ensure_stub()
            try:
                # Production and injected stubs obey the same bounded contract; never retry a
                # TypeError as an unbounded RPC.
                resp = stub.QueryStats(req, timeout=_QUERY_TIMEOUT_S)
            except grpc.RpcError as exc:
                self.fail_count += 1
                self.last_error = exc.details() if hasattr(exc, "details") else str(exc)
                self.last_error = self.last_error or type(exc).__name__
                self._close_unlocked()
                raise StatsUnavailable(self.last_error) from exc
            self.last_ok_at = self._clock()
            self.last_error = ""
            self.fail_count = 0
            return {s.name: s.value for s in resp.stat}

    def reconfigure(self, address: str) -> None:
        """Atomically move future queries to a new local StatsService address."""
        with self._lock:
            if address == self._address:
                return
            self._close_unlocked()
            self._address = address
            self.last_error = ""
            self.fail_count = 0

    def status(self) -> dict:
        with self._lock:
            return {
                "address": self._address,
                "last_ok_at": self.last_ok_at,
                "last_error": self.last_error,
                "fail_count": self.fail_count,
            }

    def close(self) -> None:
        """Release the cached gRPC channel + its background transport (call at app/recorder shutdown)."""
        with self._lock:
            self._close_unlocked()

    def _close_unlocked(self) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None
        self._stub = None
