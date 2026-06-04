from pi_gw_panel.subs.parsers.base64_vless import parse

# Real shape from the live subscription: XHTTP-over-TLS, distinguished by `path`.
XHTTP = ("vless://fcb64f70-b969@ru.pinusm.ru:443?encryption=none&type=xhttp"
         "&path=%2Fxhttp-stream-fi2v&host=ru.pinusm.ru&mode=stream-up&security=tls"
         "&sni=ru.pinusm.ru&fp=chrome&alpn=h2%2Chttp%2F1.1#%F0%9F%87%AB%F0%9F%87%AEFI2")
# Legacy reality+vision node (must stay byte-identical downstream).
REALITY = ("vless://abcd-uuid@70.34.197.74:443?encryption=none&security=reality"
           "&sni=hilex.se&pbk=PUBKEY&sid=SID&fp=chrome&flow=xtls-rprx-vision#SW1")


def test_parse_xhttp_tls_node_captures_stream_fields():
    n = parse(XHTTP + "\n")[0]
    assert n.transport == "xhttp"
    assert n.network == "xhttp"
    assert n.security == "tls"
    assert n.path == "/xhttp-stream-fi2v"
    assert n.host == "ru.pinusm.ru"
    assert n.mode == "stream-up"
    assert n.alpn == "h2,http/1.1"
    assert n.flow == ""          # XHTTP carries no Vision flow
    assert n.public_key == ""    # TLS, not reality


def test_parse_reality_vision_node_unchanged():
    n = parse(REALITY + "\n")[0]
    assert n.transport == "vision"
    assert n.network == "tcp"
    assert n.security == "reality"
    assert n.flow == "xtls-rprx-vision"
    assert n.public_key == "PUBKEY"
    assert n.short_id == "SID"
    assert n.path == ""


def test_xhttp_variants_differ_by_path():
    body = "\n".join([XHTTP, XHTTP.replace("fi2v", "fi2x").replace("FI2", "FI2x")])
    ns = parse(body)
    assert len(ns) == 2
    assert ns[0].path != ns[1].path
    assert {n.path for n in ns} == {"/xhttp-stream-fi2v", "/xhttp-stream-fi2x"}
