import os
import secrets
import subprocess
import sys
from pathlib import Path
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
        if len(existing.encode("utf-8")) >= 32:
            return existing
    except OSError:
        pass
    secret = secrets.token_urlsafe(48)
    os.makedirs(data_dir, exist_ok=True)
    with open(path, "w") as f:
        f.write(secret)
    os.chmod(path, 0o600)
    return secret


def ensure_bootstrap_token(data_dir: str) -> str:
    """Return the one-time first-run proof, creating it atomically with mode 0600."""
    os.makedirs(data_dir, mode=0o700, exist_ok=True)
    path = os.path.join(data_dir, "bootstrap_token")
    try:
        existing = Path(path).read_text().strip()
        if len(existing) >= 32:
            return existing
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = Path(path).read_text().strip()
        if len(existing) >= 32:
            return existing
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    with os.fdopen(fd, "w") as handle:
        handle.write(token)
    os.chmod(path, 0o600)
    return token


def ensure_tls_certificate(settings: Settings) -> tuple[str, str]:
    """Create a persisted self-signed management certificate for direct LAN serving."""
    if settings.tls_enabled:
        return settings.tls_cert, settings.tls_key
    cert = os.path.join(settings.data_dir, "tls.crt")
    key = os.path.join(settings.data_dir, "tls.key")
    if os.path.isfile(cert) and os.path.isfile(key):
        settings.tls_cert, settings.tls_key = cert, key
        return cert, key
    settings.ensure_dirs()
    cert_tmp, key_tmp = cert + ".tmp", key + ".tmp"
    for path in (cert_tmp, key_tmp):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", key_tmp, "-out", cert_tmp, "-days", "825",
        "-subj", "/CN=pi-gw-panel",
        "-addext", f"subjectAltName=DNS:pi-gw-panel,IP:{settings.mgmt_ip}",
    ], check=True, capture_output=True, timeout=30)
    os.chmod(key_tmp, 0o600)
    os.chmod(cert_tmp, 0o644)
    os.replace(key_tmp, key)
    os.replace(cert_tmp, cert)
    settings.tls_cert, settings.tls_key = cert, key
    return cert, key


def main() -> None:
    settings = Settings.from_env()
    # Zero-config: when no real secret is supplied via env, use the persisted/auto-generated one.
    if not settings.session_secret or settings.session_secret == "dev-insecure-secret":
        settings.session_secret = ensure_session_secret(settings.data_dir)
    if not settings.loopback_bind and not settings.tls_enabled:
        ensure_tls_certificate(settings)
    from pi_gw_panel.state import build_state
    from pi_gw_panel.auth import service as auth_service
    state = build_state(settings)
    if auth_service.needs_setup(state.store) and not settings.loopback_bind:
        bootstrap = ensure_bootstrap_token(settings.data_dir)
        print(f"pi-gw-panel first-run bootstrap token: {bootstrap}", file=sys.stderr)
    # safe-int so a typo/stray newline in PI_GW_PORT can't crash boot (audit P2)
    port = safe_int(os.environ.get("PI_GW_PORT", "8080"), 8080, "PI_GW_PORT")
    uvicorn.run(create_app(settings, state=state), host=settings.bind_host, port=port,
                log_level="info", ssl_certfile=settings.tls_cert or None,
                ssl_keyfile=settings.tls_key or None)


if __name__ == "__main__":
    main()
