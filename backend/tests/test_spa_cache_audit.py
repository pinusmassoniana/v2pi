"""The SPA mount revalidates index.html on every load and leaves every other static file alone.

After an upgrade a browser — an installed phone app most of all — must not reuse a heuristically cached
entry point whose hashed assets are gone. `Cache-Control: no-cache` goes on index.html only, on the 200
and on the 304; hashed assets and every other file keep Starlette's ETag + Last-Modified and nothing else.
"""
from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app

from conftest import _build_dryrun_state as _state

INDEX = "<!doctype html><title>pi-gw</title><div id=\"root\"></div>"


def _spa_client(settings, stub_xray, tmp_path):
    static = tmp_path / "spa"
    (static / "assets").mkdir(parents=True)
    (static / "fonts").mkdir()
    (static / "index.html").write_text(INDEX)
    (static / "assets" / "index-abc123.js").write_text("console.log('panel');\n")
    (static / "fonts" / "plex.css").write_text("@font-face{font-family:x}\n")
    settings.static_dir = str(static)
    return TestClient(create_app(settings, state=_state(settings, stub_xray)))


def test_root_serves_index_with_no_cache(settings, stub_xray, tmp_path):
    c = _spa_client(settings, stub_xray, tmp_path)
    r = c.get("/")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    assert r.text == INDEX


def test_index_html_by_name_is_no_cache_too(settings, stub_xray, tmp_path):
    c = _spa_client(settings, stub_xray, tmp_path)
    r = c.get("/index.html")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"


def test_hashed_asset_keeps_starlette_defaults(settings, stub_xray, tmp_path):
    c = _spa_client(settings, stub_xray, tmp_path)
    r = c.get("/assets/index-abc123.js")
    assert r.status_code == 200
    assert "cache-control" not in r.headers
    assert r.headers["etag"]
    assert r.headers["last-modified"]


def test_other_static_files_get_no_cache_control(settings, stub_xray, tmp_path):
    c = _spa_client(settings, stub_xray, tmp_path)
    r = c.get("/fonts/plex.css")
    assert r.status_code == 200
    assert "cache-control" not in r.headers


def test_revalidated_index_answers_304_and_keeps_the_header(settings, stub_xray, tmp_path):
    c = _spa_client(settings, stub_xray, tmp_path)
    etag = c.get("/").headers["etag"]
    r = c.get("/", headers={"If-None-Match": etag})
    # NotModifiedResponse copies only the headers the 200 already had, so the header must be set after
    # the 304 is built, not before: an unchanged panel still answers 304 and still says no-cache.
    assert r.status_code == 304
    assert r.headers["cache-control"] == "no-cache"


def test_api_responses_are_untouched(settings, stub_xray, tmp_path):
    c = _spa_client(settings, stub_xray, tmp_path)
    r = c.get("/api/health")
    assert r.status_code == 200
    assert "cache-control" not in r.headers


def test_head_on_root_carries_the_header(settings, stub_xray, tmp_path):
    c = _spa_client(settings, stub_xray, tmp_path)
    r = c.head("/")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
