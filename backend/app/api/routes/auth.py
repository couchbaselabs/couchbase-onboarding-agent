"""Administrator login: first-run password setup, login/logout, password change.

Mounted WITHOUT the bearer dependency (these are how a browser obtains a
bearer); each handler checks what it needs. Wrong passwords count against the
same per-IP throttle as wrong tokens.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.api.security import (
    _static_token_ok,
    audit,
    auth_enabled,
    bearer_token,
    client_ip,
    throttle,
)
from app.config import get_settings
from app.core.auth_store import AuthStore, password_problem

router = APIRouter()


class SetupRequest(BaseModel):
    password: str = Field(min_length=1, max_length=512)
    # Required when API_AUTH_TOKEN is configured: proves the person claiming
    # this instance is its operator, not whoever found it first.
    bootstrap_token: str = ""


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=512)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=1, max_length=512)


class SessionResponse(BaseModel):
    token: str
    expires_at: int  # epoch seconds (idle timeout; sliding)


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
    token, expires_at = store.create_session()
    return SessionResponse(token=token, expires_at=expires_at)


@router.post("/login", response_model=SessionResponse)
async def login(req: LoginRequest, request: Request) -> SessionResponse:
    store = AuthStore.instance()
    if not store.login_enabled:
        raise HTTPException(409, "The admin login is disabled for this deployment; use the API token.")
    if not store.password_set:
        raise HTTPException(409, "No administrator password yet -- set one first.")
    ip = client_ip(request.headers, request.client)
    _throttled(ip)
    if not store.check_password(req.password):
        _fail(ip, "login")
        raise HTTPException(401, "Wrong password.")
    throttle.record_success(ip)
    audit.info("auth: administrator logged in from %s", ip)
    token, expires_at = store.create_session()
    return SessionResponse(token=token, expires_at=expires_at)


def _require_bearer(request: Request) -> str | None:
    """These handlers are mounted unprotected; enforce the bearer by hand."""
    from app.api.security import _check  # noqa: PLC0415

    presented = bearer_token(request)
    code = _check(presented, client_ip(request.headers, request.client), f"{request.method} {request.url.path}")
    if code is not None:
        raise HTTPException(code, "Missing or invalid API token / session.")
    return presented


@router.get("/session")
async def session(request: Request) -> dict:
    """Who the caller is, as far as the API can tell."""
    presented = _require_bearer(request)
    store = AuthStore.instance()
    if not auth_enabled():
        method = "open"
    elif _static_token_ok(presented):
        method = "token"
    else:
        method = "session"
    return {
        "authenticated": True, "method": method,
        "login_enabled": store.login_enabled, "password_set_at": store.password_set_at,
    }


@router.post("/logout", status_code=204, response_class=Response)
async def logout(request: Request) -> Response:
    presented = _require_bearer(request)
    AuthStore.instance().revoke(presented)
    return Response(status_code=204)


@router.post("/password", status_code=204, response_class=Response)
async def change_password(req: ChangePasswordRequest, request: Request) -> Response:
    presented = _require_bearer(request)
    store = AuthStore.instance()
    if not store.login_enabled:
        raise HTTPException(409, "The admin login is disabled for this deployment.")
    if not store.password_set:
        raise HTTPException(409, "No administrator password yet -- set one first.")
    ip = client_ip(request.headers, request.client)
    _throttled(ip)
    if not store.check_password(req.current_password):
        _fail(ip, "password change (wrong current password)")
        raise HTTPException(401, "The current password is wrong.")
    problem = password_problem(req.new_password)
    if problem:
        raise HTTPException(422, problem)
    store.set_password(req.new_password)
    dropped = store.revoke_all_except(presented)
    audit.warning("auth: administrator password changed from %s (%d other session(s) signed out)", ip, dropped)
    return Response(status_code=204)
