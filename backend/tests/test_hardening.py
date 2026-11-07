"""Security hardening: response headers, auth throttling + audit, websocket
subprotocol auth, bucket-name validation, startup warnings, docs gating."""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import security
from app.config import get_settings
from app.models.schemas import (
    ContainerMigrationSpec,
    CouchbaseConnectionConfig,
    MigrationPlanCreate,
    SourceConnectionConfig,
    TargetKeyspace,
)


@pytest.fixture(autouse=True)
def _reset_throttle():
    security.throttle.reset()
    yield
    security.throttle.reset()


def _client(monkeypatch, token="tok"):
    from app.main import app

    monkeypatch.setattr(get_settings(), "api_auth_token", token)
    return TestClient(app)


def test_security_headers_on_every_api_response(monkeypatch):
    with _client(monkeypatch) as client:
        r = client.get("/api/health")
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["Referrer-Policy"] == "no-referrer"
        assert "camera=()" in r.headers["Permissions-Policy"]
        assert r.headers["Content-Security-Policy"].startswith("default-src 'none'")
        assert r.headers["Cache-Control"] == "no-store"
        assert "Strict-Transport-Security" not in r.headers  # plain HTTP
        r = client.get("/api/health", headers={"X-Forwarded-Proto": "https"})
        assert r.headers["Strict-Transport-Security"].startswith("max-age=")


def test_repeated_bad_tokens_lock_the_source_ip(monkeypatch, caplog):
    s = get_settings()
    monkeypatch.setattr(s, "auth_max_failures", 3)
    monkeypatch.setattr(s, "auth_lockout_s", 60)
    with _client(monkeypatch) as client, caplog.at_level(logging.WARNING, logger="onboarding_agent.auth"):
        bad = {"Authorization": "Bearer nope"}
        codes = [client.get("/api/migrations", headers=bad).status_code for _ in range(4)]
        assert codes == [401, 401, 401, 429]
        locked = client.get("/api/migrations", headers=bad)
        assert locked.status_code == 429 and int(locked.headers["Retry-After"]) > 0
        # the right token doesn't get through either while locked out
        assert client.get("/api/migrations", headers={"Authorization": "Bearer tok"}).status_code == 429
        # a different source IP (one trusted proxy hop) is unaffected
        ok = client.get("/api/migrations", headers={"Authorization": "Bearer tok", "X-Forwarded-For": "10.0.0.9"})
        assert ok.status_code == 200
    audit = [r.getMessage() for r in caplog.records if r.name == "onboarding_agent.auth"]
    assert any("invalid token" in m for m in audit) and any("locked out" in m for m in audit)


def test_spoofed_forwarded_header_is_ignored_when_untrusted(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "auth_max_failures", 2)
    monkeypatch.setattr(s, "trust_x_forwarded_for", False)
    with _client(monkeypatch) as client:
        for ip in ("1.1.1.1", "2.2.2.2"):
            client.get("/api/migrations", headers={"Authorization": "Bearer nope", "X-Forwarded-For": ip})
        r = client.get("/api/migrations", headers={"Authorization": "Bearer tok", "X-Forwarded-For": "3.3.3.3"})
        assert r.status_code == 429  # all three came from the same socket address


def test_websocket_token_as_subprotocol(monkeypatch):
    with _client(monkeypatch) as client:
        with client.websocket_connect("/ws/migrations", subprotocols=["coa-auth", "tok"]) as ws:
            assert ws.accepted_subprotocol == "coa-auth"
        with client.websocket_connect("/ws/migrations?token=tok"):
            pass  # legacy query-string form still works
        with client.websocket_connect("/ws/migrations", subprotocols=["coa-auth", "wrong"]) as ws:
            with pytest.raises(Exception) as exc:
                ws.receive_text()
            assert "4401" in str(exc.value) or getattr(exc.value, "code", None) == 4401


def test_bucket_names_are_validated():
    with pytest.raises(ValidationError):
        TargetKeyspace(bucket="orders`; DROP BUCKET x")
    with pytest.raises(ValidationError):
        TargetKeyspace(scope="a`b")
    assert TargetKeyspace(bucket=" orders-2 ").bucket == "orders-2"
    with pytest.raises(ValidationError):
        MigrationPlanCreate(
            name="t", source=SourceConnectionConfig(label="s", source_type="mongodb"),
            destination=CouchbaseConnectionConfig(label="d", connection_string="couchbase://x", username="u", password="p"),
            destination_bucket="bad name", containers=[ContainerMigrationSpec(container_name="c")],
        )


def test_startup_warnings_flag_defaults(monkeypatch):
    from app import main as app_main

    s = get_settings()
    monkeypatch.setattr(s, "api_auth_token", "short")
    monkeypatch.setattr(s, "memory_cb_password", "password")
    monkeypatch.setattr(s, "environment", "production")
    monkeypatch.setattr(s, "state_encryption_key", "")
    text = " ".join(app_main.startup_security_warnings())
    assert "shorter than 32" in text and "MEMORY_CB_PASSWORD" in text and "STATE_ENCRYPTION_KEY" in text


def test_docs_and_cors_default_off_outside_development(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "environment", "production")
    assert s.api_docs_enabled is False and s.cors_origin_list == []
    monkeypatch.setattr(s, "enable_api_docs", True)
    assert s.api_docs_enabled is True
    monkeypatch.setattr(s, "environment", "development")
    assert s.cors_origin_list and s.api_docs_enabled is True
