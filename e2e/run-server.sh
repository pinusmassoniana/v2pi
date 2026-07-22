#!/usr/bin/env bash
# Launch the panel for e2e: safe dry-run net backend, throwaway data dir, no real xray.
# Used both locally (Playwright webServer) and in CI. Requires the SPA to be built into
# backend/pi_gw_panel/static first (`cd frontend && npm run build`, or the CI build step).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

export PI_GW_PORT="${PI_GW_PORT:-8099}"
export PI_GW_BIND_HOST="127.0.0.1"                              # loopback keeps local e2e on HTTP
export PI_GW_NET_BACKEND="dryrun"                              # never touch the host
export PI_GW_XRAY_BIN="${PI_GW_XRAY_BIN:-/bin/true}"           # no real xray needed for the smoke
export PI_GW_SESSION_SECRET="${PI_GW_SESSION_SECRET:-e2e-not-a-real-secret-with-32-bytes}"
export PI_GW_LOGIN_LOCKOUT_SEC="${PI_GW_LOGIN_LOCKOUT_SEC:-2}"  # rate-limit spec waits it out
cleanup_data=""
if [[ -z "${PI_GW_DATA_DIR:-}" ]]; then
  PI_GW_DATA_DIR="$(mktemp -d)"
  export PI_GW_DATA_DIR
  cleanup_data="$PI_GW_DATA_DIR"
fi
server_pid=""
cleanup() {
  if [[ -n "$server_pid" ]]; then
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
  fi
  if [[ -n "$cleanup_data" ]]; then
    rm -rf -- "$cleanup_data"
  fi
}
trap cleanup EXIT INT TERM

cd "$ROOT/backend"
uv run --locked python -m pi_gw_panel &
server_pid=$!
wait "$server_pid"
