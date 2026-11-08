"""Admin login: first-run password setup, login, sessions, logout, password change."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.security import throttle
from app.config import get_settings
from app.core.auth_store import AuthStore, hash_password, verify_password

STRONG = "correct-horse-battery-staple"


@pytest.fixture
def login_on(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_login_enabled", True)
    monkeypatch.setattr(settings, "api_auth_token", "")
    AuthStore._instance = None
    AuthStore.instance().reset_for_tests()
    throttle.reset()
    yield
    AuthStore.instance().reset_for_tests()
    AuthStore._instance = None
    throttle.reset()


def test_password_hashing_round_trip():
    h = hash_password("hunter2-hunter2")
    assert h.startswith("scrypt$") and "hunter2" not in h
    assert verify_password("hunter2-hunter2", h)
    assert not verify_password("hunter2-hunter3", h)
    assert not verify_password("x", "garbage")


def test_first_run_setup_then_login(login_on):
    from app.main import app

    with TestClient(app) as client:
        cfg = client.get("/api/config").json()
        assert cfg["auth_required"] and cfg["setup_required"] and not cfg["bootstrap_token_required"]
        # closed until the password is set
        r = client.get("/api/migrations")
        assert r.status_code == 401 and "administrator password" in r.json()["detail"]
        assert client.post("/api/auth/login", json={"password": STRONG}).status_code == 409
        # weak passwords are rejected
        assert client.post("/api/auth/setup", json={"password": "short"}).status_code == 422
        r = client.post("/api/auth/setup", json={"password": STRONG})
        assert r.status_code == 200
        token = r.json()["token"]
        assert client.get("/api/config").json()["setup_required"] is False
        # the hash is on disk, the password isn't
        assert STRONG not in AuthStore.path().read_text()
        # setup can't run twice
        assert client.post("/api/auth/setup", json={"password": STRONG + "x"}).status_code == 409

        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/migrations", headers=headers).status_code == 200
        assert client.get("/api/auth/session", headers=headers).json()["method"] == "session"
        assert client.get("/api/migrations", headers={"Authorization": "Bearer nope"}).status_code == 401

        # login issues a second session; logout revokes only its own
        r = client.post("/api/auth/login", json={"password": STRONG})
        assert r.status_code == 200
        token2 = r.json()["token"]
        assert client.post("/api/auth/login", json={"password": "wrong-password-1"}).status_code == 401
        assert client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token2}"}).status_code == 204
        assert client.get("/api/migrations", headers={"Authorization": f"Bearer {token2}"}).status_code == 401
        assert client.get("/api/migrations", headers=headers).status_code == 200

        # change password: wrong current -> 401; right -> other sessions dropped, new password works
        r = client.post("/api/auth/password", json={"current_password": "nope-nope-nope", "new_password": STRONG + "2"},
                        headers=headers)
        assert r.status_code == 401
        r = client.post("/api/auth/login", json={"password": STRONG})
        other = r.json()["token"]
        r = client.post("/api/auth/password", json={"current_password": STRONG, "new_password": STRONG + "2"},
                        headers=headers)
        assert r.status_code == 204
        assert client.get("/api/migrations", headers={"Authorization": f"Bearer {other}"}).status_code == 401
        assert client.get("/api/migrations", headers=headers).status_code == 200
        assert client.post("/api/auth/login", json={"password": STRONG}).status_code == 401
        assert client.post("/api/auth/login", json={"password": STRONG + "2"}).status_code == 200


def test_setup_requires_bootstrap_token_when_api_token_configured(login_on, monkeypatch):
    from app.main import app

    monkeypatch.setattr(get_settings(), "api_auth_token", "static-token")
    with TestClient(app) as client:
        assert client.get("/api/config").json()["bootstrap_token_required"] is True
        assert client.post("/api/auth/setup", json={"password": STRONG}).status_code == 401
        assert client.post("/api/auth/setup", json={"password": STRONG, "bootstrap_token": "wrong"}).status_code == 401
        # the static token keeps working for scripts meanwhile
        assert client.get("/api/migrations", headers={"Authorization": "Bearer static-token"}).status_code == 200
        r = client.post("/api/auth/setup", json={"password": STRONG, "bootstrap_token": "static-token"})
        assert r.status_code == 200
        assert client.get("/api/auth/session", headers={"Authorization": "Bearer static-token"}).json()["method"] == "token"


def test_sessions_expire(login_on, monkeypatch):
    store = AuthStore.instance()
    store.set_password(STRONG)
    token, _ = store.create_session()
    assert store.session_valid(token)
    monkeypatch.setattr(get_settings(), "session_idle_timeout_s", 0)
    import time
    time.sleep(0.01)
    assert not store.session_valid(token)


def test_wrong_passwords_are_throttled(login_on, monkeypatch):
    from app.main import app

    monkeypatch.setattr(get_settings(), "auth_max_failures", 3)
    AuthStore.instance().set_password(STRONG)
    with TestClient(app) as client:
        for _ in range(3):
            assert client.post("/api/auth/login", json={"password": "bad-password-x"}).status_code == 401
        r = client.post("/api/auth/login", json={"password": STRONG})
        assert r.status_code == 429 and "Retry-After" in r.headers


def test_login_disabled_keeps_old_behaviour(monkeypatch):
    from app.main import app

    monkeypatch.setattr(get_settings(), "admin_login_enabled", False)
    monkeypatch.setattr(get_settings(), "api_auth_token", "")
    with TestClient(app) as client:
        cfg = client.get("/api/config").json()
        assert cfg["auth_required"] is False and cfg["setup_required"] is False
        assert client.get("/api/migrations").status_code == 200
        assert client.post("/api/auth/setup", json={"password": STRONG}).status_code == 409
