#!/usr/bin/env bash
# Stage an existing Pi gateway onto the self-provisioning container. Every mutable host layer is
# snapshotted first and guarded by the EXIT trap until the panel's full /api/ready contract passes.
set -euo pipefail

if [ "${EUID}" -ne 0 ] && [ "${PI_GW_MIGRATE_ALLOW_NONROOT:-0}" != 1 ]; then
  echo "run as root" >&2
  exit 1
fi

SEG="${PI_GW_SEGMENT_IFACE:-eth0.2}"
TABLE="${PI_GW_POLICY_TABLE:-100}"
COMPOSE_DIR="${PI_GW_COMPOSE_DIR:-/opt/v2pi}"
STATE_ROOT="${PI_GW_MIGRATE_STATE_DIR:-/var/lib}"
NM_CONF="${PI_GW_NM_CONF_PATH:-/etc/NetworkManager/conf.d/99-v2pi.conf}"
READY_URL="${PI_GW_READY_URL:-https://127.0.0.1:8080/api/ready}"
READY_ATTEMPTS="${PI_GW_READY_ATTEMPTS:-180}"
READY_DELAY="${PI_GW_READY_DELAY:-0.5}"
SNAP="${STATE_ROOT}/v2pi-migrate-$(date +%Y%m%d-%H%M%S)-$$"
SERVICES="pi-gw-dhcp.service radvd"

mkdir -p "${SNAP}"
echo "==> snapshotting host state to ${SNAP}"

# Save machine-readable restore inputs, not merely diagnostic text. A missing optional host
# facility produces an empty file and is skipped during rollback.
ip addr save > "${SNAP}/addresses.bin" 2>/dev/null || :
ip route save table all > "${SNAP}/routes4.bin" 2>/dev/null || :
ip -6 route save table all > "${SNAP}/routes6.bin" 2>/dev/null || :
ip rule save > "${SNAP}/rules4.bin" 2>/dev/null || :
ip -6 rule save > "${SNAP}/rules6.bin" 2>/dev/null || :
nft list ruleset > "${SNAP}/nftables.conf" 2>/dev/null || :

if [ -e "${NM_CONF}" ]; then
  cp -p "${NM_CONF}" "${SNAP}/networkmanager.conf"
  printf '%s\n' present > "${SNAP}/networkmanager.state"
else
  printf '%s\n' absent > "${SNAP}/networkmanager.state"
fi

: > "${SNAP}/services.tsv"
for svc in ${SERVICES}; do
  if systemctl list-unit-files "${svc}" --no-legend 2>/dev/null | grep -q "^${svc}"; then
    active=inactive
    enabled=disabled
    systemctl is-active --quiet "${svc}" 2>/dev/null && active=active
    systemctl is-enabled --quiet "${svc}" 2>/dev/null && enabled=enabled
    printf '%s|%s|%s\n' "${svc}" "${active}" "${enabled}" >> "${SNAP}/services.tsv"
  fi
done

committed=0
rollback() {
  rc=$?
  trap - EXIT INT TERM
  if [ "${committed}" = 1 ]; then
    return "${rc}"
  fi
  set +e
  echo "==> migration failed; restoring staged host state" >&2

  ( cd "${COMPOSE_DIR}" && docker compose down )

  if [ "$(cat "${SNAP}/networkmanager.state" 2>/dev/null)" = present ]; then
    mkdir -p "$(dirname "${NM_CONF}")"
    cp -p "${SNAP}/networkmanager.conf" "${NM_CONF}"
  else
    rm -f "${NM_CONF}"
  fi
  if systemctl is-active --quiet NetworkManager 2>/dev/null; then
    nmcli general reload
  fi

  # Restore the exact nft ruleset. Policy routing owned by the panel is confined to TABLE;
  # address restore follows a targeted segment flush, so unrelated interface addresses remain.
  if [ -s "${SNAP}/nftables.conf" ]; then
    nft flush ruleset
    nft -f "${SNAP}/nftables.conf"
  fi
  ip addr flush dev "${SEG}"
  [ ! -s "${SNAP}/addresses.bin" ] || ip addr restore < "${SNAP}/addresses.bin"
  ip route flush table "${TABLE}"
  ip -6 route flush table "${TABLE}"
  [ ! -s "${SNAP}/routes4.bin" ] || ip route restore < "${SNAP}/routes4.bin"
  [ ! -s "${SNAP}/routes6.bin" ] || ip -6 route restore < "${SNAP}/routes6.bin"
  ip rule flush
  ip -6 rule flush
  [ ! -s "${SNAP}/rules4.bin" ] || ip rule restore < "${SNAP}/rules4.bin"
  [ ! -s "${SNAP}/rules6.bin" ] || ip -6 rule restore < "${SNAP}/rules6.bin"

  while IFS='|' read -r svc active enabled; do
    [ -n "${svc}" ] || continue
    if [ "${enabled}" = enabled ]; then systemctl enable "${svc}"; else systemctl disable "${svc}"; fi
    if [ "${active}" = active ]; then systemctl start "${svc}"; else systemctl stop "${svc}"; fi
  done < "${SNAP}/services.tsv"
  echo "==> rollback complete; snapshot retained at ${SNAP}" >&2
  return "${rc}"
}
trap rollback EXIT
trap 'exit 130' INT TERM

echo "==> stopping legacy gateway services"
while IFS='|' read -r svc _active _enabled; do
  [ -n "${svc}" ] || continue
  systemctl disable --now "${svc}"
done < "${SNAP}/services.tsv"

echo "==> marking ${SEG} unmanaged for NetworkManager"
mkdir -p "$(dirname "${NM_CONF}")"
printf '[keyfile]\nunmanaged-devices=interface-name:%s\n' "${SEG}" > "${NM_CONF}"
if systemctl is-active --quiet NetworkManager 2>/dev/null; then
  nmcli general reload
fi

echo "==> starting the self-provisioning container"
( cd "${COMPOSE_DIR}" && docker compose up -d )

echo "==> waiting for full gateway readiness"
ready=0
attempt=1
while [ "${attempt}" -le "${READY_ATTEMPTS}" ]; do
  if curl -kfsS "${READY_URL}" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep "${READY_DELAY}"
  attempt=$((attempt + 1))
done
if [ "${ready}" != 1 ]; then
  echo "gateway did not become ready at ${READY_URL}" >&2
  exit 1
fi

committed=1
trap - EXIT INT TERM
echo "==> migration OK; snapshot retained at ${SNAP}"
