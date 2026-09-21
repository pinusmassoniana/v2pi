"""Header presets that let the gateway's subscription fetch look like a real client.

The presets themselves are form data; what the backend owes them is below: a HWID shaped like the
client's own, a fetcher that can read the gzip every one of those clients asks for, and a parser
for the Xray JSON a Remnawave panel sends Happ and v2rayNG when its operator turns that on.
"""
import gzip
import json
import re

import pytest

from pi_gw_panel.config import Settings
from pi_gw_panel.subs import fetcher
from pi_gw_panel.subs.inject import build_request, host_tokens
from pi_gw_panel.subs.parsers.dispatch import detect, parse_subscription
from pi_gw_panel.xray_config.builder import build_config

REMNAWAVE_HWID = re.compile(r"^[a-zA-Z0-9=-]{10,64}$")     # docs.rw, v3.0.0+


# --- the HWID ---

def test_hwid16_is_shaped_like_happs_and_stable_per_subscription():
    first = host_tokens("machine", app_secret="secret", subscription_id=1)["hwid16"]
    again = host_tokens("machine", app_secret="secret", subscription_id=1)["hwid16"]
    other = host_tokens("machine", app_secret="secret", subscription_id=2)["hwid16"]

    assert re.fullmatch(r"[a-z0-9]{16}", first)
    assert REMNAWAVE_HWID.match(first)                      # a panel that validates accepts it
    assert first == again                                   # the same device on every refresh
    assert first != other                                   # and unrelated across feeds
    assert first not in host_tokens("machine", app_secret="secret", subscription_id=1)["machine_id"]


def test_a_preset_header_expands_the_token():
    tokens = host_tokens("machine", app_secret="secret", subscription_id=7)
    built = build_request("https://sub.example/x", {"headers": {"x-hwid": "{hwid16}"}}, tokens)
    assert built.headers["x-hwid"] == tokens["hwid16"]


# --- gzip ---

def _serve(monkeypatch, body: bytes, headers: dict):
    monkeypatch.setattr(fetcher, "_resolve_public", lambda host, port, deadline: "1.2.3.4")
    monkeypatch.setattr(fetcher, "_request_once",
                        lambda parts, ip, h, proxy, deadline, max_bytes=None: (200, headers, body))


def test_a_gzip_answer_is_read_as_the_text_it_carries(monkeypatch):
    feed = "vless://u@1.2.3.4:443?security=tls#a\n"
    _serve(monkeypatch, gzip.compress(feed.encode()), {"Content-Encoding": "gzip",
                                                       "Content-Type": "text/plain"})
    body, _ = fetcher._http_get("https://sub.example/x", {"accept-encoding": "gzip"}, None, 5)
    assert body == feed


def test_a_gzip_bomb_is_refused_at_the_body_cap(monkeypatch):
    bomb = gzip.compress(b"\0" * (fetcher.MAX_BYTES + 1024))       # a few KB on the wire
    assert len(bomb) < 10_000
    _serve(monkeypatch, bomb, {"Content-Encoding": "gzip"})
    with pytest.raises(ValueError, match="once decompressed"):
        fetcher._http_get("https://sub.example/x", {}, None, 5)


def test_a_cut_or_unrequested_encoding_is_an_error_not_noise(monkeypatch):
    _serve(monkeypatch, gzip.compress(b"x" * 1000)[:-10], {"Content-Encoding": "gzip"})
    with pytest.raises(ValueError, match="truncated|not valid gzip"):
        fetcher._http_get("https://sub.example/x", {}, None, 5)

    _serve(monkeypatch, b"\x1b\x00", {"Content-Encoding": "br"})
    with pytest.raises(ValueError, match="unsupported content-encoding: br"):
        fetcher._http_get("https://sub.example/x", {}, None, 5)


def test_an_uncompressed_answer_is_untouched(monkeypatch):
    _serve(monkeypatch, b"plain", {"Content-Type": "text/plain"})
    assert fetcher._http_get("https://sub.example/x", {}, None, 5)[0] == "plain"


# --- Remnawave's XRAY_JSON ---

def _config(remarks, outbound):
    """One element of the array, shaped like remnawave/backend's xray-json generator output."""
    return {"remarks": remarks, "dns": {}, "routing": {"rules": []},
            "outbounds": [outbound, {"tag": "direct", "protocol": "freedom"},
                          {"tag": "block", "protocol": "blackhole"}]}


XRAY_JSON = [
    _config("🇳🇱 Reality", {
        "tag": "proxy", "protocol": "vless",
        "settings": {"vnext": [{"address": "1.2.3.4", "port": 443, "users": [
            {"id": "uuid-1", "encryption": "none", "flow": "xtls-rprx-vision"}]}]},
        "streamSettings": {"network": "tcp", "security": "reality", "realitySettings": {
            "serverName": "www.microsoft.com", "publicKey": "pbk", "shortId": "ab",
            "fingerprint": "firefox"}}}),
    _config("🇩🇪 XHTTP", {
        "tag": "proxy", "protocol": "vless",
        "settings": {"vnext": [{"address": "cdn.example.com", "port": 443,
                                "users": [{"id": "uuid-2", "encryption": "none"}]}]},
        "streamSettings": {"network": "xhttp", "security": "tls",
                           "tlsSettings": {"serverName": "cdn.example.com", "alpn": ["h2"]},
                           "xhttpSettings": {"path": "/x", "host": "cdn.example.com", "mode": "auto"}}}),
    _config("🇫🇮 Trojan", {
        "tag": "proxy", "protocol": "trojan",
        "settings": {"servers": [{"address": "5.6.7.8", "port": 443, "password": "pw"}]},
        "streamSettings": {"network": "tcp", "security": "tls",
                           "tlsSettings": {"serverName": "t.example.com"}}}),
    _config("🇸🇪 SS", {
        "tag": "proxy", "protocol": "shadowsocks",
        "settings": {"servers": [{"address": "9.9.9.9", "port": 8388, "password": "pw",
                                  "method": "aes-256-gcm"}]},
        "streamSettings": {"network": "tcp"}}),
    _config("hy2", {"tag": "proxy", "protocol": "hysteria", "settings": {"address": "1.1.1.1"}}),
    _config("ws", {
        "tag": "proxy", "protocol": "vless",
        "settings": {"vnext": [{"address": "1.2.3.5", "port": 443, "users": [{"id": "u"}]}]},
        "streamSettings": {"network": "ws", "security": "tls"}}),
]


def test_remnawave_xray_json_reads_as_nodes():
    skipped: dict = {}
    body = json.dumps(XRAY_JSON)
    nodes = parse_subscription(body, skipped=skipped)

    assert detect(body) == "xray-json"
    assert [(n.name, n.protocol) for n in nodes] == [
        ("🇳🇱 Reality", "vless"), ("🇩🇪 XHTTP", "vless"), ("🇫🇮 Trojan", "trojan"),
        ("🇸🇪 SS", "shadowsocks")]
    reality, xhttp, trojan, ss = nodes
    assert (reality.security, reality.public_key, reality.short_id, reality.fingerprint,
            reality.flow) == ("reality", "pbk", "ab", "firefox", "xtls-rprx-vision")
    assert (xhttp.transport, xhttp.security, xhttp.path, xhttp.mode, xhttp.alpn) == (
        "xhttp", "tls", "/x", "auto", "h2")
    assert (trojan.password, trojan.sni, trojan.security) == ("pw", "t.example.com", "tls")
    assert (ss.method, ss.security) == ("aes-256-gcm", "none")
    # Hysteria is counted by its name; a ws transport the panel does not build is refused rather
    # than imported as TCP — a node that looks fine and can never connect.
    assert skipped == {"hysteria2": 1, "invalid": 1}


def test_every_node_it_reads_builds_a_config():
    for node in parse_subscription(json.dumps(XRAY_JSON)):
        assert build_config(node, Settings())["outbounds"][0]["protocol"] == node.protocol


def test_the_panels_own_json_feed_still_reads_as_before():
    body = json.dumps([{"name": "own", "address": "1.2.3.4", "port": 443, "uuid": "u"}])
    assert detect(body) == "json"
    assert [n.name for n in parse_subscription(body)] == ["own"]
