import os
import platform
from pi_gw_panel.config import Settings
from pi_gw_panel.net_control.dryrun import DryRunBackend


def select_backend(settings: Settings) -> DryRunBackend:
    """Pick the net backend. Override with PI_GW_NET_BACKEND=dryrun|linux.

    `settings` is reserved for backend construction — the future LinuxBackend
    consumes it (nft/dnsmasq paths). LinuxBackend (real nft/dnsmasq) is introduced
    in the Linux smoke phase; until then non-dryrun selection on Linux raises so we
    never half-apply by accident.
    """
    choice = os.environ.get("PI_GW_NET_BACKEND")
    if choice == "dryrun":
        return DryRunBackend()
    if choice == "linux" or (choice is None and platform.system() == "Linux"):
        raise NotImplementedError("LinuxBackend lands in the pre-cutover Linux phase")
    return DryRunBackend()
