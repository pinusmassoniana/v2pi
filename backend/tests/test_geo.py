from pi_gw_panel.health import geo


class FakeReader:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = 0

    def get(self, ip):
        self.calls += 1
        return self.mapping.get(ip)


def test_lookup_cc_known_v4_and_v6():
    r = FakeReader({"1.2.3.4": {"country": {"iso_code": "US"}},
                    "2a05:f480::1": {"country": {"iso_code": "NL"}}})
    assert geo.lookup_cc(r, "1.2.3.4") == "US"
    assert geo.lookup_cc(r, "2a05:f480::1") == "NL"


def test_lookup_cc_no_reader_or_empty_ip():
    assert geo.lookup_cc(None, "1.2.3.4") is None
    assert geo.lookup_cc(FakeReader({}), "") is None
    assert geo.lookup_cc(FakeReader({}), None) is None


def test_lookup_cc_unknown_ip_and_no_country():
    assert geo.lookup_cc(FakeReader({}), "10.0.0.1") is None
    assert geo.lookup_cc(FakeReader({"1.2.3.4": {"continent": {"code": "EU"}}}), "1.2.3.4") is None


def test_lookup_cc_reader_error_is_none():
    class Boom:
        def get(self, ip):
            raise ValueError("invalid IP string")
    assert geo.lookup_cc(Boom(), "not-an-ip") is None


def test_country_code_none_when_db_absent(tmp_path):
    geo.configure(str(tmp_path / "nope.mmdb"))      # no mmdb on dev/CI → graceful None
    assert geo.country_code("1.2.3.4") is None
    assert geo.country_code("") is None
    geo.clear_cache()


def test_country_code_caches(monkeypatch):
    geo.clear_cache()
    r = FakeReader({"9.9.9.9": {"country": {"iso_code": "DE"}}})
    monkeypatch.setattr(geo, "_get_reader", lambda: r)
    assert geo.country_code("9.9.9.9") == "DE"
    assert geo.country_code("9.9.9.9") == "DE"
    assert r.calls == 1                              # second call hit the cache
    geo.clear_cache()
