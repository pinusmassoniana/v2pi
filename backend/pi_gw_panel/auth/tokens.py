"""API token primitives.

A token is a 256-bit random secret with a recognizable prefix (`pgwp_`). Only its SHA-256
hash is stored; the full secret is shown once at creation. A fast hash is correct here (unlike
the scrypt password hash): the secret is high-entropy random, so there is no brute-force surface —
the lookup is a constant-work indexed equality on the hash.
"""
import hashlib
import secrets

PREFIX = "pgwp_"
SCOPES = ("read", "readwrite")


def generate() -> tuple[str, str, str]:
    """Return (full_token, sha256_hex_hash, display_prefix).

    `full_token` is returned to the operator once and never stored. `display_prefix` (first 12
    chars, e.g. `pgwp_AbC123`) is stored for identifying a row in the token list — it is NOT a
    secret (it's a tiny, non-reversible slice that can't authenticate)."""
    full = PREFIX + secrets.token_urlsafe(32)
    return full, hash_token(full), full[:12]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
