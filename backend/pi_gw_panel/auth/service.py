"""Credential lifecycle over the settings k/v store (Wave 3a).

The single operator credential lives in two settings rows: `auth_username` and
`auth_password_hash` (scrypt). Absent ⇒ first-run setup mode. No env secret."""
import hmac
from pi_gw_panel.auth.auth import hash_password, verify_password_hash

_USER = "auth_username"
_HASH = "auth_password_hash"

# A real-shaped scrypt hash used to burn equal KDF time when the credential (or username) is
# absent/wrong, so a wrong username can't be told apart from a wrong password by response timing
# (username enumeration). Computed once at import.
_DUMMY_HASH = hash_password("\x00unused-dummy-credential\x00")


def needs_setup(store) -> bool:
    """True when no credential has been created yet (first run)."""
    return not store.get_setting(_HASH)


def create_credential(store, username: str, password: str) -> None:
    """Create the one-and-only credential. Refuses if one already exists (the
    open /api/setup route relies on this to be a one-time action)."""
    password_hash = hash_password(password)
    with store.transaction():
        if not needs_setup(store):
            raise ValueError("credential already exists")
        store.set_setting(_USER, username)
        store.set_setting(_HASH, password_hash)


def verify_login(store, username: str, password: str) -> bool:
    """Constant-time check of a username+password against the stored credential.

    Both checks always run (no short-circuit) and the KDF runs even when there is no credential
    (dummy hash) so a wrong username costs the same as a wrong password — no enumeration oracle.
    Usernames are compared as bytes (compare_digest raises TypeError on non-ASCII str, which would
    500 on a Cyrillic username and let an attacker crash /login)."""
    user = store.get_setting(_USER)
    pw_hash = store.get_setting(_HASH)
    ok_user = bool(user) and hmac.compare_digest(
        (user or "").encode("utf-8"), (username or "").encode("utf-8"))
    ok_pw = verify_password_hash(pw_hash or _DUMMY_HASH, password)   # always spends the KDF
    return ok_user and ok_pw


def set_password(store, password: str) -> None:
    """Rotate the password hash (username unchanged) AND bump the session epoch so previously-issued
    sessions are invalidated — keeps the invalidation guarantee at the credential layer, not only in
    whichever caller happens to remember to bump."""
    store.set_setting(_HASH, hash_password(password))
    bump_session_epoch(store)


def session_epoch(store) -> int:
    """Current session epoch (default 0). A session is valid only while its stamped epoch
    matches this — bumping it on a password change signs every other session out."""
    return int(store.get_setting("session_epoch") or "0")


def bump_session_epoch(store) -> int:
    e = session_epoch(store) + 1
    store.set_setting("session_epoch", str(e))
    return e
