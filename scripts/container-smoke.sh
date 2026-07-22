#!/usr/bin/env bash
# Smoke an exact published image digest: start the panel in isolated dry-run mode,
# wait for its liveness endpoint, then validate an app-generated config with bundled Xray.
set -euo pipefail

image_ref="${1:-}"
platform="${2:-}"
registry_digest=0
local_image_id=0
[[ "$image_ref" =~ @sha256:[[:xdigit:]]{64}$ ]] && registry_digest=1
[[ "$image_ref" =~ ^sha256:[[:xdigit:]]{64}$ ]] && local_image_id=1
if [[ "$registry_digest" != 1 && !( "$local_image_id" == 1 && "${V2PI_ALLOW_LOCAL_IMAGE_ID:-0}" == 1 ) ]]; then
  echo "usage: $0 <registry/image@sha256:64-hex-digest> [linux/amd64|linux/arm64]" >&2
  echo "local-only: V2PI_ALLOW_LOCAL_IMAGE_ID=1 $0 sha256:<docker-image-id>" >&2
  exit 2
fi

tmp_dir="$(mktemp -d)"
container_id=""
cleanup() {
  if [[ -n "$container_id" ]]; then
    docker rm -f "$container_id" >/dev/null 2>&1 || true
  fi
  rm -rf -- "$tmp_dir"
}
trap cleanup EXIT INT TERM

platform_args=()
if [[ -n "$platform" ]]; then
  platform_args=(--platform "$platform")
fi

container_id="$(docker run -d "${platform_args[@]}" \
  --publish 127.0.0.1::8080 \
  --volume "$tmp_dir:/data" \
  --env PI_GW_BIND_HOST=0.0.0.0 \
  --env PI_GW_DATA_DIR=/data \
  --env PI_GW_NET_BACKEND=dryrun \
  --env PI_GW_PORT=8080 \
  --env PI_GW_SESSION_SECRET=container-smoke-session-secret-32-bytes \
  "$image_ref")"

host_port="$(docker port "$container_id" 8080/tcp | sed -n '1s/.*://p')"
if [[ -z "$host_port" ]]; then
  echo "container did not publish port 8080" >&2
  docker logs "$container_id" >&2 || true
  exit 1
fi

ready=0
for _ in {1..60}; do
  if curl -kfsS "https://127.0.0.1:${host_port}/api/health" >/dev/null 2>&1 || \
     curl -fsS "http://127.0.0.1:${host_port}/api/health" >/dev/null 2>&1; then
    ready=1
    break
  fi
  if [[ "$(docker inspect -f '{{.State.Running}}' "$container_id")" != "true" ]]; then
    break
  fi
  sleep 1
done
if [[ "$ready" != 1 ]]; then
  echo "panel liveness smoke failed" >&2
  docker logs "$container_id" >&2 || true
  exit 1
fi

docker run --rm "${platform_args[@]}" \
  --volume "$tmp_dir:/smoke" \
  --entrypoint python \
  "$image_ref" -c \
  'import json; from pi_gw_panel.config import Settings; from pi_gw_panel.models import Node; from pi_gw_panel.xray_config.builder import build_config; node = Node(id=1, name="smoke", address="1.2.3.4", port=443, uuid="00000000-0000-0000-0000-000000000000", sni="www.microsoft.com", public_key="jNXHt1yRo0vDuchQlIP6Z0ZvjT3KtzVI_T4E7RoLJS0", short_id="0123abcd"); json.dump(build_config(node, Settings(data_dir="/smoke")), open("/smoke/xray.json", "w"))'

docker run --rm "${platform_args[@]}" \
  --volume "$tmp_dir:/smoke:ro" \
  --entrypoint xray \
  "$image_ref" -test -config /smoke/xray.json

echo "container smoke passed: $image_ref${platform:+ ($platform)}"
