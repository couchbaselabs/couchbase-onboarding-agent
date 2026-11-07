"""
Websocket hub for pushing live migration progress/stats to the React dashboard.
Clients subscribe by migration_id; the MigrationEngine calls `broadcast()` on every
progress tick / phase change. Frames carry the record with credentials masked, and
the endpoints require the API token when one is configured (as the `coa-auth`
websocket subprotocol, or ?token=... for older clients).
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.security import websocket_authorized
from app.models.schemas import MigrationRecord, redact_record

logger = logging.getLogger(__name__)
router = APIRouter()


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = {}

    def connect(self, migration_id: str, ws: WebSocket) -> None:
        """Register an already-accepted websocket (security.websocket_authorized
        accepts it, negotiating the auth subprotocol)."""
        self._connections.setdefault(migration_id, set()).add(ws)

    def disconnect(self, migration_id: str, ws: WebSocket) -> None:
        conns = self._connections.get(migration_id)
        if conns and ws in conns:
            conns.remove(ws)
        if conns is not None and not conns:
            self._connections.pop(migration_id, None)

    async def broadcast(self, record: MigrationRecord) -> None:
        migration_id = str(record.migration_id)
        payload = json.dumps(redact_record(record), default=str)
        for target_id in (migration_id, "*"):  # "*" subscribers get every migration
            for ws in list(self._connections.get(target_id, set())):
                try:
                    await ws.send_text(payload)
                except Exception:  # noqa: BLE001
                    self.disconnect(target_id, ws)


manager = ConnectionManager()


async def broadcast_progress(record: MigrationRecord) -> None:
    await manager.broadcast(record)


@router.websocket("/ws/migrations/{migration_id}")
async def migration_progress_ws(websocket: WebSocket, migration_id: str) -> None:
    if await websocket_authorized(websocket) is None:
        return
    manager.connect(migration_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(migration_id, websocket)


@router.websocket("/ws/migrations")
async def all_migrations_ws(websocket: WebSocket) -> None:
    """Subscribe to progress updates for every in-flight migration (dashboard overview)."""
    if await websocket_authorized(websocket) is None:
        return
    manager.connect("*", websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect("*", websocket)
