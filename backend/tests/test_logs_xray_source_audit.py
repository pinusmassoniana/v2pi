"""`GET /api/logs?source=xray-stderr` returns the supervisor's redacted in-memory stderr tail.

xray is built with `"loglevel": "warning"`, `"access": "none"` and no error path, so it writes
neither `data/xray-error.log` nor `data/xray-access.log` — the two file-backed sources answer `[]`
forever. Everything xray ever printed lives in one place: the supervisor's bounded stderr tail,
which `status()` already scrubs with the last-started config's secret vocabulary. Nothing exposed
it, so the panel could never show an operator why a connect failed.

These tests pin the new source to that tail: empty before xray ever ran, in order and clamped like
a file source, scrubbed on the way out, unreadable-by-shape tolerated, and refused for a monitor
token exactly as every other System path is.
"""
from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.net_control.dryrun import DryRunBackend
from pi_gw_panel.state import build_state

from conftest import _client, _login


def _sup(c):
    return c.app.state.app_state.supervisor


def _set_tail(c, text: str) -> None:
    """Put `text` in the supervisor's tail the way `_capture_stderr` does — under its own lock."""
    sup = _sup(c)
    with sup._stderr_lock:
        sup._stderr_tail = text


def _lines(c, query: str = "") -> list[str]:
    r = c.get(f"/api/logs?source=xray-stderr{query}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "xray-stderr"
    return body["lines"]


def test_a_gateway_whose_xray_never_ran_answers_empty_not_an_error(settings, stub_xray):
    c = _client(settings, stub_xray)
    _login(c)

    r = c.get("/api/logs?source=xray-stderr")

    assert r.status_code == 200
    assert r.json() == {"source": "xray-stderr", "lines": []}


def test_it_returns_the_tail_in_order_and_honours_the_line_count(settings, stub_xray):
    c = _client(settings, stub_xray)
    _login(c)
    _set_tail(c, "first\nsecond\nthird\n")

    assert _lines(c) == ["first", "second", "third"]
    assert _lines(c, "&lines=1") == ["third"]
    assert _lines(c, "&lines=2") == ["second", "third"]


def test_the_secrets_of_the_started_config_are_replaced_before_the_reply_leaves(settings, stub_xray):
    import json

    secret = "550e8400-e29b-41d4-a716-446655440000"
    with open(settings.config_path, "w") as f:
        json.dump({"inbounds": [{"settings": {"clients": [{"id": secret}]}}]}, f)
    c = _client(settings, stub_xray)
    _login(c)
    sup = _sup(c)
    sup.start()                       # reads the config, so the scrub vocabulary is loaded
    try:
        _set_tail(c, f"failed to start: invalid user id={secret}\n")

        r = c.get("/api/logs?source=xray-stderr")

        assert r.status_code == 200
        assert secret not in r.text, "the raw uuid reached the response body"
        assert r.json()["lines"] == ["failed to start: invalid user id=***"]
    finally:
        sup.stop()


def test_the_line_count_is_clamped_exactly_as_a_file_source_is(settings, stub_xray):
    c = _client(settings, stub_xray)
    _login(c)
    _set_tail(c, "one\ntwo\nthree\n")

    # 0 and negative are clamped up to 1 — never "the whole tail", which is what a bare
    # splitlines()[-0:] would have returned.
    assert _lines(c, "&lines=0") == ["three"]
    assert _lines(c, "&lines=-5") == ["three"]
    # above the cap, simply everything there is
    assert _lines(c, "&lines=99999") == ["one", "two", "three"]
    assert c.get("/api/logs?source=xray-stderr&lines=abc").status_code == 422


def test_the_other_sources_are_untouched(settings, stub_xray):
    import os

    c = _client(settings, stub_xray)
    _login(c)
    path = c.app.state.app_state.settings.xray_error_log
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("err line 1\nerr line 2\n")

    assert c.get("/api/logs?source=xray-error&lines=1").json() == {
        "source": "xray-error", "lines": ["err line 2"]}
    assert c.get("/api/logs?source=xray-access").json() == {"source": "xray-access", "lines": []}
    assert c.get("/api/logs").json()["source"] == "xray-error"        # the default is unchanged
    r = c.get("/api/logs?source=bogus")
    assert r.status_code == 400 and r.json()["detail"] == "unknown log source"


def test_a_monitor_token_is_refused_here_as_on_every_other_system_path(settings, stub_xray):
    settings.xray_bin = stub_xray
    app = create_app(settings, state=build_state(settings, net=DryRunBackend()))
    admin = TestClient(app)
    csrf = _login(admin)
    created = admin.post("/api/tokens", json={"name": "grafana", "scope": "monitor"},
                         headers={"X-CSRF-Token": csrf}).json()
    bearer = TestClient(app)
    headers = {"Authorization": f"Bearer {created['token']}"}

    assert bearer.get("/api/status", headers=headers).status_code == 200
    assert bearer.get("/api/logs?source=xray-stderr", headers=headers).status_code == 403
    assert bearer.get("/api/logs?source=app", headers=headers).status_code == 403


def test_a_status_dict_without_the_key_answers_empty_instead_of_500(settings, stub_xray):
    c = _client(settings, stub_xray)
    _login(c)
    # Tests stub status() with partial dicts; the handler must read the key the way the rest of
    # routes.py reads `running` — with a default, not by subscript.
    _sup(c).status = lambda: {"running": True}

    r = c.get("/api/logs?source=xray-stderr")

    assert r.status_code == 200
    assert r.json() == {"source": "xray-stderr", "lines": []}


def test_a_tail_truncated_by_the_character_cap_keeps_its_leading_fragment(settings, stub_xray):
    c = _client(settings, stub_xray)
    _login(c)
    sup = _sup(c)
    sup.STDERR_TAIL_CHARS = 24
    _set_tail(c, ("x" * 200 + "\nsecond line\n")[-sup.STDERR_TAIL_CHARS:])

    lines = _lines(c)

    # The cap cuts by characters from the left, so the first element is half a line. The panel
    # says so rather than dropping it — a fragment of xray's complaint is still the complaint.
    assert lines[-1] == "second line"
    assert lines[0].startswith("x") and len(lines[0]) < 200
