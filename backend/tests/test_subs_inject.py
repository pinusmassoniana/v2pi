from pi_gw_panel.subs.inject import build_request, default_injection, host_tokens


def test_build_request_substitutes_tokens_and_query():
    inj = {"headers": {"x-hwid": "{machine_id}", "ua": "fixed"}, "query": {"k": "{device_os}"}}
    req = build_request("https://h/sub", inj, {"machine_id": "ABC", "device_os": "linux"})
    assert req.headers["x-hwid"] == "ABC"
    assert req.headers["ua"] == "fixed"
    assert req.url == "https://h/sub?k=linux"
    assert req.method == "GET"


def test_query_appends_with_amp_when_url_already_has_query():
    req = build_request("https://h/sub?a=1", {"query": {"b": "2"}}, {})
    assert req.url == "https://h/sub?a=1&b=2"


def test_default_injection_has_hwid_token():
    assert default_injection()["headers"]["x-hwid"] == "{machine_id}"


def test_host_tokens_includes_machine_id():
    t = host_tokens("MID")
    assert t["machine_id"] == "MID" and "device_os" in t
