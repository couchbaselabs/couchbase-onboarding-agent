"""
Core migration lifecycle endpoints: create a plan, run validation, approve, start,
stop/cutover, roll back, and list migrations. This is the primary surface the React
wizard and dashboard talk to.
"""
from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.core.migration_engine import MigrationEngine
from app.core.store import MigrationStore
from app.memory.couchbase_memory import AgentMemoryStore
from app.models.enums import MigrationPhase
from app.models.schemas import (
    MigrationApproval,
    MigrationPlanCreate,
    MigrationRecord,
    ReplicationStopRequest,
    RollbackRequest,
    ValidationReport,
)
from app.websocket.progress import broadcast_progress

logger = logging.getLogger(__name__)
router = APIRouter()


def _engine() -> MigrationEngine:
    return MigrationEngine(on_progress=broadcast_progress)


@router.post("", response_model=MigrationRecord)
async def create_migration(plan: MigrationPlanCreate) -> MigrationRecord:
    record = MigrationRecord(plan=plan)
    await MigrationStore.instance().save(record)
    return record


@router.get("", response_model=list[MigrationRecord])
async def list_migrations() -> list[MigrationRecord]:
    return await MigrationStore.instance().list_all()


@router.get("/{migration_id}", response_model=MigrationRecord)
async def get_migration(migration_id: UUID) -> MigrationRecord:
    record = await MigrationStore.instance().get(migration_id)
    if not record:
        raise HTTPException(404, "Migration not found")
    return record


@router.post("/{migration_id}/validate", response_model=ValidationReport)
async def validate_migration(migration_id: UUID) -> ValidationReport:
    record = await MigrationStore.instance().get(migration_id)
    if not record:
        raise HTTPException(404, "Migration not found")
    record = await _engine().validate(record)

    try:
        await AgentMemoryStore.instance().remember(
            "validation_result",
            {
                "migration_name": record.plan.name,
                "passed": record.validation_report.passed if record.validation_report else None,
                "source_type": record.plan.source.source_type.value,
                "destination_bucket": record.plan.destination_bucket,
            },
            migration_id=str(migration_id),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to write validation memory: %s", exc)

    if not record.validation_report:
        raise HTTPException(500, "Validation did not produce a report")
    return record.validation_report


@router.post("/{migration_id}/approve", response_model=MigrationRecord)
async def approve_migration(migration_id: UUID, approval: MigrationApproval) -> MigrationRecord:
    record = await MigrationStore.instance().get(migration_id)
    if not record:
        raise HTTPException(404, "Migration not found")
    if approval.migration_id != migration_id:
        raise HTTPException(400, "migration_id mismatch between path and body")
    try:
        record = await _engine().approve(record, approval.approved_by)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    try:
        await AgentMemoryStore.instance().remember(
            "migration_approved",
            {"migration_name": record.plan.name, "approved_by": approval.approved_by},
            migration_id=str(migration_id),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to write approval memory: %s", exc)
    return record


@router.post("/{migration_id}/start", response_model=MigrationRecord)
async def start_migration(migration_id: UUID, background_tasks: BackgroundTasks) -> MigrationRecord:
    record = await MigrationStore.instance().get(migration_id)
    if not record:
        raise HTTPException(404, "Migration not found")
    if record.phase != MigrationPhase.APPROVED:
        raise HTTPException(400, f"Migration must be approved before starting (current phase: {record.phase}).")

    async def _run() -> None:
        engine = _engine()
        # For continuous strategies this blocks until stop_replication() (a separate
        # request) flips the phase away from REPLICATING -- expected for "continuous".
        finished = await engine.run_migration(record)
        memory_kind = {
            MigrationPhase.COMPLETE: "migration_completed",
            MigrationPhase.STOPPED: "replication_stopped",
        }.get(finished.phase, "migration_failed")
        try:
            await AgentMemoryStore.instance().remember(
                memory_kind,
                {
                    "migration_name": finished.plan.name,
                    "phase": finished.phase.value,
                    "docs_migrated": finished.stats.docs_migrated,
                    "mutations_replicated": finished.stats.mutations_replicated,
                    "error": finished.error_message,
                },
                migration_id=str(migration_id),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to write completion memory: %s", exc)

    # Scheduled as a background task (not run inline) so this response returns
    # immediately -- a real migration can run for a long time, and a single HTTP
    # request held open that long is exposed to any idle-connection reset along the
    # way. The wizard's progress bar comes entirely from the websocket instead.
    background_tasks.add_task(_run)
    return record


@router.post("/{migration_id}/replication/stop", response_model=MigrationRecord)
async def stop_replication(migration_id: UUID, req: ReplicationStopRequest) -> MigrationRecord:
    record = await MigrationStore.instance().get(migration_id)
    if not record:
        raise HTTPException(404, "Migration not found")
    if req.migration_id != migration_id:
        raise HTTPException(400, "migration_id mismatch between path and body")
    try:
        return await _engine().stop_replication(record, req.perform_cutover)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/{migration_id}/rollback", response_model=MigrationRecord)
async def rollback_migration(migration_id: UUID, req: RollbackRequest) -> MigrationRecord:
    record = await MigrationStore.instance().get(migration_id)
    if not record:
        raise HTTPException(404, "Migration not found")
    if req.migration_id != migration_id:
        raise HTTPException(400, "migration_id mismatch between path and body")
    return await _engine().rollback(record, req.reason, req.purge_destination_data)


@router.delete("/{migration_id}")
async def delete_migration(migration_id: UUID) -> dict:
    await MigrationStore.instance().delete(migration_id)
    return {"deleted": True}
