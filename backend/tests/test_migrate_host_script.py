import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "migrate-host.sh"


def _fake_commands(tmp_path: Path) -> tuple[Path, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "commands.log"
    shim = bindir / "shim"
    shim.write_text("""#!/bin/sh
name=${0##*/}
printf '%s %s\\n' "$name" "$*" >> "$MIGRATE_LOG"
case "$name:$1:$2" in
  systemctl:list-unit-files:*) printf 'pi-gw-dhcp.service enabled\\nradvd enabled\\n' ;;
  systemctl:is-active:*) exit 0 ;;
  systemctl:is-enabled:*) exit 0 ;;
  ip:addr:save|ip:route:save|ip:rule:save) [ "${MIGRATE_SAVE_EMPTY:-0}" = 1 ] || printf 'saved\\n' ;;
  nft:list:ruleset) [ "${MIGRATE_SAVE_EMPTY:-0}" = 1 ] || printf 'table inet original {}\\n' ;;
  curl:*:*) [ "${MIGRATE_READY:-0}" = 1 ] && exit 0 || exit 1 ;;
esac
exit 0
""")
    shim.chmod(0o755)
    for name in ("systemctl", "ip", "nft", "docker", "curl", "sleep", "nmcli"):
        os.symlink(shim, bindir / name)
    return bindir, log


def _run(tmp_path: Path, *, ready: bool, save_empty: bool = False):
    bindir, log = _fake_commands(tmp_path)
    compose = tmp_path / "compose"
    compose.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    nm_conf = tmp_path / "99-v2pi.conf"
    nm_conf.write_text("original-network-manager-config\n")
    env = os.environ.copy()
    env.update({
        "PATH": f"{bindir}:{env['PATH']}",
        "MIGRATE_LOG": str(log),
        "MIGRATE_READY": "1" if ready else "0",
        "MIGRATE_SAVE_EMPTY": "1" if save_empty else "0",
        "PI_GW_MIGRATE_ALLOW_NONROOT": "1",
        "PI_GW_MIGRATE_STATE_DIR": str(state_root),
        "PI_GW_NM_CONF_PATH": str(nm_conf),
        "PI_GW_COMPOSE_DIR": str(compose),
        "PI_GW_READY_ATTEMPTS": "2",
        "PI_GW_READY_DELAY": "0",
    })
    result = subprocess.run(["bash", str(SCRIPT)], env=env, text=True, capture_output=True)
    return result, log.read_text(), nm_conf


def _rollback_body() -> list[str]:
    lines = SCRIPT.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("rollback()"))
    end = next(i for i, line in enumerate(lines[start + 1:], start + 1) if line == "}")
    return lines[start + 1:end]


def test_migrate_script_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_every_rollback_flush_is_guarded_by_its_own_snapshot():
    """A flush the matching restore cannot undo leaves the host worse off than the failed migration."""
    guards: list[bool] = []
    flushes: list[tuple[str, bool]] = []
    for line in _rollback_body():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("if ") and not stripped.endswith("fi"):
            guards.append(stripped.startswith('if [ -s "${SNAP}/'))
        elif stripped == "fi" and guards:
            guards.pop()
        elif "flush" in stripped:
            flushes.append((stripped, any(guards)))
    assert flushes, "rollback no longer flushes anything — revisit this contract"
    assert not [cmd for cmd, guarded in flushes if not guarded], (
        f"unguarded flush in rollback: {[cmd for cmd, guarded in flushes if not guarded]}"
    )


def test_failed_readiness_rolls_back_every_snapshotted_host_layer(tmp_path):
    result, log, nm_conf = _run(tmp_path, ready=False)
    assert result.returncode != 0
    assert "docker compose down" in log
    assert "nft flush ruleset" in log and "nft -f" in log
    assert "ip addr flush dev eth0.2" in log and "ip addr restore" in log
    assert "ip route flush table 100" in log and "ip route restore" in log
    assert "ip rule flush" in log and "ip rule restore" in log
    # The shim captures no IPv6 snapshot, so those layers must be left alone rather than
    # flushed into a state nothing restores.
    assert "ip -6 route flush" not in log and "ip -6 rule flush" not in log
    assert "systemctl enable pi-gw-dhcp.service" in log
    assert "systemctl start radvd" in log
    assert nm_conf.read_text() == "original-network-manager-config\n"


def test_rollback_without_usable_snapshots_flushes_nothing(tmp_path):
    result, log, nm_conf = _run(tmp_path, ready=False, save_empty=True)
    assert result.returncode != 0
    # The rollback still runs every layer it can actually restore...
    assert "docker compose down" in log
    assert "systemctl enable pi-gw-dhcp.service" in log
    assert nm_conf.read_text() == "original-network-manager-config\n"
    # ...but destroys nothing it cannot put back.
    destructive = [
        line for line in log.splitlines()
        if {"flush", "restore"} & set(line.split())
    ]
    assert not destructive, destructive
    assert "no IPv4 rule snapshot" in result.stderr


def test_successful_readiness_disarms_rollback(tmp_path):
    result, log, nm_conf = _run(tmp_path, ready=True)
    assert result.returncode == 0, result.stderr
    assert "curl -kfsS https://127.0.0.1:8080/api/ready" in log
    assert "docker compose up -d" in log
    assert "docker compose down" not in log
    assert "unmanaged-devices=interface-name:eth0.2" in nm_conf.read_text()
