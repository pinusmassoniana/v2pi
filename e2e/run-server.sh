#!/usr/bin/env bash
# Launch the panel for e2e: safe dry-run net backend, throwaway data dir, no real xray.
# Used both locally (Playwright webServer) and in CI. Requires the SPA to be built into
# backend/pi_gw_panel/static first (npm run build:spa, or the CI build step).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

export PI_GW_PORT="${PI_GW_PORT:-8099}"
export PI_GW_NET_BACKEND="dryrun"                              # never touch the host
export PI_GW_XRAY_BIN="${PI_GW_XRAY_BIN:-/bin/true}"           # no real xray needed for the smoke
export PI_GW_SESSION_SECRET="${PI_GW_SESSION_SECRET:-e2e-not-a-real-secret}"
export PI_GW_LOGIN_LOCKOUT_SEC="${PI_GW_LOGIN_LOCKOUT_SEC:-2}"  # rate-limit spec waits it out
export PI_GW_DATA_DIR="${PI_GW_DATA_DIR:-$(mktemp -d 2>/dev/null || echo /tmp/v2pi-e2e-data)}"

cd "$ROOT/backend"
exec uv run python -m pi_gw_panel
