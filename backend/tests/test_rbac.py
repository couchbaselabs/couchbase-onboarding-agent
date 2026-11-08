"""Users, roles and permission enforcement (core/rbac.py, Settings -> RBAC & Roles)."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api.security import throttle
from app.config import get_settings
from app.core.auth_store import AuthStore, hash_password
from app.core.rbac import PERMISSIONS, required_permission

STRONG = "correct-horse-battery-staple"


@pytest.fixture
def admin(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_login_enabled", True)
    monkeypatch.setattr(settings, "api_auth_token", "")
    AuthStore._instance = None
    AuthStore.instance().reset_for_tests()
    throttle.reset()
    from app.main import app

    client = TestClient(app)
    token = client.post("/api/auth/setup", json={"password": STRONG}).json()["token"]
    yield client, {"Authorization": f"Bearer {token}"}
    AuthStore.instance().reset_for_tests()
    AuthStore._instance = None
    throttle.reset()


def _login(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_route_classification():
    assert required_permission("GET", "/api/migrations") == "migrations:read"
    assert required_permission("POST", "/api/migrations") == "migrations:write"
    assert required_permission("POST", "/api/migrations/abc/start") == "migrations:write"
    assert required_permission("POST", "/api/migrations/abc/rollback") == "migrations:rollback"
    assert required_permission("POST", "/api/migrations/abc/fallback/stop") == "migrations:rollback"
    assert required_permission("DELETE", "/api/migrations/abc") == "migrations:delete"
    assert required_permission("GET", "/api/stats/abc/logs") == "migrations:read"
    assert required_permission("POST", "/api/sources/test-connection") == "sources:test"
    assert required_permission("POST", "/api/agent/chat") == "agent:chat"
    assert required_permission("PUT", "/api/agent/provider") == "settings:write"
    assert required_permission("GET", "/api/agent/status") is None
    assert required_permission("GET", "/api/auth/session") is None
    assert required_permission("GET", "/api/auth/users") == "users:manage"
    assert required_permission("GET", "/ws/migrations") == "migrations:read"
    assert required_permission("GET", "/api/something-new") == "users:manage"  # deny by default


def test_builtin_roles_and_permission_catalog(admin):
    client, h = admin
    perms = client.get("/api/auth/permissions", headers=h).json()
    assert {p["name"] for p in perms} == set(PERMISSIONS)
    roles = {r["name"]: r for r in client.get("/api/auth/roles", headers=h).json()}
    assert roles["admin"]["builtin"] and set(roles["admin"]["permissions"]) == set(PERMISSIONS)
    assert "users:manage" not in roles["operator"]["permissions"]
    assert "migrations:write" in roles["operator"]["permissions"]
    assert roles["read_only"]["permissions"] == ["agent:chat", "migrations:read"]
    assert roles["admin"]["users"] == 1


def test_operator_and_read_only_enforcement(admin):
    client, h = admin
    assert client.post("/api/auth/users", json={"username": "ops", "password": STRONG, "role": "operator"},
                       headers=h).status_code == 201
    assert client.post("/api/auth/users", json={"username": "viewer", "password": STRONG, "role": "read_only"},
                       headers=h).status_code == 201
    ops = _login(client, "ops", STRONG)
    viewer = _login(client, "viewer", STRONG)

    me = client.get("/api/auth/session", headers=viewer).json()
    assert me["username"] == "viewer" and me["role"] == "read_only"
    assert me["permissions"] == ["agent:chat", "migrations:read"]

    # read_only: can list, can't create, can't see users
    assert client.get("/api/migrations", headers=viewer).status_code == 200
    r = client.post("/api/migrations", json={}, headers=viewer)
    assert r.status_code == 403 and "migrations:write" in r.json()["detail"]
    assert client.get("/api/auth/users", headers=viewer).status_code == 403
    assert client.put("/api/agent/provider", json={}, headers=viewer).status_code == 403
    # a 403 is not a throttled failure: still logged in afterwards
    assert client.get("/api/auth/session", headers=viewer).status_code == 200

    # operator: migrations yes, RBAC/settings no
    assert client.post("/api/migrations", json={}, headers=ops).status_code != 403
    assert client.get("/api/auth/users", headers=ops).status_code == 403
    assert client.post("/api/auth/roles", json={"name": "x", "permissions": []}, headers=ops).status_code == 403

    # own password change works for any session user
    r = client.post("/api/auth/password", json={"current_password": STRONG, "new_password": STRONG + "2"}, headers=viewer)
    assert r.status_code == 204
    _login(client, "viewer", STRONG + "2")


def test_custom_roles(admin):
    client, h = admin
    r = client.post("/api/auth/roles", json={"name": "auditor", "description": "Logs only",
                                             "permissions": ["migrations:read"]}, headers=h)
    assert r.status_code == 201 and r.json()["builtin"] is False
    assert client.post("/api/auth/roles", json={"name": "auditor", "permissions": []}, headers=h).status_code == 409
    assert client.post("/api/auth/roles", json={"name": "bad", "permissions": ["nope:x"]}, headers=h).status_code == 422
    assert client.put("/api/auth/roles/admin", json={"permissions": []}, headers=h).status_code == 409
    assert client.delete("/api/auth/roles/operator", headers=h).status_code == 409

    client.post("/api/auth/users", json={"username": "aud", "password": STRONG, "role": "auditor"}, headers=h)
    aud = _login(client, "aud", STRONG)
    assert client.post("/api/agent/chat", json={"message": "hi"}, headers=aud).status_code == 403
    # widen the role: takes effect on the next request, no re-login
    r = client.put("/api/auth/roles/auditor", json={"permissions": ["migrations:read", "agent:chat"]}, headers=h)
    assert r.status_code == 200 and r.json()["users"] == 1
    assert client.post("/api/agent/chat", json={"message": "hi"}, headers=aud).status_code != 403
    # in use -> can't delete; reassign, then delete
    assert client.delete("/api/auth/roles/auditor", headers=h).status_code == 409
    assert client.patch("/api/auth/users/aud", json={"role": "read_only"}, headers=h).status_code == 200
    assert client.delete("/api/auth/roles/auditor", headers=h).status_code == 204


def test_user_lifecycle_and_last_admin_guard(admin):
    client, h = admin
    assert client.post("/api/auth/users", json={"username": "Bad Name", "password": STRONG, "role": "operator"},
                       headers=h).status_code == 422
    assert client.post("/api/auth/users", json={"username": "ops", "password": "short", "role": "operator"},
                       headers=h).status_code == 422
    assert client.post("/api/auth/users", json={"username": "ops", "password": STRONG, "role": "nope"},
                       headers=h).status_code == 422
    client.post("/api/auth/users", json={"username": "ops", "password": STRONG, "role": "operator"}, headers=h)
    ops = _login(client, "ops", STRONG)

    # the only admin can't demote, disable or delete itself
    assert client.patch("/api/auth/users/admin", json={"role": "operator"}, headers=h).status_code == 409
    assert client.patch("/api/auth/users/admin", json={"disabled": True}, headers=h).status_code == 409
    assert client.delete("/api/auth/users/admin", headers=h).status_code == 409

    # disabling signs the user out and blocks login
    assert client.patch("/api/auth/users/ops", json={"disabled": True}, headers=h).status_code == 200
    assert client.get("/api/auth/session", headers=ops).status_code == 401
    assert client.post("/api/auth/login", json={"username": "ops", "password": STRONG}).status_code == 401
    assert client.patch("/api/auth/users/ops", json={"disabled": False}, headers=h).status_code == 200

    # admin password reset signs the user out; new password works
    ops = _login(client, "ops", STRONG)
    assert client.patch("/api/auth/users/ops", json={"password": STRONG + "3"}, headers=h).status_code == 200
    assert client.get("/api/auth/session", headers=ops).status_code == 401
    _login(client, "ops", STRONG + "3")

    # a second admin lets the first step down
    client.post("/api/auth/users", json={"username": "root2", "password": STRONG, "role": "admin"}, headers=h)
    assert client.patch("/api/auth/users/admin", json={"role": "operator"}, headers=h).status_code == 200
    # ...and that demotion revoked admin's session
    assert client.get("/api/auth/users", headers=h).status_code == 401

    users = client.get("/api/auth/users", headers=_login(client, "root2", STRONG)).json()
    assert [u["username"] for u in users] == ["admin", "ops", "root2"]
    assert client.delete("/api/auth/users/ops", headers=_login(client, "root2", STRONG)).status_code == 204


def test_login_default_username_and_probe_resistance(admin):
    client, _ = admin
    # old clients send only a password -> admin
    assert client.post("/api/auth/login", json={"password": STRONG}).status_code == 200
    unknown = client.post("/api/auth/login", json={"username": "ghost", "password": STRONG})
    wrong = client.post("/api/auth/login", json={"username": "admin", "password": "wrong-wrong-wrong"})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


def test_v1_auth_file_is_migrated(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_login_enabled", True)
    monkeypatch.setattr(settings, "migration_state_file", str(tmp_path / "state.json"))
    (tmp_path / "auth.json").write_text(json.dumps(
        {"admin_password_hash": hash_password(STRONG), "password_set_at": "2026-01-01T00:00:00+00:00"}))
    AuthStore._instance = None
    store = AuthStore.instance()
    try:
        assert store.password_set and store.check_password(STRONG, "admin")
        assert store.user("admin").role == "admin"
        assert store.password_set_at == "2026-01-01T00:00:00+00:00"
        on_disk = json.loads((tmp_path / "auth.json").read_text())
        assert on_disk["version"] == 2 and "admin" in on_disk["users"] and "admin_password_hash" not in on_disk
    finally:
        AuthStore._instance = None
