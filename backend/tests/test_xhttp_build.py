from pi_gw_panel.config import Settings
from pi_gw_panel.models import Node
from pi_gw_panel.xray_config.builder import build_config


def _xhttp():
    return Node(id=1, name="FI2", address="ru.pinusm.ru", port=443, uuid="u1",
                transport="xhttp", network="xhttp", security="tls", sni="ru.pinusm.ru",
                path="/xhttp-stream-fi2v", host="ru.pinusm.ru", mode="stream-up",
                alpn="h2,http/1.1", fingerprint="chrome")


def _reality():
    return Node(id=2, name="SW1", address="70.34.197.74", port=443, uuid="u2",
                transport="vision", network="tcp", security="reality", sni="hilex.se",
                public_key="PUBKEY", short_id="SID", flow="xtls-rprx-vision")


def _proxy(cfg):
    return next(o for o in cfg["outbounds"] if o["tag"] == "proxy")


def test_build_xhttp_tls():
    out = _proxy(build_config(_xhttp(), Settings()))
    ss = out["streamSettings"]
    assert ss["network"] == "xhttp"
    assert ss["security"] == "tls"
    assert ss["xhttpSettings"] == {"path": "/xhttp-stream-fi2v", "host": "ru.pinusm.ru", "mode": "stream-up"}
    assert ss["tlsSettings"]["serverName"] == "ru.pinusm.ru"
    assert ss["tlsSettings"]["alpn"] == ["h2", "http/1.1"]
    assert "realitySettings" not in ss
    assert "flow" not in out["settings"]["vnext"][0]["users"][0]   # XHTTP: no Vision flow


def test_build_reality_vision_intact():
    out = _proxy(build_config(_reality(), Settings()))
    ss = out["streamSettings"]
    assert ss["network"] == "tcp"
    assert ss["security"] == "reality"
    assert ss["realitySettings"] == {"serverName": "hilex.se", "fingerprint": "chrome",
                                     "publicKey": "PUBKEY", "shortId": "SID"}
    assert "tlsSettings" not in ss and "xhttpSettings" not in ss
    assert out["settings"]["vnext"][0]["users"][0]["flow"] == "xtls-rprx-vision"
