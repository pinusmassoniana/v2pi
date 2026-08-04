<div align="center">

<img src="assets/hero.png" alt="v2pi, self-hosted control panel for an Xray VPN gateway" width="100%">

<br/>

**A web panel that turns a dedicated Linux box into a managed [Xray](https://github.com/XTLS/Xray-core) VPN gateway.** Nodes, anti-DPI tuning, rule-based routing, health failover, remote access, and full control of the host network. No monitor, no keyboard.

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

## Why v2pi

A VPN app covers one device. You install it on the laptop, then on the phone, and the TV, the console,
the e-reader and a guest's phone keep going out over the naked connection. Most of them cannot run a
VPN client at all.

v2pi covers the network instead. One always-on box becomes the gateway for a LAN segment, and
everything that joins that segment goes through the [Xray](https://github.com/XTLS/Xray-core) tunnel
transparently, wired or Wi-Fi, VPN-capable or not. There is nothing to install per device.

It is selective about what it tunnels:

Ordered geoip/geosite rules keep local and in-country traffic direct, so banking and government sites
stay fast and unbroken, and send only the rest through the proxy. There is a one-click RU-direct preset.

Getting through DPI is the point, so nodes speak VLESS Vision, XHTTP and REALITY, and each one carries
its own tuning profile: uTLS fingerprint, TLS fragmentation, mux, DoH. Changes apply to the live tunnel.

If the tunnel drops, the kill-switch stops the segment rather than letting it fall back to the naked
connection. Active probes move traffic to a working node on their own, and a reboot comes back to a
clean gateway without help.

It runs on hardware you own, with no third-party app and no per-seat client, behind a dashboard that
shows traffic, node health and every client lease.

The image ships for amd64 and arm64, but this is not a generic "any Docker host" workload. It needs
Linux host networking, the documented interfaces, and appliance-level privileges on a box dedicated to
the job.

## Features

<table>
<tr>
<td width="50%" valign="top">

### Nodes
Xray node management: VLESS Vision and XHTTP, including XHTTP-over-TLS. Subscriptions support custom
header and query injection, and keep the feed's order on import.

</td>
<td width="50%" valign="top">

### Anti-DPI tuning
Per-node profiles for uTLS fingerprint, TLS fragmentation, mux, DoH, QUIC and the rest. Applying a
profile restarts Xray briefly and reconnects the tunnel.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### Routing
Ordered rules (`geoip` / `geosite` / domain / ip / port to direct, proxy or block) with staged presets,
per-rule validation, and a ready-made RU-direct preset.

</td>
<td width="50%" valign="top">

### Health and traffic
Active probes with automatic failover to a healthy node, plus a live up/down traffic graph fed by Xray
stats over a WebSocket.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### Network control
Editable gateway network (segment interface and IP, DHCP range, client DNS) with a fail-closed
kill-switch, applied to the host as real nftables tproxy and policy routing.

</td>
<td width="50%" valign="top">

### Remote access
A VLESS/Reality inbound so your own devices can dial back into the gateway from outside and reach the
LAN. One credential per device, revocable individually, with a ready-to-import client config.

</td>
</tr>
<tr>
<td colspan="2" valign="top">

### Operations
Backup and restore, first-run admin setup, light and dark themes, and boot self-heal so the box comes
back clean after a reboot.

</td>
</tr>
</table>

## Quickstart (Docker)

You need a 64-bit Linux host (amd64 or arm64, headless is fine) with Docker and Docker Compose.

Fresh install:

```bash
git clone https://github.com/pinusmassoniana/v2pi.git
cd v2pi
cp .env.example .env
# Set V2PI_IMAGE in .env to the manifest digest from the release you picked.
docker compose pull
docker compose up -d
```

Open `https://<device-ip>:8080` and finish the first-run admin setup. The one-time proof is written to
`/data/bootstrap_token` with mode `0600`, printed once to container stderr, and deleted once setup
succeeds. On a LAN bind the panel uses the certificate and key you configure, or generates and keeps
`/data/tls.crt` and `/data/tls.key`. Check that certificate before you type a password. Plain HTTP is
allowed only when the panel is bound to loopback.

A fresh install is fail-closed on purpose: the segment has no path to the internet until you configure
a node and connect it. That is the expected first boot, not a fault.

To update, pick a new immutable digest and review it:

```bash
cd v2pi
git pull                 # refresh docker-compose.yml if it changed
${EDITOR:-vi} .env       # replace V2PI_IMAGE with the new ...@sha256:<digest>
docker compose pull
docker compose up -d
```

> [!WARNING]
> The shipped Compose file runs the container `privileged` with `network_mode: host` so it can own the
> gateway on the host: sysctls, the client VLAN, addressing, DHCP and IPv6 RA. That is the trade-off
> for a single-purpose appliance box. Dropping `privileged` needs a cutover and forced-rollback
> acceptance run on the target hardware first, because `cap_add` alone does not cover the sysctls.

The published image is `ghcr.io/pinusmassoniana/v2pi-x`, a multi-arch manifest for `linux/amd64` and
`linux/arm64`. Compose requires `V2PI_IMAGE` in canonical `image@sha256:digest` form, and Docker
resolves the right architecture from it. Release tags are for finding a version, not for deploying one.
The image bundles a pinned Xray-core with `nftables`, `dnsmasq`, `isc-dhcp-client` and `iproute2`, and
provisions the whole gateway on `up`, so the host needs nothing but Docker and you only configure your
router.

For development, build the Dockerfile directly (`docker build -t v2pi-local .`) or use the
non-container workflow in [CONTRIBUTING.md](CONTRIBUTING.md). Building through Compose needs the same
swap, because Compose tags the built image with the `image:` value and a digest is not a valid tag:
`V2PI_IMAGE=v2pi-local docker compose up --build`. Do not leave a mutable local tag in place of the
production digest on a real gateway.

## Router setup

The panel owns the gateway. Your router is the one box it never touches, and you configure it once:

- Create the client VLAN (default VLAN 2) and tag the client switch port into it.
- Turn off the router's DHCP on that VLAN, because the gateway serves it.
- Leave the gateway's home leg (`eth0`) on your normal LAN with internet.

> [!IMPORTANT]
> If you use IPv6, turn off the router's IPv6 and Router Advertisement on that VLAN. The gateway
> advertises IPv6 itself, and a second advertiser makes clients leak around the tunnel. The panel
> detects this and shows a red banner.

The Network screen checks each of these and lists the exact steps.

<details>
<summary><b>Migrating an existing manual install</b></summary>

<br/>

If you previously set up `pi-gw-dhcp.service`, `radvd` or the VLAN by hand, run
`scripts/migrate-host.sh` as root on the host once. It snapshots the current state, stops the legacy
host services, hands the segment to the container, and verifies the result, restoring on failure.
Fresh installs do not need it.

</details>

## Configuration

> [!TIP]
> `V2PI_IMAGE` is required for a Compose deployment. Runtime overrides live in `.env` (see
> [`.env.example`](.env.example)). Keep secrets and private keys out of source control.

| Variable | Default | Purpose |
|---|---|---|
| `PI_GW_SESSION_SECRET` | auto-generated, persisted | session-cookie signing key |
| `PI_GW_BIND_HOST` | `0.0.0.0` | bind address (LAN-reachable, auth-gated) |
| `PI_GW_PORT` | `8080` | HTTPS port for LAN binds; HTTP only on loopback |
| `PI_GW_TLS_CERT` / `PI_GW_TLS_KEY` | generated for LAN binds | PEM certificate and key paths; set both or neither |
| `PI_GW_DATA_DIR` | `/data` (image) | SQLite, Xray config, logs, session secret |
| `PI_GW_XRAY_BIN` | `xray` | Xray binary path |
| `PI_GW_NET_BACKEND` | `linux` (Compose) / `dryrun` (dev) | `linux` applies nft, routing and dnsmasq to the host; anything else only renders |
| `PI_GW_MGMT_IFACE` / `PI_GW_MGMT_IP` | `eth0` / `192.168.1.120` | management and uplink interface and address |
| `PI_GW_SEGMENT_IFACE` / `PI_GW_SEGMENT_IP` | `eth0.2` / `192.168.10.2` | client-facing interface and gateway address |
| `PI_GW_DHCP_START` / `PI_GW_DHCP_END` / `PI_GW_DHCP_LEASE` | `192.168.10.30` / `.200` / `12h` | client DHCP pool and lease |
| `PI_GW_CLIENT_DNS` / `PI_GW_CLIENT_DNS6` | `1.1.1.1` / Cloudflare IPv6 | DNS handed to clients |

> [!NOTE]
> Dev and CI default to a dry-run network backend (`PI_GW_NET_BACKEND` unset or `dryrun`). It renders
> the nftables and dnsmasq rulesets without touching the host, so you can run the panel on a laptop
> safely. Only `PI_GW_NET_BACKEND=linux` applies anything for real.

API token scopes are deliberately asymmetric. `monitor` reads status, health history and non-secret
telemetry. `read` and `readwrite` are administrator scopes that can expose secret configuration, so
store them as credentials. Restoring a backup validates the document before replacing anything,
restores the network and guard intent, and always finishes disconnected, so you review the restored
node and reconnect it yourself.

## Tested on

Release automation builds and smoke-tests exact image digests for both published architectures, amd64
natively and arm64 under QEMU. These are reference environments, not a claim that every Linux host or
network layout works.

<details open>
<summary><b>amd64</b> Proxmox VE virtual machine <sub>(current reference)</sub></summary>

<br/>

| | |
|---|---|
| Platform | Proxmox VE virtual machine (KVM/QEMU) |
| vCPU | 4 × AMD Ryzen 5 8645HS (x86-64) |
| RAM | 4 GB |
| OS | Ubuntu 24.04.4 LTS, kernel `6.8.0-136-generic` |
| Container engine | Docker 29.5 |
| Bundled Xray-core | v26.3.27 (`linux/amd64`) |

</details>

<details>
<summary><b>arm64</b> Raspberry Pi 5 Model B</summary>

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

Before you put it in front of real traffic, run deployment acceptance on the target gateway: the native
data path and cutover, DNS and IPv4/IPv6 leak checks, readiness, a forced rollback, and recovery of the
previous host network. The portable local and CI suites cannot prove any of that.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). In short:

```bash
cd backend  && uv run --locked pytest  # backend tests
cd frontend && npm ci && npm run check && npm test
cd backend  && uv run --locked python -m pi_gw_panel  # local app, dry-run network backend
```

## License

[AGPL-3.0](LICENSE) © Pinus Massoniana

Egress country flags use the
[DB-IP IP-to-Country Lite](https://db-ip.com/db/download/ip-to-country-lite) database by DB-IP, licensed
under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). It is bundled in the image and
refreshed on each rebuild.
