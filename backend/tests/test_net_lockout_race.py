"""Two concurrent `PUT /api/network` edits must not COMBINE into a config that locks the panel out.

The cross-field guard refuses a segment config that would install the kill-switch drop, the tproxy
redirect and the segment's DHCP server onto the management leg. It judges the EFFECTIVE config —
the request merged over the stored plan — precisely so a one-field edit cannot slip past a stored
collision it never mentions.

That merge is only as honest as the plan it reads. While the check ran on arrival, outside
`apply_lock`, two partial edits could each merge safely against the same pre-change plan and then
commit the combination neither of them validated: one setting `client_dns` to an address that is
harmless next to the stored segment, the other moving `segment_ip` onto that resolver's subnet —
where a private destination is never tunneled, so nothing answers DNS for any client.

The interleaving below is real: two threads, the real `apply_lock`, and a wrapper that only
observes it. Neither the check, the store, nor the lock's semantics are stubbed.

Defaults these tests lean on: mgmt eth0 / 192.168.1.120, segment eth0.2 / 192.168.10.2,
client_dns 1.1.1.1.
"""
import threading

from conftest import _build_dryrun_state, _login
from fastapi.testclient import TestClient

from pi_gw_panel import controller
from pi_gw_panel.api import routes
from pi_gw_panel.app import create_app


class _ObservedLock:
    """The real `apply_lock`, wrapped so two requests can be interleaved deterministically.

    The semantics are the lock's own — every acquire and release goes to the real RLock, and code
    nested under a route re-enters it through `controller.apply_lock` as always. The wrapper adds
    two observation points and nothing else: `arrived` is released by each thread just BEFORE it
    blocks on the acquire, and the FIRST thread through is parked inside the critical section on
    `hold` until the test lets it go. Parked there — before the route touches the store — a second
    thread that validates on arrival still reads the pre-change plan, which is the race.
    """

    def __init__(self, real: threading.RLock):
        self._real = real
        self._first = True
        self._guard = threading.Lock()
        self.arrived = threading.Semaphore(0)   # released once per thread reaching the door
        self.inside = threading.Event()         # the first thread now holds the lock
        self.hold = threading.Event()           # ...and stays there until the test sets this

    def __enter__(self):
        self.arrived.release()
        self._real.acquire()
        with self._guard:
            first, self._first = self._first, False
        if first:
            self.inside.set()
            assert self.hold.wait(20), "the test never released the thread holding apply_lock"
        return self

    def __exit__(self, *exc) -> bool:
        self._real.release()
        return False


def _net_client(settings, stub_xray):
    state = _build_dryrun_state(settings, stub_xray)
    c = TestClient(create_app(settings, state=state))
    return c, {"X-CSRF-Token": _login(c)}, state


def test_two_safe_partial_edits_cannot_combine_into_a_lockout(settings, stub_xray, monkeypatch):
    """Each request is safe against the plan on disk; together they are not. The one that commits
    second must be refused — it is validated against what the first one left behind."""
    c, h, state = _net_client(settings, stub_xray)
    gate = _ObservedLock(controller.apply_lock)
    monkeypatch.setattr(routes, "apply_lock", gate)
    results: dict[str, tuple[int, str]] = {}

    def put(name: str, body: dict) -> None:
        r = c.put("/api/network", json=body, headers=h)
        results[name] = (r.status_code, r.text)

    # Safe against the stored plan: the stored resolver (1.1.1.1) is nowhere near 10.44.7.0/24.
    mover = threading.Thread(name="move-segment", target=put, args=(
        "segment", {"segment_ip": "10.44.7.2",
                    "dhcp_start": "10.44.7.30", "dhcp_end": "10.44.7.200"}))
    # Also safe against the stored plan: 10.44.7.9 is outside the stored segment (192.168.10.0/24).
    resolver = threading.Thread(name="move-dns", target=put,
                                args=("dns", {"client_dns": "10.44.7.9"}))

    mover.start()
    assert gate.inside.wait(20), "the first request never reached apply_lock"
    gate.arrived.acquire()                                  # the first thread's own arrival
    resolver.start()
    assert gate.arrived.acquire(timeout=20), "the second request never reached apply_lock"
    gate.hold.set()                     # the second is at the door; let the first one commit
    mover.join(30)
    resolver.join(30)
    assert not mover.is_alive() and not resolver.is_alive(), "a request never finished"

    assert results["segment"][0] == 200, results["segment"]
    status, text = results["dns"]
    assert status == 422, \
        f"the second request was validated against a plan the first had already replaced: {text}"
    assert "client_dns" in text and "segment" in text, \
        f"the refusal did not name the collision the two requests built: {text!r}"
    assert state.store.get_setting("segment_ip") == "10.44.7.2", "the first edit did not land"
    assert (state.store.get_setting("client_dns") or settings.client_dns) == settings.client_dns, \
        "the resolver inside the new segment was persisted"
    assert not any("10.44.7.9" in rendered for rendered in state.net.applied), \
        "the combined config was rendered onto the host"


def test_a_refusal_inside_the_lock_still_stores_nothing_and_applies_nothing(settings, stub_xray):
    """Moving the check under `apply_lock` must not move it past anything: it still runs before the
    candidate record, the transaction and every host command, and it still releases the lock."""
    c, h, state = _net_client(settings, stub_xray)
    applied_before = len(state.net.applied)

    r = c.put("/api/network",
              json={"segment_iface": settings.mgmt_iface, "dhcp_lease": "6h"}, headers=h)

    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "segment_iface" in detail and "mgmt_iface" in detail, \
        f"the operator was not told which two fields collide: {detail!r}"
    assert state.store.get_setting("segment_iface") in (None, ""), \
        "the collision was refused only after it had been persisted"
    assert state.store.get_setting("dhcp_lease") in (None, ""), \
        "an unrelated field of a refused request was persisted"
    assert len(state.net.applied) == applied_before, "a refused config still reached the host"
    # A refusal raised inside the critical section must not leave the lock held: every later apply,
    # failover tick and health probe would hang on it.
    assert controller.apply_lock.acquire(timeout=5), "a 422 left apply_lock held"
    controller.apply_lock.release()


def test_a_legitimate_config_still_applies_through_the_lock(settings, stub_xray):
    """The other half: the re-read under the lock must see committed state, not refuse it. A second
    edit that only makes sense next to the first one's value still lands."""
    c, h, state = _net_client(settings, stub_xray)

    assert c.put("/api/network", json={"segment_iface": "eth1.7", "segment_ip": "10.44.7.2",
                                       "dhcp_start": "10.44.7.30", "dhcp_end": "10.44.7.200",
                                       "client_dns": "1.1.1.1"},
                 headers=h).status_code == 200
    # The gateway itself is a valid resolver — and only recognisable as one against the segment_ip
    # the previous request committed.
    assert c.put("/api/network", json={"client_dns": "10.44.7.2"},
                 headers=h).status_code == 200, "the check under the lock read a stale segment_ip"
    assert state.store.get_setting("client_dns") == "10.44.7.2"
