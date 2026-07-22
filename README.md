<div align="center">

<img src="assets/hero.png" alt="v2pi — self-hosted control panel for an Xray VPN gateway" width="100%">

<br/>

**Turns a dedicated Linux gateway in the supported reference topology into a managed [Xray](https://github.com/XTLS/Xray-core) VPN gateway** — nodes, anti-DPI tuning, rule-based routing, health failover, and full host-network control, from one light/dark web dashboard. No monitor, no keyboard.

<br/>

[![CI](https://github.com/pinusmassoniana/v2pi/actions/workflows/ci-release.yml/badge.svg)](https://github.com/pinusmassoniana/v2pi/actions/workflows/ci-release.yml)
[![Release](https://img.shields.io/github/v/release/pinusmassoniana/v2pi?label=release&color=3fd17e)](https://github.com/pinusmassoniana/v2pi/releases)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-3fd17e.svg)](LICENSE)
[![GHCR image](https://img.shields.io/badge/ghcr.io-v2pi--x-2496ED?logo=docker&logoColor=white)](https://github.com/pinusmassoniana/v2pi/pkgs/container/v2pi-x)
![Platform](https://img.shields.io/badge/platform-amd64%20%C2%B7%20arm64-30363d)

![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![Svelte](https://img.shields.io/badge/Svelte-5-FF3E00?logo=svelte&logoColor=white)
![Xray-core](https://img.shields.io/badge/Xray--core-26.3.27-000000)
![SQLite](https://img.shields.io/badge/SQLite-003B57?logo=sqlite&logoColor=white)
![nftables](https://img.shields.io/badge/nftables-tproxy-30363d)

<br/>

**English** · [Русский](README.ru.md)

[Why](#why-v2pi) · [Features](#features) · [Quickstart](#quickstart-docker) · [Router setup](#router-setup) · [Configuration](#configuration) · [Tested on](#tested-on) · [Development](#development)

</div>

## Why v2pi?

Commercial VPN apps protect **one device at a time** — you install a client on your laptop, another on
your phone, and hope you remembered the rest. Your TV, console, e-reader, smart-home gadgets, a guest's
phone? Most can't run a VPN client at all, so they quietly leak around it.

**v2pi flips that around: you protect the _network_, not each device.** One small always-on box becomes
the gateway for an entire LAN segment. Every device that joins it — wired or Wi-Fi, VPN-capable or not —
is routed through your [Xray](https://github.com/XTLS/Xray-core) tunnel transparently. Nothing to
install and nothing to configure per device: plug in, and the whole segment is covered.

And it's **smart** about it — this isn't "shove everything through one proxy":

- **Only what needs the tunnel takes it.** Ordered geoip/geosite rules keep local and in-country
  traffic direct (fast, nothing breaks) and send the rest out through the proxy — with a one-click
  **RU-direct** preset.
- **Built to get through DPI.** VLESS Vision / XHTTP / REALITY plus per-node anti-DPI tuning — uTLS
  fingerprints, TLS fragmentation, mux, DoH — applied **live** to slip past deep-packet inspection and
  blocking.
- **Fail-closed by default.** If the tunnel drops, the kill-switch stops the whole segment from leaking
  around it — no device silently falls back to the naked connection.
- **Stays up on its own.** Active health probes fail over to a working node automatically, and a reboot
  self-heals straight back to a clean gateway.
- **Yours, not a subscription.** It runs on hardware you own — an inexpensive Pi or any mini-PC — with
  no third-party app and no per-seat client, behind a live NOC-style dashboard that shows traffic, node
  health, and every client lease.

The image ships for amd64 and arm64, but the gateway is not a generic "any Docker host" workload: it
requires Linux host networking, the documented interfaces, and appliance-level privileges. Raspberry
Pi 5 is the native reference target; other hardware must pass the same deployment acceptance checks.

## Features

<table>
<tr>
<td width="50%" valign="top">

### 🔌 Nodes
Xray node management — VLESS Vision / XHTTP, including **XHTTP-over-TLS**. Subscriptions with custom
header / query injection and ordered import.

</td>
<td width="50%" valign="top">

### 🛡️ Anti-DPI tuning
Per-node profiles: uTLS fingerprint, TLS fragmentation, mux, DoH, QUIC and other anti-DPI knobs.
Applying a profile briefly restarts Xray and reconnects the tunnel.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🧭 Routing
Ordered rules (`geoip` / `geosite` / domain / ip / port → direct / proxy / block) with preset staging,
per-rule validation, and a ready-made **RU-direct** preset.

</td>
<td width="50%" valign="top">

### 📈 Health & traffic
Active probes with **automatic failover** to a healthy node, plus a live up/down traffic graph fed by
Xray stats over a WebSocket.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🌐 Network control
Editable gateway network (segment interface / IP, DHCP range, client DNS) with a **fail-closed
kill-switch**, applied to the host as real nftables tproxy + policy routing.

</td>
<td width="50%" valign="top">

### 🧰 Operations
Backup / restore, first-run admin setup, light/dark theming, and boot self-heal — so the box comes
back clean after a reboot.

</td>
</tr>
</table>

## Quickstart (Docker)

**Requirements:** a 64-bit **Linux** host (amd64/x86-64 or arm64/aarch64, headless is fine) with
**Docker** + **Docker Compose**.

**Deploy** — fresh install:

```bash
git clone https://github.com/pinusmassoniana/v2pi.git
cd v2pi
cp .env.example .env
# Replace V2PI_IMAGE in .env with the manifest digest from the chosen GitHub release/package.
docker compose pull      # pull the pre-built multi-arch image — Docker picks your host's arch, no on-device build
docker compose up -d
```

Open `https://<device-ip>:8080` and complete the first-run admin setup with the one-time proof written
to `/data/bootstrap_token` and printed once to container stderr. The file is mode `0600` and is removed
after successful setup. With a LAN bind, the panel uses a configured certificate/key pair or creates
and persists `/data/tls.crt` and `/data/tls.key`; verify/accept that certificate before entering
credentials. Plain HTTP is allowed only for an explicit loopback bind. The session secret is generated
and persisted in the data volume when it is not supplied.

A fresh installation is deliberately **fail-closed**: segment traffic has no Internet path until a node
has been configured and successfully connected. This is expected first-boot behaviour, not an outage.

**Update** by choosing and reviewing a new immutable manifest digest:

```bash
cd v2pi
git pull                 # refresh docker-compose.yml if it changed
${EDITOR:-vi} .env       # replace V2PI_IMAGE with the new ...@sha256:<digest>
docker compose pull
docker compose up -d
```

> [!WARNING]
> The shipped Compose file runs the container **`privileged`** with **`network_mode: host`** so it can
> own the whole gateway on the host (sysctls, the client VLAN, addressing, DHCP, IPv6 RA). This is a
> dedicated single-purpose appliance box — that's the trade-off. Removing `privileged` is supported
> only after native Pi cutover and forced-rollback acceptance prove that sysctls and host networking
> still work; `cap_add` alone is not assumed sufficient.

The published image is `ghcr.io/pinusmassoniana/v2pi-x` — a multi-arch manifest (`linux/amd64` +
`linux/arm64`). Compose requires `V2PI_IMAGE` in canonical `image@sha256:digest` form; release tags are
discovery aids and are not deployment inputs. Docker resolves the matching architecture from that
immutable manifest. The image bundles a pinned **Xray-core** plus
`nftables` / `dnsmasq` / `isc-dhcp-client` / `iproute2`, and on `up` it provisions the entire gateway
on the host itself — so the host needs nothing but Docker, and you only configure your router.

For development, build the Dockerfile directly (`docker build -t v2pi-local .`) or use the non-container
workflow in [CONTRIBUTING.md](CONTRIBUTING.md). Do not silently replace the production digest pin with a
mutable local tag on a gateway.

## Router setup

The panel owns the gateway; your router is **the one box it never touches**. Configure it once:

- Create the client VLAN (default **VLAN 2**) and tag the client switch port to it.
- **Disable the router's DHCP** on that VLAN — the gateway serves it.
- Keep the gateway's Home leg (`eth0`) on your normal LAN with internet.

> [!IMPORTANT]
> If you use IPv6, **disable the router's IPv6 / Router Advertisement** on that VLAN — the gateway
> advertises IPv6 itself, and a second advertiser makes clients leak around the tunnel. The panel
> detects this and shows a red banner.

The **Network** screen verifies each of these visually and lists the exact steps.

<details>
<summary><b>Migrating an existing manual install</b></summary>

<br/>

If you previously set up `pi-gw-dhcp.service` / `radvd` / the VLAN by hand, run `scripts/migrate-host.sh`
as root on the host **once** — it snapshots state, stops the legacy host services, hands the segment to
the container, and verifies (restoring on failure). Fresh installs don't need it.

</details>

## Configuration

> [!TIP]
> `V2PI_IMAGE` is required for Compose deployment. Runtime overrides live in `.env` (see
> [`.env.example`](.env.example)); keep secrets and private keys out of source control.

| Variable | Default | Purpose |
|---|---|---|
| `PI_GW_SESSION_SECRET` | auto-generated, persisted | session-cookie signing key |
| `PI_GW_BIND_HOST` | `0.0.0.0` | bind address (LAN-reachable, auth-gated) |
| `PI_GW_PORT` | `8080` | HTTPS port for LAN binds; HTTP only on loopback |
| `PI_GW_TLS_CERT` / `PI_GW_TLS_KEY` | generated for LAN binds | PEM certificate and private-key paths; both or neither |
| `PI_GW_DATA_DIR` | `/data` (image) | SQLite + Xray config + logs + session secret |
| `PI_GW_XRAY_BIN` | `xray` | Xray binary path |
| `PI_GW_NET_BACKEND` | `linux` (Compose) / `dryrun` (dev) | `linux` applies nft + routing + dnsmasq to the host; anything else renders only |
| `PI_GW_MGMT_IFACE` / `PI_GW_MGMT_IP` | `eth0` / `192.168.1.120` | management/uplink interface and address |
| `PI_GW_SEGMENT_IFACE` / `PI_GW_SEGMENT_IP` | `eth0.2` / `192.168.10.2` | client-facing interface and gateway address |
| `PI_GW_DHCP_START` / `PI_GW_DHCP_END` / `PI_GW_DHCP_LEASE` | `192.168.10.30` / `.200` / `12h` | client DHCP pool and lease |
| `PI_GW_CLIENT_DNS` / `PI_GW_CLIENT_DNS6` | `1.1.1.1` / Cloudflare IPv6 | DNS handed to clients |

> [!NOTE]
> Dev / CI default to a **dry-run** network backend (`PI_GW_NET_BACKEND` unset or `dryrun`) that renders
> the nftables + dnsmasq rulesets but never touches the host — so you can run the panel on a laptop
> safely. Only `PI_GW_NET_BACKEND=linux` applies for real.

API token scopes are intentionally asymmetric. `monitor` can read only safe status, health/history,
and non-secret telemetry. Legacy `read` and `readwrite` are administrator scopes and may expose secret
configuration; store them as credentials. A strict backup restore validates before replacing state,
restores the network/guard intent, and always finishes **disconnected** so the operator explicitly
reviews and reconnects the restored node.

## Tested on

Release automation builds and smokes exact image digests for both published architectures (native
amd64 and arm64 under QEMU). The following are reference environments, not a promise that every Linux
host or network layout is compatible:

<details open>
<summary><b>arm64</b> — Raspberry Pi 5 Model B <sub>(reference device)</sub></summary>

<br/>

| | |
|---|---|
| Board | Raspberry Pi 5 Model B Rev 1.1 (BCM2712) |
| CPU | Quad-core Arm Cortex-A76 @ 2.4 GHz (aarch64) |
| RAM | 16 GB |
| OS | Debian GNU/Linux 13 (trixie), kernel `6.12.75+rpt-rpi-2712` |
| Container engine | Docker 26.1 |
| Bundled Xray-core | v26.3.27 (`linux/arm64`) |

</details>

<details>
<summary><b>amd64</b> — Proxmox VE virtual machine</summary>

<br/>

| | |
|---|---|
| Platform | Proxmox VE virtual machine (KVM/QEMU) |
| vCPU | 4 × AMD Ryzen 5 8645HS (x86-64) |
| RAM | 4 GB |
| OS | Ubuntu 24.04.4 LTS, kernel `6.8.0-134-generic` |
| Container engine | Docker 29.5 |
| Bundled Xray-core | v26.3.27 (`linux/amd64`) |

</details>

Before production, run deployment acceptance on the target gateway: native data-path/cutover, DNS and
IPv4/IPv6 leak checks, readiness, forced rollback, and recovery of the prior host network. Those tests
cannot be proven by the portable local/CI suite.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). In short:

```bash
cd backend  && uv run --locked pytest  # backend tests
cd frontend && npm ci && npm run check && npm test
cd backend  && uv run --locked python -m pi_gw_panel  # local app; dry-run network backend
```

## License

[AGPL-3.0](LICENSE) © Pinus Massoniana

**Attribution** — egress country flags use the
[DB-IP IP-to-Country Lite](https://db-ip.com/db/download/ip-to-country-lite) database by DB-IP, licensed
under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) (bundled in the image and refreshed on
each rebuild).
