"""A6: a VLESS-only panel reading a mixed feed must SAY what it dropped.

Before this, a subscription that lists 12 servers and yields 6 nodes looked like a smaller
subscription; the six Trojan/Hysteria2 entries left no trace anywhere in the panel.
"""
import json

from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.models import Subscription
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.subs.parsers import SKIP_LABELS, note_skip, skip_label
from pi_gw_panel.subs.parsers.dispatch import parse_subscription

CLASH = """
proxies:
  - {name: a, type: vless, server: 1.2.3.4, port: 443, uuid: u1}
  - {name: b, type: trojan, server: 1.2.3.5, port: 443, password: p}
  - {name: c, type: hysteria2, server: 1.2.3.6, port: 443, password: p}
  - {name: d, type: ss, server: 1.2.3.7, port: 8388, cipher: aes-128-gcm, password: p}
  - {name: e, type: vless, server: 1.2.3.8, port: 70000, uuid: u2}
  - not-a-mapping
"""

BASE64_BODY = "\n".join([
    "vless://u1@1.2.3.4:443?security=tls#a",
    "trojan://p@1.2.3.5:443#b",
    "hy2://p@1.2.3.6:443#c",
    "ss://YWVzLTEyOC1nY206cA==@1.2.3.7:8388#d",
    "vless://@:0#broken",
    "",
    "just some text a captcha page would carry",
])


def test_clash_feed_counts_every_dropped_entry_by_protocol():
    skipped: dict = {}
    nodes = parse_subscription(CLASH, skipped=skipped)
    assert [n.name for n in nodes] == ["a"]
    # e is a VLESS entry with an out-of-range port and the last item is not a mapping: both are
    # "invalid", not a protocol we chose not to support.
    assert skipped == {"trojan": 1, "hysteria2": 1, "ss": 1, "invalid": 2}


def test_base64_feed_counts_other_schemes_and_unusable_vless_lines():
    skipped: dict = {}
    nodes = parse_subscription(BASE64_BODY, skipped=skipped)
    assert [n.name for n in nodes] == ["a"]
    assert skipped == {"trojan": 1, "hysteria2": 1, "ss": 1, "invalid": 1}


def test_free_text_lines_are_not_counted_as_entries():
    """A mis-sniffed HTML page decodes to prose. Counting every line would report hundreds of
    dropped 'entries' for a feed that carried none."""
    skipped: dict = {}
    parse_subscription("nothing here\nnor here\n", skipped=skipped)
    assert skipped == {}


def test_a_feed_cannot_choose_the_label():
    """The keys reach the API and the UI, so a hostile `type` must land in the fixed vocabulary."""
    assert skip_label("<script>alert(1)</script>") == "other"
    assert skip_label("x" * 500) == "other"
    assert skip_label(None) == "other"
    assert skip_label("HY2") == "hysteria2"        # alias + case
    assert skip_label("Shadowsocks") == "ss"
    assert set(SKIP_LABELS) >= {"trojan", "ss", "hysteria2", "invalid", "other"}

    counts: dict = {}
    note_skip(counts, "TROJAN")
    note_skip(counts, "trojan")
    assert counts == {"trojan": 2}


def test_parsers_still_work_for_callers_that_do_not_ask():
    """`skipped` is opt-in: nothing in the parse path changes for a caller that passes nothing."""
    assert len(parse_subscription(CLASH)) == 1
    assert len(parse_subscription(BASE64_BODY)) == 1


def test_counts_survive_a_restart(tmp_path):
    conn = connect(str(tmp_path / "t.db"), check_same_thread=False)
    init_schema(conn)
    store = NodeStore(conn)
    sid = store.add_subscription(Subscription(id=None, name="s", url="https://e.example/s"))
    sub = store.get_subscription(sid)
    assert sub.last_skipped == {}                      # nothing refreshed yet

    sub.last_skipped = {"trojan": 3}
    store.update_subscription_refresh(sub, success=True)
    assert store.get_subscription(sid).last_skipped == {"trojan": 3}

    # A corrupt value degrades to "nothing known", never to a 500 on GET /api/subs.
    store._conn.execute("UPDATE subscriptions SET last_skipped='{' WHERE id=?", (sid,))
    store._conn.commit()
    assert store.get_subscription(sid).last_skipped == {}

    store._conn.execute("UPDATE subscriptions SET last_skipped=? WHERE id=?",
                        (json.dumps({"ss": 2}), sid))
    store._conn.commit()
    assert store.get_subscription(sid).last_skipped == {"ss": 2}
