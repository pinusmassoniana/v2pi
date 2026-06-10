def safe_port(value, default: int = 443) -> int | None:
    """Defensive node-port parse shared by the subscription parsers (audit B4): None for
    garbage or out-of-range values, so the caller skips that node instead of crashing the
    refresh or shipping an invalid port into the xray config."""
    try:
        port = int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None
