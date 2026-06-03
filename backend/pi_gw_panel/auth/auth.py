"""Auth primitives for the single-user LAN panel.

Threat model: ONE operator, behind login, bound to the Home interface. As of
Wave 3a the credential (username + password) is **created in the UI at first run**
and stored in the DB (settings k/v), the password scrypt-hashed with a random
per-credential salt (see auth/service.py). This supersedes the earlier single
env-secret model (PI_GW_PASSWORD); we use stdlib hashlib.scrypt — NOT bcrypt
(no third-party dependency). CSRF is double-submit.
"""
import hashlib
import hmac
import secrets

SESSION_AUTHED = "authed"
SESSION_CSRF = "csrf"

# scrypt cost — fine for an infrequent single-user login, incl. on the Pi (ARM).
_SCRYPT = dict(n=16384, r=8, p=1, dklen=32)


def hash_password(password: str) -> str:
    """Salted scrypt hash → 'salt_hex$hash_hex' (random 16-byte salt per call)."""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"{salt.hex()}${dk.hex()}"


def verify_password_hash(stored: str, given: str) -> bool:
    """Constant-time check of `given` against a 'salt$hash' string; never matches
    on empty/malformed input."""
    if not stored or not given or "$" not in stored:
        return False
    salt_hex, hash_hex = stored.split("$", 1)
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    dk = hashlib.scrypt(given.encode(), salt=salt, **_SCRYPT)
    return hmac.compare_digest(dk.hex(), hash_hex)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_matches(session_token: str | None, header_token: str | None) -> bool:
    if not session_token or not header_token:
        return False
    return hmac.compare_digest(session_token, header_token)
