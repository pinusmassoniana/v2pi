<div align="center">

# v2pi

**Self-hosted control panel that turns any headless Linux box into a managed [Xray](https://github.com/XTLS/Xray-core) VPN gateway.**

Nodes · anti-DPI tuning · rule-based routing · health failover · full host-network control — from one light/dark web dashboard. No monitor, no keyboard.

<br/>

[![CI](https://github.com/pinusmassoniana/v2pi/actions/workflows/ci-release.yml/badge.svg)](https://github.com/pinusmassoniana/v2pi/actions/workflows/ci-release.yml)
[![Release](https://img.shields.io/github/v/release/pinusmassoniana/v2pi?label=release&color=3fd17e)](https://github.com/pinusmassoniana/v2pi/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-3fd17e.svg)](LICENSE)
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

[Features](#features) · [Quickstart](#quickstart-docker) · [Router setup](#router-setup) · [Configuration](#configuration) · [Tested on](#tested-on) · [Development](#development)

</div>

## Overview

**v2pi** turns any **headless Linux box** — **amd64/x86-64** (Intel or AMD) **or arm64/aarch64** — into a
managed VPN gateway. It supervises the Xray proxy engine, manages your nodes and subscriptions, tunes
anti-DPI evasion per node, routes traffic by ordered rules, health-checks upstreams with automatic
failover, and controls the device's own network (segment / DHCP / DNS plus a fail-closed kill-switch) —
all from a light/dark web dashboard, driven entirely over your LAN.

It is **not** Pi-specific. Any x86-64 mini-PC, thin client, or VPS — or any arm64 single-board computer
(Raspberry Pi, Orange Pi, Radxa Rock, NanoPi…) that runs Docker — will do. The image is built and
published for **both architectures**; the Raspberry Pi 5 is just the [reference device](#tested-on)
it's developed and tested on.

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
Per-node profiles: uTLS fingerprint, TLS fragmentation, mux, DoH, QUIC and other anti-DPI knobs —
applied **live**, without dropping the tunnel.

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
docker compose pull      # pull the pre-built multi-arch image — Docker picks your host's arch, no on-device build
docker compose up -d
```

Open `http://<device-ip>:8080` and complete the first-run admin setup. The session secret is
auto-generated and persisted in the data volume; the panel binds `0.0.0.0` (reachable over the LAN,
protected by login).

**Update** to the latest published image later:

```bash
cd v2pi
git pull                 # refresh docker-compose.yml if it changed
docker compose pull
docker compose up -d
```

> [!WARNING]
> The shipped Compose file runs the container **`privileged`** with **`network_mode: host`** so it can
> own the whole gateway on the host (sysctls, the client VLAN, addressing, DHCP, IPv6 RA). This is a
> dedicated single-purpose appliance box — that's the trade-off.

The published image is `ghcr.io/pinusmassoniana/v2pi-x` — a multi-arch manifest (`linux/amd64` +
`linux/arm64`), `:latest` plus a tag per version (e.g. `:1.14`); `docker compose pull` resolves your
host's architecture automatically. It's fully self-contained: it bundles a pinned **Xray-core** plus
`nftables` / `dnsmasq` / `isc-dhcp-client` / `iproute2`, and on `up` it provisions the entire gateway
on the host itself — so the host needs nothing but Docker, and you only configure your router.

<details>
<summary><b>Build from source instead</b> (dev / local changes)</summary>

<br/>

Swap the two `compose` lines above for a local build (it builds for the host's own arch):

```bash
docker compose up -d --build
```

</details>

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
> Everything is optional — the defaults work out of the box. Override via `.env` (see
> [`.env.example`](.env.example)) or the environment.

| Variable | Default | Purpose |
|---|---|---|
| `PI_GW_SESSION_SECRET` | auto-generated, persisted | session-cookie signing key |
| `PI_GW_BIND_HOST` | `0.0.0.0` | bind address (LAN-reachable, auth-gated) |
| `PI_GW_PORT` | `8080` | HTTP port |
| `PI_GW_DATA_DIR` | `/data` (image) | SQLite + Xray config + logs + session secret |
| `PI_GW_XRAY_BIN` | `xray` | Xray binary path |
| `PI_GW_NET_BACKEND` | `linux` (Compose) / `dryrun` (dev) | `linux` applies nft + routing + dnsmasq to the host; anything else renders only |

> [!NOTE]
> Dev / CI default to a **dry-run** network backend (`PI_GW_NET_BACKEND` unset or `dryrun`) that renders
> the nftables + dnsmasq rulesets but never touches the host — so you can run the panel on a laptop
> safely. Only `PI_GW_NET_BACKEND=linux` applies for real.

## Tested on

<details open>
<summary>Developed and continuously tested on a <b>Raspberry Pi 5 Model B</b></summary>

<br/>

| | |
|---|---|
| Board | Raspberry Pi 5 Model B Rev 1.1 (BCM2712) |
| CPU | Quad-core Arm Cortex-A76 @ 2.4 GHz (aarch64) |
| RAM | 16 GB |
| OS | Debian GNU/Linux 13 (trixie), kernel `6.12.75+rpt-rpi-2712` |
| Container engine | Docker 26.1 |
| Bundled Xray-core | v26.3.27 (`linux/arm64` on this board; the image also ships `linux/amd64`) |

</details>

A Pi 5 is **not** required — the panel itself is lightweight; any amd64/x86-64 (Intel or AMD) or arm64
host that can run Docker and Xray will do.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). In short:

```bash
cd backend  && uv run pytest           # backend tests
cd frontend && npm ci && npm test      # frontend tests
python -m pi_gw_panel                   # run the whole app locally (safe dry-run network backend by default)
```

## License

[MIT](LICENSE) © Pinus Massoniana

**Attribution** — egress country flags use the
[DB-IP IP-to-Country Lite](https://db-ip.com/db/download/ip-to-country-lite) database by DB-IP, licensed
under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) (bundled in the image and refreshed on
each rebuild).
