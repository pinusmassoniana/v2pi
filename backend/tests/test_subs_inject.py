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
    assert "x-device-ver" not in default_injection()["headers"]
    assert "x-device-model" not in default_injection()["headers"]


def test_host_tokens_pseudonymizes_machine_id_per_subscription():
    first = host_tokens("MID", app_secret="s" * 32, subscription_id=1)
    again = host_tokens("MID", app_secret="s" * 32, subscription_id=1)
    other = host_tokens("MID", app_secret="s" * 32, subscription_id=2)
    assert first["machine_id"] == again["machine_id"] != other["machine_id"]
    assert first["machine_id"] != "MID"
    assert first["device_os"]


def test_host_tokens_exposes_exact_values_only_via_explicit_placeholders(monkeypatch):
    monkeypatch.setattr("pi_gw_panel.subs.inject.platform.release", lambda: "6.12.34-exact")
    monkeypatch.setattr("pi_gw_panel.subs.inject.platform.machine", lambda: "aarch64-exact")
    tokens = host_tokens("MID", app_secret="s" * 32, subscription_id=1)
    assert tokens["host_machine_id"] == "MID"
    assert tokens["host_device_ver"] == "6.12.34-exact"
    assert tokens["host_device_model"] == "aarch64-exact"
    assert tokens["device_ver"] != tokens["host_device_ver"]
