# syntax=docker/dockerfile:1
# v2pi panel — single self-contained multi-arch image (amd64 + arm64; panel supervises xray inside).
# Host-net + caps + bundled nft/dnsmasq/iproute2; the real nft/dnsmasq apply
# (LinuxBackend) is enabled in production via PI_GW_NET_BACKEND=linux (see compose).
ARG NODE_VERSION=20-bookworm-slim
ARG NODE_IMAGE_DIGEST=sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0
ARG PYTHON_VERSION=3.13-slim-bookworm
ARG PYTHON_IMAGE_DIGEST=sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64
ARG XRAY_VERSION=v26.3.27
ARG UV_VERSION=0.11.6
ARG UV_IMAGE_DIGEST=sha256:b1e699368d24c57cda93c338a57a8c5a119009ba809305cc8e86986d4a006754
ARG DBIP_VERSION=2026-07
ARG DBIP_SHA256=989c57a9ad1c1c93032e28acc643afdf03597ea28480520f6f1c76ea6420507f
# SHA256 of the pinned release assets (from the release's .dgst files) — release assets are
# mutable on GitHub, so the version pin alone doesn't guarantee the bytes. Bump together
# with XRAY_VERSION.
ARG XRAY_SHA256_AMD64=23cd9af937744d97776ee35ecad4972cf4b2109d1e0fe6be9930467608f7c8ae
ARG XRAY_SHA256_ARM64=4d30283ae614e3057f730f67cd088a42be6fdf91f8639d82cb69e48cde80413c

# The installer is copied from one immutable, multi-arch upstream image. Keep the
# version and manifest digest together when upgrading uv.
FROM ghcr.io/astral-sh/uv:${UV_VERSION}@${UV_IMAGE_DIGEST} AS uv

# --- frontend: build the SPA into /spa (decoupled from the repo's ../backend outDir) ---
FROM node:${NODE_VERSION}@${NODE_IMAGE_DIGEST} AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npx vite build --outDir /spa --emptyOutDir

# --- runtime: panel + pinned xray + nft/dnsmasq/iproute2 (cutover-ready) ---
FROM python:${PYTHON_VERSION}@${PYTHON_IMAGE_DIGEST} AS runtime
ARG XRAY_VERSION
ARG XRAY_SHA256_AMD64
ARG XRAY_SHA256_ARM64
ARG DBIP_VERSION
ARG DBIP_SHA256
# buildx sets TARGETARCH per target platform (amd64/arm64); for a plain non-BuildKit
# `docker build` it's empty, so fall back to the base image's own dpkg arch (same names).
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends \
      nftables iptables dnsmasq isc-dhcp-client iproute2 ca-certificates curl openssl unzip gzip tini \
    && rm -rf /var/lib/apt/lists/*
# DB-IP IP-to-Country Lite (CC-BY-4.0) for the egress country flag. Keep the dated artifact
# and checksum together: a build from the same commit must not silently ingest newer bytes.
RUN curl -fsSL \
      "https://download.db-ip.com/free/dbip-country-lite-${DBIP_VERSION}.mmdb.gz" \
      -o /tmp/cc.gz \
    && echo "${DBIP_SHA256}  /tmp/cc.gz" | sha256sum -c - \
    && gunzip -c /tmp/cc.gz > /usr/local/share/dbip-country-lite.mmdb \
    && rm -f /tmp/cc.gz
# arch-aware xray fetch — XTLS names the amd64 asset `Xray-linux-64.zip`, arm64 `…-arm64-v8a.zip`.
# The download is checksum-verified against the pinned SHA256 (audit I1).
RUN set -eu; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      arm64) XR=Xray-linux-arm64-v8a.zip; SUM="$XRAY_SHA256_ARM64" ;; \
      amd64) XR=Xray-linux-64.zip; SUM="$XRAY_SHA256_AMD64" ;; \
      *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/xray.zip \
      "https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/${XR}" \
    && echo "${SUM}  /tmp/xray.zip" | sha256sum -c - \
    && unzip -o /tmp/xray.zip xray geoip.dat geosite.dat -d /usr/local/bin/ \
    && chmod +x /usr/local/bin/xray && rm /tmp/xray.zip
COPY --from=uv /uv /uvx /bin/
WORKDIR /app/backend
COPY backend/ ./
COPY --from=frontend /spa ./pi_gw_panel/static
RUN uv sync --locked --no-dev --no-editable
ENV PATH="/app/backend/.venv/bin:${PATH}"
ENV PI_GW_DATA_DIR=/data PI_GW_PORT=8080
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s \
  CMD curl -kfsS "https://127.0.0.1:${PI_GW_PORT}/api/health" || \
      curl -fsS "http://127.0.0.1:${PI_GW_PORT}/api/health" || exit 1
ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "pi_gw_panel"]
