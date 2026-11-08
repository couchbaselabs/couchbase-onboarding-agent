"""LDAP sign-in with group -> role mapping, against ldap3's offline mock directory."""
from __future__ import annotations

import ldap3
import pytest
from fastapi.testclient import TestClient

import app.core.ldap_auth as ldap_mod
from app.api.security import throttle
from app.config import get_settings
from app.core.auth_store import AuthStore
from app.core.ldap_auth import LdapAuthenticator, LdapConfig, LdapError, LdapStore, RoleMapping

STRONG = "correct-horse-battery-staple"
BASE = "dc=corp,dc=example"
OPS_GROUP = "cn=cb-operators,ou=groups,dc=corp,dc=example"
RO_GROUP = "cn=cb-viewers,ou=groups,dc=corp,dc=example"


def _mock_factory(cfg: LdapConfig, user: str | None, password: str | None):
    """Stand-in for the real connection factory: an in-memory directory with
    a service account, two people and two groups. Binding as a person checks
    their userPassword like a real server would."""
    server = ldap3.Server("fake", get_info=ldap3.OFFLINE_SLAPD_2_4)
    conn = ldap3.Connection(server, user="cn=svc,dc=corp,dc=example", password="svc-pw",
                            client_strategy=ldap3.MOCK_SYNC)
    conn.strategy.add_entry("cn=svc,dc=corp,dc=example", {"objectClass": ["person"], "userPassword": "svc-pw", "sn": "svc"})
    conn.strategy.add_entry(f"uid=alice,ou=people,{BASE}", {
        "objectClass": ["inetOrgPerson"], "uid": "alice", "cn": "Alice Ops", "sn": "Ops",
        "userPassword": "alice-pw-123456", "memberOf": [OPS_GROUP, "cn=everyone,ou=groups,dc=corp,dc=example"]})
    conn.strategy.add_entry(f"uid=bob,ou=people,{BASE}", {
        "objectClass": ["inetOrgPerson"], "uid": "bob", "cn": "Bob Nobody", "sn": "Nobody",
        "userPassword": "bob-pw-1234567", "memberOf": ["cn=everyone,ou=groups,dc=corp,dc=example"]})
    conn.strategy.add_entry(OPS_GROUP, {"objectClass": ["groupOfNames"], "cn": "cb-operators",
                                        "member": [f"uid=alice,ou=people,{BASE}"]})
    if user is not None:
        # Re-bind as the person: MOCK_SYNC validates userPassword on bind.
        conn.unbind()
        conn = ldap3.Connection(server, user=user, password=password, client_strategy=ldap3.MOCK_SYNC)
        for dn, attrs in _ENTRIES.items():
            conn.strategy.add_entry(dn, attrs)
        if not conn.bind():
            raise LdapError(401, "Wrong username or password.")
        return conn
    if cfg.bind_dn and cfg.bind_password != "svc-pw":
        raise LdapError(502, "Service-account bind failed: invalidCredentials")
    assert conn.bind()
    return conn


_ENTRIES = {
    "cn=svc,dc=corp,dc=example": {"objectClass": ["person"], "userPassword": "svc-pw", "sn": "svc"},
    f"uid=alice,ou=people,{BASE}": {"objectClass": ["inetOrgPerson"], "uid": "alice", "sn": "Ops",
                                    "userPassword": "alice-pw-123456", "memberOf": [OPS_GROUP]},
    f"uid=bob,ou=people,{BASE}": {"objectClass": ["inetOrgPerson"], "uid": "bob", "sn": "Nobody",
                                  "userPassword": "bob-pw-1234567"},
}


def _cfg(**over) -> LdapConfig:
    base = dict(enabled=True, server_url="ldaps://dir.corp.example:636", bind_dn="cn=svc,dc=corp,dc=example",
                bind_password="svc-pw", user_base_dn=f"ou=people,{BASE}",
                user_filter="(&(objectClass=inetOrgPerson)(uid={username}))", username_attribute="uid",
                group_attribute="memberOf",
                role_mappings=[RoleMapping(group=OPS_GROUP, role="operator"), RoleMapping(group=RO_GROUP, role="read_only")])
    base.update(over)
    return LdapConfig(**base)


@pytest.fixture
def env(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_login_enabled", True)
    monkeypatch.setattr(settings, "api_auth_token", "")
    monkeypatch.setattr(settings, "migration_state_file", str(tmp_path / "state" / "migrations.json"))
    monkeypatch.setattr(ldap_mod, "_default_connection_factory", _mock_factory)
    AuthStore._instance = None
    LdapStore._instance = None
    throttle.reset()
    from app.main import app

    client = TestClient(app)
    token = client.post("/api/auth/setup", json={"password": STRONG}).json()["token"]
    yield client, {"Authorization": f"Bearer {token}"}
    LdapStore.instance().reset_for_tests()
    AuthStore.instance().reset_for_tests()
    AuthStore._instance = LdapStore._instance = None
    throttle.reset()


def test_authenticator_maps_groups_to_roles():
    auth = LdapAuthenticator(connection_factory=_mock_factory)
    ident = auth.authenticate("alice", "alice-pw-123456", _cfg())
    assert ident.dn == f"uid=alice,ou=people,{BASE}" and ident.role == "operator"
    assert OPS_GROUP in ident.groups
    with pytest.raises(LdapError) as exc:
        auth.authenticate("alice", "wrong-password", _cfg())
    assert exc.value.status == 401
    with pytest.raises(LdapError) as exc:
        auth.authenticate("nobody", "x", _cfg())
    assert exc.value.status == 401
    # bob authenticates but has no mapped group -> denied unless there's a default role
    with pytest.raises(LdapError) as exc:
        auth.authenticate("bob", "bob-pw-1234567", _cfg())
    assert exc.value.status == 403
    assert auth.authenticate("bob", "bob-pw-1234567", _cfg(default_role="read_only")).role == "read_only"
    # mapping order: first match wins
    both = _cfg(role_mappings=[RoleMapping(group=RO_GROUP, role="read_only"), RoleMapping(group=OPS_GROUP, role="operator")])
    assert auth.authenticate("alice", "alice-pw-123456", both).role == "operator"
    # filter injection is escaped
    assert auth.lookup(_cfg(), "*)(uid=alice") is None


def test_settings_roundtrip_validation_and_test_endpoint(env):
    client, h = env
    cfg = client.get("/api/settings/ldap", headers=h).json()
    assert cfg["config"]["enabled"] is False and "active_directory" in cfg["presets"] and "operator" in cfg["roles"]

    body = {"enabled": True, "server_url": "ldaps://dir.corp.example:636", "bind_dn": "cn=svc,dc=corp,dc=example",
            "bind_password": "svc-pw", "user_base_dn": f"ou=people,{BASE}",
            "user_filter": "(&(objectClass=inetOrgPerson)(uid={username}))", "username_attribute": "uid",
            "group_attribute": "memberOf", "role_mappings": [{"group": OPS_GROUP, "role": "operator"}]}
    bad = {**body, "role_mappings": [{"group": OPS_GROUP, "role": "superuser"}]}
    assert client.put("/api/settings/ldap", json=bad, headers=h).status_code == 422
    assert client.put("/api/settings/ldap", json={**body, "server_url": "dir.corp.example"}, headers=h).status_code == 422
    assert client.put("/api/settings/ldap", json={**body, "role_mappings": []}, headers=h).status_code == 422

    # test before saving: service bind + trial login
    r = client.post("/api/settings/ldap/test", json={**body, "test_username": "alice", "test_password": "alice-pw-123456"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] and r.json()["login"] == "ok" and r.json()["user"]["role"] == "operator"
    r = client.post("/api/settings/ldap/test", json={**body, "test_username": "bob"}, headers=h)
    assert r.json()["ok"] is False and "none of their groups" in r.json()["message"]
    r = client.post("/api/settings/ldap/test", json={**body, "bind_password": "nope"}, headers=h)
    assert r.status_code == 502

    r = client.put("/api/settings/ldap", json=body, headers=h)
    assert r.status_code == 200 and r.json()["config"]["bind_password"] == "" and r.json()["config"]["bind_password_set"]
    assert client.get("/api/config").json()["ldap_enabled"] is True
    # the stored password is encrypted on disk and kept when the UI sends ""
    on_disk = LdapStore.path().read_text()
    assert "svc-pw" not in on_disk
    r = client.put("/api/settings/ldap", json={**body, "bind_password": ""}, headers=h)
    assert r.status_code == 200 and LdapStore.instance().config.bind_password == "svc-pw"


def test_ldap_login_sessions(env):
    client, h = env
    body = {"enabled": True, "server_url": "ldaps://dir.corp.example:636", "bind_dn": "cn=svc,dc=corp,dc=example",
            "bind_password": "svc-pw", "user_base_dn": f"ou=people,{BASE}",
            "user_filter": "(&(objectClass=inetOrgPerson)(uid={username}))", "username_attribute": "uid",
            "group_attribute": "memberOf", "role_mappings": [{"group": OPS_GROUP, "role": "operator"}]}
    assert client.put("/api/settings/ldap", json=body, headers=h).status_code == 200

    # local admin still logs in locally
    assert client.post("/api/auth/login", json={"username": "admin", "password": STRONG}).status_code == 200
    # directory user
    r = client.post("/api/auth/login", json={"username": "alice", "password": "alice-pw-123456"})
    assert r.status_code == 200 and r.json()["role"] == "operator"
    alice = {"Authorization": f"Bearer {r.json()['token']}"}
    me = client.get("/api/auth/session", headers=alice).json()
    assert me["username"] == "alice" and me["source"] == "ldap" and "migrations:write" in me["permissions"]
    assert client.get("/api/auth/users", headers=alice).status_code == 403
    assert client.get("/api/migrations", headers=alice).status_code == 200
    # not a local user: no password change here
    r = client.post("/api/auth/password", json={"current_password": "alice-pw-123456", "new_password": STRONG}, headers=alice)
    assert r.status_code == 409 and "directory" in r.json()["detail"]
    # LDAP users don't appear among local users
    assert [u["username"] for u in client.get("/api/auth/users", headers=h).json()] == ["admin"]
    # wrong password / unmapped user
    assert client.post("/api/auth/login", json={"username": "alice", "password": "nope"}).status_code == 401
    r = client.post("/api/auth/login", json={"username": "bob", "password": "bob-pw-1234567"})
    assert r.status_code == 403
    # logout revokes
    assert client.post("/api/auth/logout", headers=alice).status_code == 204
    assert client.get("/api/auth/session", headers=alice).status_code == 401
    # with LDAP disabled, directory users can't sign in
    client.put("/api/settings/ldap", json={**body, "enabled": False}, headers=h)
    assert client.post("/api/auth/login", json={"username": "alice", "password": "alice-pw-123456"}).status_code == 401
