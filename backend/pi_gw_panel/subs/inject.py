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


def build_request(url: str, injection: dict, tokens: dict) -> BuiltRequest:
    """Compose the subscription GET: injected headers + query with {token} substitution.
    Pure — no network. Used by both the live preview and the fetcher."""
    headers = {k: _subst(str(v), tokens) for k, v in (injection.get("headers") or {}).items()}
    query = {k: _subst(str(v), tokens) for k, v in (injection.get("query") or {}).items()}
    full = url
    if query:
        sep = "&" if urllib.parse.urlparse(url).query else "?"
        full = url + sep + urllib.parse.urlencode(query)
    return BuiltRequest(method="GET", url=full, headers=headers, query=query)


def default_injection() -> dict:
    """impl-design defaults for a fresh subscription."""
    return {
        "headers": {
            "x-hwid": "{machine_id}",
            "x-device-os": "{device_os}",
            "x-device-ver": "{device_ver}",
            "x-device-model": "{device_model}",
            "user-agent": "pi-gw-panel/1.0",
        },
        "query": {},
    }


def host_tokens(machine_id: str) -> dict:
    return {
        "machine_id": machine_id,
        "device_os": platform.system().lower(),
        "device_ver": platform.release(),
        "device_model": platform.machine(),
    }
