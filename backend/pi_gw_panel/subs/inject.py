import hashlib
import hmac
import platform
import urllib.parse
from dataclasses import dataclass


@dataclass
class BuiltRequest:
    method: str
    url: str
    headers: dict
    query: dict


def _subst(value: str, tokens: dict) -> str:
    out = value
    for k, v in tokens.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _no_crlf(value: str) -> str:
    """Strip CR/LF so an admin-editable injection value (or a {token} expansion) can't smuggle
    extra headers into the urllib Request — HTTP header/CRLF injection (audit P3)."""
    return value.replace("\r", "").replace("\n", "")


def build_request(url: str, injection: dict, tokens: dict) -> BuiltRequest:
    """Compose the subscription GET: injected headers + query with {token} substitution.
    Pure — no network. Used by both the live preview and the fetcher."""
    headers = {_no_crlf(str(k)): _no_crlf(_subst(str(v), tokens))
               for k, v in (injection.get("headers") or {}).items()}
    query = {k: _subst(str(v), tokens) for k, v in (injection.get("query") or {}).items()}
    full = url
    if query:
        sep = "&" if urllib.parse.urlparse(url).query else "?"
        full = url + sep + urllib.parse.urlencode(query)
    return BuiltRequest(method="GET", url=full, headers=headers, query=query)


def default_injection() -> dict:
    """Privacy-preserving defaults for a fresh subscription."""
    return {
        "headers": {
            "x-hwid": "{machine_id}",
            "x-device-os": "{device_os}",
            "user-agent": "v2pi/1.0",
        },
        "query": {},
    }


def host_tokens(machine_id: str, *, app_secret: str = "", subscription_id=None) -> dict:
    """Coarse defaults plus explicit ``host_*`` escape hatches for custom provider contracts.

    Existing ``{machine_id}`` templates become a per-subscription pseudonym. An operator must
    deliberately use a ``{host_*}`` token to disclose an exact host fingerprint.
    """
    subject = str(subscription_id if subscription_id is not None else "preview")
    # Production always supplies the persisted app secret. The local/test fallback still varies
    # by host instead of producing one cross-install identifier from the public dev default.
    key_source = app_secret if app_secret and app_secret != "dev-insecure-secret" else machine_id
    key = key_source.encode("utf-8")
    pseudonym = hmac.new(key, subject.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    release = platform.release()
    machine = platform.machine()
    return {
        "machine_id": pseudonym,
        "device_os": platform.system().lower(),
        "device_ver": release.split(".", 1)[0],
        "device_model": "arm" if machine.lower().startswith(("arm", "aarch")) else "other",
        "host_machine_id": machine_id,
        "host_device_ver": release,
        "host_device_model": machine,
    }
