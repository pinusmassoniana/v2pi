from pi_gw_panel.net_control.plan import NetPlan, NetResult
from pi_gw_panel.net_control.render import render_nft, render_dnsmasq


class DryRunBackend:
    """macOS / CI backend: renders the ruleset, records it, mutates nothing."""

    def __init__(self):
        self.applied: list[str] = []

    def apply_tproxy(self, plan: NetPlan) -> NetResult:
        # Record the combined nft + dnsmasq render as one entry — changing the
        # segment/DHCP config re-renders both (the real write+reload is the
        # LinuxBackend's job). One entry keeps the apply count == 1.
        text = render_nft(plan) + "\n" + render_dnsmasq(plan)
        self.applied.append(text)
        return NetResult(ok=True, rendered=text)

    def teardown(self) -> NetResult:
        self.applied.clear()
        return NetResult(ok=True)
