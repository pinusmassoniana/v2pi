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
