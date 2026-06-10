import base64
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from fastapi.testclient import TestClient
from pi_gw_panel.app import create_app
from pi_gw_panel.state import build_state
from pi_gw_panel.net_control.dryrun import DryRunBackend


def _sub_server(body: str):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))  # so urllib returns on read, not EOF
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/sub"


def test_real_fetch_refresh_creates_nodes(settings, stub_xray, monkeypatch):
    """End-to-end through the REAL urllib fetch path (direct, no tunnel) against a live
    local HTTP server → base64-vless parse → reconcile → node appears under the sub.
    The stub server is loopback, which production fetch refuses (audit B6) — opt in here."""
    from pi_gw_panel.subs import fetcher
    monkeypatch.setattr(fetcher, "ALLOW_LOOPBACK", True)
    uri = ("vless://u-int@7.7.7.7:443?security=reality&sni=s&pbk=PK&sid=sid"
           "&flow=xtls-rprx-vision#int1")
    body = base64.b64encode((uri + "\n").encode()).decode()
    srv, url = _sub_server(body)
    try:
        settings.xray_bin = stub_xray
        c = TestClient(create_app(settings, state=build_state(settings, net=DryRunBackend())))
        c.post("/api/setup", json={"username": "admin", "password": "changeme"})
        h = {"X-CSRF-Token": c.get("/api/csrf").json()["csrf"]}
        sid = c.post("/api/subs", json={"name": "int", "url": url}, headers=h).json()["id"]
        r = c.post(f"/api/subs/{sid}/refresh", headers=h)
        assert r.status_code == 200
        assert r.json()["added"] == 1 and r.json()["path"] == "direct"
        nodes = c.get("/api/nodes").json()
        assert any(n["address"] == "7.7.7.7" and n["subscription_id"] == sid for n in nodes)
    finally:
        srv.shutdown()
