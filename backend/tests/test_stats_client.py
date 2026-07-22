import grpc
import pytest

from pi_gw_panel.stats.client import StatsClient, StatsUnavailable
from pi_gw_panel.stats.proto import command_pb2


class _FakeStub:
    """Stands in for StatsServiceStub — returns a real QueryStatsResponse built from
    a dict, so we exercise the real proto parse without a channel or running xray."""
    def __init__(self, stats):
        self._stats = stats
        self.last_req = None

    def QueryStats(self, req, timeout=None):
        self.last_req = req
        self.timeout = timeout
        resp = command_pb2.QueryStatsResponse()
        for name, value in self._stats.items():
            s = resp.stat.add()
            s.name = name
            s.value = value
        return resp


def test_query_parses_stats_into_dict():
    fake = _FakeStub({"outbound>>>proxy>>>traffic>>>uplink": 100,
                      "outbound>>>proxy>>>traffic>>>downlink": 250})
    c = StatsClient("127.0.0.1:10085", stub_factory=lambda: fake)
    out = c.query(pattern="outbound>>>", reset=False)
    assert out == {"outbound>>>proxy>>>traffic>>>uplink": 100,
                   "outbound>>>proxy>>>traffic>>>downlink": 250}
    assert fake.last_req.pattern == "outbound>>>" and fake.last_req.reset is False
    assert fake.timeout == 4.0


def test_query_empty_response():
    c = StatsClient("x", stub_factory=lambda: _FakeStub({}))
    assert c.query() == {}


class _RpcFailure(grpc.RpcError):
    def details(self):
        return "stats unavailable"


class _FailStub:
    def QueryStats(self, req, timeout=None):
        raise _RpcFailure()


def test_rpc_failure_is_typed_and_observable_without_fake_zero():
    clock = iter([10.0])
    c = StatsClient("old", stub_factory=lambda: _FailStub(), clock=lambda: next(clock))
    with pytest.raises(StatsUnavailable, match="stats unavailable"):
        c.query()
    assert c.status() == {
        "address": "old", "last_ok_at": None,
        "last_error": "stats unavailable", "fail_count": 1,
    }


def test_reconfigure_closes_cached_channel_and_uses_new_address():
    c = StatsClient("old", stub_factory=lambda: _FakeStub({}))
    c.query()
    assert c._stub is not None
    c.reconfigure("new")
    assert c.status()["address"] == "new" and c._stub is None
