"""B4 + B3: nodes that are not VLESS, and the on-demand diagnosis.

A mixed subscription used to yield fewer nodes with the rest counted as "dropped". Trojan and
Shadowsocks now import and connect. Hysteria2 still does not, and that is not an oversight:
xray 26.3.27 refuses the outbound outright, so a node the panel cannot ever connect is not one
it offers to store.
"""
import pytest

from pi_gw_panel import diagnose
from pi_gw_panel.backup import BACKUP_SCHEMA, export_state, import_state, validate_document
from pi_gw_panel.config import Settings
from pi_gw_panel.db import connect, init_schema
import base64

from pi_gw_panel.models import SS_METHODS, Node, ss_password_issue
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.health.probe import _probe_outbound
from pi_gw_panel.subs.parsers.dispatch import parse_subscription
from pi_gw_panel.xray_config.builder import build_config
from tests.conftest import _client, _login


def _node(**over) -> Node:
    base = dict(id=None, name="n", address="1.2.3.4", port=443, uuid="u")
    return Node(**{**base, **over})


def _trojan(**over) -> Node:
    return _node(**{"uuid": "", "protocol": "trojan", "password": "pw",
                    "sni": "a.example", **over})


def _ss(**over) -> Node:
    return _node(**{"uuid": "", "protocol": "shadowsocks", "password": "pw",
                    "method": "aes-256-gcm", **over})


# --- the model ---

def test_a_non_vless_node_carries_no_vless_shape():
    trojan = _trojan(transport="xhttp", path="/p", host="h", flow="xtls-rprx-vision")
    assert (trojan.network, trojan.flow, trojan.path, trojan.host) == ("tcp", "", "", "")
    assert trojan.security == "tls"                      # no reality key → tls, as for vless
    assert _trojan(public_key="k").security == "reality"  # xray builds trojan over reality too

    ss = _ss(method="")
    assert ss.security == "none" and ss.method == "aes-128-gcm"
    assert _node(protocol="nonsense").protocol == "vless"   # never rendered into a config


def test_a_2022_cipher_takes_a_key_not_a_passphrase():
    """xray refuses to START on a 2022 key it cannot decode — `-test` catches it, but only as a
    base64 error in the apply log. The rule lives where the key is typed instead."""
    assert ss_password_issue("aes-128-gcm", "any passphrase at all") is None
    assert "base64" in ss_password_issue("2022-blake3-aes-128-gcm", "passphrase")
    assert ss_password_issue("2022-blake3-aes-128-gcm", base64.b64encode(b"k" * 16).decode()) is None
    assert "32-byte" in ss_password_issue("2022-blake3-aes-256-gcm", base64.b64encode(b"k" * 16).decode())
    assert set(SS_METHODS) >= {"aes-128-gcm", "chacha20-ietf-poly1305", "2022-blake3-aes-256-gcm"}
    assert "none" not in SS_METHODS and "rc4-md5" not in SS_METHODS


# --- the config ---

def test_each_protocol_builds_its_own_outbound():
    settings = Settings()
    vless = build_config(_node(), settings)["outbounds"][0]
    assert vless["protocol"] == "vless" and vless["settings"]["vnext"][0]["users"][0]["id"] == "u"

    trojan = build_config(_trojan(), settings)["outbounds"][0]
    assert trojan["protocol"] == "trojan"
    assert trojan["settings"]["servers"] == [{"address": "1.2.3.4", "port": 443, "password": "pw"}]
    assert trojan["streamSettings"]["tlsSettings"]["serverName"] == "a.example"

    ss = build_config(_ss(), settings)["outbounds"][0]
    assert ss["protocol"] == "shadowsocks"
    assert ss["settings"]["servers"][0]["method"] == "aes-256-gcm"
    # No TLS block at all — shadowsocks IS the encryption, and an empty tlsSettings would be a
    # claim the config does not make.
    assert ss["streamSettings"]["security"] == "none"
    assert "tlsSettings" not in ss["streamSettings"] and "realitySettings" not in ss["streamSettings"]


def test_a_cipher_this_build_cannot_render_is_refused_not_written():
    node = _ss()
    node.method = "rc4-md5"                 # xray 26.3.27: "unknown cipher method"
    with pytest.raises(ValueError, match="cipher"):
        build_config(node, Settings())


def test_the_probe_dials_the_same_credential_as_the_tunnel():
    for node in (_trojan(), _ss()):
        out = _probe_outbound(node, "9.9.9.9")
        built = build_config(node, Settings())["outbounds"][0]
        assert out["protocol"] == built["protocol"]
        assert out["settings"]["servers"][0]["password"] == "pw"
        assert out["settings"]["servers"][0]["address"] == "9.9.9.9"


def test_mux_is_offered_to_trojan_and_never_to_shadowsocks():
    from pi_gw_panel.models import TuningProfile
    profile = TuningProfile(id=1, name="p", mux_enabled=True, mux_concurrency="8")
    trojan = build_config(_trojan(), Settings(), profile=profile)["outbounds"][0]
    assert trojan["mux"] == {"enabled": True, "concurrency": 8}
    assert "mux" not in build_config(_ss(), Settings(), profile=profile)["outbounds"][0]


# --- the feeds ---

def test_uri_feeds_carry_trojan_and_shadowsocks():
    body = "\n".join([
        "trojan://p%40ss@1.2.3.5:443?sni=b.example&alpn=h2#t",
        "ss://YWVzLTI1Ni1nY206c2VjcmV0@1.2.3.7:8388#s",              # SIP002
        "ss://YWVzLTEyOC1nY206cHdAMS4yLjMuODo4Mzg4#legacy",          # whole-URI base64
        "trojan://p@1.2.3.5:443?type=ws#ws-is-refused",
        "ss://YWVzLTI1Ni1nY206c2VjcmV0@1.2.3.7:8388?plugin=obfs-local#plugin-is-refused",
    ])
    skipped: dict = {}
    nodes = parse_subscription(body, skipped=skipped)

    assert [(n.protocol, n.name) for n in nodes] == [
        ("trojan", "t"), ("shadowsocks", "s"), ("shadowsocks", "legacy")]
    assert nodes[0].password == "p@ss" and nodes[0].alpn == "h2"
    assert (nodes[1].method, nodes[1].password) == ("aes-256-gcm", "secret")
    assert (nodes[2].address, nodes[2].port) == ("1.2.3.8", 8388)
    # A transport or a plugin the panel does not build is refused rather than imported as plain
    # TCP: it would be a node that looks fine in the list and can never connect.
    assert skipped == {"invalid": 2}


def test_clash_feeds_carry_them_too():
    body = """
proxies:
  - {name: t, type: trojan, server: 1.2.3.5, port: 443, password: p, sni: b.example}
  - {name: s, type: ss, server: 1.2.3.7, port: 8388, cipher: aes-128-gcm, password: p}
  - {name: bad, type: ss, server: 1.2.3.7, port: 8388, cipher: rc4-md5, password: p}
"""
    skipped: dict = {}
    nodes = parse_subscription(body, skipped=skipped)
    assert [(n.protocol, n.name) for n in nodes] == [("trojan", "t"), ("shadowsocks", "s")]
    assert skipped == {"invalid": 1}


# --- the API ---

def test_the_api_asks_each_protocol_for_its_own_credential(settings, stub_xray):
    client = _client(settings, stub_xray)
    headers = {"X-CSRF-Token": _login(client)}

    def post(body):
        return client.post("/api/nodes", json={"name": "n", "address": "1.2.3.4", "port": 443, **body},
                           headers=headers)

    assert post({"protocol": "trojan"}).status_code == 422             # no password
    assert post({"protocol": "shadowsocks", "password": "p"}).status_code == 422   # no cipher
    assert post({"protocol": "shadowsocks", "password": "p", "method": "rc4-md5"}).status_code == 422
    assert post({"protocol": "shadowsocks", "password": "p",
                 "method": "2022-blake3-aes-128-gcm"}).status_code == 422   # not a base64 key
    assert post({"uuid": ""}).status_code == 422                       # vless still needs a uuid

    created = post({"protocol": "trojan", "password": "pw", "name": "t"})
    assert created.status_code == 200
    row = created.json()
    # The credential never comes back out — the same rule the Reality private key follows.
    assert row["protocol"] == "trojan" and row["has_password"] is True
    assert "password" not in row and row["uuid"] == ""

    # A patch is judged as the node it produces: a vless node cannot become a trojan one by
    # changing one field.
    vless = post({"uuid": "u", "name": "v"}).json()
    assert client.patch(f"/api/nodes/{vless['id']}", json={"protocol": "trojan"},
                        headers=headers).status_code == 422
    assert client.patch(f"/api/nodes/{vless['id']}", json={"protocol": "trojan", "password": "pw"},
                        headers=headers).status_code == 200


def test_a_trojan_node_applies(settings, stub_xray):
    client = _client(settings, stub_xray)
    headers = {"X-CSRF-Token": _login(client)}
    node = client.post("/api/nodes", json={"name": "t", "address": "1.2.3.4", "port": 443,
                                           "protocol": "trojan", "password": "pw"},
                       headers=headers).json()
    assert client.post(f"/api/nodes/{node['id']}/apply", headers=headers).status_code == 200


# --- the backup document ---

def test_the_document_carries_the_protocol_and_still_reads_an_older_one(tmp_path):
    def store(name):
        conn = connect(str(tmp_path / name), check_same_thread=False)
        init_schema(conn)
        return NodeStore(conn)

    src = store("src.db")
    src.add_node(_trojan(name="t"))
    src.add_node(_ss(name="s"))
    document = export_state(src)
    assert document["schema_version"] == BACKUP_SCHEMA == 3
    assert [n["protocol"] for n in document["nodes"]] == ["trojan", "shadowsocks"]

    dst = store("dst.db")
    import_state(dst, document)
    restored = {n.name: n for n in dst.list_nodes()}
    assert restored["t"].password == "pw" and restored["s"].method == "aes-256-gcm"

    # A document written before v2.4 names no protocol at all; every node in it was VLESS.
    old = dict(document, schema_version=2,
               nodes=[{"id": 1, "name": "v", "address": "1.2.3.4", "port": 443, "uuid": "u"}])
    assert validate_document(old).nodes[0].protocol == "vless"
    # ...and the credential rule is the document's too, not only the API's.
    with pytest.raises(ValueError, match="needs a password"):
        validate_document(dict(document, nodes=[
            {"id": 1, "name": "t", "address": "1.2.3.4", "port": 443, "protocol": "trojan"}]))


# --- B3: the diagnosis ---

def test_the_verdict_names_what_it_was_read_from():
    clean = {"ttfb_ms": 80, "bytes": 262_144, "transfer_ms": 400, "kbps": 5_242,
             "stalled": False, "error": ""}
    assert diagnose.classify(30, "", clean)[0] == "ok"
    assert diagnose.classify(None, "", clean)[0] == "down"

    handshake_failed = diagnose.classify(30, "handshake timed out", {**clean, "ttfb_ms": None, "error": "x"})
    assert handshake_failed[0] == "down" and "TLS handshake" in handshake_failed[1]

    after_handshake = diagnose.classify(30, "", {**clean, "ttfb_ms": None, "error": "timed out"})
    assert after_handshake[0] == "down" and "nothing came back" in after_handshake[1]

    stalled = diagnose.classify(30, "", {**clean, "bytes": 96 * 1024, "stalled": True})
    assert stalled[0] == "stalls" and "96 KB" in stalled[1]
    # The copy never claims proof — the same measurement has innocent explanations.
    assert "also what" in stalled[1]

    assert diagnose.classify(30, "", {**clean, "kbps": 120})[0] == "slow"
    assert diagnose.classify(30, "", {**clean, "ttfb_ms": 4_000})[0] == "slow"


def test_diagnose_is_one_at_a_time_and_only_for_a_node_that_exists(settings, stub_xray, monkeypatch):
    client = _client(settings, stub_xray)
    headers = {"X-CSRF-Token": _login(client)}
    node = client.post("/api/nodes", json={"name": "n", "address": "1.2.3.4", "port": 443,
                                           "uuid": "u"}, headers=headers).json()

    assert client.post("/api/nodes/9999/diagnose", headers=headers).status_code == 404
    assert client.post(f"/api/nodes/{node['id']}/diagnose").status_code == 403      # csrf

    monkeypatch.setattr(diagnose, "run", lambda *a, **k: {
        "verdict": "stalls", "detail": "…", "tcp_ms": 20, "tls_ms": 60, "ttfb_ms": 90,
        "transfer_ms": 500, "bytes": 65_536, "kbps": 1_000, "requested_bytes": 262_144,
        "error": "", "url": "https://e.example/d"})
    answer = client.post(f"/api/nodes/{node['id']}/diagnose", headers=headers)
    assert answer.status_code == 200 and answer.json()["verdict"] == "stalls"
    assert answer.json()["node_id"] == node["id"]

    # A second press while one is running is refused rather than queued: two throwaway xrays
    # measuring at once would each be measuring the other's traffic.
    assert diagnose.lock.acquire(blocking=False)
    try:
        assert client.post(f"/api/nodes/{node['id']}/diagnose", headers=headers).status_code == 409
    finally:
        diagnose.lock.release()


def test_a_diagnosis_url_that_is_not_public_is_refused(settings, stub_xray):
    client = _client(settings, stub_xray)
    headers = {"X-CSRF-Token": _login(client)}
    node = client.post("/api/nodes", json={"name": "n", "address": "1.2.3.4", "port": 443,
                                           "uuid": "u"}, headers=headers).json()
    assert client.put("/api/settings", json={"diag_url": "http://127.0.0.1:9/x"},
                      headers=headers).status_code in (200, 422)
    response = client.post(f"/api/nodes/{node['id']}/diagnose", headers=headers)
    assert response.status_code == 422 and "refused" in response.json()["detail"]
