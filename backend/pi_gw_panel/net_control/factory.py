import os
from pi_gw_panel.config import Settings
from pi_gw_panel.net_control.dryrun import DryRunBackend


def select_backend(settings: Settings):
    """Pick the net backend from ``PI_GW_NET_BACKEND``.

    ``linux`` → the real ``LinuxBackend`` (applies nft tproxy + policy routing to the
    host; set in the container via ``PI_GW_NET_BACKEND=linux``). Anything else — ``dryrun``,
    unset, or unknown — returns ``DryRunBackend`` (renders only), so dev/CI never
    half-applies real rules by accident. Explicit opt-in is the safety boundary.
    """
    if os.environ.get("PI_GW_NET_BACKEND") == "linux":
        from pi_gw_panel.net_control.linux import LinuxBackend
        return LinuxBackend(settings)
    return DryRunBackend()
