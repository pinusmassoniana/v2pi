#!/usr/bin/env bash
# Migrate an existing, manually-set-up Pi gateway to the self-provisioning container.
# Reversible: snapshots state first; on a failed verify it re-enables the legacy services.
# Fresh installs: the legacy stops are no-ops, so this is safe to run anywhere. Run as root
# on the Pi (the container re-provisions sysctls/VLAN/addresses/DHCP/RA itself on `up`).
set -euo pipefail

SEG="${PI_GW_SEGMENT_IFACE:-eth0.2}"
COMPOSE_DIR="${PI_GW_COMPOSE_DIR:-/opt/v2pi}"
SNAP="/var/lib/v2pi-migrate-$(date +%Y%m%d-%H%M%S).snap"

echo "==> snapshotting host net state to ${SNAP}"
{
  echo "# $(date -Is)"
  echo "## ip addr"; ip addr || true
  echo "## ip -6 route"; ip -6 route || true
  echo "## services"; systemctl is-active pi-gw-dhcp.service radvd 2>/dev/null || true
} > "${SNAP}" 2>/dev/null || true

echo "==> stopping legacy host services (no-op if absent)"
for svc in pi-gw-dhcp.service radvd; do
  if systemctl list-unit-files 2>/dev/null | grep -q "^${svc}"; then
    systemctl disable --now "${svc}" || true
  fi
done

echo "==> marking ${SEG} unmanaged for NetworkManager"
install -d /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/99-v2pi.conf <<EOF
[keyfile]
unmanaged-devices=interface-name:${SEG}
EOF
if systemctl is-active --quiet NetworkManager 2>/dev/null; then nmcli general reload || true; fi

echo "==> (re)starting the container so it re-provisions"
( cd "${COMPOSE_DIR}" && docker compose up -d )

echo "==> verifying (segment up + container healthy)"
sleep 8
ok=1
ip link show "${SEG}" >/dev/null 2>&1 || { echo "   !! ${SEG} missing"; ok=0; }
curl -fsS http://127.0.0.1:8080/api/health >/dev/null 2>&1 || { echo "   !! panel unhealthy"; ok=0; }
if [ "${ok}" = 1 ]; then
  echo "==> migration OK. Snapshot kept at ${SNAP}"
else
  echo "==> verify FAILED — re-enabling legacy services"
  for svc in pi-gw-dhcp.service radvd; do systemctl enable --now "${svc}" 2>/dev/null || true; done
  echo "   restored. Inspect ${SNAP} and the container logs (docker compose logs)."
  exit 1
fi
