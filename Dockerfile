# syntax=docker/dockerfile:1
# v2pi panel — single self-contained arm64 image (panel supervises xray inside).
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
RUN apt-get update && apt-get install -y --no-install-recommends \
      nftables dnsmasq odhcp6c iproute2 ca-certificates curl unzip tini \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /tmp/xray.zip \
      "https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/Xray-linux-arm64-v8a.zip" \
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
