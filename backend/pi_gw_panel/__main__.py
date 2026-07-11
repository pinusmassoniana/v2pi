import os
import secrets
import uvicorn
from pi_gw_panel.config import Settings, safe_int
from pi_gw_panel.app import create_app


def ensure_session_secret(data_dir: str) -> str:
    """Read the persisted session secret, generating + persisting a strong one on first
    run so the panel needs no configured secret. Lives in the data dir (a Docker volume),
    so it's stable across restarts and sessions stay valid. Mode 0600."""
    path = os.path.join(data_dir, "session_secret")
    try:
        with open(path) as f:
            existing = f.read().strip()
        if existing:
            return existing
    except OSError:
        pass
    secret = secrets.token_urlsafe(48)
    os.makedirs(data_dir, exist_ok=True)
    with open(path, "w") as f:
        f.write(secret)
    os.chmod(path, 0o600)
    return secret


def main() -> None:
    settings = Settings.from_env()
    # Zero-config: when no real secret is supplied via env, use the persisted/auto-generated one.
    if not settings.session_secret or settings.session_secret == "dev-insecure-secret":
        settings.session_secret = ensure_session_secret(settings.data_dir)
    # safe-int so a typo/stray newline in PI_GW_PORT can't crash boot (audit P2)
    port = safe_int(os.environ.get("PI_GW_PORT", "8080"), 8080, "PI_GW_PORT")
    uvicorn.run(create_app(settings), host=settings.bind_host, port=port, log_level="info")


if __name__ == "__main__":
    main()
