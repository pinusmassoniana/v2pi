import pytest
from pi_gw_panel.db import connect, init_schema
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.auth import service
from pi_gw_panel.auth.auth import hash_password, verify_password_hash


def _store(tmp_path):
    conn = connect(str(tmp_path / "t.sqlite"), check_same_thread=False)
    init_schema(conn)
    return NodeStore(conn)


def test_hash_roundtrip_distinct_salt_and_constant_time():
    h = hash_password("hunter2")
    assert "$" in h and h != "hunter2"
    assert verify_password_hash(h, "hunter2") is True
    assert verify_password_hash(h, "wrong") is False
    assert verify_password_hash("", "x") is False
    assert verify_password_hash("nodollar", "x") is False
    assert hash_password("hunter2") != h            # random per-credential salt


def test_setup_create_login_flow(tmp_path):
    s = _store(tmp_path)
    assert service.needs_setup(s) is True
    service.create_credential(s, "admin", "hunter2")
    assert service.needs_setup(s) is False
    assert service.verify_login(s, "admin", "hunter2") is True
    assert service.verify_login(s, "admin", "nope") is False
    assert service.verify_login(s, "wronguser", "hunter2") is False


def test_create_refuses_second_credential(tmp_path):
    s = _store(tmp_path)
    service.create_credential(s, "admin", "pw")
    with pytest.raises(ValueError):
        service.create_credential(s, "admin2", "pw2")


def test_concurrent_setup_has_one_atomic_winner(tmp_path, monkeypatch):
    import threading

    store = _store(tmp_path)
    barrier = threading.Barrier(2)
    real_hash = service.hash_password

    def synchronized_hash(password: str) -> str:
        result = real_hash(password)
        barrier.wait(timeout=3)
        return result

    monkeypatch.setattr(service, "hash_password", synchronized_hash)
    outcomes: list[tuple[str, str]] = []

    def claim(username: str, password: str) -> None:
        try:
            service.create_credential(store, username, password)
            outcomes.append((username, "ok"))
        except ValueError:
            outcomes.append((username, "conflict"))

    threads = [
        threading.Thread(target=claim, args=("one", "password-one")),
        threading.Thread(target=claim, args=("two", "password-two")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert sorted(result for _, result in outcomes) == ["conflict", "ok"]
    winner = next(username for username, result in outcomes if result == "ok")
    password = "password-one" if winner == "one" else "password-two"
    assert store.get_setting("auth_username") == winner
    assert service.verify_login(store, winner, password)


def test_set_password_rotates_hash(tmp_path):
    s = _store(tmp_path)
    service.create_credential(s, "admin", "old")
    service.set_password(s, "new")
    assert service.verify_login(s, "admin", "new") is True
    assert service.verify_login(s, "admin", "old") is False
