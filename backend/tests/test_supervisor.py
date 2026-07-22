import json

from pi_gw_panel.xray_supervisor.supervisor import XraySupervisor


def test_start_then_status_running(settings, stub_xray):
    sup = XraySupervisor(xray_bin=stub_xray, config_path=settings.config_path)
    with open(settings.config_path, "w") as f:
        f.write("{}")
    sup.start()
    try:
        st = sup.status()
        assert st["running"] is True
        assert isinstance(st["pid"], int)
    finally:
        sup.stop()


def test_stop_clears_running(settings, stub_xray):
    sup = XraySupervisor(xray_bin=stub_xray, config_path=settings.config_path)
    with open(settings.config_path, "w") as f:
        f.write("{}")
    sup.start()
    sup.stop()
    assert sup.status()["running"] is False


def test_reload_replaces_process(settings, stub_xray):
    sup = XraySupervisor(xray_bin=stub_xray, config_path=settings.config_path)
    with open(settings.config_path, "w") as f:
        f.write("{}")
    sup.start()
    pid1 = sup.status()["pid"]
    sup.reload()
    try:
        pid2 = sup.status()["pid"]
        assert pid2 != pid1
        assert sup.status()["running"] is True
    finally:
        sup.stop()


def test_reload_stops_child_that_never_becomes_ready(settings, stub_xray):
    sup = XraySupervisor(xray_bin=stub_xray, config_path=settings.config_path,
                         ready_check=lambda: False)
    sup.READY_TIMEOUT = 0.02
    sup.READY_STEP = 0.001
    with open(settings.config_path, "w") as f:
        f.write("{}")

    assert sup.reload() is False
    assert sup.status()["running"] is False


def test_status_reports_bounded_sanitized_stderr_and_exit_code(tmp_path):
    secret = "550e8400-e29b-41d4-a716-446655440000"
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"id": secret}))
    xray = tmp_path / "failing-xray"
    xray.write_text(
        "#!/bin/sh\n"
        f"printf '%0200d id={secret}\\n' 0 >&2\n"
        "exit 7\n"
    )
    xray.chmod(0o755)
    sup = XraySupervisor(str(xray), str(config_path))
    sup.STDERR_TAIL_CHARS = 128

    sup.start()
    sup._proc.wait(timeout=10)
    status = sup.status()
    assert status["last_exit_code"] == 7
    assert status["last_error"] == status["stderr_tail"]
    assert secret not in status["stderr_tail"]
    assert "***" in status["stderr_tail"]
    assert len(status["stderr_tail"]) <= sup.STDERR_TAIL_CHARS


def test_reload_reports_missing_binary_without_raising(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}")
    sup = XraySupervisor(str(tmp_path / "missing-xray"), str(config_path))

    assert sup.reload() is False
    status = sup.status()
    assert status["running"] is False
    assert "not found" in status["last_error"].lower()


def test_start_is_idempotent(settings, stub_xray):
    sup = XraySupervisor(xray_bin=stub_xray, config_path=settings.config_path)
    with open(settings.config_path, "w") as f:
        f.write("{}")
    sup.start()
    try:
        pid1 = sup.status()["pid"]
        sup.start()  # second call while already running
        assert sup.status()["pid"] == pid1
    finally:
        sup.stop()


# --- 3-way state for the sidebar xray-core box: stopped | working | error ---
def test_state_stopped_initially():
    assert XraySupervisor("xray", "/tmp/none.json").state() == "stopped"


def test_state_working_then_stopped(settings, stub_xray):
    sup = XraySupervisor(xray_bin=stub_xray, config_path=settings.config_path)
    with open(settings.config_path, "w") as f:
        f.write("{}")
    sup.start()
    try:
        assert sup.state() == "working"
    finally:
        sup.stop()
    assert sup.state() == "stopped"


def test_state_error_when_process_exits_while_wanted():
    # `false` exits non-zero immediately → we wanted it running but it died → error
    sup = XraySupervisor(xray_bin="false", config_path="/tmp/none.json")
    sup.start()
    sup._proc.wait()
    assert sup.state() == "error"
