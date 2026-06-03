from pi_gw_panel.stats.client import StatsClient
from pi_gw_panel.stats.proto import command_pb2


class _FakeStub:
    """Stands in for StatsServiceStub — returns a real QueryStatsResponse built from
    a dict, so we exercise the real proto parse without a channel or running xray."""
    def __init__(self, stats):
        self._stats = stats
        self.last_req = None

    def QueryStats(self, req):
        self.last_req = req
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


def test_query_empty_response():
    c = StatsClient("x", stub_factory=lambda: _FakeStub({}))
    assert c.query() == {}
