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
SESSION_EPOCH = "epoch"        # bumped on password change → invalidates other sessions
SESSION_LASTSEEN = "last_seen"  # for the optional idle timeout

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
    """Constant-time double-submit comparison.

    Both sides are encoded first: Starlette decodes request headers as latin-1, and
    `hmac.compare_digest` raises TypeError on a `str` holding a codepoint above U+007F. Comparing
    the raw strings turned a non-ASCII `X-CSRF-Token` into a 500 instead of a clean 403."""
    if not session_token or not header_token:
        return False
    return hmac.compare_digest(session_token.encode("utf-8"), header_token.encode("utf-8"))
