"""Credential lifecycle over the settings k/v store (Wave 3a).

The single operator credential lives in two settings rows: `auth_username` and
`auth_password_hash` (scrypt). Absent ⇒ first-run setup mode. No env secret."""
import hmac
from pi_gw_panel.auth.auth import hash_password, verify_password_hash

_USER = "auth_username"
_HASH = "auth_password_hash"


def needs_setup(store) -> bool:
    """True when no credential has been created yet (first run)."""
    return not store.get_setting(_HASH)


def create_credential(store, username: str, password: str) -> None:
    """Create the one-and-only credential. Refuses if one already exists (the
    open /api/setup route relies on this to be a one-time action)."""
    if not needs_setup(store):
        raise ValueError("credential already exists")
    store.set_setting(_USER, username)
    store.set_setting(_HASH, hash_password(password))


def verify_login(store, username: str, password: str) -> bool:
    """Constant-time check of a username+password against the stored credential."""
    user = store.get_setting(_USER)
    pw_hash = store.get_setting(_HASH)
    if not user or not pw_hash:
        return False
    return hmac.compare_digest(user, username) and verify_password_hash(pw_hash, password)


def set_password(store, password: str) -> None:
    """Rotate the password hash (username unchanged)."""
    store.set_setting(_HASH, hash_password(password))
