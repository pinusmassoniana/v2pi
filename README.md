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
- The shipped Compose file runs the container `privileged` with `network_mode: host` so it can own
  the whole gateway on the host (sysctls, the client VLAN, addressing, DHCP, IPv6 RA). This is a
  dedicated single-purpose appliance box — that's the trade-off.

The container is fully self-contained: it bundles a pinned **Xray-core (arm64)** plus
`nftables` / `dnsmasq` / `isc-dhcp-client` / `iproute2`, and on `up` it **provisions the entire
gateway on the host itself** — so the host needs nothing but Docker, and you only configure your router.

## Quickstart (Docker)

**Deploy** — fresh install on an arm64 Linux host with Docker:

```bash
git clone https://github.com/pinusmassoniana/v2pi.git
cd v2pi
docker compose pull      # pull the pre-built arm64 image — no on-device build
docker compose up -d
```

**Update** to the latest published image later:

```bash
cd v2pi
git pull                 # refresh docker-compose.yml if it changed
docker compose pull
docker compose up -d
```

The published image is `ghcr.io/pinusmassoniana/v2pi-x` — `:latest` plus a tag per version
(e.g. `:1.6`). To **build from source** instead (dev / local changes), swap the two `compose`
lines above for `docker compose up -d --build`.

Open `http://<device-ip>:8080` and complete the first-run admin setup. The session secret is
auto-generated and persisted in the data volume; the panel binds `0.0.0.0` (reachable over the LAN,
protected by login).

The shipped `docker-compose.yml` runs `privileged` with `network_mode: host`, sets
`PI_GW_NET_BACKEND=linux` (so the panel applies everything to the host for real — sysctls, the
client VLAN + addresses, nftables tproxy + policy routing, and its own `dnsmasq` for DHCP + IPv6
RA), and persists all state (SQLite, xray config, logs, session secret) in the `v2pi-data` volume.
`restart: unless-stopped` brings it back after a reboot.

**What you set up on your router** (the one box the panel never touches):

- Create the client VLAN (default **VLAN 2**) and tag the client switch port to it.
- **Disable the router's DHCP** on that VLAN — the Pi serves it.
- If you use IPv6, **disable the router's IPv6 / Router Advertisement** on that VLAN — the Pi
  advertises IPv6 itself; a second advertiser makes clients leak around the tunnel. (The panel
  detects this and shows a red banner.)
- Keep the Pi's Home leg (`eth0`) on your normal LAN with internet.

The Network screen verifies each of these visually and lists the exact steps.

**Migrating an existing manual install** (you previously set up `pi-gw-dhcp.service` / `radvd` /
the VLAN by hand): run `scripts/migrate-host.sh` as root on the Pi once — it snapshots state, stops
the legacy host services, hands the segment to the container, and verifies (restoring on failure).
Fresh installs don't need it.

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

## Attribution

Egress country flags use the [DB-IP IP-to-Country Lite](https://db-ip.com/db/download/ip-to-country-lite)
database by DB-IP, licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) (bundled
in the image and refreshed on each rebuild).

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
- Готовый Compose-файл запускает контейнер `privileged` с `network_mode: host`, чтобы он сам владел
  всем шлюзом на хосте (sysctl, клиентский VLAN, адресация, DHCP, IPv6 RA). Это выделенная коробка
  под одну задачу — таков размен.

Контейнер полностью самодостаточен: внутри уже лежат зафиксированный **Xray-core (arm64)** плюс
`nftables` / `dnsmasq` / `isc-dhcp-client` / `iproute2`, и при `up` он **сам разворачивает весь шлюз
на хосте** — так что хосту не нужно ничего, кроме Docker, а вам — только настроить роутер.

## Быстрый старт (Docker)

**Деплой** — установка с нуля на arm64 Linux-хосте с Docker:

```bash
git clone https://github.com/pinusmassoniana/v2pi.git
cd v2pi
docker compose pull      # скачать готовый arm64-образ — без сборки на устройстве
docker compose up -d
```

**Обновление** до свежего опубликованного образа:

```bash
cd v2pi
git pull                 # обновить docker-compose.yml, если менялся
docker compose pull
docker compose up -d
```

Опубликованный образ — `ghcr.io/pinusmassoniana/v2pi-x`: тег `:latest` плюс тег на каждую версию
(например `:1.6`). Чтобы **собрать из исходников** (разработка / локальные правки), замените две
строки `compose` выше на `docker compose up -d --build`.

Откройте `http://<ip-устройства>:8080` и пройдите настройку администратора при первом запуске.
Секрет сессии генерируется автоматически и сохраняется в data-томе; панель слушает `0.0.0.0`
(доступна по локальной сети, защищена логином).

Готовый `docker-compose.yml` запускается `privileged` с `network_mode: host`, выставляет
`PI_GW_NET_BACKEND=linux` (то есть панель реально применяет всё к хосту — sysctl, клиентский VLAN и
адреса, nftables tproxy + policy-routing и собственный `dnsmasq` для DHCP + IPv6 RA) и хранит всё
состояние (SQLite, конфиг xray, логи, секрет сессии) в томе `v2pi-data`. `restart: unless-stopped`
поднимает контейнер после перезагрузки.

**Что настроить на роутере** (единственная коробка, которую панель не трогает):

- Создайте клиентский VLAN (по умолчанию **VLAN 2**) и протегируйте в него клиентский порт свитча.
- **Отключите DHCP роутера** на этом VLAN — его раздаёт Pi.
- Если используете IPv6 — **отключите IPv6 / Router Advertisement роутера** на этом VLAN: RA
  раздаёт сам Pi, а второй источник заставит клиентов течь мимо туннеля. (Панель это детектит и
  показывает красный баннер.)
- Домашнюю ногу Pi (`eth0`) держите в обычной LAN с интернетом.

Экран Network визуально проверяет каждый пункт и показывает точные шаги.

**Миграция существующей ручной установки** (раньше вы вручную поднимали `pi-gw-dhcp.service` /
`radvd` / VLAN): один раз запустите на Pi от root `scripts/migrate-host.sh` — он снимет снапшот,
остановит легаси-сервисы хоста, передаст сегмент контейнеру и проверит результат (с откатом при
ошибке). Чистым установкам это не нужно.

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

## Атрибуция

Флаги стран у egress используют базу [DB-IP IP-to-Country Lite](https://db-ip.com/db/download/ip-to-country-lite)
от DB-IP по лицензии [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) (вшита в образ,
обновляется при каждой пересборке).
