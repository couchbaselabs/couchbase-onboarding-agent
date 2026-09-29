"""Optional bearer-token authentication for the REST API and websockets.

Enabled by setting API_AUTH_TOKEN. REST callers send `Authorization: Bearer
<token>`; browsers can't set headers on a WebSocket, so /ws endpoints accept
`?token=<token>` instead. /api/health stays open for liveness probes.
"""
from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, WebSocket, status

from app.config import get_settings


def _token_ok(presented: str | None) -> bool:
    expected = get_settings().api_auth_token
    if not expected:
        return True
    return bool(presented) and hmac.compare_digest(presented.encode(), expected.encode())


async def require_token(request: Request) -> None:
    header = request.headers.get("authorization", "")
    presented = header[7:].strip() if header.lower().startswith("bearer ") else None
    if not _token_ok(presented):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def websocket_authorized(websocket: WebSocket) -> bool:
    if _token_ok(websocket.query_params.get("token")):
        return True
    # Accept, then close with an application code the UI recognizes as "log in again".
    await websocket.accept()
    await websocket.close(code=4401)
    return False


def auth_enabled() -> bool:
    return bool(get_settings().api_auth_token)
