"""Login and access control: first-run password setup, login/logout, own
password change, and -- for `users:manage` -- users, roles and the permission
catalog (Settings -> RBAC & Roles).

Mounted WITHOUT the bearer dependency (these are how a browser obtains a
bearer); each handler checks what it needs via `_require_bearer`, which also
applies the route's permission (core/rbac.py). Wrong passwords count against
the same per-IP throttle as wrong tokens.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.api.security import (
    _static_token_ok,
    audit,
    authorize,
    bearer_token,
    client_ip,
    throttle,
)
from app.config import get_settings
from app.core.auth_store import ADMIN_ROLE, AuthError, AuthStore, password_problem
from app.core.ldap_auth import LdapAuthenticator, LdapError, LdapStore
from app.core.rbac import PERMISSIONS, Principal

router = APIRouter()


class SetupRequest(BaseModel):
    password: str = Field(min_length=1, max_length=512)
    # Required when API_AUTH_TOKEN is configured: proves the person claiming
    # this instance is its operator, not whoever found it first.
    bootstrap_token: str = ""


class LoginRequest(BaseModel):
    username: str = Field(default=ADMIN_ROLE, min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=512)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=1, max_length=512)


class SessionResponse(BaseModel):
    token: str
    expires_at: int  # epoch seconds (idle timeout; sliding)
    username: str = ADMIN_ROLE
    role: str = ADMIN_ROLE


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=512)
    role: str = Field(min_length=1, max_length=64)


class UpdateUserRequest(BaseModel):
    role: str | None = Field(default=None, max_length=64)
    disabled: bool | None = None
    # Set by an administrator; the user's own change goes through /password.
    password: str | None = Field(default=None, max_length=512)


class RoleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=200)
    permissions: list[str] = Field(default_factory=list)


class UpdateRoleRequest(BaseModel):
    description: str | None = Field(default=None, max_length=200)
    permissions: list[str] | None = None


def _throttled(ip: str) -> None:
    wait = throttle.retry_after(ip)
    if wait:
        audit.warning("auth: rejected login from %s -- locked out for another %ss", ip, wait)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed attempts; try again later.",
            headers={"Retry-After": str(wait)},
        )


def _fail(ip: str, what: str) -> None:
    locked = throttle.record_failure(ip)
    if locked:
        audit.warning("auth: %s from %s failed; too many failures, locked out for %ss", what, ip, locked)
    else:
        audit.warning("auth: %s from %s failed", what, ip)


@router.post("/setup", response_model=SessionResponse)
async def setup(req: SetupRequest, request: Request) -> SessionResponse:
    store = AuthStore.instance()
    if not store.login_enabled:
        raise HTTPException(409, "The admin login is disabled for this deployment (ADMIN_LOGIN_ENABLED=false).")
    if store.password_set:
        raise HTTPException(409, "The administrator password is already set. Log in to change it.")
    ip = client_ip(request.headers, request.client)
    _throttled(ip)
    if get_settings().api_auth_token and not _static_token_ok(req.bootstrap_token or bearer_token(request)):
        _fail(ip, "first-run setup (wrong bootstrap token)")
        raise HTTPException(401, "This instance has an API_AUTH_TOKEN; enter it to set the administrator password.")
    problem = password_problem(req.password)
    if problem:
        raise HTTPException(422, problem)
    store.set_password(req.password)
    throttle.record_success(ip)
    audit.warning("auth: administrator password set from %s", ip)
    token, expires_at = store.create_session(ADMIN_ROLE)
    return SessionResponse(token=token, expires_at=expires_at, username=ADMIN_ROLE, role=ADMIN_ROLE)


@router.post("/login", response_model=SessionResponse)
async def login(req: LoginRequest, request: Request) -> SessionResponse:
    store = AuthStore.instance()
    if not store.login_enabled:
        raise HTTPException(409, "The admin login is disabled for this deployment; use the API token.")
    if not store.password_set:
        raise HTTPException(409, "No administrator password yet -- set one first.")
    ip = client_ip(request.headers, request.client)
    _throttled(ip)
    username = req.username.strip().lower()
    user = store.user(username)
    if user is None and LdapStore.instance().enabled:
        # Not a local account: try the directory (Settings -> LDAP).
        try:
            ident = LdapAuthenticator().authenticate(username, req.password)
        except LdapError as exc:
            if exc.status == 401:
                _fail(ip, f"ldap login as '{username}'")
                raise HTTPException(401, "Wrong username or password.") from exc
            if exc.status == 403:
                audit.warning("auth: ldap user %s denied from %s (no mapped role)", username, ip)
                raise HTTPException(403, str(exc)) from exc
            audit.error("auth: ldap login for %s from %s errored: %s", username, ip, exc)
            raise HTTPException(502, "The directory couldn't be reached; try again or contact an administrator.") from exc
        throttle.record_success(ip)
        audit.info("auth: %s logged in via LDAP as %s (%s) from %s", username, ident.dn, ident.role, ip)
        token, expires_at = store.create_session(username, source="ldap", role=ident.role)
        return SessionResponse(token=token, expires_at=expires_at, username=username, role=ident.role or "")
    if not store.check_password(req.password, username):
        _fail(ip, f"login as '{username}'")
        # Same answer for unknown, disabled and wrong-password so usernames can't be probed.
        raise HTTPException(401, "Wrong username or password.")
    throttle.record_success(ip)
    audit.info("auth: %s (%s) logged in from %s", username, user.role if user else "?", ip)
    token, expires_at = store.create_session(username)
    return SessionResponse(token=token, expires_at=expires_at, username=username, role=user.role if user else "")


def _require_bearer(request: Request) -> tuple[str | None, Principal]:
    """These handlers are mounted unprotected; enforce the bearer (and the
    route's permission) by hand."""
    return bearer_token(request), authorize(request)


@router.get("/session")
async def session(request: Request) -> dict:
    """Who the caller is, as far as the API can tell, and what they may do."""
    _, who = _require_bearer(request)
    store = AuthStore.instance()
    return {
        "authenticated": True, "method": who.method, "source": who.source, "username": who.username, "role": who.role,
        "permissions": sorted(who.permissions),
        "login_enabled": store.login_enabled,
        "password_set_at": (store.user(who.username).password_set_at if who.source == "local" and store.user(who.username) else None),
    }


@router.post("/logout", status_code=204, response_class=Response)
async def logout(request: Request) -> Response:
    presented, _ = _require_bearer(request)
    AuthStore.instance().revoke(presented)
    return Response(status_code=204)


@router.post("/password", status_code=204, response_class=Response)
async def change_password(req: ChangePasswordRequest, request: Request) -> Response:
    presented, who = _require_bearer(request)
    store = AuthStore.instance()
    if not store.login_enabled:
        raise HTTPException(409, "The admin login is disabled for this deployment.")
    if not store.password_set:
        raise HTTPException(409, "No administrator password yet -- set one first.")
    if who.method != "session":
        raise HTTPException(409, "Log in with a username and password to change it (the API token has none).")
    if who.source == "ldap":
        raise HTTPException(409, "Your password is managed by your directory (LDAP), not here.")
    ip = client_ip(request.headers, request.client)
    _throttled(ip)
    if not store.check_password(req.current_password, who.username):
        _fail(ip, f"password change for '{who.username}' (wrong current password)")
        raise HTTPException(401, "The current password is wrong.")
    problem = password_problem(req.new_password)
    if problem:
        raise HTTPException(422, problem)
    store.set_password(req.new_password, who.username)
    dropped = store.revoke_all_except(presented, who.username)
    audit.warning("auth: %s changed their password from %s (%d other session(s) signed out)", who.username, ip, dropped)
    return Response(status_code=204)


# ---------------------------------------------------------------- RBAC (users:manage)

def _rbac(fn):
    """Translate AuthStore refusals into HTTP errors."""
    try:
        return fn()
    except AuthError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.get("/permissions")
async def list_permissions(request: Request) -> list[dict]:
    _require_bearer(request)
    return [{"name": name, "description": desc} for name, desc in PERMISSIONS.items()]


@router.get("/roles")
async def list_roles(request: Request) -> list[dict]:
    _require_bearer(request)
    return AuthStore.instance().list_roles()


@router.post("/roles", status_code=201)
async def create_role(req: RoleRequest, request: Request) -> dict:
    _, who = _require_bearer(request)
    name = req.name.strip().lower()
    out = _rbac(lambda: AuthStore.instance().create_role(name, req.description, req.permissions))
    audit.warning("rbac: %s created role '%s' with %s", who.username, name, sorted(req.permissions))
    return out


@router.put("/roles/{name}")
async def update_role(name: str, req: UpdateRoleRequest, request: Request) -> dict:
    _, who = _require_bearer(request)
    out = _rbac(lambda: AuthStore.instance().update_role(name, req.description, req.permissions))
    audit.warning("rbac: %s updated role '%s' -> %s", who.username, name, out["permissions"])
    return out


@router.delete("/roles/{name}", status_code=204, response_class=Response)
async def delete_role(name: str, request: Request) -> Response:
    _, who = _require_bearer(request)
    _rbac(lambda: AuthStore.instance().delete_role(name))
    audit.warning("rbac: %s deleted role '%s'", who.username, name)
    return Response(status_code=204)


@router.get("/users")
async def list_users(request: Request) -> list[dict]:
    _require_bearer(request)
    return AuthStore.instance().list_users()


@router.post("/users", status_code=201)
async def create_user(req: CreateUserRequest, request: Request) -> dict:
    _, who = _require_bearer(request)
    username = req.username.strip().lower()
    out = _rbac(lambda: AuthStore.instance().create_user(username, req.password, req.role))
    audit.warning("rbac: %s created user '%s' with role '%s'", who.username, username, req.role)
    return out


@router.patch("/users/{username}")
async def update_user(username: str, req: UpdateUserRequest, request: Request) -> dict:
    _, who = _require_bearer(request)
    out = _rbac(lambda: AuthStore.instance().update_user(
        username, role=req.role, disabled=req.disabled, password=req.password, acting_user=who.username))
    changes = [k for k, v in (("role", req.role), ("disabled", req.disabled), ("password", req.password)) if v is not None]
    audit.warning("rbac: %s updated user '%s' (%s)", who.username, username, ", ".join(changes) or "no-op")
    return out


@router.delete("/users/{username}", status_code=204, response_class=Response)
async def delete_user(username: str, request: Request) -> Response:
    _, who = _require_bearer(request)
    _rbac(lambda: AuthStore.instance().delete_user(username, acting_user=who.username))
    audit.warning("rbac: %s deleted user '%s'", who.username, username)
    return Response(status_code=204)
