"""
Migration lifecycle endpoints: create, validate, approve, start, stop/cutover,
resume, reconcile, roll back (with a dry-run plan), post-cutover fallback, and
list/delete. Every response masks credentials (models.schemas.redact_record).

Long-running work (bulk copy + sync, rollback, reconciliation, fallback) runs as
a named background task (core/runner.py), so a second click can't start a
duplicate and a backend restart can pick it up again (see main.py).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException

from app.core import runner
from app.core.migration_engine import MigrationEngine
from app.core.store import MigrationStore
from app.memory.couchbase_memory import AgentMemoryStore
from app.models.enums import MigrationPhase
from app.models.schemas import (
    FallbackStopRequest,
    MigrationApproval,
    MigrationPlanCreate,
    MigrationRecord,
    ReconcileRequest,
    ReplicationStopRequest,
    RollbackPlan,
    RollbackRequest,
    ValidationReport,
    redact_record,
)
from app.websocket.progress import broadcast_progress

logger = logging.getLogger(__name__)
router = APIRouter()

# Phases in which a migration is doing work and can't be deleted.
_ACTIVE_PHASES = {
    MigrationPhase.VALIDATING, MigrationPhase.MIGRATING, MigrationPhase.REPLICATING,
    MigrationPhase.VERIFYING, MigrationPhase.ROLLING_BACK, MigrationPhase.FALLBACK_SYNCING,
}


def _engine() -> MigrationEngine:
    return MigrationEngine(on_progress=broadcast_progress)


async def _get(migration_id: UUID) -> MigrationRecord:
    record = await MigrationStore.instance().get(migration_id)
    if not record:
        raise HTTPException(404, "Migration not found")
    return record


def _check_body_id(path_id: UUID, body_id: UUID) -> None:
    if path_id != body_id:
        raise HTTPException(400, "migration_id mismatch between path and body")


async def _remember(kind: str, payload: dict, migration_id: UUID) -> None:
    try:
        await AgentMemoryStore.instance().remember(kind, payload, migration_id=str(migration_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to write agent memory (%s): %s", kind, exc)


async def _settle() -> None:
    """Give a just-spawned task a moment to record its first phase change, so the
    response reflects it."""
    await asyncio.sleep(0.05)


@router.post("")
async def create_migration(plan: MigrationPlanCreate) -> dict[str, Any]:
    record = MigrationRecord(plan=plan)
    await MigrationStore.instance().save(record)
    return redact_record(record)


@router.get("")
async def list_migrations() -> list[dict[str, Any]]:
    return [redact_record(r) for r in await MigrationStore.instance().list_all()]


@router.get("/{migration_id}")
async def get_migration(migration_id: UUID) -> dict[str, Any]:
    return redact_record(await _get(migration_id))


@router.post("/{migration_id}/validate", response_model=ValidationReport)
async def validate_migration(migration_id: UUID) -> ValidationReport:
    record = await _get(migration_id)
    if record.phase in _ACTIVE_PHASES:
        raise HTTPException(400, f"Can't re-validate while the migration is {record.phase.value}.")
    record = await _engine().validate(record)
    await _remember("validation_result", {
        "migration_name": record.plan.name,
        "passed": record.validation_report.passed if record.validation_report else None,
        "source_type": record.plan.source.source_type.value,
        "destination_bucket": record.plan.destination_bucket,
    }, migration_id)
    if not record.validation_report:
        raise HTTPException(500, "Validation did not produce a report")
    return record.validation_report


@router.post("/{migration_id}/approve")
async def approve_migration(migration_id: UUID, approval: MigrationApproval) -> dict[str, Any]:
    record = await _get(migration_id)
    _check_body_id(migration_id, approval.migration_id)
    try:
        record = await _engine().approve(record, approval.approved_by)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await _remember("migration_approved", {"migration_name": record.plan.name, "approved_by": approval.approved_by},
                    migration_id)
    return redact_record(record)


async def _run_and_remember(migration_id: UUID, coro) -> None:
    finished = await coro
    memory_kind = {
        MigrationPhase.COMPLETE: "migration_completed",
        MigrationPhase.STOPPED: "replication_stopped",
        MigrationPhase.ROLLED_BACK: "migration_rolled_back",
    }.get(finished.phase, "migration_failed" if finished.phase == MigrationPhase.FAILED else None)
    if memory_kind:
        await _remember(memory_kind, {
            "migration_name": finished.plan.name,
            "phase": finished.phase.value,
            "docs_migrated": finished.stats.docs_migrated,
            "mutations_replicated": finished.stats.mutations_replicated,
            "error": finished.error_message,
        }, migration_id)


@router.post("/{migration_id}/start")
async def start_migration(migration_id: UUID) -> dict[str, Any]:
    record = await _get(migration_id)
    if record.phase != MigrationPhase.APPROVED:
        raise HTTPException(400, f"Migration must be approved before starting (current phase: {record.phase.value}).")
    # A background task, not an open request: a migration can run for days.
    # Progress streams over the websocket.
    if not runner.spawn(str(migration_id), "run", _run_and_remember(migration_id, _engine().run_migration(record))):
        raise HTTPException(409, "This migration is already running.")
    await _settle()
    return redact_record(record)


@router.post("/{migration_id}/replication/stop")
async def stop_replication(migration_id: UUID, req: ReplicationStopRequest) -> dict[str, Any]:
    record = await _get(migration_id)
    _check_body_id(migration_id, req.migration_id)
    try:
        return redact_record(await _engine().stop_replication(record, req.perform_cutover))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/{migration_id}/resume")
async def resume_migration(migration_id: UUID) -> dict[str, Any]:
    record = await _get(migration_id)
    engine = _engine()
    blocker = engine.resume_blocker(record)
    if blocker:
        raise HTTPException(400, blocker)
    if not runner.spawn(str(migration_id), "run", _run_and_remember(migration_id, engine.resume(record))):
        raise HTTPException(409, "This migration is already running.")
    await _settle()
    return redact_record(record)


@router.post("/{migration_id}/reconcile")
async def reconcile_migration(migration_id: UUID, req: ReconcileRequest) -> dict[str, Any]:
    record = await _get(migration_id)
    _check_body_id(migration_id, req.migration_id)
    if record.phase not in (MigrationPhase.REPLICATING, MigrationPhase.STOPPED):
        raise HTTPException(400, "Reconciliation runs while continuous sync is replicating or stopped.")
    if not runner.spawn(str(migration_id), "reconcile", _engine().run_reconcile(record, force=req.force)):
        raise HTTPException(409, "Reconciliation is already running for this migration.")
    await _settle()
    return redact_record(record)


@router.post("/{migration_id}/rollback/plan", response_model=RollbackPlan)
async def rollback_plan(migration_id: UUID, req: RollbackRequest) -> RollbackPlan:
    record = await _get(migration_id)
    _check_body_id(migration_id, req.migration_id)
    return _engine().plan_rollback(record, req)


@router.post("/{migration_id}/rollback")
async def rollback_migration(migration_id: UUID, req: RollbackRequest) -> dict[str, Any]:
    record = await _get(migration_id)
    _check_body_id(migration_id, req.migration_id)
    engine = _engine()
    plan = engine.plan_rollback(record, req)
    if not plan.allowed:
        raise HTTPException(400, plan.warnings[0] if plan.warnings else "Rollback isn't available in this phase.")
    if plan.requires_cutover_confirmation and not req.confirm_after_cutover:
        raise HTTPException(400, "This migration was already cut over; set confirm_after_cutover to roll back anyway.")
    if not runner.spawn(str(migration_id), "rollback", _run_and_remember(migration_id, engine.rollback(record, req))):
        raise HTTPException(409, "A rollback is already running for this migration.")
    await _settle()
    return redact_record(record)


@router.post("/{migration_id}/fallback/start")
async def start_fallback(migration_id: UUID) -> dict[str, Any]:
    record = await _get(migration_id)
    engine = _engine()
    try:
        record = await engine.begin_fallback(record)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    runner.spawn(str(migration_id), "fallback", engine.run_fallback(record, fresh=True))
    return redact_record(record)


@router.post("/{migration_id}/fallback/stop")
async def stop_fallback(migration_id: UUID, req: FallbackStopRequest) -> dict[str, Any]:
    record = await _get(migration_id)
    _check_body_id(migration_id, req.migration_id)
    try:
        return redact_record(await _engine().stop_fallback(record, req.fall_back_to_source))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/{migration_id}")
async def delete_migration(migration_id: UUID) -> dict:
    record = await MigrationStore.instance().get(migration_id)
    if record is not None:
        busy = any(runner.is_running(str(migration_id), k) for k in ("run", "rollback", "reconcile", "fallback"))
        if busy or record.phase in _ACTIVE_PHASES:
            raise HTTPException(
                400, f"Stop or roll back this migration before deleting it (phase: {record.phase.value}).",
            )
    from app.core.fallback import discard_state

    discard_state(str(migration_id))
    await MigrationStore.instance().delete(migration_id)
    return {"deleted": True}
