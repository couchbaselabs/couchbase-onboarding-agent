"""Bearer authentication for the REST API and websockets, with brute-force
throttling and an auth audit trail.

Two kinds of bearer are accepted, both as `Authorization: Bearer <token>`:
  - the static API_AUTH_TOKEN from the environment (scripts, automation), and
  - a browser session token issued by /api/auth/login once the administrator
    has set a password (core/auth_store.py; ADMIN_LOGIN_ENABLED, on by default).
While the admin login is enabled but no password has been set yet, the API is
closed except for /api/auth/setup (and API_AUTH_TOKEN, if one is configured),
so a fresh instance can't be used before it's claimed. Websockets can't set headers from a browser, so /ws endpoints accept
the token as a WebSocket subprotocol (`Sec-WebSocket-Protocol: coa-auth, <token>`,
which the UI uses -- it never appears in a URL or an access log) or, for older
clients, as `?token=<token>`. /api/health and /api/config stay open for
liveness probes and for the UI to learn whether a token is needed.

Throttling: a client that keeps presenting a wrong token is locked out per
source IP (AUTH_MAX_FAILURES failures within AUTH_FAILURE_WINDOW_S locks that
IP for AUTH_LOCKOUT_S) and gets 429 with Retry-After before the token is even
compared. State is in-memory and per-process -- it resets on restart and isn't
shared across replicas, which matches the single-instance deployments this app
targets. Every rejected attempt and every lockout is logged with the source IP
under the `onboarding_agent.auth` logger (NIST AU-2, PCI DSS 10.2).
"""
from __future__ import annotations

import hmac
import logging
import time
from collections import deque

from fastapi import HTTPException, Request, WebSocket, status

from app.config import get_settings
from app.core.auth_store import AuthStore

audit = logging.getLogger("onboarding_agent.auth")

WS_SUBPROTOCOL = "coa-auth"


class _Throttle:
    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}

    def retry_after(self, ip: str) -> int:
        until = self._locked_until.get(ip, 0.0)
        now = time.monotonic()
        if until > now:
            return int(until - now) + 1
        if until:
            self._locked_until.pop(ip, None)
        return 0

    def record_failure(self, ip: str) -> int:
        """Returns the lockout length in seconds if this failure triggered one."""
        s = get_settings()
        now = time.monotonic()
        window = self._failures.setdefault(ip, deque())
        window.append(now)
        while window and now - window[0] > s.auth_failure_window_s:
            window.popleft()
        if len(window) >= s.auth_max_failures:
            self._locked_until[ip] = now + s.auth_lockout_s
            window.clear()
            return s.auth_lockout_s
        return 0

    def record_success(self, ip: str) -> None:
        self._failures.pop(ip, None)

    def reset(self) -> None:
        self._failures.clear()
        self._locked_until.clear()


throttle = _Throttle()


def client_ip(headers, client) -> str:
    """Source IP, trusting exactly one proxy hop (the bundled nginx / the
    ingress sets X-Forwarded-For); falls back to the socket address."""
    if get_settings().trust_x_forwarded_for:
        forwarded = headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip() or "unknown"
    return client.host if client else "unknown"


def _static_token_ok(presented: str | None) -> bool:
    expected = get_settings().api_auth_token
    return bool(expected) and bool(presented) and hmac.compare_digest(presented.encode(), expected.encode())


def _token_ok(presented: str | None) -> bool:
    if not auth_enabled():
        return True
    if _static_token_ok(presented):
        return True
    return AuthStore.instance().session_valid(presented)


def _check(presented: str | None, ip: str, what: str) -> int | None:
    """None when authorized; otherwise the HTTP status to reject with."""
    if not auth_enabled():
        return None
    wait = throttle.retry_after(ip)
    if wait:
        audit.warning("auth: rejected %s from %s -- locked out for another %ss", what, ip, wait)
        return status.HTTP_429_TOO_MANY_REQUESTS
    if _token_ok(presented):
        throttle.record_success(ip)
        return None
    locked = throttle.record_failure(ip)
    if locked:
        audit.warning("auth: %s from %s presented an invalid token; too many failures, locked out for %ss",
                      what, ip, locked)
    else:
        audit.warning("auth: %s from %s presented %s token", what, ip, "an invalid" if presented else "no")
    return status.HTTP_401_UNAUTHORIZED


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else None


def auth_detail() -> str:
    if AuthStore.instance().setup_required and not get_settings().api_auth_token:
        return "Set the administrator password first (POST /api/auth/setup or open the UI)."
    return "Missing or invalid API token / session."


async def require_token(request: Request) -> None:
    presented = bearer_token(request)
    ip = client_ip(request.headers, request.client)
    code = _check(presented, ip, f"{request.method} {request.url.path}")
    if code == status.HTTP_429_TOO_MANY_REQUESTS:
        raise HTTPException(
            status_code=code, detail="Too many failed authentication attempts; try again later.",
            headers={"Retry-After": str(throttle.retry_after(ip))},
        )
    if code is not None:
        raise HTTPException(
            status_code=code, detail=auth_detail(), headers={"WWW-Authenticate": "Bearer"},
        )


def _ws_token(websocket: WebSocket) -> tuple[str | None, str | None]:
    """(token, subprotocol to accept with). The token follows the `coa-auth`
    marker in Sec-WebSocket-Protocol; `?token=` is the legacy fallback."""
    protocols = [p.strip() for p in websocket.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
    if WS_SUBPROTOCOL in protocols:
        idx = protocols.index(WS_SUBPROTOCOL)
        token = protocols[idx + 1] if idx + 1 < len(protocols) else None
        return token, WS_SUBPROTOCOL
    return websocket.query_params.get("token"), None


async def websocket_authorized(websocket: WebSocket) -> str | None:
    """Accept the connection and return the negotiated subprotocol (or "" when
    none). Returns None after closing it when the token is missing/invalid."""
    token, subprotocol = _ws_token(websocket)
    ip = client_ip(websocket.headers, websocket.client)
    code = _check(token, ip, f"websocket {websocket.url.path}")
    if code is None:
        await websocket.accept(subprotocol=subprotocol)
        return subprotocol or ""
    # Accept, then close with an application code the UI recognizes as "log in again".
    await websocket.accept(subprotocol=subprotocol)
    await websocket.close(code=4429 if code == status.HTTP_429_TOO_MANY_REQUESTS else 4401)
    return None


def auth_enabled() -> bool:
    """True when callers must present a bearer: a static token is configured
    and/or the admin login is enabled (even before its password is set)."""
    return bool(get_settings().api_auth_token) or AuthStore.instance().login_enabled
