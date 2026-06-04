# v2pi

<a id="english"></a>
**Self-hosted control panel for an xray VPN gateway — runs on any arm64 Linux device.**

[English](#english) · [Русский](#russian)

v2pi turns any **headless arm64 (aarch64) Linux box** into a managed VPN gateway. It supervises
the xray proxy engine, manages your nodes and subscriptions, tunes anti-DPI evasion per node,
routes traffic by ordered rules, health-checks upstreams with automatic failover, and controls the
device's own network (segment / DHCP / DNS plus a fail-closed kill-switch) — all from a light/dark
web dashboard. No monitor, no keyboard: everything is driven over your LAN.

It is **not** Pi-specific. Any arm64 single-board computer (Raspberry Pi, Orange Pi, Radxa Rock,
NanoPi…), an arm64 mini-PC, or an arm64 VPS that can run Docker will do. The Raspberry Pi 5 is just
the reference device it's developed and tested on (see [Tested on](#tested-on)).

## Features

- **Nodes** — xray node management (VLESS Vision / XHTTP, including XHTTP-over-TLS); subscriptions
  with custom header / query injection and ordered import.
- **Anti-DPI tuning** — per-node profiles: uTLS fingerprint, TLS fragmentation, mux, DoH, QUIC, and
  other anti-DPI knobs, applied live without dropping the tunnel.
- **Routing** — ordered rules (geoip / geosite / domain / ip / port → direct / proxy / block) with
  preset staging, per-rule validation, and an RU-direct preset.
- **Health & traffic** — active probes with automatic failover to a healthy node; a live up/down
  traffic graph fed by xray stats over a WebSocket.
- **Network control** — editable gateway network (segment interface / IP, DHCP range, client DNS)
  with a fail-closed kill-switch, applied to the host as real nftables tproxy + policy routing.
- **Operations** — backup / restore, first-run admin setup, light/dark theming, boot self-heal.

## Requirements

- A 64-bit **arm64 / aarch64 Linux** host (headless is fine — and the point).
- **Docker** + **Docker Compose**.
- Host networking with `NET_ADMIN` / `NET_RAW` (the shipped Compose file already requests these).

The container is fully self-contained: it bundles a pinned **Xray-core (arm64)** plus
`nftables` / `dnsmasq` / `iproute2`, so the host needs nothing but Docker.

## Quickstart (Docker)

Grab `docker-compose.yml` (clone the repo, or just download that one file), then **pull the
pre-built arm64 image** — no on-device build:

```bash
docker compose pull
docker compose up -d
```

The published image is `ghcr.io/pinusmassoniana/v2pi-x` — `:latest` plus an immutable tag per
version (e.g. `:1.5`, `:1.5.x.x`). Prefer to **build from source** instead (dev / local changes)?

```bash
docker compose up -d --build
```

Open `http://<device-ip>:8080` and complete the first-run admin setup. The session secret is
auto-generated and persisted in the data volume; the panel binds `0.0.0.0` (reachable over the LAN,
protected by login).

The shipped `docker-compose.yml` runs with `network_mode: host` and the `NET_ADMIN` / `NET_RAW`
caps, sets `PI_GW_NET_BACKEND=linux` (so network changes are applied to the host for real — nftables
tproxy + policy routing + dnsmasq), and persists all state (SQLite, xray config, logs, session
secret) in the `v2pi-data` volume. `restart: unless-stopped` brings it back after a reboot.

> Dev / CI default to a **dry-run** network backend (`PI_GW_NET_BACKEND` unset or `dryrun`) that
> renders the nftables + dnsmasq rulesets but never touches the host — so you can run the panel on a
> laptop safely. Only `PI_GW_NET_BACKEND=linux` applies for real.

## Tested on

Developed and continuously tested on a **Raspberry Pi 5 Model B**:

| | |
|---|---|
| Board | Raspberry Pi 5 Model B Rev 1.1 (BCM2712) |
| CPU | Quad-core Arm Cortex-A76 @ 2.4 GHz (aarch64) |
| RAM | 16 GB |
| OS | Debian GNU/Linux 13 (trixie), kernel `6.12.75+rpt-rpi-2712` |
| Container engine | Docker 26.1 |
| Bundled Xray-core | v26.3.27 (linux/arm64) |

A Pi 5 is not required — the panel itself is lightweight; any arm64 board that can run Docker and
xray will do.

## Configuration

All optional — the defaults work out of the box. Override via `.env` (see `.env.example`) or the
environment:

| Variable | Default | Purpose |
|---|---|---|
| `PI_GW_SESSION_SECRET` | auto-generated, persisted | session-cookie signing key |
| `PI_GW_BIND_HOST` | `0.0.0.0` | bind address (LAN-reachable, auth-gated) |
| `PI_GW_PORT` | `8080` | HTTP port |
| `PI_GW_DATA_DIR` | `/data` (image) | SQLite + xray config + logs + session secret |
| `PI_GW_XRAY_BIN` | `xray` | xray binary path |
| `PI_GW_NET_BACKEND` | `linux` (Compose) / `dryrun` (dev) | `linux` applies nft + routing + dnsmasq to the host; anything else renders only |

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). In short: backend `cd backend && uv run pytest`; frontend
`cd frontend && npm ci && npm test`; and `python -m pi_gw_panel` runs the whole app locally (with the
safe dry-run network backend by default).

## License

[MIT](LICENSE) © Pinus Massoniana

---

<a id="russian"></a>
# v2pi — Русский

**Self-hosted панель управления VPN-шлюзом на базе xray — работает на любом arm64 Linux-устройстве.**

[English](#english) · [Русский](#russian)

v2pi превращает **любую headless arm64 (aarch64) Linux-машину** в управляемый VPN-шлюз. Она
супервизит прокси-движок xray, управляет нодами и подписками, настраивает обход DPI индивидуально
для каждой ноды, маршрутизирует трафик по упорядоченным правилам, проверяет доступность аплинков с
автопереключением (failover) и управляет собственной сетью устройства (сегмент / DHCP / DNS плюс
fail-closed kill-switch) — всё из веб-дашборда со светлой и тёмной темой. Без монитора и клавиатуры:
всё управляется по локальной сети.

Это **не** привязано к Pi. Подойдёт любой arm64 одноплатник (Raspberry Pi, Orange Pi, Radxa Rock,
NanoPi…), arm64 мини-ПК или arm64 VPS, на котором запускается Docker. Raspberry Pi 5 — лишь
референсное устройство, на котором проект разрабатывается и тестируется (см.
[На чём протестировано](#на-чём-протестировано)).

## Возможности

- **Ноды** — управление нодами xray (VLESS Vision / XHTTP, включая XHTTP-over-TLS); подписки с
  инъекцией произвольных заголовков / query-параметров и упорядоченным импортом.
- **Anti-DPI тюнинг** — профили для каждой ноды: uTLS-fingerprint, фрагментация TLS, mux, DoH, QUIC и
  другие anti-DPI ручки, применяются на лету без обрыва туннеля.
- **Маршрутизация** — упорядоченные правила (geoip / geosite / domain / ip / port → direct / proxy /
  block) со стейджингом пресетов, валидацией каждого правила и пресетом «RU напрямую».
- **Здоровье и трафик** — активные пробы с автопереключением на живую ноду; график трафика
  вверх/вниз в реальном времени по статистике xray через WebSocket.
- **Управление сетью** — редактируемая сеть шлюза (интерфейс / IP сегмента, диапазон DHCP, DNS для
  клиентов) с fail-closed kill-switch, применяется к хосту как реальный nftables tproxy +
  policy-routing.
- **Эксплуатация** — резервное копирование / восстановление, настройка администратора при первом
  запуске, светлая/тёмная тема, самовосстановление после перезагрузки.

## Требования

- 64-битный хост на **arm64 / aarch64 Linux** (headless — это норма и сам смысл).
- **Docker** + **Docker Compose**.
- Host-networking с `NET_ADMIN` / `NET_RAW` (готовый Compose-файл уже их запрашивает).

Контейнер полностью самодостаточен: внутри уже лежат зафиксированный **Xray-core (arm64)** плюс
`nftables` / `dnsmasq` / `iproute2`, так что хосту не нужно ничего, кроме Docker.

## Быстрый старт (Docker)

Возьмите `docker-compose.yml` (склонируйте репозиторий или скачайте только этот файл) и **скачайте
готовый arm64-образ** — без сборки на устройстве:

```bash
docker compose pull
docker compose up -d
```

Опубликованный образ — `ghcr.io/pinusmassoniana/v2pi-x`: тег `:latest` плюс неизменяемый тег на
каждую версию (например `:1.5`, `:1.5.x.x`). Хотите **собрать из исходников** (разработка / локальные
правки)?

```bash
docker compose up -d --build
```

Откройте `http://<ip-устройства>:8080` и пройдите настройку администратора при первом запуске.
Секрет сессии генерируется автоматически и сохраняется в data-томе; панель слушает `0.0.0.0`
(доступна по локальной сети, защищена логином).

Готовый `docker-compose.yml` запускается с `network_mode: host` и капабилити `NET_ADMIN` /
`NET_RAW`, выставляет `PI_GW_NET_BACKEND=linux` (то есть сетевые изменения реально применяются к
хосту — nftables tproxy + policy-routing + dnsmasq) и хранит всё состояние (SQLite, конфиг xray,
логи, секрет сессии) в томе `v2pi-data`. `restart: unless-stopped` поднимает контейнер после
перезагрузки.

> Dev / CI по умолчанию используют **dry-run** сетевой бэкенд (`PI_GW_NET_BACKEND` не задан или
> `dryrun`): он рендерит наборы правил nftables + dnsmasq, но не трогает хост — так панель можно
> безопасно запускать на ноутбуке. Реально применяет изменения только `PI_GW_NET_BACKEND=linux`.

## На чём протестировано

Разрабатывается и постоянно тестируется на **Raspberry Pi 5 Model B**:

| | |
|---|---|
| Плата | Raspberry Pi 5 Model B Rev 1.1 (BCM2712) |
| CPU | 4 ядра Arm Cortex-A76 @ 2.4 ГГц (aarch64) |
| ОЗУ | 16 ГБ |
| ОС | Debian GNU/Linux 13 (trixie), ядро `6.12.75+rpt-rpi-2712` |
| Движок контейнеров | Docker 26.1 |
| Встроенный Xray-core | v26.3.27 (linux/arm64) |

Pi 5 не обязателен — сама панель лёгкая; подойдёт любая arm64-плата, способная запустить Docker и
xray.

## Конфигурация

Всё опционально — значения по умолчанию работают «из коробки». Переопределяется через `.env` (см.
`.env.example`) или переменные окружения:

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `PI_GW_SESSION_SECRET` | генерируется, сохраняется | ключ подписи cookie сессии |
| `PI_GW_BIND_HOST` | `0.0.0.0` | адрес прослушивания (доступ по LAN, под логином) |
| `PI_GW_PORT` | `8080` | HTTP-порт |
| `PI_GW_DATA_DIR` | `/data` (в образе) | SQLite + конфиг xray + логи + секрет сессии |
| `PI_GW_XRAY_BIN` | `xray` | путь к бинарю xray |
| `PI_GW_NET_BACKEND` | `linux` (Compose) / `dryrun` (dev) | `linux` применяет nft + routing + dnsmasq к хосту; иначе — только рендер |

## Разработка

См. [CONTRIBUTING.md](CONTRIBUTING.md). Вкратце: бэкенд `cd backend && uv run pytest`; фронтенд
`cd frontend && npm ci && npm test`; `python -m pi_gw_panel` запускает приложение целиком локально (по
умолчанию — с безопасным dry-run сетевым бэкендом).

## Лицензия

[MIT](LICENSE) © Pinus Massoniana
