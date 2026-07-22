import base64
from pi_gw_panel.subs.parsers.dispatch import parse_subscription


def test_base64_vless_list():
    uri = ("vless://11111111-1111-1111-1111-111111111111@1.2.3.4:443"
           "?security=reality&sni=www.microsoft.com&pbk=PK&sid=ab12&flow=xtls-rprx-vision&fp=chrome&type=tcp#n1")
    body = base64.b64encode((uri + "\n").encode()).decode()
    nodes = parse_subscription(body)
    assert len(nodes) == 1
    n = nodes[0]
    assert n.name == "n1" and n.address == "1.2.3.4" and n.port == 443
    assert n.uuid == "11111111-1111-1111-1111-111111111111"
    assert n.sni == "www.microsoft.com" and n.public_key == "PK" and n.short_id == "ab12"
    assert n.transport == "vision" and n.flow == "xtls-rprx-vision"


def test_raw_vless_list_without_base64():
    uri = "vless://u@h:8443?type=xhttp&sni=a#x"
    nodes = parse_subscription(uri)
    assert nodes[0].transport == "xhttp" and nodes[0].flow == ""  # vision-only flow cleared


def test_clash_yaml():
    body = (
        "proxies:\n"
        "  - name: c1\n    type: vless\n    server: 5.6.7.8\n    port: 443\n"
        "    uuid: u-2\n    servername: ya.ru\n    network: tcp\n"
        "    flow: xtls-rprx-vision\n    client-fingerprint: chrome\n"
        "    reality-opts:\n      public-key: PK2\n      short-id: cd34\n"
    )
    nodes = parse_subscription(body)
    assert len(nodes) == 1 and nodes[0].name == "c1"
    assert nodes[0].public_key == "PK2" and nodes[0].short_id == "cd34"


def test_json_array():
    body = '[{"name":"j1","address":"9.9.9.9","port":443,"uuid":"u-3","sni":"s","public_key":"PK3"}]'
    nodes = parse_subscription(body)
    assert nodes[0].name == "j1" and nodes[0].public_key == "PK3"


def test_base64_vless_accepts_bracketed_ipv6():
    uri = "vless://u@[2001:db8::1]:8443?type=xhttp&sni=cdn.example#v6"
    node = parse_subscription(uri)[0]
    assert node.address == "2001:db8::1" and node.port == 8443 and node.name == "v6"


def test_clash_skips_malformed_entries_individually():
    body = (
        "proxies:\n"
        "  - broken\n"
        "  - name: bad-opts\n    type: vless\n    server: 1.1.1.1\n    port: 443\n"
        "    uuid: u1\n    reality-opts: broken\n"
        "  - name: good\n    type: vless\n    server: 2.2.2.2\n    port: 443\n"
        "    uuid: u2\n"
    )
    nodes = parse_subscription(body)
    assert [node.name for node in nodes] == ["bad-opts", "good"]


def test_parser_stops_constructing_at_limit():
    body = "[" + ",".join(
        f'{{"name":"n{i}","address":"1.1.1.{i}","port":443,"uuid":"u{i}"}}'
        for i in range(1, 20)
    ) + "]"
    nodes = parse_subscription(body, limit=4)
    assert len(nodes) == 4 and nodes[-1].name == "n4"
