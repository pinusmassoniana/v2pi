from pi_gw_panel.stats.sampler import TrafficSampler


def test_first_sample_is_zero():
    s = TrafficSampler(query_fn=lambda: {"outbound>>>proxy>>>traffic>>>uplink": 1000},
                       clock=lambda: 0.0)
    assert s.sample() == {"proxy": {"up_bps": 0.0, "down_bps": 0.0}}


def test_rate_from_deltas():
    times = iter([0.0, 2.0])                                   # 2 s apart
    counters = iter([
        {"outbound>>>proxy>>>traffic>>>uplink": 1000, "outbound>>>proxy>>>traffic>>>downlink": 2000},
        {"outbound>>>proxy>>>traffic>>>uplink": 3000, "outbound>>>proxy>>>traffic>>>downlink": 6000},
    ])
    s = TrafficSampler(query_fn=lambda: next(counters), clock=lambda: next(times))
    s.sample()                                                 # prime
    out = s.sample()
    assert out["proxy"]["up_bps"] == 8000.0                    # (3000-1000)*8/2
    assert out["proxy"]["down_bps"] == 16000.0                 # (6000-2000)*8/2


def test_counter_reset_yields_zero():
    times = iter([0.0, 1.0])
    counters = iter([
        {"outbound>>>proxy>>>traffic>>>uplink": 5000},
        {"outbound>>>proxy>>>traffic>>>uplink": 100},          # dropped → xray restart/reset
    ])
    s = TrafficSampler(query_fn=lambda: next(counters), clock=lambda: next(times))
    s.sample()
    assert s.sample()["proxy"]["up_bps"] == 0.0


def test_ignores_non_outbound_series_and_splits_tags():
    times = iter([0.0, 1.0])
    counters = iter([
        {"outbound>>>proxy>>>traffic>>>uplink": 0, "outbound>>>direct>>>traffic>>>downlink": 0,
         "inbound>>>tproxy-in>>>traffic>>>uplink": 0},
        {"outbound>>>proxy>>>traffic>>>uplink": 100, "outbound>>>direct>>>traffic>>>downlink": 50,
         "inbound>>>tproxy-in>>>traffic>>>uplink": 999},
    ])
    s = TrafficSampler(query_fn=lambda: next(counters), clock=lambda: next(times))
    s.sample()
    out = s.sample()
    assert out["proxy"]["up_bps"] == 800.0                     # 100*8/1
    assert out["direct"]["down_bps"] == 400.0                  # 50*8/1
    assert "tproxy-in" not in out                              # inbound>>> ignored


def test_query_gap_preserves_previous_baseline_and_totals():
    times = iter([0.0, 1.0, 2.0])
    calls = iter([
        {"outbound>>>proxy>>>traffic>>>uplink": 100},
        RuntimeError("gap"),
        {"outbound>>>proxy>>>traffic>>>uplink": 300},
    ])

    def query():
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    sampler = TrafficSampler(query_fn=query, clock=lambda: next(times))
    sampler.sample()
    try:
        sampler.sample()
    except RuntimeError:
        pass
    assert sampler.totals["proxy"]["up"] == 100
    assert sampler.sample()["proxy"]["up_bps"] == 800.0  # 200 bytes over two seconds
