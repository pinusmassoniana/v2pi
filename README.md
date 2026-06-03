# v2pi

A self-hosted control panel for a Raspberry Pi VPN gateway — a FastAPI + Svelte
replacement for v2rayA. It manages xray nodes, subscriptions, anti-DPI tuning, routing,
health checks with auto-failover, a live traffic graph, and the Pi's own network
(segment / DHCP / DNS + a fail-closed kill-switch), behind a light/dark dashboard UI.

> **Status:** the network apply path runs in **dry-run** — it renders the nftables +
> dnsmasq rulesets but does not apply them to the host yet. Real apply (the Linux backend)
> plus an install script and systemd unit are the next milestone.

## Features

- xray node management (VLESS Vision / XHTTP); subscriptions with header/query injection
- Per-node anti-DPI tuning profiles (fingerprint, TLS fragmentation, mux, DoH, QUIC)
- Ordered routing rules (geoip / geosite / domain / ip / port → direct / proxy / block) + an RU-direct preset
- Health probes with automatic failover; live up/down traffic graph (xray stats over a WebSocket)
- Editable Pi network (segment iface/IP, DHCP range, client DNS) + a fail-closed kill-switch
- Backup / restore, first-run admin setup, light/dark theming

## Quickstart (Docker, Raspberry Pi arm64)

Requires Docker + Docker Compose on a 64-bit Pi OS. No configuration needed:

```bash
docker compose up -d --build
```

Open `http://<pi-ip>:8080` and complete the first-run admin setup. The session secret is
auto-generated and persisted in the data volume; the panel binds `0.0.0.0` (reachable over
the LAN, protected by login).

The container uses host networking with `NET_ADMIN` / `NET_RAW`, bundles a pinned Xray-core
(arm64) plus nftables / dnsmasq / iproute2, and persists data (SQLite, xray config, logs,
session secret) in the `v2pi-data` volume.

## Configuration

All optional — the defaults work out of the box. Override via `.env` (see `.env.example`)
or the environment:

| Variable | Default | Purpose |
|---|---|---|
| `PI_GW_SESSION_SECRET` | auto-generated, persisted | session-cookie signing key |
| `PI_GW_BIND_HOST` | `0.0.0.0` | uvicorn bind address |
| `PI_GW_PORT` | `8080` | uvicorn port |
| `PI_GW_DATA_DIR` | `/data` (image) | SQLite + xray config + logs |
| `PI_GW_XRAY_BIN` | `xray` | xray binary path |
| `PI_GW_NET_BACKEND` | `dryrun` | network backend (`dryrun` only, for now) |

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). In short: backend `cd backend && uv run pytest`;
frontend `cd frontend && npm ci && npm test`; and `python -m pi_gw_panel` runs the whole app
locally.

## License

[MIT](LICENSE) © Pinus Massoniana
