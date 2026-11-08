"""Settings -> Log forwarding: config, the five shippers, the background handler."""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from app.api.security import throttle
from app.config import get_settings
from app.core.auth_store import AuthStore
from app.core.log_forwarding import ForwardConfig, Forwarder, ForwardError, ForwardStore, make_shipper, send_test_event

STRONG = "correct-horse-battery-staple"


class _Capture(BaseHTTPRequestHandler):
    received: list[tuple[str, dict, bytes]] = []
    status = 200

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _Capture.received.append((self.path, dict(self.headers), body))
        self.send_response(_Capture.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"errors":false,"items":[]}' if "_bulk" in self.path else b"{}")

    def log_message(self, *_):
        pass


@pytest.fixture
def http_sink():
    _Capture.received = []
    _Capture.status = 200
    srv = HTTPServer(("127.0.0.1", 0), _Capture)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
def env(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_login_enabled", True)
    monkeypatch.setattr(settings, "api_auth_token", "")
    monkeypatch.setattr(settings, "migration_state_file", str(tmp_path / "state" / "migrations.json"))
    AuthStore._instance = None
    ForwardStore._instance = None
    throttle.reset()
    from app.main import app

    client = TestClient(app)
    token = client.post("/api/auth/setup", json={"password": STRONG}).json()["token"]
    yield client, {"Authorization": f"Bearer {token}"}
    ForwardStore.instance().reset_for_tests()
    AuthStore.instance().reset_for_tests()
    AuthStore._instance = ForwardStore._instance = None
    throttle.reset()


def _cfg(dest, **settings):
    return ForwardConfig(enabled=True, destination=dest, settings=settings, extra_fields={"env": "test"})


def test_http_shippers_payloads(http_sink):
    send_test_event(_cfg("splunk_hec", url=http_sink, token="tok", index="main"))
    path, headers, body = _Capture.received[-1]
    assert path == "/services/collector/event" and headers["Authorization"] == "Splunk tok"
    ev = json.loads(body)
    assert ev["index"] == "main" and ev["event"]["level"] == "INFO" and ev["event"]["extra"]["env"] == "test"

    send_test_event(_cfg("elasticsearch", url=http_sink, index="coa", username="u", password="p"))
    path, headers, body = _Capture.received[-1]
    lines = body.decode().strip().split("\n")
    assert path == "/_bulk" and json.loads(lines[0]) == {"create": {"_index": "coa"}}
    assert json.loads(lines[1])["log.level"] == "info" and headers["Authorization"].startswith("Basic ")

    send_test_event(_cfg("loki", url=http_sink, tenant="t1", labels="env=prod"))
    path, headers, body = _Capture.received[-1]
    payload = json.loads(body)
    assert path == "/loki/api/v1/push" and headers["X-Scope-OrgID"] == "t1"
    assert payload["streams"][0]["stream"]["env"] == "prod" and payload["streams"][0]["values"][0][0].isdigit()

    _Capture.status = 503
    with pytest.raises(ForwardError) as exc:
        send_test_event(_cfg("splunk_hec", url=http_sink, token="tok"))
    assert "503" in str(exc.value)


def test_datadog_payload(monkeypatch):
    seen = {}

    class R:
        status_code = 202
        text = ""

    def fake_post(url, **kw):
        seen.update(url=url, **kw)
        return R()

    monkeypatch.setattr("app.core.log_forwarding.requests.post", fake_post)
    send_test_event(_cfg("datadog", site="datadoghq.eu", api_key="k", service="svc", tags="team:data"))
    assert seen["url"] == "https://http-intake.logs.datadoghq.eu/api/v2/logs"
    assert seen["headers"]["DD-API-KEY"] == "k"
    assert seen["json"][0]["service"] == "svc" and "team:data" in seen["json"][0]["ddtags"] and seen["json"][0]["status"] == "info"


def test_syslog_udp_rfc5424():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(3)
    port = sock.getsockname()[1]
    send_test_event(_cfg("syslog", host="127.0.0.1", port=str(port), protocol="udp", facility="local3", app_name="coa"))
    line = sock.recv(8192).decode()
    assert line.startswith("<158>1 ")      # local3 (19) * 8 + info (6)
    assert " coa " in line and '[coa@47476 logger="onboarding_agent.test" env="test" test="True"]' in line
    assert line.endswith("log-forwarding test from " + line.split(" ")[2])  # HOSTNAME field
    sock.close()


def test_handler_batches_from_logging(http_sink):
    fwd = Forwarder.instance()
    cfg = ForwardConfig(enabled=True, destination="splunk_hec", settings={"url": http_sink, "token": "t"},
                        min_level="WARNING", batch_size=2, flush_interval_s=1)
    fwd.apply(cfg)
    try:
        logging.getLogger("onboarding_agent.auth").warning("audit line one")
        logging.getLogger("app.something").info("ignored: below level")
        logging.getLogger("onboarding_agent.logfwd").warning("ignored: never forwarded")
        logging.getLogger("app.something").error("app line two")
        deadline = time.time() + 5
        while fwd.stats["sent"] < 2 and time.time() < deadline:
            time.sleep(0.1)
        assert fwd.stats["sent"] >= 2, fwd.status()
        texts = "".join(b.decode() for _, _, b in _Capture.received)
        assert "audit line one" in texts and "app line two" in texts
        assert "below level" not in texts and "never forwarded" not in texts
    finally:
        fwd.apply(ForwardConfig())
    assert fwd.status()["active"] is False


def test_settings_api_and_secret_handling(env, http_sink):
    client, h = env
    r = client.get("/api/settings/log-forwarding", headers=h).json()
    assert r["config"]["enabled"] is False and set(r["destinations"]) == {"splunk_hec", "datadog", "elasticsearch", "loki", "syslog"}

    body = {"enabled": True, "destination": "splunk_hec", "settings": {"url": http_sink, "token": "secret-tok"},
            "min_level": "INFO", "batch_size": 10, "flush_interval_s": 2}
    assert client.put("/api/settings/log-forwarding", json={**body, "settings": {"url": http_sink}}, headers=h).status_code == 422
    assert client.put("/api/settings/log-forwarding", json={**body, "min_level": "LOUD"}, headers=h).status_code == 422
    r = client.post("/api/settings/log-forwarding/test", json=body, headers=h)
    assert r.status_code == 200 and r.json()["ok"], r.text
    r = client.put("/api/settings/log-forwarding", json=body, headers=h)
    assert r.status_code == 200
    cfg = r.json()["config"]
    assert cfg["settings"]["token"] == "" and cfg["secrets_set"] == ["token"] and r.json()["status"]["active"]
    assert "secret-tok" not in ForwardStore.path().read_text()
    # blank secret on a later save keeps the stored one; test endpoint reuses it too
    assert client.put("/api/settings/log-forwarding", json={**body, "settings": {"url": http_sink, "token": ""}}, headers=h).status_code == 200
    assert ForwardStore.instance().config.settings["token"] == "secret-tok"
    r = client.post("/api/settings/log-forwarding/test", json={**body, "settings": {"url": http_sink, "token": ""}}, headers=h)
    assert r.status_code == 200 and _Capture.received[-1][1]["Authorization"] == "Splunk secret-tok"
    # unreachable destination -> 502 from test, saved config still accepted (best effort)
    r = client.post("/api/settings/log-forwarding/test", json={**body, "settings": {"url": "http://127.0.0.1:9", "token": "x"}}, headers=h)
    assert r.status_code == 502
    # the shipper is a Forwarder on the root logger: an audit event gets forwarded after the save above
    assert make_shipper(ForwardStore.instance().config).__class__.__name__ == "_Splunk"
