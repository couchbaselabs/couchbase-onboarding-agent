"""Read-only endpoints for migration statistics (used by the dashboard on initial
load; live updates stream over the /ws/migrations websocket)."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException

from app.core.store import MigrationStore
from app.models.schemas import MigrationStats

router = APIRouter()


@router.get("/{migration_id}", response_model=MigrationStats)
async def get_stats(migration_id: UUID):
    store = MigrationStore.instance()
    record = await store.get(migration_id)
    if not record:
        raise HTTPException(404, "Migration not found")
    return record.stats


@router.get("/{migration_id}/logs")
async def get_logs(migration_id: UUID):
    store = MigrationStore.instance()
    record = await store.get(migration_id)
    if not record:
        raise HTTPException(404, "Migration not found")
    return {"log_tail": record.log_tail}
