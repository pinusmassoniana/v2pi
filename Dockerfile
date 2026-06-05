# syntax=docker/dockerfile:1
# v2pi panel — single self-contained multi-arch image (amd64 + arm64; panel supervises xray inside).
# Host-net + caps + bundled nft/dnsmasq/iproute2; the real nft/dnsmasq apply
# (LinuxBackend) is enabled in production via PI_GW_NET_BACKEND=linux (see compose).
ARG NODE_VERSION=20-bookworm-slim
ARG PYTHON_VERSION=3.13-slim-bookworm
ARG XRAY_VERSION=v26.3.27

# --- frontend: build the SPA into /spa (decoupled from the repo's ../backend outDir) ---
FROM node:${NODE_VERSION} AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npx vite build --outDir /spa --emptyOutDir

# --- runtime: panel + pinned xray + nft/dnsmasq/iproute2 (cutover-ready) ---
FROM python:${PYTHON_VERSION} AS runtime
ARG XRAY_VERSION
# buildx sets TARGETARCH per target platform (amd64/arm64); for a plain non-BuildKit
# `docker build` it's empty, so fall back to the base image's own dpkg arch (same names).
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends \
      nftables dnsmasq isc-dhcp-client iproute2 ca-certificates curl unzip gzip tini \
    && rm -rf /var/lib/apt/lists/*
# DB-IP IP-to-Country Lite (CC-BY-4.0) for the egress country flag. Try the current month, fall
# back to the previous one (a build on the 1st may pre-date that month's file). Best-effort: if
# the download fails the panel just shows egress IPs without flags (geo degrades to None).
RUN set -eu; \
    for ym in "$(date +%Y-%m)" "$(date -d 'last month' +%Y-%m)"; do \
      if curl -fsSL "https://download.db-ip.com/free/dbip-country-lite-${ym}.mmdb.gz" -o /tmp/cc.gz; then \
        gunzip -c /tmp/cc.gz > /usr/local/share/dbip-country-lite.mmdb && break; \
      fi; \
    done; \
    rm -f /tmp/cc.gz; \
    ls -l /usr/local/share/dbip-country-lite.mmdb || echo "WARN: geoip db not bundled (flags disabled)"
# arch-aware xray fetch — XTLS names the amd64 asset `Xray-linux-64.zip`, arm64 `…-arm64-v8a.zip`.
RUN set -eu; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      arm64) XR=Xray-linux-arm64-v8a.zip ;; \
      amd64) XR=Xray-linux-64.zip ;; \
      *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/xray.zip \
      "https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/${XR}" \
    && unzip -o /tmp/xray.zip xray geoip.dat geosite.dat -d /usr/local/bin/ \
    && chmod +x /usr/local/bin/xray && rm /tmp/xray.zip
WORKDIR /app
COPY backend/ /app/backend/
COPY --from=frontend /spa /app/backend/pi_gw_panel/static
RUN pip install --no-cache-dir /app/backend
ENV PI_GW_DATA_DIR=/data PI_GW_PORT=8080
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=4s --start-period=20s \
  CMD curl -fsS "http://127.0.0.1:${PI_GW_PORT}/api/health" || exit 1
ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "pi_gw_panel"]
