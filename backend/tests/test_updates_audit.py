"""B2: the panel can say it is behind, and can never install anything.

The deploy is a digest re-pin the operator runs; all this does is answer "is there a newer
release?" once a day, through the tunnel when one is up, and survive being offline.
"""
import pytest

from pi_gw_panel import updates


class _Store:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_setting(self, key):
        return self.values.get(key)

    def set_setting(self, key, value):
        self.values[key] = value


class _Supervisor:
    def __init__(self, running=True):
        self._running = running

    def status(self):
        return {"running": self._running}


class _Settings:
    local_proxy_port = 10808


class _State:
    def __init__(self, store, running=True):
        self.store = store
        self.supervisor = _Supervisor(running)
        self.settings = _Settings()


@pytest.mark.parametrize("latest,current,expected", [
    ("v2.1", "2.0.2", True),
    ("v2.0.2", "2.0.2", False),
    ("v2.0.1.3.4", "2.0.2", False),      # a deeper chain is still the EARLIER release here
    ("v2.0.2", "2.0.1.3.4", True),
    ("v2.10", "2.9", True),              # segments are integers, not decimals: 10 > 9
    ("v2.9", "2.10", False),
    ("v26.3.28", "Xray 26.3.27 (Xray, Penetrates Everything.) Custom", True),
    ("v26.3.27", "Xray 26.3.27 (Xray, Penetrates Everything.) Custom", False),
    ("", "2.0.2", False),                # unreadable → never nag
    ("v2.1", "unavailable", False),
])
def test_is_newer_reads_this_project_s_version_chain(latest, current, expected):
    assert updates.is_newer(latest, current) is expected


def test_a_failed_check_is_recorded_not_raised(monkeypatch):
    """An offline gateway is a normal gateway. The check must leave a timestamp and a reason,
    so the card can say "checked 3 h ago, failed" instead of showing nothing at all."""
    store = _Store()
    state = _State(store, running=False)

    def boom(url, **kwargs):
        raise OSError("no route to host")

    monkeypatch.setattr(updates, "fetch_url", boom)
    result = updates.check_now(state, force=True)
    assert result["checked"] is True
    assert store.get_setting(updates.CHECKED_KEY)                    # when we tried
    assert "OSError" in store.get_setting(updates.ERROR_KEY)         # and why it did not work
    assert not store.get_setting(updates.PANEL_KEY)                  # no version invented


def test_a_successful_check_stores_both_tags_and_clears_the_error(monkeypatch):
    # A live tunnel: the check must go through it, the way a subscription fetch does.
    store = _Store({updates.ERROR_KEY: "v2pi: OSError", "active_node_id": "3"})
    state = _State(store)
    seen = []

    def fake(url, *, headers=None, proxy=None, timeout=None):
        seen.append((url, proxy))
        tag = "v2.1" if updates.PANEL_REPO in url else "v26.4.0"
        return ('{"tag_name": "%s"}' % tag, [])

    monkeypatch.setattr(updates, "fetch_url", fake)
    result = updates.check_now(state, force=True)
    assert (result["panel"], result["xray"]) == ("v2.1", "v26.4.0")
    assert store.get_setting(updates.ERROR_KEY) == ""
    # Through the tunnel while one is up, like a subscription fetch.
    assert [proxy for _url, proxy in seen] == ["http://127.0.0.1:10808"] * 2


def test_the_daily_check_obeys_its_switch_but_the_button_does_not(monkeypatch):
    calls = []
    monkeypatch.setattr(updates, "fetch_url",
                        lambda url, **kwargs: (calls.append(url), '{"tag_name": "v9"}')[1])

    off = _State(_Store({updates.ENABLED_KEY: "0"}))
    assert updates.check_now(off) == {"checked": False}
    assert calls == []                                   # the schedule respects the switch

    assert updates.check_now(off, force=True)["checked"] is True
    assert len(calls) == 2                               # the button still works


def test_a_hostile_release_payload_cannot_smuggle_a_tag(monkeypatch):
    """`tag_name` is rendered in the panel, so it is bounded and must actually be a string."""
    state = _State(_Store())
    monkeypatch.setattr(updates, "fetch_url",
                        lambda url, **kwargs: ('{"tag_name": "v%s"}' % ("9" * 500), []))
    updates.check_now(state, force=True)
    assert len(state.store.get_setting(updates.PANEL_KEY)) <= 64

    monkeypatch.setattr(updates, "fetch_url", lambda url, **kwargs: ('{"tag_name": null}', []))
    updates.check_now(state, force=True)
    assert "ValueError" in state.store.get_setting(updates.ERROR_KEY)


def test_the_first_check_after_a_boot_is_soon_and_then_daily():
    import time
    store = _Store()
    scheduler = updates.UpdateScheduler(_State(store), interval_sec=86400.0)
    assert scheduler._initial_delay() == 60.0                        # never checked

    store.set_setting(updates.CHECKED_KEY, str(int(time.time()) - 3600))
    assert 82000 < scheduler._initial_delay() <= 86400               # checked an hour ago

    store.set_setting(updates.CHECKED_KEY, str(int(time.time()) - 200000))
    assert scheduler._initial_delay() == 0.0                         # overdue → now
